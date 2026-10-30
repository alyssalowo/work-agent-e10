using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using Digiwin.Common.Services;
using Digiwin.Common.Torridity;
using Digiwin.ERP.GenerateIssueReturnService.Business;

public sealed partial class E10AccountSession
{
    private string ReturnFingerprint(DependencyObject workorder, List<Dictionary<string,object>> history)
    {
        history.Sort((a,b)=>String.CompareOrdinal(Convert.ToString(a["LINE_ID"]),Convert.ToString(b["LINE_ID"])));
        var text = json.Serialize(new {version=workorder["Version"], history=history});
        using(var hash=SHA256.Create()) return Convert.ToBase64String(hash.ComputeHash(Encoding.UTF8.GetBytes(text)));
    }

    public string PrepareProductionReturn(string number)
    {
        try
        {
            RequireAction("MO.I01", "BROWSE"); RequireAction("ISSUE_RECEIPT.I02", "BROWSE"); RequireAction("ISSUE_RECEIPT.I02", "CREATE");
            var workorder = ReadDocument("MO", number);
            if(Convert.ToString(workorder["ApproveStatus"]) != "Y") throw new InvalidOperationException("工单尚未审核。");
            if((decimal)workorder["COMPLETED_QTY"] != 0 || (decimal)workorder["SCRAP_QTY"] != 0 || (decimal)workorder["DESTROYED_QTY"] != 0)
                throw new InvalidOperationException("该工单存在已入库或损耗数量，需要先核算已耗用料，不能直接全部退回。");
            var history=ReturnHistory(number);
            foreach(var row in history)
                if(Convert.ToString(row["CATEGORY"])=="57" && Convert.ToString(row["APPROVE_STATUS"])!="V")
                    throw new InvalidOperationException("已有退料单 " + Convert.ToString(row["DOC_NO"]) + "，需先核对，不能重复开单。");
            var materials = new List<object>(); decimal totalStockQty=0;
            foreach(DependencyObject row in (DependencyObjectCollection)workorder["MO_D"])
            {
                var qty=(decimal)row["ISSUED_QTY"]; if(qty<=0) continue;
                decimal issued=0; string warehouseName=""; Guid warehouse=Guid.Empty;
                foreach(var movement in history)
                    if((Guid)movement["MO_D_ID"]==(Guid)row["MO_D_ID"] && Convert.ToString(movement["CATEGORY"])=="56" && Convert.ToString(movement["APPROVE_STATUS"])=="Y")
                    {
                        if ((Guid)movement["WAREHOUSE_ID"] != (Guid)row["WAREHOUSE_ID"])
                            throw new InvalidOperationException("原领料记录包含不同于工单默认仓的仓库，请先确定退入仓库。");
                        issued+=Convert.ToDecimal(movement["INVENTORY_QTY"]); warehouseName=Convert.ToString(movement["WAREHOUSE_NAME"]); warehouse=(Guid)movement["WAREHOUSE_ID"];
                    }
                if(issued != qty) throw new InvalidOperationException("工单已领量与审核领料记录不一致，未执行退料。");
                if(warehouse != (Guid)row["WAREHOUSE_ID"]) throw new InvalidOperationException("工单默认仓与原领料仓不一致，请先确定退入仓库。");
                totalStockQty+=qty;
                materials.Add(new { mo_detail_id=row["MO_D_ID"], item_id=row["ITEM_ID"], item_name=row["ITEM_DESCRIPTION"], stock_quantity=qty, warehouse_id=warehouse, warehouse_name=warehouseName });
            }
            if(materials.Count==0) return json.Serialize(new {ok=true,no_changes=true,note="没有可退的已领材料。"});
            var docTypes=ProductionReturnDocTypes(); Dictionary<string,object> docType=null;
            foreach(var item in docTypes)
                if(Convert.ToString(item["PROCESS_TYPE"])=="1" && Convert.ToString(item["NAME"])=="生产退料单")
                { if(docType!=null) throw new InvalidOperationException("生产退料单类型不唯一。"); docType=item; }
            if(docType==null) throw new InvalidOperationException("未找到生产退料单类型。");
            if(Convert.ToBoolean(docType["AUTO_APPROVE"])) throw new InvalidOperationException("该单据类型会自动审核，需先明确审核要求。");
            var person=ReturnOperator(); var source=(DependencyObject)workorder["SOURCE_ID"];
            var parameters=new GenerateIssueReturnParameters {
                MO_ID=new List<string>{Convert.ToString(workorder["MO_ID"])}, SOURCE_ID_RTK=Convert.ToString(source["RTK"]), SOURCE_ID_ROid=(Guid)source["ROid"],
                DOC_ID=(Guid)docType["ID"], ISSUED_DATE=DateTime.Today, Owner_Emp=(Guid)person["EMPLOYEE_ID"], Owner_Dept=(Guid)person["DEPARTMENT_ID"],
                OperatioinOperator=1, OPERATION_CODE="", Split_Warehouse=true
            };
            return StorePlan(new OperationPlan {Module="ISSUE_RETURN",Action="generate",Number=number,Oid=workorder["MO_ID"],Version=Convert.ToInt64(workorder["Version"]),
                ReturnFingerprint=ReturnFingerprint(workorder,history),ReturnParameters=parameters,
                Summary=new {workorder_no=number,doc_type_code=docType["CODE"],doc_type_name=docType["NAME"],date=DateTime.Today.ToString("yyyy-MM-dd"),
                    all_return=true,material_lines=materials.Count,total_stock_quantity=totalStockQty,materials=materials,operator_info=person,auto_approve=false} });
        }
        catch(Exception e){return Error(e);}
    }

