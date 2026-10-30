using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using Digiwin.Common.Services;
using Digiwin.Common.Torridity;
using Digiwin.ERP.Common.Business;
using Digiwin.ERP.CommonManufacture.Business;
using Digiwin.ERP.MO.Business;

public sealed partial class E10AccountSession
{
    private string manualStage;
    private string ManualError(Exception e)
    {
        var data=json.Deserialize<Dictionary<string,object>>(Error(e));
        data["stage"]=manualStage;
        return json.Serialize(data);
    }
    private object ManualValue(DependencyObject entity, string field)
    {
        return entity.DependencyObjectType.Properties.IndexOfName(field) < 0 ? null : entity[field];
    }

    private DependencyObject ManualItem(string code)
    {
        if (String.IsNullOrWhiteSpace(code) || code.Length > 100) throw new ArgumentException("必须提供完整品号。");
        bool more;
        var rows = BrowseExact("ITEM", "ITEM.I01", "MCP_REFERENCE_EXACT", "ITEM", "ITEM_CODE", code,
            new string[,] {{"ID", "ITEM_ID"}}, 2, false, out more);
        if (rows.Count != 1 || more) throw new InvalidOperationException("品号未唯一匹配：" + code);
        return (DependencyObject)((IReadService)connector.GetService(typeof(IReadService), "ITEM")).Read(Guid.Parse(Convert.ToString(rows[0]["ID"])));
    }

    private DependencyObject ManualFeature(DependencyObject item, string code)
    {
        var features = (DependencyObjectCollection)item["ITEM_FEATURE"];
        if (String.IsNullOrEmpty(code))
        {
            if (features.Count > 0) throw new ArgumentException("该品号存在特征码，必须指定完整特征码。");
            return null;
        }
        DependencyObject found = null;
        foreach (DependencyObject feature in features)
            if (String.Equals(Convert.ToString(feature["ITEM_FEATURE_CODE"]), code, StringComparison.Ordinal))
            {
                if (found != null) throw new InvalidOperationException("特征码匹配不唯一。");
                found = feature;
            }
        if (found == null) throw new ArgumentException("该品号未找到指定特征码：" + code);
        return found;
    }

    public string LookupItemFeature(string itemCode, string featureCode)
    {
        try
        {
            RequireAction("MO.I01", "BROWSE");
            var item = ManualItem(itemCode);
            var feature = ManualFeature(item, featureCode);
            return json.Serialize(new {ok=true, unique=true, item_code=itemCode, feature_code=featureCode,
                item_id=item["ITEM_ID"], item_name=item["ITEM_NAME"],
                feature_id=feature == null ? Guid.Empty : feature["ITEM_FEATURE_ID"],
                specification=feature == null ? ManualValue(item,"ITEM_SPECIFICATION") : ManualValue(feature,"ITEM_SPECIFICATION")});
        }
        catch (Exception e) { return Error(e); }
    }

    private sealed class ManualContext
    {
        public DependencyObject Item, Feature, Plant, ItemPlant, Draft, Bom, WorkCenter, ProductionDepartment;
        public DependencyObjectCollection BomRows;
        public Dictionary<int, BomExtractParameter> BomParameters;
        public string BomVersion;
    }

