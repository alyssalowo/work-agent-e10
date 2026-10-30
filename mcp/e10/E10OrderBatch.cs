using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text.RegularExpressions;
using System.Xml;
using Digiwin.Common.Services;
using Digiwin.Common.Torridity;
using Digiwin.Common.Torridity.Query;
using Digiwin.ERP.MO_B002.Business;

public sealed partial class E10AccountSession
{
    private Dictionary<string, object> OrderPlant(string name)
    {
        if (String.IsNullOrWhiteSpace(name) || name != name.Trim() || name.Length > 80) throw new ArgumentException("请指定完整工厂名称。");
        bool more;
        var rows = BrowseExact("PLANT", "MO_B002.B01", "MCP_ORDER_PLANT", "PLANT", "PLANT_NAME", name,
            new string[,] {{"ID","PLANT_ID"},{"CODE","PLANT_CODE"},{"NAME","PLANT_NAME"}}, 2, false, out more);
        if (more || rows.Count != 1 || (Guid)rows[0]["ID"] == Guid.Empty) throw new InvalidOperationException("工厂名称没有唯一匹配。");
        return rows[0];
    }

    public string QueryOrderWorkorderSources(string plantName, string dateFrom, string departmentsJson, string tokensJson, int start, int limit, string clientDirectory)
    {
        try
        {
            RequireAction("MO_B002.B01", "CREATE");
            if (start < 1 || limit < 1 || limit > 1000 || start > Int32.MaxValue - limit) throw new ArgumentException("start_row从1开始，max_rows为1至1000。");
            var date = PickingDate(dateFrom);
            var departments = json.Deserialize<string[]>(departmentsJson);
            var tokens = json.Deserialize<string[]>(tokensJson);
            if (departments == null || tokens == null || departments.Length > 30 || tokens.Length > 30) throw new ArgumentException("排除条件格式或数量不正确。");
            var plant = OrderPlant(plantName);
            var condition = new ConditionItemGroup { LogicalOperator = LogicalOperator.And };
            condition.Items.Add(PickingCondition("DOC_DATE", date, GeneralDBType.DateTime, CompareOperator.GreaterEqual));
            foreach (var department in departments)
            {
                if (String.IsNullOrWhiteSpace(department) || department.Length > 80) throw new ArgumentException("排除部门必须是完整名称。");
                condition.Items.Add(PickingCondition("ADMIN_UNIT_NAME", department, GeneralDBType.String, CompareOperator.NotEqual));
            }
            foreach (var token in tokens)
            {
                if (token == null || !Regex.IsMatch(token, @"\A[A-Za-z0-9]+-\z")) throw new ArgumentException("订单排除值须为单别加连字符，例如2319-。");
                condition.Items.Add(PickingCondition("DOC_NO", "%" + token + "%", GeneralDBType.String, CompareOperator.NotLike));
            }
            // Use the module's actual selector, including its built-in approved/open
            // order and delivery-plant conditions, rather than a generic order browse.
            var input = new QueryInputData { Id = "QPD_0002", SourceTypeKey = "MO_B002", MainEntityPath = "SALES_ORDER_DOC",
                ProgramId = "MO_B002.B01", IsIncludeDetail = false, IsDistinct = true };
            var fields = new string[,] {{"SALES_ORDER_DOC_ID","SALES_ORDER_DOC_ID"},{"DOC_NO","DOC_NO"},{"DOC_DATE","DOC_DATE"},
                {"ADMIN_UNIT_NAME","ADMIN_UNIT_NAME"},{"CUSTOMER_CODE","CUSTOMER_CODE"},{"CUSTOMER_NAME","CUSTOMER_NAME"}};
            for (int i=0;i<fields.GetLength(0);i++) input.QueryColumns.Add(new QueryColumn { Alias=fields[i,0], Value=new PropertyValue {Path=fields[i,1]} });
            input.OrderColumns.Add(new OrderByDict(new PropertyValue {Path="DOC_NO"}, SortType.Asc, 1, "DOC_NO"));
            input.OrderColumns.Add(new OrderByDict(new PropertyValue {Path="SALES_ORDER_DOC_ID"}, SortType.Asc, 2, "SALES_ORDER_DOC_ID"));
            var parameters = new ParameterCollection(); parameters.Add(new Parameter("PLANT_ID", plant["ID"]) {DBType=GeneralDBType.Guid});
            var definition = new XmlDocument(); definition.Load(Path.Combine(clientDirectory,"BatchObjects","MO_B002","QueryProjectContainer.dcxml"));
            var scriptNode = definition.SelectSingleNode("//*[local-name()='OoqlQueryProject'][*[local-name()='Id']='QPD_0002']/*[local-name()='OoqlScript']");
            if(scriptNode==null || String.IsNullOrWhiteSpace(scriptNode.InnerText)) throw new InvalidOperationException("本机订单选择器脚本不存在，不能改用无约束查询。");
            var result = ((IBrowseService)connector.GetService(typeof(IBrowseService),"MO_B002")).ExecuteQuery(new RowNumberInfo(start,start+limit-1),input,scriptNode.InnerText,condition,parameters,CultureInfo.GetCultureInfo("zh-CN"));
            var rows = new List<Dictionary<string, object>>(); var seen = new HashSet<string>();
            foreach (DependencyObject row in (IEnumerable)result.DataSource)
            {
                var values = new Dictionary<string, object>();
                foreach(var field in input.QueryColumns) values[field.Alias] = CleanValue(row[field.Alias]);
                values["ID"]=values["SALES_ORDER_DOC_ID"]; values["SALES_DEPARTMENT"]=values["ADMIN_UNIT_NAME"];
                var number = Convert.ToString(values["DOC_NO"]); ValidateDocumentNumber(number);
                if(!seen.Add(Convert.ToString(values["ID"]))) throw new InvalidOperationException("订单查询出现重复主键。");
                if(DateTime.Parse(Convert.ToString(values["DOC_DATE"]),CultureInfo.InvariantCulture) < date) throw new InvalidOperationException("查询返回日期范围外的订单。");
                foreach(var department in departments) if(Convert.ToString(values["SALES_DEPARTMENT"])==department) throw new InvalidOperationException("查询返回应排除的销售部门。");
                foreach(var token in tokens) if(number.IndexOf(token,StringComparison.OrdinalIgnoreCase)>=0) throw new InvalidOperationException("查询返回应排除的订单单别。");
                rows.Add(values);
            }
            if(rows.Count>limit || (result.HasNextPage && rows.Count==0)) throw new InvalidOperationException("订单分页范围异常。");
            return json.Serialize(new {ok=true,plant=plant,filters=new {date_from=dateFrom,excluded_sales_departments=departments,excluded_order_tokens=tokens},
                module="MO_B002",query_id="QPD_0002",start_row=start,next_start_row=start+rows.Count,has_next_page=result.HasNextPage,rows=rows});
        }
        catch(Exception e){return Error(e);}
    }

