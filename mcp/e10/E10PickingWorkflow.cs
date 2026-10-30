using System;
using System.Collections;
using System.Collections.Generic;
using System.Data;
using System.Globalization;
using Digiwin.Common.Services;
using Digiwin.Common.Torridity;
using Digiwin.Common.Torridity.Query;

public sealed partial class E10AccountSession
{
    private static DateTime PickingDate(string value)
    {
        DateTime date;
        if (!DateTime.TryParseExact(value, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out date))
            throw new ArgumentException("日期必须为 yyyy-MM-dd。");
        return date;
    }

    private Dictionary<string, object> PickingApplicant(string name, DateTime date)
    {
        // Native MO_B012/ISSUE_RECEIPT_REQ selectors expose EMPLOYEE_S4 reference data
        // under production permissions; employee maintenance permission is unrelated.
        RequireAction("MO_B012.B01", "CREATE");
        bool more;
        Guid employeeId;
        if (String.IsNullOrEmpty(name))
        {
            var users = BrowseExact("USER", "USER.I01", "MCP_PICKING_USER", "USER", "USER_ID", logOnService.CurrentUserId.ToString(),
                new string[,] {{"EMPLOYEE_ID", "EMPLOYEE_ID"}}, 2, false, out more);
            if (more || users.Count != 1 || users[0]["EMPLOYEE_ID"] == null || (Guid)users[0]["EMPLOYEE_ID"] == Guid.Empty)
                throw new InvalidOperationException("当前登录账号没有唯一对应的员工，请指定完整员工姓名。");
            employeeId = (Guid)users[0]["EMPLOYEE_ID"];
        }
        else
        {
            if (name != name.Trim() || name.Length > 80) throw new ArgumentException("请输入完整员工姓名，不带前后空格。");
            var employees = BrowseExact("EMPLOYEE", "MO_B012.B01", "MCP_PICKING_EMPLOYEE", "EMPLOYEE", "EMPLOYEE_NAME", name,
                new string[,] {{"ID", "EMPLOYEE_ID"}}, 2, false, out more);
            if (more || employees.Count != 1) throw new InvalidOperationException("员工姓名没有唯一匹配，不能采用登录名或首条记录代替。匹配数：" + employees.Count);
            employeeId = (Guid)employees[0]["ID"];
        }
        return PickingApplicantById(employeeId, date);
    }