    private ManualContext GetManualContext(string itemCode, string featureCode, string plantId, decimal quantity,
        string docDate, string startDate, string completeDate)
    {
        RequireAction("MO.I01", "BROWSE");
        RequireAction("MO.I01", "CREATE");
        if (!itemCode.StartsWith("1-", StringComparison.Ordinal)) throw new ArgumentException("此入口仅用于1-开头的包装成品。");
        if (quantity <= 0) throw new ArgumentException("预计产量必须大于零。");
        var date=DateTime.ParseExact(docDate,"yyyy-MM-dd",CultureInfo.InvariantCulture);
        var start=DateTime.ParseExact(startDate,"yyyy-MM-dd",CultureInfo.InvariantCulture);
        var complete=DateTime.ParseExact(completeDate,"yyyy-MM-dd",CultureInfo.InvariantCulture);
        if (complete < start) throw new ArgumentException("预计完工日期不能早于开工日期。");
        var context=new ManualContext();
        manualStage="item"; context.Item=ManualItem(itemCode);
        context.Feature=ManualFeature(context.Item,featureCode);
        var plantGuid=Guid.Parse(plantId);
        manualStage="plant"; context.Plant=(DependencyObject)((IReadService)connector.GetService(typeof(IReadService),"PLANT")).Read(plantGuid);
        if ((Guid)context.Plant["PLANT_ID"] != plantGuid) throw new InvalidOperationException("工厂资料不一致。");
        bool more;
        manualStage="item_plant";
        var plants=BrowseExact("ITEM_PLANT","ITEM_PLANT.I01","MCP_MANUAL_ITEM_PLANT","ITEM_PLANT","ITEM_ID",Convert.ToString(context.Item["ITEM_ID"]),
            new string[,]{{"ID","ITEM_PLANT_ID"},{"PLANT_ID","Owner_Org.ROid"}},50,false,out more);
        if(more) throw new InvalidOperationException("品号工厂资料超出查询上限。");
        foreach(var plant in plants) if(Convert.ToString(plant["PLANT_ID"])==plantGuid.ToString())
        {
            if(context.ItemPlant!=null) throw new InvalidOperationException("品号工厂资料不唯一。");
            context.ItemPlant=(DependencyObject)((IReadService)connector.GetService(typeof(IReadService),"ITEM_PLANT")).Read(Guid.Parse(Convert.ToString(plant["ID"])));
        }
        if(context.ItemPlant==null) throw new InvalidOperationException("未找到品号工厂资料。");
        manualStage="work_center";
        var workCenterId=(Guid)context.ItemPlant["WORK_CENTER_ID"];
        if(workCenterId==Guid.Empty)
        {
            var centers=BrowseExact("WORK_CENTER","WORK_CENTER.I01","MCP_MANUAL_WORK_CENTER","WORK_CENTER","WORK_CENTER_NAME","生产车间",
                new string[,]{{"ID","WORK_CENTER_ID"},{"NAME","WORK_CENTER_NAME"}},2,false,out more);
            if(more || centers.Count!=1 || Convert.ToString(centers[0]["NAME"])!="生产车间")
                throw new InvalidOperationException("工作中心缺失，且生产车间未唯一匹配；未准备或保存。");
            workCenterId=Guid.Parse(Convert.ToString(centers[0]["ID"]));
        }
        context.WorkCenter=(DependencyObject)((IReadService)connector.GetService(typeof(IReadService),"WORK_CENTER")).Read(workCenterId);
        if((Guid)context.WorkCenter["WORK_CENTER_ID"]!=workCenterId || workCenterId==Guid.Empty)
            throw new InvalidOperationException("工作中心资料不一致。");
        manualStage="production_department";
        var productionDepartmentValue=ManualValue(context.ItemPlant,"PRODUCTION_ADMIN_UNIT_ID");
        var productionDepartmentId=productionDepartmentValue==null ? Guid.Empty : (Guid)productionDepartmentValue;
        if(productionDepartmentId==Guid.Empty)
        {
            var departments=BrowseExact("ADMIN_UNIT","ADMIN_UNIT.I01","MCP_MANUAL_PRODUCTION_DEPARTMENT","ADMIN_UNIT","ADMIN_UNIT_NAME","生产部",
                new string[,]{{"ID","ADMIN_UNIT_ID"},{"NAME","ADMIN_UNIT_NAME"}},2,false,out more);
            if(more || departments.Count!=1 || Convert.ToString(departments[0]["NAME"])!="生产部")
                throw new InvalidOperationException("生产部门缺失，且生产部未唯一匹配；未准备或保存。");
            productionDepartmentId=Guid.Parse(Convert.ToString(departments[0]["ID"]));
        }
        context.ProductionDepartment=(DependencyObject)((IReadService)connector.GetService(typeof(IReadService),"ADMIN_UNIT")).Read(productionDepartmentId);
        if(productionDepartmentId==Guid.Empty || (Guid)context.ProductionDepartment["ADMIN_UNIT_ID"]!=productionDepartmentId ||
            Convert.ToString(ManualValue(context.ProductionDepartment,"ApproveStatus"))!="Y")
            throw new InvalidOperationException("生产部门未审核或资料无效；未准备或保存。");
        if((productionDepartmentValue==null || (Guid)productionDepartmentValue==Guid.Empty) &&
            Convert.ToString(context.ProductionDepartment["ADMIN_UNIT_NAME"])!="生产部")
            throw new InvalidOperationException("默认生产部门名称不符；未准备或保存。");
        manualStage="bom_version";
        var boms=BrowseExact("BOM","BOM.I01","MCP_MANUAL_BOM","BOM","ITEM_ID",Convert.ToString(context.Item["ITEM_ID"]),
            new string[,]{{"ID","BOM_ID"},{"PLANT_ID","Owner_Org.ROid"},{"VERSION","VERSION_TIMES"}},50,false,out more);
        if(more) throw new InvalidOperationException("BOM资料超出查询上限。");
        foreach(var bom in boms) if(Convert.ToString(bom["PLANT_ID"])==plantGuid.ToString())
        {
            if(context.BomVersion!=null) throw new InvalidOperationException("当前BOM资料不唯一。");
            context.BomVersion=Convert.ToString(bom["VERSION"]);
            context.Bom=(DependencyObject)((IReadService)connector.GetService(typeof(IReadService),"BOM")).Read(Guid.Parse(Convert.ToString(bom["ID"])));
        }
        if(String.IsNullOrWhiteSpace(context.BomVersion)) throw new InvalidOperationException("未找到当前工厂的BOM版次。");
        var parameters=new BomExtractAutoInsteadParameters(context.Item["ITEM_ID"],logOnService.CurrentUserId);
        parameters.CurrentUserId=logOnService.CurrentUserId;
        parameters.PlantId=plantGuid; parameters.ItemId=context.Item["ITEM_ID"];
        parameters.ItemFeatureId=context.Feature==null ? Guid.Empty : context.Feature["ITEM_FEATURE_ID"];
        parameters.EffectiveDate=date; parameters.PlanQty=quantity; parameters.ExtractMode=ExtractMode.SingleStage;
        parameters.IsHasLoss=true; parameters.IsIngoreParentItem=false;
        var extract=(IItemBomExtractService)connector.GetService(typeof(IItemBomExtractService),"MO");
        manualStage="bom_auto_replace";
        try {parameters.IsMOAutoReplace=Convert.ToBoolean(((ISysParameterService)connector.GetService(typeof(ISysParameterService),null)).GetValue("MO_AUTO_REPLACE",context.Item["ITEM_ID"]));}
        catch {parameters.IsMOAutoReplace=false;} // Matches the installed client's ItemBomExtractWindow constructor.
        manualStage="bom_extract"; var result=extract.DoBomExtract(parameters,plantGuid,"",start);
        if(result==null || result.Length<2) throw new InvalidOperationException("原生BOM服务未返回完整展开结果。");
        context.BomRows=result[0] as DependencyObjectCollection;
        context.BomParameters=result[1] as Dictionary<int,BomExtractParameter>;
        if(context.BomRows==null || context.BomParameters==null) throw new InvalidOperationException("BOM结果结构不受支持。");
        manualStage="draft"; context.Draft=(DependencyObject)((ICreateService)connector.GetService(typeof(ICreateService),"MO")).Create();
        return context;
    }