    private List<Dictionary<string, object>> OrderBrowseIn(string type, string entity, string field, IEnumerable<string> keys, GeneralDBType dbType, string[,] fields, int limit)
    {
        var values = new HashSet<string>(keys, StringComparer.OrdinalIgnoreCase);
        var rows = new List<Dictionary<string, object>>();
        if(values.Count==0) return rows;
        if(values.Count>1000) throw new ArgumentException("关联记录超过1000，请缩小批次。");
        var input = new QueryInputData {Id="MCP_ORDER_REFERENCE",SourceTypeKey=type,MainEntityPath=entity,ProgramId="MO_B002.B01",IsIncludeDetail=type=="MO",IsDistinct=true};
        for(int i=0;i<fields.GetLength(0);i++) input.QueryColumns.Add(new QueryColumn {Alias=fields[i,0],Value=new PropertyValue {Path=fields[i,1]}});
        var condition=new ConditionItem {Left=new PropertyValue {Path=field},CompareOperator=CompareOperator.In};
        foreach(var value in values) condition.Values.Add(new ConstantValue {Value=value,DataType=dbType});
        var result=((IBrowseService)connector.GetService(typeof(IBrowseService),type)).ExecuteQuery(new RowNumberInfo(1,limit),input,condition,new ParameterCollection(),CultureInfo.GetCultureInfo("zh-CN"));
        if(result.HasNextPage) throw new InvalidOperationException("关联查询未完整，不能据此生成或确认结果。");
        foreach(DependencyObject row in (IEnumerable)result.DataSource) {
            var record=new Dictionary<string,object>();
            foreach(var column in input.QueryColumns) record[column.Alias]=CleanValue(row[column.Alias]);
            rows.Add(record);
        }
        return rows;
    }