    private List<Dictionary<string, object>> ReturnHistory(string number)
    {
        bool more;
        var rows = BrowseExact("ISSUE_RECEIPT", "ISSUE_RECEIPT.I02", "QPD-0014", "ISSUE_RECEIPT", "ISSUE_RECEIPT_D.MO_ID.DOC_NO", number,
            new string[,] {
                {"ID","ISSUE_RECEIPT_ID"},{"DOC_NO","DOC_NO"},{"CATEGORY","CATEGORY"},{"DOC_NAME","DOC_ID.DOC_NAME"},
                {"DOC_DATE","DOC_DATE"},{"APPROVE_STATUS","ApproveStatus"},{"LINE_ID","ISSUE_RECEIPT_D.ISSUE_RECEIPT_D_ID"},
                {"MO_D_ID","ISSUE_RECEIPT_D.MO_D_ID"},{"ITEM_ID","ISSUE_RECEIPT_D.ITEM_ID"},
                {"ITEM_CODE","ISSUE_RECEIPT_D.ITEM_ID.ITEM_CODE"},{"ITEM_NAME","ISSUE_RECEIPT_D.ITEM_DESCRIPTION"},
                {"QTY","ISSUE_RECEIPT_D.ISSUE_RECEIPT_QTY"},{"ACTUAL_QTY","ISSUE_RECEIPT_D.ACTUAL_ISSUE_RECEIPT_QTY"},
                {"INVENTORY_QTY","ISSUE_RECEIPT_D.INVENTORY_QTY"},{"WAREHOUSE_ID","ISSUE_RECEIPT_D.WAREHOUSE_ID"},
                {"WAREHOUSE_NAME","ISSUE_RECEIPT_D.WAREHOUSE_ID.WAREHOUSE_NAME"},{"BIN_ID","ISSUE_RECEIPT_D.BIN_ID"},
                {"ITEM_LOT_ID","ISSUE_RECEIPT_D.ITEM_LOT_ID"}
            }, 500, true, out more);
        if (more) throw new InvalidOperationException("领退料记录超过查询上限，不能据此执行全部退料。");
        return rows;
    }

    private List<Dictionary<string, object>> ProductionReturnDocTypes()
    {
        bool more;
        var rows = BrowseExact("DOC", "ISSUE_RECEIPT.I02", "MCP_RETURN_DOC", "DOC", "CATEGORY", "57",
            new string[,] {{"ID","DOC_ID"},{"CODE","DOC_CODE"},{"NAME","DOC_NAME"},{"CATEGORY","CATEGORY"},{"AUTO_APPROVE","AUTO_APPROVE"},{"PROCESS_TYPE","PROCESS_TYPE"}}, 10, false, out more);
        if (more) throw new InvalidOperationException("退料单据类型不唯一。");
        return rows;
    }