    public string PreviewManualWorkorder(string itemCode, string featureCode, string plantId, decimal quantity,
        string docDate, string startDate, string completeDate)
    {
        try
        {
            var c=GetManualContext(itemCode,featureCode,plantId,quantity,docDate,startDate,completeDate);
            var bomRows=new List<object>();
            foreach(DependencyObject row in c.BomRows) bomRows.Add(EntityData(row,0));
            var allocations=new List<object>();
            foreach(var pair in c.BomParameters) allocations.Add(new {index=pair.Key, warehouse_id=pair.Value.WarehouseId,
                bin_id=pair.Value.BinId, plan_issue_date=pair.Value.PlanIssueDate, lot_count=pair.Value.ItemLotIds==null ? 0 : pair.Value.ItemLotIds.Count});
            manualStage="employee"; var employee=ReturnOperator();
            return json.Serialize(new {ok=true,saved=false,item_code=itemCode,feature_code=featureCode,quantity=quantity,
                item=EntityData(c.Item,0),feature=c.Feature==null?null:EntityData(c.Feature,0),plant=EntityData(c.Plant,0),
                item_plant=EntityData(c.ItemPlant,0),work_center=EntityData(c.WorkCenter,0),work_center_fallback=(Guid)c.ItemPlant["WORK_CENTER_ID"]==Guid.Empty,bom_version=c.BomVersion,bom_rows=bomRows,bom_parameters=allocations,
                production_department=EntityData(c.ProductionDepartment,0),
                production_department_fallback=ManualValue(c.ItemPlant,"PRODUCTION_ADMIN_UNIT_ID")==null || (Guid)c.ItemPlant["PRODUCTION_ADMIN_UNIT_ID"]==Guid.Empty,
                default_draft=EntityData(c.Draft,0),current_employee=employee});
        }
        catch(Exception e){return ManualError(e);}
    }