    private List<Dictionary<string,object>> OrderHeadVersions(string[] numbers)
    {
        return OrderBrowseIn("SALES_ORDER_DOC","SALES_ORDER_DOC","DOC_NO",numbers,GeneralDBType.String,
            new string[,] {{"ID","SALES_ORDER_DOC_ID"},{"DOC_NO","DOC_NO"},{"VERSION","Version"},{"APPROVAL_STATUS","ApproveStatus"},{"CLOSE","CLOSE"}},numbers.Length+1);
    }

    private List<Dictionary<string,object>> OrderLinkedWorkorders(string[] sourceIds)
    {
        return OrderBrowseIn("MO","MO","MO_DEMAND.DEMAND_DOC.ROid",sourceIds,GeneralDBType.Guid,
            new string[,] {{"ID","MO_ID"},{"DOC_NO","DOC_NO"},{"DOC_DATE","DOC_DATE"},{"DOC_ID","DOC_ID"},
                {"ITEM_CODE","ITEM_ID.ITEM_CODE"},{"ITEM_NAME","ITEM_DESCRIPTION"},{"PLAN_QTY","PLAN_QTY"},
                {"APPROVAL_STATUS","ApproveStatus"},{"PLANT_ID","Owner_Org.ROid"},{"SOURCE_ID","MO_DEMAND.DEMAND_DOC.ROid"},
                {"CREATE_DATE","CreateDate"},{"CREATE_BY","CreateBy"}},5000);
    }

    public string QueryOrderHeadVersions(string numbersJson)
    {
        try {
            RequireAction("MO_B002.B01","CREATE");
            var numbers=json.Deserialize<string[]>(numbersJson);
            if(numbers==null || numbers.Length<1 || numbers.Length>50) throw new ArgumentException("Require 1 to 50 order numbers.");
            var seen=new HashSet<string>(StringComparer.Ordinal);
            foreach(var number in numbers) {ValidateDocumentNumber(number); if(!seen.Add(number)) throw new ArgumentException("Duplicate order number.");}
            var rows=OrderHeadVersions(numbers);
            return json.Serialize(new {ok=true,complete=rows.Count==numbers.Length,has_next_page=false,order_nos=numbers,rows=rows});
        } catch(Exception e){return Error(e);}
    }

    public string QueryOrderWorkorderResult(string sourceIdsJson)
    {
        try {
            RequireAction("MO.I01","BROWSE");
            var sourceIds=json.Deserialize<string[]>(sourceIdsJson);
            if(sourceIds==null || sourceIds.Length<1 || sourceIds.Length>100) throw new ArgumentException("请指定1至100个已核对的订单子明细ID。");
            foreach(var id in sourceIds) if(Guid.Parse(id)==Guid.Empty) throw new ArgumentException("订单子明细ID不能为空。");
            var rows=OrderLinkedWorkorders(sourceIds);
            return json.Serialize(new {ok=true,source_ids=sourceIds,has_next_page=false,rows=rows,current_user_id=logOnService.CurrentUserId});
        } catch(Exception e){return Error(e);}
    }