    private Dictionary<string, object> PickingApplicantById(Guid id, DateTime date)
    {
        var employee = (DependencyObject)((IReadService)connector.GetService(typeof(IReadService), "EMPLOYEE")).Read(id);
        if (employee == null || (Guid)employee["EMPLOYEE_ID"] != id) throw new InvalidOperationException("员工资料不存在。");
        DependencyObject primary = null;
        foreach (DependencyObject row in (DependencyObjectCollection)employee["EMPLOYEE_D"])
        {
            if (Convert.ToBoolean(row["IS_PRIMARY"]) && (DateTime)row["START_DATE"] <= date && (DateTime)row["END_DATE"] > date)
            {
                if (primary != null) throw new InvalidOperationException("申请日期存在多个有效主部门，不能自动选择。");
                primary = row;
            }
        }
        if (primary == null || (Guid)primary["ADMIN_UNIT_ID"] == Guid.Empty)
            throw new InvalidOperationException("申请日期未找到唯一有效主部门。");
        return new Dictionary<string, object> {
            {"EMPLOYEE_ID", id}, {"EMPLOYEE_NAME", employee["EMPLOYEE_NAME"]}, {"EMPLOYEE_CODE", employee["EMPLOYEE_CODE"]},
            {"ADMIN_UNIT_ID", primary["ADMIN_UNIT_ID"]}, {"employee", EntityData(employee, 2)},
            {"apply_date", date.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture)}, {"unique", true}
        };
    }

    public string ResolvePickingApplicant(string name, string applyDate)
    {
        try
        {
            var result = PickingApplicant(name, String.IsNullOrEmpty(applyDate) ? DateTime.Today : PickingDate(applyDate));
            result["ok"] = true;
            return json.Serialize(result);
        }
        catch (Exception e) { return Error(e); }
    }

    private static ConditionItem PickingCondition(string path, object value, GeneralDBType type, CompareOperator op)
    {
        var condition = new ConditionItem { Left = new PropertyValue { Path = path }, CompareOperator = op };
        condition.Values.Add(new ConstantValue { Value = value is DateTime ? ((DateTime)value).ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture) : Convert.ToString(value, CultureInfo.InvariantCulture), DataType = type });
        return condition;
    }

    public string QueryPickingWorkflow(string filtersJson, bool detail, int start, int limit)
    {
        try
        {
            if (start < 1 || limit < 1 || limit > 1000 || start > Int32.MaxValue - limit)
                throw new ArgumentException("start_row 从1开始，max_rows 为1至1000。");
            var filters = json.Deserialize<Dictionary<string, object>>(filtersJson);
            var group = new ConditionItemGroup { LogicalOperator = LogicalOperator.And };
            if (filters.ContainsKey("workorder_no") && filters.ContainsKey("workorder_nos"))
                throw new ArgumentException("workorder_no与workorder_nos不能同时指定。");
            foreach (var pair in filters)
            {
                if (!("|applicant_name|approval_status|date_from|detail_close|workorder_no|workorder_nos|").Contains("|" + pair.Key + "|"))
                    throw new ArgumentException("不支持的筛选条件：" + pair.Key);
                if (pair.Key == "workorder_nos")
                {
                    var rawNumbers = pair.Value as IEnumerable;
                    if (rawNumbers == null || pair.Value is string) throw new ArgumentException("workorder_nos必须是工单号列表。");
                    var numbers = new List<string>(); var uniqueNumbers = new HashSet<string>(StringComparer.Ordinal);
                    foreach (var raw in rawNumbers)
                    {
                        var number = raw as string;
                        if (String.IsNullOrWhiteSpace(number)) throw new ArgumentException("workorder_nos中的工单号必须是非空字符串。");
                        ValidateDocumentNumber(number);
                        if (!uniqueNumbers.Add(number)) throw new ArgumentException("workorder_nos不能有重复工单号。");
                        numbers.Add(number);
                        if (numbers.Count > 500) throw new ArgumentException("workorder_nos最多500个工单号。");
                    }
                    if (numbers.Count == 0) throw new ArgumentException("workorder_nos至少包含一个工单号。");
                    var condition = new ConditionItem { Left = new PropertyValue { Path = "ISSUE_RECEIPT_REQ_D.MO_ID.DOC_NO" }, CompareOperator = CompareOperator.In };
                    foreach (var number in numbers) condition.Values.Add(new ConstantValue { Value = number, DataType = GeneralDBType.String });
                    group.Items.Add(condition);
                    continue;
                }
                if (!(pair.Value is string) || String.IsNullOrWhiteSpace((string)pair.Value)) throw new ArgumentException("筛选值必须是非空字符串。");
                var value = (string)pair.Value;
                switch (pair.Key)
                {
                    case "applicant_name":
                        // Exact name belongs to employee master, never to the login account.
                        bool more;
                        RequireAction("ISSUE_RECEIPT_REQ.I01", "BROWSE");
                        var employees = BrowseExact("EMPLOYEE", "ISSUE_RECEIPT_REQ.I01", "MCP_PICKING_NAME", "EMPLOYEE", "EMPLOYEE_NAME", value,
                            new string[,] {{"ID", "EMPLOYEE_ID"}}, 2, false, out more);
                        if (more || employees.Count != 1) throw new InvalidOperationException("申请人员姓名必须唯一匹配员工资料。");
                        group.Items.Add(PickingCondition("Owner_Emp", employees[0]["ID"], GeneralDBType.Guid, CompareOperator.Equal));
                        break;
                    case "approval_status":
                        if (value != "N" && value != "Y" && value != "V") throw new ArgumentException("approval_status 只能为 N、Y、V。");
                        group.Items.Add(PickingCondition("ApproveStatus", value, GeneralDBType.String, CompareOperator.Equal));
                        break;
                    case "date_from":
                        group.Items.Add(PickingCondition("DOC_DATE", PickingDate(value), GeneralDBType.DateTime, CompareOperator.GreaterEqual));
                        break;
                    case "detail_close":
                        if (value != "0" && value != "1" && value != "2") throw new ArgumentException("detail_close 只能为 0、1、2。");
                        group.Items.Add(PickingCondition("ISSUE_RECEIPT_REQ_D.CLOSE", value, GeneralDBType.String, CompareOperator.Equal));
                        break;
                    case "workorder_no":
                        ValidateDocumentNumber(value);
                        group.Items.Add(PickingCondition("ISSUE_RECEIPT_REQ_D.MO_ID.DOC_NO", value, GeneralDBType.String, CompareOperator.Equal));
                        break;
                }
            }
            if (group.Items.Count == 0) throw new ArgumentException("至少指定一个明确筛选条件，不默认套用日期或人员。");
            RequireAction("ISSUE_RECEIPT_REQ.I01", "BROWSE");
            var input = new QueryInputData { Id = "MCP_PICKING_WORKFLOW", MainEntityPath = "ISSUE_RECEIPT_REQ",
                ProgramId = "ISSUE_RECEIPT_REQ.I01", SourceTypeKey = "ISSUE_RECEIPT_REQ", IsIncludeDetail = detail, IsDistinct = true };
            var fields = new List<string[]> {
                new[] {"ID", "ISSUE_RECEIPT_REQ_ID"}, new[] {"DOC_NO", "DOC_NO"}, new[] {"DOC_DATE", "DOC_DATE"},
                new[] {"APPLICANT_ID", "Owner_Emp"}, new[] {"APPLICANT_NAME", "Owner_Emp.EMPLOYEE_NAME"}, new[] {"APPROVAL_STATUS", "ApproveStatus"}
            };
            if (detail) fields.AddRange(new[] {
                new[] {"DETAIL_ID", "ISSUE_RECEIPT_REQ_D.ISSUE_RECEIPT_REQ_D_ID"}, new[] {"WORKORDER_NO", "ISSUE_RECEIPT_REQ_D.MO_ID.DOC_NO"},
                new[] {"WAREHOUSE_NAME", "ISSUE_RECEIPT_REQ_D.WAREHOUSE_ID.WAREHOUSE_NAME"}, new[] {"ITEM_CODE", "ISSUE_RECEIPT_REQ_D.ITEM_ID.ITEM_CODE"},
                new[] {"DETAIL_CLOSE", "ISSUE_RECEIPT_REQ_D.CLOSE"}, new[] {"REQUEST_QTY", "ISSUE_RECEIPT_REQ_D.REQUEST_QTY"}
            });
            foreach (var field in fields) input.QueryColumns.Add(new QueryColumn { Alias = field[0], Value = new PropertyValue { Path = field[1] } });
            input.OrderColumns.Add(new OrderByDict(new PropertyValue { Path = "ISSUE_RECEIPT_REQ_ID" }, SortType.Asc, 1, "ID"));
            if (detail) input.OrderColumns.Add(new OrderByDict(new PropertyValue { Path = "ISSUE_RECEIPT_REQ_D.ISSUE_RECEIPT_REQ_D_ID" }, SortType.Asc, 2, "DETAIL_ID"));
            var service = (IBrowseService)connector.GetService(typeof(IBrowseService), "ISSUE_RECEIPT_REQ");
            // RowNumberInfo's second argument is the inclusive ending row, not the page size.
            var result = service.ExecuteQuery(new RowNumberInfo(start, start + limit - 1), input, group,
                new ParameterCollection(), CultureInfo.GetCultureInfo("zh-CN"));
            var rows = new List<Dictionary<string, object>>(); var seen = new HashSet<string>();
            foreach (DependencyObject row in (IEnumerable)result.DataSource)
            {
                var values = new Dictionary<string, object>();
                foreach (var field in fields) values[field[0]] = CleanValue(row[field[0]]);
                var key = Convert.ToString(values["ID"]) + (detail ? "/" + Convert.ToString(values["DETAIL_ID"]) : "");
                if (String.IsNullOrEmpty(Convert.ToString(values["ID"])) || (detail && String.IsNullOrEmpty(Convert.ToString(values["DETAIL_ID"]))) || !seen.Add(key))
                    throw new InvalidOperationException("分页返回缺少主键或重复行，不能作为完整清单。");
                rows.Add(values);
            }
            if (rows.Count > limit || (result.HasNextPage && rows.Count == 0)) throw new InvalidOperationException("分页结果未遵循指定范围。");
            return json.Serialize(new { ok = true, filters = filters, include_detail = detail, start_row = start,
                next_start_row = start + rows.Count, has_next_page = result.HasNextPage, rows = rows,
                order_by = detail ? "ID,DETAIL_ID" : "ID", consistency = "live; concurrent ERP changes require a fresh collection" });
        }
        catch (Exception e) { return Error(e); }
    }

    private List<Dictionary<string, object>> ReadPickingBatchWorkorders(string[] numbers)
    {
        var selected = new HashSet<string>(numbers, StringComparer.Ordinal);
        var input = new QueryInputData { Id = "QPD-0019", MainEntityPath = "MO", ProgramId = "MO.I01", SourceTypeKey = "MO", IsIncludeDetail = false };
        for (int i = 0; i < WorkorderBrowseFields.GetLength(0); i++)
            input.QueryColumns.Add(new QueryColumn { Alias = WorkorderBrowseFields[i, 0], Value = new PropertyValue { Path = WorkorderBrowseFields[i, 1] } });
        var condition = new ConditionItem { Left = new PropertyValue { Path = "DOC_NO" }, CompareOperator = CompareOperator.In };
        foreach (var number in numbers) condition.Values.Add(new ConstantValue { Value = number, DataType = GeneralDBType.String });
        var result = ((IBrowseService)connector.GetService(typeof(IBrowseService), "MO")).ExecuteQuery(new RowNumberInfo(1, numbers.Length + 1),
            input, condition, new ParameterCollection(), CultureInfo.GetCultureInfo("zh-CN"));
        if (result.HasNextPage) throw new InvalidOperationException("批量工单查询结果超过指定范围，不能继续准备。");
        var rows = new List<Dictionary<string, object>>(); var seen = new HashSet<string>();
        foreach (DependencyObject row in (IEnumerable)result.DataSource)
        {
            var number = Convert.ToString(row["DOC_NO"]);
            if (!selected.Contains(number) || !seen.Add(number)) throw new InvalidOperationException("查询返回范围外或重复的工单，不能继续准备。");
            var values = new Dictionary<string, object>();
            foreach (var column in input.QueryColumns) values[column.Alias] = CleanValue(row[column.Alias]);
            rows.Add(values);
        }
        return rows;
    }

    public string PreparePickingBatch(string numbersJson, string optionsJson)
    {
        try
        {
            var numbers = json.Deserialize<string[]>(numbersJson);
            if (numbers == null || numbers.Length < 1 || numbers.Length > 200) throw new ArgumentException("每批必须有1至200个完整工单号。");
            var selected = new HashSet<string>(StringComparer.Ordinal);
            foreach (var number in numbers) { ValidateDocumentNumber(number); if (!selected.Add(number)) throw new ArgumentException("工单号不能重复：" + number); }
            RequireAction("MO.I01", "BROWSE"); RequireAction("MO_B012.B01", "CREATE");
            var options = json.Deserialize<Dictionary<string, object>>(optionsJson);
            foreach (var pair in options)
                if (!("|NEW_DOC_ID|EMPLOYEE_ID|ADMIN_UNIT_ID|WORK_CENTER_SUPPLIER_ID_NEW|PROCESS_TYPE_NEW|APPLY_DATE|APPLY_TRANSACTION_DATE|IS_ISSUE_MULTIPLE|BY_IVENTORY_QTY|BY_SOURCE_QTY|WARHOUSE_TYPE|WAREHOUSE_ID|BIN_ID|LOT_TYPE|SOURCE_DIFF|WAREHOUSE_DIFF|PROJECT_DIFF|OPERATION_DIFF|ISSUE_MODE|").Contains("|" + pair.Key + "|"))
                    throw new ArgumentException("不支持的领料生成选项：" + pair.Key);
            var workorders = ReadPickingBatchWorkorders(numbers); var missing = new List<object>();
            var byNumber = new Dictionary<string, Dictionary<string, object>>(StringComparer.Ordinal);
            foreach (var workorder in workorders) byNumber.Add(Convert.ToString(workorder["DOC_NO"]), workorder);
            foreach (var number in numbers)
                if (!byNumber.ContainsKey(number)) missing.Add(new { workorder_no = number, reason = "not_found" });
            if (workorders.Count == 0) return json.Serialize(new { ok = true, no_changes = true, saved = false, candidate_count = 0,
                candidates = new object[0], excluded_workorders = missing, workorder_nos = numbers });
            var first = workorders[0]; var items = new HashSet<string>(); var found = new List<string>();
            foreach (var workorder in workorders)
            {
                if (!Object.Equals(first["PLANT_ID"], workorder["PLANT_ID"]) || !Object.Equals(first["SOURCE_TYPE"], workorder["SOURCE_TYPE"]) || !Object.Equals(first["SOURCE_ID"], workorder["SOURCE_ID"]))
                    throw new InvalidOperationException("批内工单的实际工厂或工作中心/供应商不同，请按实际来源分批。");
                if ((Guid)workorder["PLANT_ID"] == Guid.Empty || (Guid)workorder["SOURCE_ID"] == Guid.Empty)
                    throw new InvalidOperationException("工单缺少实际工厂或来源，不能使用空GUID准备。");
                if (Convert.ToString(workorder["SOURCE_TYPE"]) != "WORK_CENTER" && Convert.ToString(workorder["SOURCE_TYPE"]) != "SUPPLIER")
                    throw new InvalidOperationException("当前批量准备只支持实际工作中心或供应商来源。");
                found.Add(Convert.ToString(workorder["DOC_NO"])); items.Add(Convert.ToString(workorder["ITEM_CODE"]));
            }
            var context = PickingPreviewContext(found[0], first);
            context["MO_B012_MOS"].Value = found.ToArray();
            context["MO_B012_ITEMS"].Value = new List<string>(items).ToArray();
            context["WORK_CENTER_SUPPLIER_ID_NEW"].Value = first["SOURCE_ID"];
            foreach (var pair in options)
            {
                if (!("|NEW_DOC_ID|EMPLOYEE_ID|ADMIN_UNIT_ID|WORK_CENTER_SUPPLIER_ID_NEW|PROCESS_TYPE_NEW|APPLY_DATE|APPLY_TRANSACTION_DATE|IS_ISSUE_MULTIPLE|BY_IVENTORY_QTY|BY_SOURCE_QTY|WARHOUSE_TYPE|WAREHOUSE_ID|BIN_ID|LOT_TYPE|SOURCE_DIFF|WAREHOUSE_DIFF|PROJECT_DIFF|OPERATION_DIFF|ISSUE_MODE|").Contains("|" + pair.Key + "|"))
                    throw new ArgumentException("不支持的领料生成选项：" + pair.Key);
                context[pair.Key].Value = ConvertField(pair.Value, context[pair.Key].Value.GetType());
            }
            if (options.ContainsKey("APPLY_DATE") && !options.ContainsKey("APPLY_TRANSACTION_DATE"))
                context["APPLY_TRANSACTION_DATE"].Value = context["APPLY_DATE"].Value;
            if (!Object.Equals(context["WORK_CENTER_SUPPLIER_ID_NEW"].Value, first["SOURCE_ID"]) || !Object.Equals(context["PROCESS_TYPE_NEW"].Value, first["SOURCE_TYPE"]))
                throw new ArgumentException("批量准备必须使用工单实际工作中心/供应商及性质。");
            foreach (var field in new[] { "NEW_DOC_ID", "EMPLOYEE_ID", "ADMIN_UNIT_ID" })
                if ((Guid)context[field].Value == Guid.Empty) throw new ArgumentException("请先取得并指定当前主数据：" + field);
            // Match MO_B012 QPD_006's actual document-type selector conditions.
            bool moreDocs;
            var documents = BrowseExact("DOC", "MO_B012.B01", "MCP_PICKING_DOC", "DOC", "DOC_ID", context["NEW_DOC_ID"].Value.ToString(),
                new string[,] { {"ID","DOC_ID"}, {"CATEGORY","CATEGORY"}, {"STATUS","ApproveStatus"}, {"CODING_BY","CODING_BY"},
                    {"ORG_TYPE","Owner_Org.RTK"}, {"PLANT_ID","Owner_Org.ROid"}, {"AUTO_APPROVE","AUTO_APPROVE"} }, 2, false, out moreDocs);
            if (moreDocs || documents.Count != 1 || Convert.ToString(documents[0]["CATEGORY"]) != "5D" || Convert.ToString(documents[0]["STATUS"]) != "Y"
                || Convert.ToString(documents[0]["CODING_BY"]) == "5" || Convert.ToString(documents[0]["ORG_TYPE"]) != "PLANT"
                || !Object.Equals(documents[0]["PLANT_ID"], first["PLANT_ID"]))
                throw new ArgumentException("单据类型不是当前工厂可用且已审核的领料申请单类型。");
            if (Convert.ToBoolean(documents[0]["AUTO_APPROVE"])) throw new ArgumentException("该类型会自动审核，当前批量工具仅支持生成后另行审核的类型。");
            var applicant = PickingApplicantById((Guid)context["EMPLOYEE_ID"].Value, (DateTime)context["APPLY_DATE"].Value);
            if (!Object.Equals(applicant["ADMIN_UNIT_ID"], context["ADMIN_UNIT_ID"].Value)) throw new ArgumentException("申请部门不是该员工申请日期的唯一有效主部门。");
            var service = (IBatchPreviewService)connector.GetService(typeof(IBatchPreviewService), "MO_B012");
            if (!service.CanPreview()) throw new InvalidOperationException("当前模块没有提供预览能力。");
            var table = service.GetPreviewData(context); var candidates = new List<object>(); var candidateOrders = new HashSet<string>();
            var foundScope = new HashSet<string>(found, StringComparer.Ordinal);
            if (table != null) foreach (DataRow row in table.Rows)
            {
                if (!table.Columns.Contains("DOC_NO") || !foundScope.Contains(Convert.ToString(row["DOC_NO"])))
                    throw new InvalidOperationException("预览包含本批以外的工单，未保存准备操作。");
                var selection = new PropertyValueCollection(); var values = new Dictionary<string, object>();
                foreach (DataColumn col in table.Columns) { selection.Add(col.ColumnName, row[col]); values[col.ColumnName] = CleanValue(row[col]); }
                context.PreviewSelectedData.Add(selection); candidates.Add(values); candidateOrders.Add(Convert.ToString(row["DOC_NO"]));
            }
            foreach (var number in found) if (!candidateOrders.Contains(number)) missing.Add(new { workorder_no = number, reason = "no_preview_candidate" });
            if (candidates.Count == 0) return json.Serialize(new { ok = true, no_changes = true, saved = false, candidate_count = 0,
                candidates = candidates, excluded_workorders = missing, workorder_nos = numbers });
            var effective = new Dictionary<string, object>();
            foreach (var key in new[] { "NEW_DOC_ID", "EMPLOYEE_ID", "ADMIN_UNIT_ID", "WORK_CENTER_SUPPLIER_ID_NEW", "PROCESS_TYPE_NEW", "APPLY_DATE", "APPLY_TRANSACTION_DATE", "SOURCE_DIFF", "WAREHOUSE_DIFF" })
                effective[key] = CleanValue(context[key].Value);
            var stored = json.Deserialize<Dictionary<string, object>>(StorePlan(new OperationPlan { Module = "MO_B012", Action = "generate", Number = String.Join(",", found),
                WorkorderNumbers = found.ToArray(), Batch = context, Summary = new { workorder_nos = numbers, options = effective, candidate_count = candidates.Count,
                    candidates = candidates, excluded_workorders = missing } }));
            stored["candidate_count"] = candidates.Count; stored["candidates"] = candidates; stored["excluded_workorders"] = missing; stored["workorder_nos"] = numbers;
            return json.Serialize(stored);
        }
        catch (Exception e) { return Error(e); }
    }
}