    private string ManualBomFingerprint(ManualContext c)
    {
        var rows=new List<object>();
        foreach(DependencyObject row in c.BomRows) rows.Add(EntityData(row,0));
        var allocations=new List<object>();
        for(int i=0;i<c.BomRows.Count;i++)
        {
            if(!c.BomParameters.ContainsKey(i)) throw new InvalidOperationException("BOM仓库参数不完整。");
            var p=c.BomParameters[i];
            allocations.Add(new{warehouse=p.WarehouseId,bin=p.BinId,issue_date=p.PlanIssueDate});
        }
        var raw=json.Serialize(new{version=c.BomVersion,item_version=ManualValue(c.Item,"Version"),
            plant_version=ManualValue(c.ItemPlant,"Version"),work_center_id=c.WorkCenter["WORK_CENTER_ID"],work_center_version=ManualValue(c.WorkCenter,"Version"),production_department_id=c.ProductionDepartment["ADMIN_UNIT_ID"],production_department_version=ManualValue(c.ProductionDepartment,"Version"),production_department_name=c.ProductionDepartment["ADMIN_UNIT_NAME"],production_department_approval=ManualValue(c.ProductionDepartment,"ApproveStatus"),bom_version=ManualValue(c.Bom,"Version"),rows=rows,allocations=allocations});
        using(var sha=SHA256.Create()) return Convert.ToBase64String(sha.ComputeHash(Encoding.UTF8.GetBytes(raw)));
    }

    private List<Dictionary<string,object>> ManualExisting(string reference, Guid itemId, Guid featureId, decimal quantity)
    {
        bool more;
        var rows=BrowseExact("MO","MO.I01","MCP_MANUAL_DUPLICATE","MO","REMARK",reference,
            new string[,]{{"DOC_NO","DOC_NO"},{"ITEM_ID","ITEM_ID"},{"ITEM_FEATURE_ID","ITEM_FEATURE_ID"},
                {"PLAN_QTY","PLAN_QTY"},{"STATUS","STATUS"},{"APPROVE_STATUS","ApproveStatus"}},100,false,out more);
        if(more) throw new InvalidOperationException("相同来源的工单超出查询上限，无法排除重复开单。");
        var found=new List<Dictionary<string,object>>();
        foreach(var row in rows)
            if(Convert.ToString(row["ITEM_ID"])==itemId.ToString() && Convert.ToString(row["ITEM_FEATURE_ID"])==featureId.ToString()
                && Convert.ToDecimal(row["PLAN_QTY"])==quantity && Convert.ToString(row["APPROVE_STATUS"])!="V") found.Add(row);
        return found;
    }