    public string QueryWorkorderItemDate(string plantName, string docCode, string itemCodesJson, string docDate)
    {
        try {
            RequireAction("MO.I01","BROWSE");var plant=OrderPlant(plantName);var date=PickingDate(docDate);
            var itemCodes=json.Deserialize<string[]>(itemCodesJson);
            if(itemCodes==null || itemCodes.Length<1 || itemCodes.Length>50) throw new ArgumentException("请指定1至50个完整品号。");
            bool more;var docs=BrowseExact("DOC","MO.I01","MCP_RESULT_DOC","DOC","DOC_CODE",docCode,new string[,]{{"ID","DOC_ID"}},2,false,out more);
            if(more || docs.Count!=1) throw new ArgumentException("单据类型没有唯一匹配。");
            var condition=new ConditionItemGroup {LogicalOperator=LogicalOperator.And};
            condition.Items.Add(PickingCondition("Owner_Org.ROid",plant["ID"],GeneralDBType.Guid,CompareOperator.Equal));
            condition.Items.Add(PickingCondition("DOC_ID",docs[0]["ID"],GeneralDBType.Guid,CompareOperator.Equal));
            condition.Items.Add(PickingCondition("DOC_DATE",date,GeneralDBType.DateTime,CompareOperator.Equal));
            var products=new ConditionItem {Left=new PropertyValue {Path="ITEM_ID.ITEM_CODE"},CompareOperator=CompareOperator.In};
            foreach(var code in itemCodes) {
                if(String.IsNullOrWhiteSpace(code) || code.Length>100) throw new ArgumentException("品号格式不正确。");
                products.Values.Add(new ConstantValue {Value=code,DataType=GeneralDBType.String});
            }
            condition.Items.Add(products);
            var input=new QueryInputData {Id="MCP_ORDER_RESULT_HEADER",SourceTypeKey="MO",MainEntityPath="MO",ProgramId="MO.I01",IsIncludeDetail=false,IsDistinct=true};
            var fields=new string[,] {{"ID","MO_ID"},{"DOC_NO","DOC_NO"},{"DOC_DATE","DOC_DATE"},{"ITEM_CODE","ITEM_ID.ITEM_CODE"},
                {"ITEM_NAME","ITEM_DESCRIPTION"},{"PLAN_QTY","PLAN_QTY"},{"DEMAND_NO","DEMAND_NO"},{"APPROVAL_STATUS","ApproveStatus"},{"CREATE_DATE","CreateDate"},{"CREATE_BY","CreateBy"}};
            for(int i=0;i<fields.GetLength(0);i++) input.QueryColumns.Add(new QueryColumn {Alias=fields[i,0],Value=new PropertyValue {Path=fields[i,1]}});
            var result=((IBrowseService)connector.GetService(typeof(IBrowseService),"MO")).ExecuteQuery(new RowNumberInfo(1,1000),input,condition,new ParameterCollection(),CultureInfo.GetCultureInfo("zh-CN"));
            var rows=new List<Dictionary<string,object>>();
            foreach(DependencyObject row in (IEnumerable)result.DataSource) {var values=new Dictionary<string,object>();foreach(var column in input.QueryColumns) values[column.Alias]=CleanValue(row[column.Alias]);rows.Add(values);}
            return json.Serialize(new {ok=true,plant=plant,doc_type_code=docCode,doc_date=docDate,item_codes=itemCodes,has_next_page=result.HasNextPage,rows=rows,current_user_id=logOnService.CurrentUserId});
        }catch(Exception e){return Error(e);}
    }

    private static string PackagingOrderExclusion(string code, string name, string[] vKeywords, string[] hKeywords)
    {
        if(!code.StartsWith("1-",StringComparison.Ordinal)) return "not_1_finished_product";
        if(code.StartsWith("1-V",StringComparison.OrdinalIgnoreCase)) foreach(var word in vKeywords)
            if(name.IndexOf(word,StringComparison.OrdinalIgnoreCase)>=0) return "excluded_1_V_name_"+word;
        if(code.StartsWith("1-H",StringComparison.OrdinalIgnoreCase)) foreach(var word in hKeywords)
            if(name.IndexOf(word,StringComparison.OrdinalIgnoreCase)>=0) return "excluded_1_H_name_"+word;
        return null;
    }