    private Dictionary<string, object> ReturnOperator()
    {
        bool more;
        var user = BrowseExact("USER", "USER.I01", "MCP_CURRENT_OPERATOR", "USER", "USER_ID", Convert.ToString(logOnService.CurrentUserId),
            new string[,] {{"EMPLOYEE_ID","EMPLOYEE_ID"},{"EMPLOYEE_NAME","EMPLOYEE_ID.EMPLOYEE_NAME"}}, 2, false, out more);
        if (more || user.Count != 1 || (Guid)user[0]["EMPLOYEE_ID"] == Guid.Empty) throw new InvalidOperationException("当前用户没有唯一对应的员工资料。");
        var employee = (Guid)user[0]["EMPLOYEE_ID"];
        var departments = BrowseExact("EMPLOYEE", "EMPLOYEE.I01", "MCP_OPERATOR_DEPARTMENT", "EMPLOYEE.EMPLOYEE_D", "Parent.EMPLOYEE_ID", employee.ToString(),
            new string[,] {{"DEPARTMENT_ID","ADMIN_UNIT_ID"},{"DEPARTMENT_NAME","ADMIN_UNIT_ID.ADMIN_UNIT_NAME"},{"PRIMARY","IS_PRIMARY"},{"START_DATE","START_DATE"},{"END_DATE","END_DATE"}}, 30, false, out more);
        Dictionary<string,object> match = null;
        foreach (var department in departments)
        {
            var start = DateTime.Parse(Convert.ToString(department["START_DATE"]), CultureInfo.InvariantCulture);
            var end = DateTime.Parse(Convert.ToString(department["END_DATE"]), CultureInfo.InvariantCulture);
            if (Convert.ToBoolean(department["PRIMARY"]) && start <= DateTime.Today && end > DateTime.Today)
            { if(match != null) throw new InvalidOperationException("当前员工存在多个生效主部门。"); match = department; }
        }
        if (more || match == null) throw new InvalidOperationException("未找到当前员工的生效主部门。");
        return new Dictionary<string, object> {{"EMPLOYEE_ID", employee},{"EMPLOYEE_NAME", user[0]["EMPLOYEE_NAME"]},{"DEPARTMENT_ID", match["DEPARTMENT_ID"]},{"DEPARTMENT_NAME",match["DEPARTMENT_NAME"]}};
    }

    public string InspectProductionReturn(string number)
    {
        try
        {
            RequireAction("MO.I01", "BROWSE"); RequireAction("ISSUE_RECEIPT.I02", "CREATE"); RequireAction("ISSUE_RECEIPT.I02", "BROWSE");
            var workorder = ReadDocument("MO", number);
            var history = ReturnHistory(number); var documentTypes = ProductionReturnDocTypes();
            object operatorInfo;
            try { operatorInfo = ReturnOperator(); } catch(Exception e) { operatorInfo = json.DeserializeObject(Error(e)); }
            return json.Serialize(new { ok = true, workorder_no = number, workorder = EntityData(workorder,2), history = history,
                document_types = documentTypes, operator_info = operatorInfo });
        }
        catch (Exception e) { return json.Serialize(new {ok=false,detail=json.DeserializeObject(Error(e)),trace=e.StackTrace}); }
    }

    public string InspectReturnDraft()
    {
        try {
            RequireAction("ISSUE_RECEIPT.I02","CREATE");
            var draft=(DependencyObject)((ICreateService)connector.GetService(typeof(ICreateService),"ISSUE_RECEIPT")).Create();
            var source=(DependencyObject)((IReadService)connector.GetService(typeof(IReadService),"ISSUE_RECEIPT")).Read(new Guid("c983efa6-71e0-4092-3cbe-1e03152bc5f8"));
            return json.Serialize(new {ok=true,draft=EntityData(draft,2),source=EntityData(source,2),schema=SchemaData(draft.DependencyObjectType,1)});
        } catch(Exception e){return Error(e);}
    }