    private void ValidateManualPlan(OperationPlan plan)
    {
        if(plan.ManualSpec==null) return;
        var spec=json.Deserialize<Dictionary<string,object>>(plan.ManualSpec);
        var c=GetManualContext(Convert.ToString(spec["item_code"]),Convert.ToString(spec["feature_code"]),Convert.ToString(spec["plant_id"]),
            Convert.ToDecimal(spec["quantity"]),Convert.ToString(spec["doc_date"]),Convert.ToString(spec["start_date"]),Convert.ToString(spec["complete_date"]));
        if(ManualBomFingerprint(c)!=plan.ManualFingerprint) throw new InvalidOperationException("准备后BOM或主资料已变化，未保存，请重新核对。");
        if(ManualExisting(Convert.ToString(spec["reference"]),(Guid)c.Item["ITEM_ID"],c.Feature==null?Guid.Empty:(Guid)c.Feature["ITEM_FEATURE_ID"],Convert.ToDecimal(spec["quantity"])).Count>0)
            throw new InvalidOperationException("同来源、品号、特征码及数量已有工单，未重复保存。");
    }

    public string PrepareManualWorkorder(string itemCode, string featureCode, string plantId, decimal quantity,
        string docDate, string startDate, string completeDate, string reference)
    {
        try
        {
            if(String.IsNullOrWhiteSpace(reference) || reference.Length>200) throw new ArgumentException("必须指定本次包装计划及原单号，长度不超过200字符。");
            var c=GetManualContext(itemCode,featureCode,plantId,quantity,docDate,startDate,completeDate);
            var featureId=c.Feature==null?Guid.Empty:(Guid)c.Feature["ITEM_FEATURE_ID"];
            var existing=ManualExisting(reference,(Guid)c.Item["ITEM_ID"],featureId,quantity);
            if(existing.Count>0) return json.Serialize(new{ok=false,error="相同来源及产品数量已存在工单，未重复准备。",existing=existing});
            manualStage="doc_type";
            bool more;
            var docs=BrowseExact("DOC","DOC.I01","MCP_PACKAGING_DOC","DOC","DOC_CODE","5101",
                new string[,]{{"ID","DOC_ID"},{"NAME","DOC_NAME"},{"CATEGORY","CATEGORY"},{"AUTO_APPROVE","AUTO_APPROVE"}},2,false,out more);
            if(more || docs.Count!=1 || Convert.ToString(docs[0]["CATEGORY"])!="51") throw new InvalidOperationException("5101未唯一匹配到一般工单类型。");
            if(Convert.ToBoolean(docs[0]["AUTO_APPROVE"])) throw new InvalidOperationException("该单据类型自动审核，需要先明确审核要求。");
            if(Convert.ToString(c.Item["UNIT_MODE"])!="S") throw new InvalidOperationException("此准备入口尚未验证双单位产品，未保存。");
            if(Convert.ToString(c.ItemPlant["ITEM_ROUTING_CONTROL"])!="0") throw new InvalidOperationException("该产品启用工艺管理，当前入口未展开工序，未保存。");
            if(c.BomRows.Count==0) throw new InvalidOperationException("BOM未返回子件，禁止保存空子件工单。");
            var bomProducts=ManualValue(c.Bom,"BOM_PRODUCT") as DependencyObjectCollection;
            if(bomProducts!=null) foreach(DependencyObject p in bomProducts)
                if((Guid)p["ITEM_ID"]!=(Guid)c.Item["ITEM_ID"] || Convert.ToString(p["PRODUCT_TYPE"])!="1")
                    throw new InvalidOperationException("BOM包含联副产品，当前入口需要专用产出展开，未保存。");
            manualStage="build_header";
            var draft=c.Draft;
            var date=DateTime.ParseExact(docDate,"yyyy-MM-dd",CultureInfo.InvariantCulture);
            var start=DateTime.ParseExact(startDate,"yyyy-MM-dd",CultureInfo.InvariantCulture);
            var complete=DateTime.ParseExact(completeDate,"yyyy-MM-dd",CultureInfo.InvariantCulture);
            var person=ReturnOperator();
            draft["DOC_ID"]=Guid.Parse(Convert.ToString(docs[0]["ID"])); draft["DOC_DATE"]=date; draft["TRANSACTION_DATE"]=date;
            var org=(DependencyObject)draft["Owner_Org"];org["RTK"]="PLANT";org["ROid"]=Guid.Parse(plantId);
            var source=(DependencyObject)draft["SOURCE_ID"];source["RTK"]="WORK_CENTER";source["ROid"]=c.WorkCenter["WORK_CENTER_ID"];
            if((Guid)source["ROid"]==Guid.Empty) throw new InvalidOperationException("品号工厂资料未设置工作中心。");
            draft["ADMIN_UNIT_ID"]=c.ProductionDepartment["ADMIN_UNIT_ID"];
            draft["Owner_Emp"]=person["EMPLOYEE_ID"];draft["Owner_Dept"]=person["DEPARTMENT_ID"];
            draft["ITEM_ID"]=c.Item["ITEM_ID"];draft["ITEM_FEATURE_ID"]=featureId;
            // Match the installed MO client FLD-0013 and FLD_0069 defaults.
            // This entry rejects routing-enabled items above; a blank control would wrongly require a routing ID.
            draft["STATUS"]="1";draft["ITEM_ROUTING_CONTROL"]=c.ItemPlant["ITEM_ROUTING_CONTROL"];
            draft["ITEM_ROUTING_ID"]=Guid.Empty;draft["ROUTING_CONTROL"]=c.ItemPlant["ROUTING_CONTROL"];
            draft["ITEM_DESCRIPTION"]=c.Item["ITEM_NAME"];
            draft["ITEM_SPECIFICATION"]=c.Feature==null?c.Item["ITEM_SPECIFICATION"]:c.Feature["ITEM_SPECIFICATION"];
            draft["BUSINESS_UNIT_ID"]=c.Item["STOCK_UNIT_ID"];draft["PLAN_QTY"]=quantity;
            draft["PLAN_START_DATE"]=start;draft["PLAN_COMPLETE_DATE"]=complete;
            draft["BOM_DATE"]=date;draft["BOM_VERSION_TIMES"]=c.BomVersion;draft["REMARK"]=reference;
            draft["WAREHOUSE_ID"]=c.ItemPlant["INBOUND_WAREHOUSE_ID"];
            var products=(DependencyObjectCollection)draft["MO_PRODUCT"];
            if(products.Count!=0) throw new InvalidOperationException("新工单出现意外的默认产出行。");
            var product=products.AddNew();product["MO_PRODUCT_ID"]=Guid.NewGuid();product["SequenceNumber"]=1;
            foreach(var field in new[]{"ITEM_ID","ITEM_FEATURE_ID","ITEM_DESCRIPTION","ITEM_SPECIFICATION","PLAN_QTY","WAREHOUSE_ID"}) product[field]=draft[field];
            product["UNIT_ID"]=draft["BUSINESS_UNIT_ID"];product["PRODUCT_TYPE"]="1";product["PER_QTY"]=1m;
            manualStage="build_components";
            var lines=(DependencyObjectCollection)draft["MO_D"];
            if(lines.Count!=0) throw new InvalidOperationException("新工单出现意外的默认子件行。");
            var evidence=new List<object>();
            for(int i=0;i<c.BomRows.Count;i++)
            {
                var row=c.BomRows[i];
                if(Convert.ToInt32(row["LEV"])==0) continue;
                if(!c.BomParameters.ContainsKey(i)) throw new InvalidOperationException("BOM子件缺少仓库参数。");
                var allocation=c.BomParameters[i];
                var line=lines.AddNew();line["MO_D_ID"]=Guid.NewGuid();line["SequenceNumber"]=lines.Count;
                foreach(var field in new[]{"ITEM_ID","ITEM_SPECIFICATION","UNIT_ID","ITEM_FEATURE_ID","UP_LEVEL_ITEM_ID","UP_LEVEL_ITEM_FEATURE_ID",
                    "REPLACED_ITEM_ID","REPLACED_ITEM_FEATURE_ID","ITEM_GROUP","REPLACE_GROUP","REQUIRED_QTY","REQU_INCLUDE_MULT_QTY","LEAST_REQUIRED_QTY",
                    "OPERATION_ID","ITEM_TYPE","REMARK","ISSUE_OVERRUN_RATE","ISSUE_SHORTAGE_RATE","ISSUE_MATERIAL_PERIOD","COMPONENT_LOCATION"}) line[field]=row[field];
                line["ITEM_DESCRIPTION"]=row["ITEM_NAME"];line["REPLACE_ITEM"]=row["REPLACE_TYPE"];
                line["REPLACED_QTY"]=row["REPLACED_QTY"];
                line["WAREHOUSE_ID"]=allocation.WarehouseId ?? Guid.Empty;line["BIN_ID"]=allocation.BinId ?? Guid.Empty;
                line["PLAN_ISSUE_DATE"]=start;line["ACTUAL_ISSUE_DATE"]=new DateTime(1900,1,1);
                if((Guid)line["ITEM_ID"]==Guid.Empty || (Guid)line["WAREHOUSE_ID"]==Guid.Empty || Convert.ToDecimal(line["REQUIRED_QTY"])<0)
                    throw new InvalidOperationException("BOM子件品号、仓库或数量不完整，禁止保存。");
                evidence.Add(new{item_code=row["ITEM_CODE"],quantity=line["REQUIRED_QTY"],warehouse_id=line["WAREHOUSE_ID"]});
            }
            if(lines.Count==0) throw new InvalidOperationException("未生成有效子件。");
            manualStage="validate";ValidateDraft("MO",draft);
            var spec=json.Serialize(new{item_code=itemCode,feature_code=featureCode,plant_id=plantId,quantity=quantity,
                doc_date=docDate,start_date=startDate,complete_date=completeDate,reference=reference});
            return StorePlan(new OperationPlan{Module="MO",Action="create",Entity=draft,Oid=draft["MO_ID"],Version=Convert.ToInt64(draft["Version"]),
                ManualSpec=spec,ManualFingerprint=ManualBomFingerprint(c),Summary=new{item_code=itemCode,feature_code=featureCode,quantity=quantity,
                    plant_code=c.Plant["PLANT_CODE"],plant_name=c.Plant["PLANT_NAME"],work_center_id=c.WorkCenter["WORK_CENTER_ID"],work_center_name=c.WorkCenter["WORK_CENTER_NAME"],work_center_fallback=(Guid)c.ItemPlant["WORK_CENTER_ID"]==Guid.Empty,bom_version=c.BomVersion,component_count=lines.Count,
                    production_department=EntityData(c.ProductionDepartment,0),production_department_fallback=ManualValue(c.ItemPlant,"PRODUCTION_ADMIN_UNIT_ID")==null || (Guid)c.ItemPlant["PRODUCTION_ADMIN_UNIT_ID"]==Guid.Empty,
                    reference=reference,components=evidence,draft=EntityData(draft,2),validation="原生BOM已完整展开；提交前重新检查BOM及重复单据。"}});
        }
        catch(Exception e){return ManualError(e);}
    }
}