    public string PreparePackagingOrderBatch(string numbersJson, string plantName, string docCode, string vJson, string hJson, string clientDirectory)
    {
        try {
            RequireAction("MO_B002.B01","CREATE"); RequireAction("MO.I01","BROWSE");
            var numbers=json.Deserialize<string[]>(numbersJson);
            if(numbers==null || numbers.Length<1 || numbers.Length>50) throw new ArgumentException("每批1至50个去重完整订单号。");
            var scope=new HashSet<string>(StringComparer.Ordinal);
            foreach(var number in numbers) {ValidateDocumentNumber(number);if(!scope.Add(number)) throw new ArgumentException("订单号重复。");}
            var vWords=json.Deserialize<string[]>(vJson); var hWords=json.Deserialize<string[]>(hJson);
            if(vWords==null || hWords==null || vWords.Length>30 || hWords.Length>30) throw new ArgumentException("品名排除词格式不正确。");
            foreach(var words in new[] {vWords,hWords}) foreach(var word in words)
                if(String.IsNullOrWhiteSpace(word) || word.Length>80) throw new ArgumentException("排除词不能为空或超过80字符。");
            var plant=OrderPlant(plantName); bool more;
            var docs=BrowseExact("DOC","MO_B002.B01","MCP_ORDER_PACKAGING_DOC","DOC","DOC_CODE",docCode,
                new string[,] {{"ID","DOC_ID"},{"CODE","DOC_CODE"},{"NAME","DOC_NAME"},{"CATEGORY","CATEGORY"},{"STATUS","ApproveStatus"},{"AUTO_APPROVE","AUTO_APPROVE"}},2,false,out more);
            if(more || docs.Count!=1 || Convert.ToString(docs[0]["NAME"])!="包装工单" || Convert.ToString(docs[0]["CATEGORY"])!="51" || Convert.ToString(docs[0]["STATUS"])!="Y")
                throw new ArgumentException("指定单据类型未唯一匹配到已审核的包装工单。");
            if(Convert.ToBoolean(docs[0]["AUTO_APPROVE"])) throw new ArgumentException("该包装工单类型会自动审核，当前入口不自动扩大审核范围。");
            var docId=(Guid)docs[0]["ID"];
            var versions=OrderHeadVersions(numbers);
            if(versions.Count!=numbers.Length) throw new InvalidOperationException("订单未唯一匹配，请重新查询。");
            foreach(var version in versions) if(Convert.ToString(version["APPROVAL_STATUS"])!="Y" || Convert.ToString(version["CLOSE"])!="0")
                throw new InvalidOperationException("订单审核或结束状态已变化，请重新查询。");
            var root=NewOrderWizard(clientDirectory);
            var org=(DependencyObject)root["Owner_Org"];org["RTK"]="PLANT";org["ROid"]=plant["ID"];
            var range=(DependencyObject)root["SALES_ORDER_DOCS"];range["Operator"]=0;
            int sequence=0;
            foreach(var number in numbers) {var row=((DependencyObjectCollection)range["RangeEntityItemDatas"]).AddNew();row["ID"]=Guid.NewGuid();row["CODE"]=number;row["SN"]=++sequence;}
            root["PLAN_SHIP_DATE_S"]=new DateTime(1900,1,1);root["PLAN_SHIP_DATE_E"]=new DateTime(9998,12,31);
            root["DOC_DATE"]=DateTime.Today;root["DOC_DEFAULT_TYPE"]="2";root["DOC_ID"]=docId;
            root["UNASSIGN_STATUS"]=true;
            root["CUSTOMER_MERGER"]=false;root["SO_MERGER"]=false;
            var person=ReturnOperator();root["Owner_Emp"]=person["EMPLOYEE_ID"];root["Owner_Dept"]=person["DEPARTMENT_ID"];
            var data=((IGetDataService)connector.GetService(typeof(IGetDataService),"MO_B002")).Read(root,"2",docId.ToString());
            CopyOrderQueryResult(data,root);data=root;
            var rows=(DependencyObjectCollection)data["MO_B002_MO"];
            var itemKeys=new HashSet<string>();foreach(DependencyObject row in rows) itemKeys.Add(Convert.ToString(row["ITEM_ID"]));
            var items=OrderBrowseIn("ITEM","ITEM","ITEM_ID",itemKeys,GeneralDBType.Guid,
                new string[,] {{"ID","ITEM_ID"},{"CODE","ITEM_CODE"},{"NAME","ITEM_NAME"}},itemKeys.Count+1);
            var byItem=new Dictionary<Guid,Dictionary<string,object>>();foreach(var item in items) byItem.Add((Guid)item["ID"],item);
            var selected=new List<object>();var excluded=new List<object>();var sources=new HashSet<string>();var guardedOrderSet=new HashSet<string>(StringComparer.Ordinal);
            var candidateCount=rows.Count;
            for(int i=rows.Count-1;i>=0;i--) {
                var row=rows[i];var itemId=(Guid)row["ITEM_ID"];
                if(!byItem.ContainsKey(itemId)) throw new InvalidOperationException("候选品号无法解析，未准备生成。");
                var code=Convert.ToString(byItem[itemId]["CODE"]);var name=Convert.ToString(row["ITEM_DESCRIPTION"]);
                if(String.IsNullOrWhiteSpace(name)) name=Convert.ToString(byItem[itemId]["NAME"]);
                if(String.IsNullOrWhiteSpace(code) || String.IsNullOrWhiteSpace(name)) throw new InvalidOperationException("候选品号或品名为空，无法执行筛选规则。");
                var sourceNumbers=new List<string>();var sourceIds=new List<string>();
                foreach(DependencyObject source in (DependencyObjectCollection)row["MO_B002_SALES"]) {
                    var sourceNumber=Convert.ToString(source["DOC_NO"]);bool inside=false;
                    foreach(var number in numbers) if(sourceNumber==number || sourceNumber.StartsWith(number+"-",StringComparison.Ordinal)) inside=true;
                    if(!inside) throw new InvalidOperationException("候选包含本批订单以外的来源，未准备生成。");
                    if((Guid)source["SALES_ORDER_DOC_SD_ID"]==Guid.Empty) throw new InvalidOperationException("候选缺少订单子明细主键。");
                    sourceNumbers.Add(sourceNumber);sourceIds.Add(Convert.ToString(source["SALES_ORDER_DOC_SD_ID"]));
                }
                if(sourceNumbers.Count==0) throw new InvalidOperationException("候选工单没有明确订单来源。");
                var reason=PackagingOrderExclusion(code,name,vWords,hWords);
                if(Convert.ToDecimal(row["PLAN_QTY"])<=0) reason="non_positive_plan_quantity";
                if(reason!=null) {excluded.Add(new {item_code=code,item_name=name,plan_quantity=row["PLAN_QTY"],source_doc_nos=sourceNumbers,reason=reason});rows.RemoveAt(i);continue;}
                if((Guid)row["DOC_ID"]!=docId) throw new InvalidOperationException("候选工单单别与指定包装工单不符。");
                row["SELECT"]=true;row["AUTO_APPROVE"]=false;
                foreach(DependencyObject source in (DependencyObjectCollection)row["MO_B002_SALES"]) source["SELECT"]=true;
                foreach(var sourceId in sourceIds) sources.Add(sourceId);
                foreach(var sourceNumber in sourceNumbers) {
                    string parent=null;int matches=0;
                    foreach(var number in numbers) if(sourceNumber==number || sourceNumber.StartsWith(number+"-",StringComparison.Ordinal)) {parent=number;matches++;}
                    if(matches!=1) throw new InvalidOperationException("选中候选来源无法唯一对应订单，未准备生成。");
                    guardedOrderSet.Add(parent);
                }
                selected.Add(new {candidate_id=row["MO_B002_MO_ID"],item_code=code,item_name=name,plan_quantity=row["PLAN_QTY"],source_doc_nos=sourceNumbers,source_ids=sourceIds,
                    draft=EntityData(row,2)});
            }
            var guardedOrderNos=new List<string>();foreach(var number in numbers) if(guardedOrderSet.Contains(number)) guardedOrderNos.Add(number);
            var guardedVersions=new List<Dictionary<string,object>>();var guardedVersionNos=new HashSet<string>(StringComparer.Ordinal);
            foreach(var version in versions) if(guardedOrderSet.Contains(Convert.ToString(version["DOC_NO"]))) {
                var versionNo=Convert.ToString(version["DOC_NO"]);
                if(!guardedVersionNos.Add(versionNo)) throw new InvalidOperationException("选中来源订单版本快照重复，未准备生成。");
                guardedVersions.Add(version);
            }
            if(selected.Count>0 && (guardedVersions.Count!=guardedOrderNos.Count || !guardedVersionNos.SetEquals(guardedOrderSet)))
                throw new InvalidOperationException("选中来源订单版本快照不完整，未准备生成。");
            var summary=new {order_nos=numbers,commit_guard_scope="selected_source_orders",guarded_order_nos=guardedOrderNos,order_versions=guardedVersions,
                plant=plant,document_type=docs[0],doc_default_type="2",doc_date=DateTime.Today.ToString("yyyy-MM-dd"),
                selection_rules=new {allowed_item_prefix="1-",excluded_1_v_name_keywords=vWords,excluded_1_h_name_keywords=hWords},
                raw_candidate_count=candidateCount,selected_count=selected.Count,excluded_count=excluded.Count,selected=selected,excluded=excluded,
                auto_approve=false,skip_previously_generated_unassigned=true};
            if(selected.Count==0) return json.Serialize(new {ok=true,no_changes=true,saved=false,summary=summary});
            var sourceArray=new List<string>(sources).ToArray();
            var existing=OrderLinkedWorkorders(sourceArray);var existingIds=new HashSet<string>();
            foreach(var existingRow in existing) existingIds.Add(Convert.ToString(existingRow["ID"]));
            var durableSummary=json.Deserialize<Dictionary<string,object>>(json.Serialize(summary));
            durableSummary["existing_workorder_ids"]=new List<string>(existingIds);durableSummary["source_ids"]=sourceArray;
            return StorePlan(new OperationPlan {Module="MO_B002",Action="generate",Number=String.Join(",",numbers),Entity=data,Summary=durableSummary,
                OrderSourceIds=sourceArray,OrderExistingIds=existingIds,OrderNumbers=guardedOrderNos.ToArray(),OrderVersions=guardedVersions,OrderDocId=docId,OrderPlantId=(Guid)plant["ID"]});
        }
        catch(Exception e){return Error(e);}
    }

