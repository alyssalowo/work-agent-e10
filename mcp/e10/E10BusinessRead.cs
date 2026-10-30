using System;
using System.Collections;
using System.Collections.Generic;
using System.Data;
using System.Globalization;
using System.Text.RegularExpressions;
using Digiwin.Common.Services;
using Digiwin.Common.Torridity;
using Digiwin.Common.Torridity.Query;

public sealed partial class E10AccountSession
{
    private void RequireAction(string programId, string action)
    {
        if (!logOnService.IsAuthenticated) throw new InvalidOperationException("请先登录 E10。");
        var service = (IProgramRoleService)connector.GetService(typeof(IProgramRoleService), null);
        var roles = service.GetCurrentUserProgramAuthorizations();
        if (roles.IsAdministrator) return;
        foreach (var role in roles)
            if (role.ProgramId == programId && (role.IsAdministrator || role.Actions.Contains(action))) return;
        throw new UnauthorizedAccessException("当前账号缺少模块权限：" + programId + "/" + action);
    }

    private void ValidateDocumentNumber(string number)
    {
        if (String.IsNullOrWhiteSpace(number) || !Regex.IsMatch(number, @"\A[A-Za-z0-9]+-[A-Za-z0-9]+\z") || number.Length > 80)
            throw new ArgumentException("请提供完整单号，例如 5101-260924055。");
    }

    // All business access uses the client's browse service, fixed fields and exact equality.
    private List<Dictionary<string, object>> BrowseExact(string typeKey, string programId,
        string queryId, string entity, string conditionPath, string number,
        string[,] fields, int limit, bool detail, out bool hasNextPage)
    {
        var input = new QueryInputData {
            Id = queryId, MainEntityPath = entity, ProgramId = programId,
            SourceTypeKey = typeKey, IsIncludeDetail = detail
        };
        for (int i = 0; i < fields.GetLength(0); i++)
            input.QueryColumns.Add(new QueryColumn { Alias = fields[i, 0], Value = new PropertyValue { Path = fields[i, 1] } });
        var condition = new ConditionItem {
            Left = new PropertyValue { Path = conditionPath }, CompareOperator = CompareOperator.Equal
        };
        condition.Values.Add(new ConstantValue { Value = number, DataType = GeneralDBType.String });
        var service = (IBrowseService)connector.GetService(typeof(IBrowseService), typeKey);
        var result = service.ExecuteQuery(new RowNumberInfo(1, limit), input, condition,
            new ParameterCollection(), CultureInfo.GetCultureInfo("zh-CN"));
        hasNextPage = result.HasNextPage;
        var rows = new List<Dictionary<string, object>>();
        foreach (DependencyObject row in (IEnumerable)result.DataSource)
        {
            var values = new Dictionary<string, object>();
            foreach (var column in input.QueryColumns) values[column.Alias] = CleanValue(row[column.Alias]);
            rows.Add(values);
        }
        return rows;
    }