    public string PrepareManualProductionReturn(string number)
    {
        OperationPlan plan = null;
        try {
            var prepared=json.Deserialize<Dictionary<string,object>>(PrepareProductionReturn(number));
            if(!prepared.ContainsKey("operation_id")) return json.Serialize(prepared);
            plan=operationPlans[Convert.ToString(prepared["operation_id"])];
            var workorder=ReadDocument("MO",number);
            var history=ReturnHistory(number);
            var draft=(DependencyObject)((ICreateService)connector.GetService(typeof(ICreateService),"ISSUE_RECEIPT")).Create();
            draft["CATEGORY"]="57"; draft["DOC_ID"]=plan.ReturnParameters.DOC_ID;
            draft["DOC_DATE"]=DateTime.Today; draft["TRANSACTION_DATE"]=DateTime.Today;
            draft["Owner_Emp"]=plan.ReturnParameters.Owner_Emp; draft["Owner_Dept"]=plan.ReturnParameters.Owner_Dept;
            foreach(var name in new[]{"Owner_Org","SOURCE_ID"}) {
                var to=(DependencyObject)draft[name]; var from=(DependencyObject)workorder[name];
                to["RTK"]=from["RTK"]; to["ROid"]=from["ROid"];
            }
            ((DependencyObject)draft["SOURCE_DOC_ID"])["RTK"]="OTHER";
            foreach(var movement in history) {
                if(Convert.ToString(movement["CATEGORY"])!="56" || Convert.ToString(movement["APPROVE_STATUS"])!="Y") continue;
                var returnQty=Convert.ToDecimal(movement["INVENTORY_QTY"]);
                if(returnQty < 0) throw new InvalidOperationException("原领料包含负数调整，不能按全部原量直接退回。");
                if(returnQty == 0) continue;
                bool eligible=false;
                foreach(DependencyObject material in (DependencyObjectCollection)workorder["MO_D"])
                    if((Guid)material["MO_D_ID"]==(Guid)movement["MO_D_ID"] && (decimal)material["ISSUED_QTY"] > 0) eligible=true;
                if(!eligible) throw new InvalidOperationException("原领料与当前工单可退明细不一致，未保存。");
                var source=(DependencyObject)((IReadService)connector.GetService(typeof(IReadService),"ISSUE_RECEIPT")).Read(movement["ID"]);
                foreach(var property in source.GetDataEntityType().CollectionProperties)
                    if(property.Name!="ISSUE_RECEIPT_D" && property.Name!="ISSUE_RECEIPT_MO" && ((DependencyObjectCollection)source[property.Name]).Count > 0)
                        throw new InvalidOperationException("原领料存在附加子明细，需专门核对批号或序列号，未保存。");
                DependencyObject original=null;
                foreach(DependencyObject line in (DependencyObjectCollection)source["ISSUE_RECEIPT_D"])
                    if((Guid)line["ISSUE_RECEIPT_D_ID"]==(Guid)movement["LINE_ID"]) original=line;
                if(original==null) throw new InvalidOperationException("原领料明细不存在，未保存。");
                foreach(var property in original.GetDataEntityType().CollectionProperties)
                    if(((DependencyObjectCollection)original[property.Name]).Count > 0) throw new InvalidOperationException("原领料行存在附加子明细，当前全部退料工具不自动复制。");
                var rows=(DependencyObjectCollection)draft["ISSUE_RECEIPT_D"]; var row=rows.AddNew();
                row["ISSUE_RECEIPT_D_ID"]=Guid.NewGuid(); row["SequenceNumber"]=rows.Count;
                foreach(var field in new[]{"MO_ID","MO_D_ID","ITEM_ID","ITEM_FEATURE_ID","ITEM_DESCRIPTION","ITEM_SPECIFICATION","ITEM_TYPE","UNIT_ID","WAREHOUSE_ID","BIN_ID","ITEM_LOT_ID","MO_ROUTING_D_ID","OPERATION_ID","PROJECT_ID","REPLACED_MO_D_ID"}) row[field]=original[field];
                foreach(var field in new[]{"ISSUE_RECEIPT_QTY","ACTUAL_ISSUE_RECEIPT_QTY","INVENTORY_QTY","ACTUAL_INVENTORY_QTY"}) row[field]=returnQty;
                row["REPLACED_QTY"]=original["REPLACED_QTY"];
                if(Convert.ToDecimal(original["ISSUE_RECEIPT_QTY"])!=Convert.ToDecimal(movement["INVENTORY_QTY"])) throw new InvalidOperationException("业务单位与库存单位数量不同，需先换算，未保存。");
                row["SOURCE_TYPE"]="1";
                ((DependencyObject)row["ISSUE_DESTINATION"])["RTK"]="MO";
                var cost=(DependencyObject)row["COST_DOMAIN_ID"]; var oldCost=(DependencyObject)original["COST_DOMAIN_ID"];
                cost["RTK"]=oldCost["RTK"]; cost["ROid"]=oldCost["ROid"];
            }
            var mo=((DependencyObjectCollection)draft["ISSUE_RECEIPT_MO"]).AddNew();
            mo["ISSUE_RECEIPT_MO_ID"]=Guid.NewGuid(); mo["MO_ID"]=plan.Oid; mo["ISSUE_MODE"]="3"; mo["ISSUE_RECEIPT_SETS"]=0m; mo["PLAN_ISSUE_DATE"]=DateTime.Today;
            ((DependencyObject)mo["ISSUE_DESTINATION"])["RTK"]="MO";
            ValidateDraft("ISSUE_RECEIPT",draft);
            plan.Action="create";plan.Entity=draft;
            var summary=AsMap(prepared["summary"]);
            summary["save_route"]="standard_create"; summary["draft"]=EntityData(draft,2);
            plan.Summary=summary;
            prepared["action"]="create"; prepared["summary"]=summary;
            return json.Serialize(prepared);
        } catch(Exception e){if(plan!=null) operationPlans.Remove(plan.Id); return Error(e);}
    }
}