    private void ValidatePackagingOrderPlan(OperationPlan plan)
    {
        if(plan.OrderNumbers==null) return;
        var current=OrderHeadVersions(plan.OrderNumbers);var versions=new Dictionary<string,long>();
        foreach(var row in plan.OrderVersions) versions.Add(Convert.ToString(row["ID"]),Convert.ToInt64(row["VERSION"]));
        if(current.Count!=versions.Count) throw new InvalidOperationException("订单记录已变化，未生成。");
        foreach(var row in current) if(!versions.ContainsKey(Convert.ToString(row["ID"])) || versions[Convert.ToString(row["ID"])]!=Convert.ToInt64(row["VERSION"]) || Convert.ToString(row["APPROVAL_STATUS"])!="Y" || Convert.ToString(row["CLOSE"])!="0")
            throw new InvalidOperationException("订单版本或状态在准备后变化，未生成。");
        var linked=OrderLinkedWorkorders(plan.OrderSourceIds);var ids=new HashSet<string>();foreach(var row in linked) ids.Add(Convert.ToString(row["ID"]));
        if(!ids.SetEquals(plan.OrderExistingIds)) throw new InvalidOperationException("关联工单集合在准备后变化，未重复生成。");
    }

    private object PackagingOrderResult(OperationPlan plan)
    {
        var linked=OrderLinkedWorkorders(plan.OrderSourceIds);var documents=new Dictionary<string,Dictionary<string,object>>();
        foreach(var row in linked) {
            var id=Convert.ToString(row["ID"]);if(plan.OrderExistingIds.Contains(id)) continue;
            if((Guid)row["DOC_ID"]!=plan.OrderDocId || (Guid)row["PLANT_ID"]!=plan.OrderPlantId || !Convert.ToString(row["ITEM_CODE"]).StartsWith("1-",StringComparison.Ordinal))
                throw new InvalidOperationException("发现新增工单的工厂、单别或成品范围不符，需要核实实际结果。");
            if(!documents.ContainsKey(id)) documents.Add(id,row);
        }
        if(documents.Count==0) throw new InvalidOperationException("生成接口已调用，但未确认到新增工单；请查实际结果，不自动重试。");
        return new {order_nos=plan.OrderNumbers,generated=true,generated_count=documents.Count,documents=new List<Dictionary<string,object>>(documents.Values),auto_approve=false};
    }
}