    private object CleanValue(object value)
    {
        if (value == null || value == DBNull.Value) return null;
        if (value is DateTime) return ((DateTime)value).ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);
        return value;
    }

    private static readonly string[,] WorkorderBrowseFields = new string[,] {
                { "MO_ID", "MO_ID" }, { "DOC_NO", "DOC_NO" },
                { "ITEM_CODE", "ITEM_ID.ITEM_CODE" }, { "ITEM_NAME", "ITEM_DESCRIPTION" },
                { "ITEM_FEATURE_CODE", "ITEM_FEATURE_ID.ITEM_FEATURE_CODE" },
                { "PLAN_QTY", "PLAN_QTY" }, { "COMPLETED_QTY", "COMPLETED_QTY" },
                { "PLANT_ID", "Owner_Org.ROid(PLANT).PLANT_ID" },
                { "SOURCE_TYPE", "SOURCE_ID.RTK" }, { "SOURCE_ID", "SOURCE_ID.ROid" },
                { "STATUS", "STATUS" }, { "DEMAND_NO", "DEMAND_NO" }
            };

    private List<Dictionary<string, object>> ReadWorkorderRows(string number, out bool more)
    {
        return BrowseExact("MO", "MO.I01", "QPD-0019", "MO", "DOC_NO", number, WorkorderBrowseFields, 5, false, out more);
    }

    public string QueryWorkorder(string number)
    {
        try
        {
            ValidateDocumentNumber(number);
            RequireAction("MO.I01", "BROWSE");
            bool more;
            var rows = ReadWorkorderRows(number, out more);
            return json.Serialize(new { ok = true, workorder_no = number, found = rows.Count > 0, rows = rows, has_next_page = more });
        }
        catch (Exception e) { return Error(e); }
    }

    public string QueryPackagingWorkorders(string prefix, string dateFrom, int start, int limit)
    {
        try {
            RequireAction("MO.I01","BROWSE");
            if(prefix!="5101-") throw new ArgumentException("此入口仅支持包装工单5101-。");
            var date=PickingDate(dateFrom);
            if(start<1 || limit<1 || limit>1000) throw new ArgumentException("分页范围不正确。");
            var conditions=new ConditionItemGroup {LogicalOperator=LogicalOperator.And};
            conditions.Items.Add(PickingCondition("DOC_NO",prefix+"%",GeneralDBType.String,CompareOperator.Like));
            conditions.Items.Add(PickingCondition("DOC_DATE",date,GeneralDBType.DateTime,CompareOperator.GreaterEqual));
            conditions.Items.Add(PickingCondition("ApproveStatus","V",GeneralDBType.String,CompareOperator.NotEqual));
            conditions.Items.Add(PickingCondition("STATUS","y",GeneralDBType.String,CompareOperator.NotEqual));
            conditions.Items.Add(PickingCondition("STATUS","Y",GeneralDBType.String,CompareOperator.NotEqual));
            var input=new QueryInputData {Id="MCP_PACKAGING_MATCH_SOURCE",SourceTypeKey="MO",MainEntityPath="MO",ProgramId="MO.I01",IsIncludeDetail=false,IsDistinct=true};
            var fields=new string[,] {{"ID","MO_ID"},{"DOC_NO","DOC_NO"},{"DOC_DATE","DOC_DATE"},{"APPROVAL_STATUS","ApproveStatus"},{"STATUS","STATUS"},
                {"DEMAND_NO","DEMAND_NO"},{"ITEM_CODE","ITEM_ID.ITEM_CODE"},{"ITEM_FEATURE_CODE","ITEM_FEATURE_ID.ITEM_FEATURE_CODE"},{"PLAN_QTY","PLAN_QTY"},
                {"PLANT_ID","Owner_Org.ROid(PLANT).PLANT_ID"},{"SOURCE_TYPE","SOURCE_ID.RTK"},{"SOURCE_ID","SOURCE_ID.ROid"}};
            for(int i=0;i<fields.GetLength(0);i++) input.QueryColumns.Add(new QueryColumn {Alias=fields[i,0],Value=new PropertyValue {Path=fields[i,1]}});
            input.OrderColumns.Add(new OrderByDict(new PropertyValue {Path="DOC_NO"},SortType.Asc,1,"DOC_NO"));
            input.OrderColumns.Add(new OrderByDict(new PropertyValue {Path="MO_ID"},SortType.Asc,2,"ID"));
            var result=((IBrowseService)connector.GetService(typeof(IBrowseService),"MO")).ExecuteQuery(new RowNumberInfo(start,start+limit-1),input,conditions,new ParameterCollection(),CultureInfo.GetCultureInfo("zh-CN"));
            var rows=new List<Dictionary<string,object>>();
            foreach(DependencyObject row in (IEnumerable)result.DataSource){var values=new Dictionary<string,object>();foreach(var field in input.QueryColumns)values[field.Alias]=CleanValue(row[field.Alias]);
                if(!Convert.ToString(values["DOC_NO"]).StartsWith(prefix,StringComparison.Ordinal) || Convert.ToDateTime(row["DOC_DATE"])<date || Convert.ToString(values["APPROVAL_STATUS"])=="V" || Convert.ToString(values["STATUS"])=="y" || Convert.ToString(values["STATUS"])=="Y") throw new InvalidOperationException("工单结果超出截图筛选范围。");
                rows.Add(values);}
            return json.Serialize(new {ok=true,filters=new {doc_no_prefix=prefix,date_from=dateFrom,excluded_approval_status="V",excluded_statuses=new [] {"y","Y"}},start_row=start,next_start_row=start+rows.Count,has_next_page=result.HasNextPage,rows=rows});
        }catch(Exception e){return Error(e);}
    }

    public string QueryWorkorderWorkflow(string plannerName, string docCode, string approvalStatus, string docDate, int start, int limit)
    {
        try {
            RequireAction("MO.I01","BROWSE");
            if(String.IsNullOrWhiteSpace(plannerName) || plannerName.Length>80) throw new ArgumentException("必须指定完整生管人员姓名。");
            if(approvalStatus!="N" && approvalStatus!="Y" && approvalStatus!="V") throw new ArgumentException("审核状态必须为N、Y或V。");
            if(start<1 || limit<1 || limit>500) throw new ArgumentException("分页范围不正确。");
            DateTime date;
            if (!DateTime.TryParseExact(docDate, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out date) ||
                date.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture) != docDate)
                throw new ArgumentException("doc_date必须为ISO格式yyyy-MM-dd。");
            date = date.Date;
            bool more;
            var employees=BrowseExact("EMPLOYEE","MO.I01","MCP_MO_PLANNER","EMPLOYEE","EMPLOYEE_NAME",plannerName,
                new string[,] {{"ID","EMPLOYEE_ID"},{"CODE","EMPLOYEE_CODE"},{"NAME","EMPLOYEE_NAME"}},2,false,out more);
            if(more || employees.Count!=1) throw new ArgumentException("生管人员姓名未唯一匹配，不能批量处理。");
            var docs=BrowseExact("DOC","MO.I01","MCP_MO_WORKFLOW_DOC","DOC","DOC_CODE",docCode,
                new string[,] {{"ID","DOC_ID"},{"CODE","DOC_CODE"},{"NAME","DOC_NAME"},{"CATEGORY","CATEGORY"}},2,false,out more);
            if(more || docs.Count!=1 || Convert.ToString(docs[0]["NAME"])!="包装工单" || Convert.ToString(docs[0]["CATEGORY"])!="51")
                throw new ArgumentException("单据类型未唯一匹配包装工单。");
            var conditions=new ConditionItemGroup {LogicalOperator=LogicalOperator.And};
            conditions.Items.Add(PickingCondition("Owner_Emp",employees[0]["ID"],GeneralDBType.Guid,CompareOperator.Equal));
            conditions.Items.Add(PickingCondition("DOC_ID",docs[0]["ID"],GeneralDBType.Guid,CompareOperator.Equal));
            conditions.Items.Add(PickingCondition("ApproveStatus",approvalStatus,GeneralDBType.String,CompareOperator.Equal));
            conditions.Items.Add(PickingCondition("DOC_DATE",date,GeneralDBType.DateTime,CompareOperator.GreaterEqual));
            conditions.Items.Add(PickingCondition("DOC_DATE",date.AddDays(1),GeneralDBType.DateTime,CompareOperator.LessThan));
            var input=new QueryInputData {Id="MCP_MO_WORKFLOW",SourceTypeKey="MO",MainEntityPath="MO",ProgramId="MO.I01",IsIncludeDetail=false,IsDistinct=true};
            var fields=new string[,] {{"ID","MO_ID"},{"DOC_NO","DOC_NO"},{"DOC_DATE","DOC_DATE"},{"DOC_ID","DOC_ID"},{"DOC_CODE","DOC_ID.DOC_CODE"},
                {"PLANNER_ID","Owner_Emp"},{"PLANNER_NAME","Owner_Emp.EMPLOYEE_NAME"},{"APPROVAL_STATUS","ApproveStatus"},{"VERSION","Version"},
                {"ITEM_CODE","ITEM_ID.ITEM_CODE"},{"ITEM_NAME","ITEM_DESCRIPTION"},{"PLAN_QTY","PLAN_QTY"},{"PLANT_ID","Owner_Org.ROid"}};
            for(int i=0;i<fields.GetLength(0);i++) input.QueryColumns.Add(new QueryColumn {Alias=fields[i,0],Value=new PropertyValue {Path=fields[i,1]}});
            input.OrderColumns.Add(new OrderByDict(new PropertyValue {Path="DOC_NO"},SortType.Asc,1,"DOC_NO"));
            input.OrderColumns.Add(new OrderByDict(new PropertyValue {Path="MO_ID"},SortType.Asc,2,"ID"));
            var result=((IBrowseService)connector.GetService(typeof(IBrowseService),"MO")).ExecuteQuery(new RowNumberInfo(start,start+limit-1),input,conditions,new ParameterCollection(),CultureInfo.GetCultureInfo("zh-CN"));
            var rows=new List<Dictionary<string,object>>();
            foreach(DependencyObject row in (IEnumerable)result.DataSource){var values=new Dictionary<string,object>();foreach(var field in input.QueryColumns)values[field.Alias]=CleanValue(row[field.Alias]);
                var rowDate = Convert.ToDateTime(values["DOC_DATE"], CultureInfo.InvariantCulture);
                if((Guid)values["PLANNER_ID"]!=(Guid)employees[0]["ID"] || Convert.ToString(values["PLANNER_NAME"])!=plannerName || (Guid)values["DOC_ID"]!=(Guid)docs[0]["ID"] || Convert.ToString(values["APPROVAL_STATUS"])!=approvalStatus || rowDate < date || rowDate >= date.AddDays(1))
                    throw new InvalidOperationException("工单查询结果超出筛选范围。");
                rows.Add(values);}
            return json.Serialize(new {ok=true,filters=new {planner_name=plannerName,doc_type_code=docCode,approval_status=approvalStatus,doc_date=docDate},planner=employees[0],document_type=docs[0],start_row=start,next_start_row=start+rows.Count,has_next_page=result.HasNextPage,rows=rows});
        }catch(Exception e){return Error(e);}
    }

    public string QueryPickingRequests(string number, int limit)
    {
        try
        {
            ValidateDocumentNumber(number);
            if (limit < 1 || limit > 500) throw new ArgumentException("查询上限需在 1 至 500 之间。");
            RequireAction("ISSUE_RECEIPT_REQ.I01", "BROWSE");
            bool more;
            var rows = BrowseExact("ISSUE_RECEIPT_REQ", "ISSUE_RECEIPT_REQ.I01", "QPD_0003",
                "ISSUE_RECEIPT_REQ", "ISSUE_RECEIPT_REQ_D.MO_ID.DOC_NO", number,
                new string[,] {
                    { "DOC_NO", "DOC_NO" }, { "DOC_DATE", "DOC_DATE" },
                    { "WORKORDER_NO", "ISSUE_RECEIPT_REQ_D.MO_ID.DOC_NO" },
                    { "REQUEST_QTY", "ISSUE_RECEIPT_REQ_D.REQUEST_QTY" },
                    { "WAREHOUSE_NAME", "ISSUE_RECEIPT_REQ_D.WAREHOUSE_ID.WAREHOUSE_NAME" }
                }, limit, true, out more);
            return json.Serialize(new { ok = true, workorder_no = number, rows = rows, has_next_page = more });
        }
        catch (Exception e) { return Error(e); }
    }

    private BatchContext PickingPreviewContext(string number, Dictionary<string, object> workorder)
    {
        var sourceType = Convert.ToString(workorder["SOURCE_TYPE"], CultureInfo.InvariantCulture);
        var processType = sourceType == "WORK_CENTER" ? "1" : sourceType == "SUPPLIER" ? "2" : "3";
        var context = new BatchContext(new DataParameter[] {
            SimpleDataParameter.CreateSingleParameter("PLANT_ID", workorder["PLANT_ID"]),
            SimpleDataParameter.CreateListParameter("MO_B012_MOS", new object[] { number }),
            SimpleDataParameter.CreateListParameter("MO_B012_ITEMS", new object[] { workorder["ITEM_CODE"] }),
            SimpleDataParameter.CreateSingleParameter("PROCESS_TYPE", processType),
            SimpleDataParameter.CreateSingleParameter("ISSUE_MODE", "1")
        });
        // Unset list filters must be null; empty arrays have different E10 semantics.
        foreach (var name in new[] { "MO_B012_DOCS", "MO_B012_OPERATIONS", "MO_B012_ITEM_FEATURE_VALUES",
            "MO_B012_PROJECTS", "MO_B012_FEATURE_GROUPS", "MO_B012_ITEM_FEATURES", "MO_ROUTING_D_OPERATION_SEQ", "MO_B012_SALES_ORDER_DOCS" })
            context.Add(new SimpleDataParameter(name, ParameterStyle.List, null));
        foreach (var name in new[] { "PLAN_COMPLETE_DATE", "PLAN_START_DATE", "MO_DOC_DATE" })
            context.Add(SimpleDataParameter.CreateRangeParameter(name, new DateTime(1900, 1, 1), new DateTime(9998, 12, 31)));
        foreach (var name in new[] { "OPERATION_DIFF", "PROJECT_DIFF", "WAREHOUSE_DIFF", "SOURCE_DIFF",
            "BY_SOURCE_QTY", "BY_IVENTORY_QTY", "IS_ISSUE_MULTIPLE" })
            context.Add(SimpleDataParameter.CreateSingleParameter(name, false));
        foreach (var name in new[] { "ADMIN_UNIT_ID", "EMPLOYEE_ID", "BIN_ID", "WAREHOUSE_ID", "NEW_DOC_ID",
            "FEATURE_ID", "WORK_CENTER_SUPPLIER_ID_NEW" })
            context.Add(SimpleDataParameter.CreateSingleParameter(name, Guid.Empty));
        context.Add(SimpleDataParameter.CreateSingleParameter("WORK_CENTER_SUPPLIER_ID", workorder["SOURCE_ID"]));
        context.Add(SimpleDataParameter.CreateSingleParameter("LOT_TYPE", "1"));
        context.Add(SimpleDataParameter.CreateSingleParameter("WARHOUSE_TYPE", "1"));
        context.Add(SimpleDataParameter.CreateSingleParameter("PROCESS_TYPE_NEW", sourceType));
        context.Add(SimpleDataParameter.CreateSingleParameter("APPLY_TRANSACTION_DATE", DateTime.Today));
        context.Add(SimpleDataParameter.CreateSingleParameter("APPLY_DATE", DateTime.Today));
        context.Add(SimpleDataParameter.CreateSingleParameter("FEATURE_CODE", ""));
        context.Add(SimpleDataParameter.CreateSingleParameter("ISSUE_DESTINATION_TYPE", "MO"));
        return context;
    }

    public string PreviewPickingRequest(string number)
    {
        try
        {
            ValidateDocumentNumber(number);
            RequireAction("MO.I01", "BROWSE");
            RequireAction("MO_B012.B01", "CREATE");
            bool more;
            var workorders = ReadWorkorderRows(number, out more);
            if (more || workorders.Count != 1)
                return json.Serialize(new { ok = false, error = "未找到唯一工单，不能执行预览。", matches = workorders.Count });
            var service = (IBatchPreviewService)connector.GetService(typeof(IBatchPreviewService), "MO_B012");
            if (!service.CanPreview()) throw new InvalidOperationException("该模块未提供预览接口。");
            var table = service.GetPreviewData(PickingPreviewContext(number, workorders[0]));
            var rows = new List<Dictionary<string, object>>();
            if (table != null)
                foreach (DataRow row in table.Rows)
                {
                    if (!table.Columns.Contains("DOC_NO") || Convert.ToString(row["DOC_NO"]) != number)
                        throw new InvalidOperationException("预览返回了所选工单以外的数据，本次结果未交付。");
                    var item = new Dictionary<string, object>();
                    foreach (DataColumn column in table.Columns) item[column.ColumnName] = CleanValue(row[column]);
                    rows.Add(item);
                }
            return json.Serialize(new { ok = true, preview_only = true, workorder_no = number,
                candidate_count = rows.Count, rows = rows, issue_destination = "MO", issue_mode = "1",
                note = rows.Count == 0 ? "接口调用成功，当前筛选条件下没有候选工单；未生成单据。" : "仅返回候选工单，未生成单据。" });
        }
        catch (Exception e) { return Error(e); }
    }
}
