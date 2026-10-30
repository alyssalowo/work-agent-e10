using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using Digiwin.Common.Services;
using Digiwin.Common.Torridity;
using Digiwin.Common.Torridity.Metadata;
using Digiwin.ERP.MO_B002.Business;
using Digiwin.ERP.GenerateIssueReturnService.Business;

public sealed partial class E10AccountSession
{
    private sealed class OperationPlan
    {
        public string Id, Module, Action, Number, User, State;
        public DateTime Created;
        public object Oid;
        public long Version;
        public DependencyObject Entity;
        public BatchContext Batch;
        public object Summary;
        public GenerateIssueReturnParameters ReturnParameters;
        public string ReturnFingerprint;
        public string ManualSpec, ManualFingerprint;
        public string[] WorkorderNumbers;
        public string[] OrderSourceIds, OrderNumbers;
        public HashSet<string> OrderExistingIds;
        public List<Dictionary<string,object>> OrderVersions;
        public Guid OrderDocId, OrderPlantId;
        public string PlanKind, PlannerName, DocTypeCode, ApprovalStatus, SelectionScope, DocDateFilter;
        public Guid PreparedUserId;
        public List<ConfirmBatchItem> ConfirmItems;
        public List<ConfirmBatchItem> ExpectedConfirmItems;
        public int CollectedCount;
        public string[] ExcludedIds, SelectedIds;
    }
    private sealed class ConfirmBatchItem
    {
        public Guid Oid, DocId, PlannerId;
        public string Number, DocCode, PlannerName;
        public DateTime DocDate;
        public long Version;
        public object Draft;
    }
    private readonly Dictionary<string, OperationPlan> operationPlans = new Dictionary<string, OperationPlan>();

    public string DiscardOperation(string id)
    {
        OperationPlan plan;
        if (!operationPlans.TryGetValue(id, out plan)) return json.Serialize(new { ok = true, discarded = false, note = "该内存草稿已不存在。" });
        if (plan.State == "started" || plan.State == "unknown") return json.Serialize(new { ok = false, error = "操作已进入执行阶段，需先核对 E10 实际结果。" });
        operationPlans.Remove(id);
        return json.Serialize(new { ok = true, discarded = true, saved = false });
    }

    private Dictionary<string, object> NormalizeInterruptedConfirmBatch(string path, Dictionary<string, object> record)
    {
        if (!record.ContainsKey("action") || Convert.ToString(record["action"]) != "confirm_batch" ||
            !record.ContainsKey("state") || Convert.ToString(record["state"]) != "started") return record;
        var confirmed = new List<string>(); var remaining = new List<string>(); object failed = null;
        foreach (var raw in (IEnumerable)record["items"])
        {
            var item = AsMap(raw); var state = Convert.ToString(item["state"]); var itemId = Convert.ToString(item["id"]);
            if (state == "in_flight")
            {
                item["state"] = "unknown";
                failed = new { id = itemId, document_no = item["document_no"], state = "unknown", error = "进程中断时该项处于执行中，结果需人工对账。" };
                state = "unknown";
            }
            if (state == "completed") confirmed.Add(itemId);
            else if (state == "prepared" || state == "not_attempted") remaining.Add(itemId);
            else if (state == "unknown" && failed == null)
                failed = new { id = itemId, document_no = item["document_no"], state = "unknown", error = "该项审核结果未确认，需人工对账。" };
        }
        record["ok"] = false; record["state"] = "unknown";
        record["confirmed_ids"] = confirmed.ToArray(); record["remaining_ids"] = remaining.ToArray(); record["failed_item"] = failed;
        record["note"] = "发现未完成的批量审核回执，未自动续跑；请对账后由用户另行指定剩余集合。";
        PersistBatchReceipt(path, record);
        return record;
    }

    private Dictionary<string, object> BatchStatusQueryResponse(string path, Dictionary<string, object> record)
    {
        var normalized = NormalizeInterruptedConfirmBatch(path, record);
        var response = new Dictionary<string, object>(normalized);
        var operationOk = normalized.ContainsKey("ok") && Convert.ToBoolean(normalized["ok"]);
        response["operation_ok"] = operationOk;
        response["ok"] = true;
        return response;
    }

    public string OperationStatus(string id, string receiptDirectory)
    {
        try
        {
            if (!logOnService.IsAuthenticated) throw new InvalidOperationException("请先登录 E10。");
            Guid parsed; if (!Guid.TryParseExact(id, "N", out parsed)) throw new ArgumentException("操作编号格式无效。");
            OperationPlan plan;
            if (operationPlans.TryGetValue(id, out plan))
            {
                if (plan.User != logOnService.CurrentUser.LogOnName) throw new UnauthorizedAccessException("该操作不属于当前账号。");
                if (plan.PlanKind == "workorder_confirm_batch")
                {
                    var batchPath = Path.Combine(receiptDirectory, id + ".json");
                    if (File.Exists(batchPath)) return json.Serialize(BatchStatusQueryResponse(batchPath,
                        json.Deserialize<Dictionary<string, object>>(File.ReadAllText(batchPath))));
                }
                return json.Serialize(new { ok = true, operation_id = id, state = plan.State, module = plan.Module, action = plan.Action, summary = plan.Summary });
            }
            var path = Path.Combine(receiptDirectory, id + ".json");
            if (!File.Exists(path)) return json.Serialize(new { ok = false, error = "未找到该操作记录。" });
            var record = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
            if (!record.ContainsKey("user") || Convert.ToString(record["user"]) != logOnService.CurrentUser.LogOnName) throw new UnauthorizedAccessException("该操作不属于当前账号。");
            if (record.ContainsKey("action") && Convert.ToString(record["action"]) == "confirm_batch")
                record = BatchStatusQueryResponse(path, record);
            return json.Serialize(record);
        }
        catch (Exception e) { return Error(e); }
    }

    public string PreparePickingGeneration(string number, string optionsJson)
    {
        try
        {
            ValidateDocumentNumber(number); RequireAction("MO.I01", "BROWSE"); RequireAction("MO_B012.B01", "CREATE");
            bool more; var workorders = ReadWorkorderRows(number, out more);
            if (more || workorders.Count != 1) throw new InvalidOperationException("必须找到唯一工单。");
            var context = PickingPreviewContext(number, workorders[0]);
            var options = json.Deserialize<Dictionary<string, object>>(optionsJson);
            foreach (var pair in options)
            {
                if (!("|NEW_DOC_ID|EMPLOYEE_ID|ADMIN_UNIT_ID|WORK_CENTER_SUPPLIER_ID_NEW|PROCESS_TYPE_NEW|APPLY_DATE|APPLY_TRANSACTION_DATE|IS_ISSUE_MULTIPLE|BY_IVENTORY_QTY|BY_SOURCE_QTY|WARHOUSE_TYPE|WAREHOUSE_ID|BIN_ID|LOT_TYPE|SOURCE_DIFF|WAREHOUSE_DIFF|PROJECT_DIFF|OPERATION_DIFF|ISSUE_MODE|").Contains("|" + pair.Key + "|"))
                    throw new ArgumentException("不支持的领料生成选项：" + pair.Key);
                var current = context[pair.Key].Value;
                context[pair.Key].Value = ConvertField(pair.Value, current.GetType());
            }
            var table = ((IBatchPreviewService)connector.GetService(typeof(IBatchPreviewService), "MO_B012")).GetPreviewData(context);
            var candidates = new List<object>();
            if (table != null)
                foreach (System.Data.DataRow row in table.Rows)
                {
                    if (Convert.ToString(row["DOC_NO"]) != number) throw new InvalidOperationException("预览包含所选工单以外的数据，已停止。");
                    var selection = new PropertyValueCollection(); var values = new Dictionary<string, object>();
                    foreach (System.Data.DataColumn col in table.Columns) { selection.Add(col.ColumnName, row[col]); values[col.ColumnName] = CleanValue(row[col]); }
                    context.PreviewSelectedData.Add(selection); candidates.Add(values);
                }
            if (candidates.Count == 0) return json.Serialize(new { ok = true, no_changes = true, workorder_no = number, candidate_count = 0, saved = false });
            foreach (var required in new[] { "NEW_DOC_ID", "EMPLOYEE_ID", "ADMIN_UNIT_ID", "WORK_CENTER_SUPPLIER_ID_NEW" })
                if ((Guid)context[required].Value == Guid.Empty) throw new ArgumentException("正式生成前请指定：" + required);
            return StorePlan(new OperationPlan { Module = "MO_B012", Action = "generate", Number = number, Batch = context,
                Summary = new { workorder_no = number, options = options, candidate_count = candidates.Count, candidates = candidates } });
        }
        catch (Exception e) { return Error(e); }
    }

    private string CheckDocumentModule(string module)
    {
        if (module != "MO" && module != "ISSUE_RECEIPT_REQ") throw new ArgumentException("module 只能是 MO 或 ISSUE_RECEIPT_REQ。");
        return module + ".I01";
    }

    private object ConvertField(object value, Type type)
    {
        if (type == typeof(Guid)) return value is Guid ? value : Guid.Parse(Convert.ToString(value, CultureInfo.InvariantCulture));
        if (type == typeof(DateTime)) return value is DateTime ? value : DateTime.ParseExact(Convert.ToString(value), new[] { "yyyy-MM-dd", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-ddTHH:mm:ss" }, CultureInfo.InvariantCulture, DateTimeStyles.None);
        if (type == typeof(bool))
        {
            if (!(value is bool)) throw new ArgumentException("布尔字段必须使用 true 或 false。");
            return value;
        }
        if (value == null) throw new ArgumentException("字段不能为 null；清空文本请使用空字符串。");
        return Convert.ChangeType(value, type, CultureInfo.InvariantCulture);
    }

    private DependencyObject ReadDocument(string module, string number)
    {
        ValidateDocumentNumber(number);
        bool more;
        var rows = BrowseExact(module, CheckDocumentModule(module), module == "MO" ? "QPD-0019" : "QPD_0003",
            module, "DOC_NO", number, new string[,] { { "ID", module + "_ID" }, { "DOC_NO", "DOC_NO" } }, 2, false, out more);
        if (more || rows.Count != 1) throw new InvalidOperationException("必须找到唯一单据，实际匹配数：" + rows.Count);
        var reader = (IReadService)connector.GetService(typeof(IReadService), module);
        return (DependencyObject)reader.Read(rows[0]["ID"]);
    }

    private object EntityData(DependencyObject entity, int depth)
    {
        var data = new SortedDictionary<string, object>();
        foreach (var property in entity.GetDataEntityType().SimpleProperties) data[property.Name] = CleanValue(property.GetValue(entity));
        if (depth > 0)
        {
            foreach (var property in entity.GetDataEntityType().ComplexProperties)
            {
                var value = property.GetValue(entity) as DependencyObject;
                if (value != null) data[property.Name] = EntityData(value, depth - 1);
            }
            foreach (var property in entity.GetDataEntityType().CollectionProperties)
            {
                var rows = new List<object>();
                foreach (DependencyObject row in property.GetValue(entity)) rows.Add(EntityData(row, depth - 1));
                data[property.Name] = rows;
            }
        }
        return data;
    }

    public string GetDocument(string module, string number)
    {
        try { RequireAction(CheckDocumentModule(module), "BROWSE"); return json.Serialize(new { ok = true, module = module, document = EntityData(ReadDocument(module, number), 3) }); }
        catch (Exception e) { return Error(e); }
    }

    public string LookupMaster(string kind, string code)
    {
        try
        {
            var allowed = new HashSet<string>(new[] { "ITEM", "PLANT", "DOC", "WORK_CENTER", "WAREHOUSE", "EMPLOYEE", "ADMIN_UNIT", "UNIT", "SUPPLIER", "PROJECT" });
            if (!allowed.Contains(kind) || String.IsNullOrWhiteSpace(code) || code.Length > 100) throw new ArgumentException("请指定受支持的基础资料类型和完整编码。");
            RequireAction("MO.I01", "BROWSE");
            var codeField = kind + "_CODE";
            var nameField = kind + "_NAME";
            bool more;
            var rows = BrowseExact(kind, kind + ".I01", "MCP_REFERENCE_EXACT", kind, codeField, code,
                new string[,] { { "ID", kind + "_ID" }, { "CODE", codeField }, { "NAME", nameField } }, 10, false, out more);
            return json.Serialize(new { ok = true, kind = kind, rows = rows, has_next_page = more, unique = rows.Count == 1 && !more });
        }
        catch (Exception e) { return Error(e); }
    }

    private bool WritableField(string name)
    {
        var allowed = "|DOC_DATE|DOC_ID|Owner_Emp|Owner_Dept|REMARK|URGENT|PLAN_QTY|PLAN_SECOND_QTY|PLAN_START_DATE|PLAN_COMPLETE_DATE|PLAN_LOT|ITEM_ID|ITEM_FEATURE_ID|BUSINESS_UNIT_ID|UNIT_ID|ITEM_ROUTING_ID|ITEM_ROUTING_CONTROL|BOM_DATE|BOM_VERSION_TIMES|ROUTING_CONTROL|RECEIPT_REQ_CONTROL|WAREHOUSE_ID|BIN_ID|ITEM_LOT_ID|PROJECT_ID|ADMIN_UNIT_ID|SOURCE_MO_ID|RMA|ISSUE_BY_OP_SEQ|REQUEST_QTY|INVENTORY_QTY|SECOND_QTY|REQUEST_SETS|ITEM_TYPE|MO_ID|MO_D_ID|MO_ROUTING_D_ID|LOT_MO_ID|REPLACED_MO_D_ID|REPLACED_QTY|SOURCE_TYPE|ITEM_DESCRIPTION|ITEM_SPECIFICATION|QTY_PER|DENOMINATOR|NUMERATOR|SCRAP_RATE|FIXED_SCRAP_QTY|REQUIRED_QTY|";
        return allowed.Contains("|" + name + "|") || System.Text.RegularExpressions.Regex.IsMatch(name, @"\AUDF(00[123]|01[123]|02[1-6]|04[12]|05[1-4])\z");
    }

    private void ApplyFields(DependencyObject entity, Dictionary<string, object> fields)
    {
        foreach (var pair in fields)
        {
            if (pair.Key == "Owner_Org" || pair.Key == "SOURCE_ID")
            {
                var values = pair.Value as Dictionary<string, object>;
                if (values == null || !values.ContainsKey("RTK") || !values.ContainsKey("ROid") || values.Count != 2) throw new ArgumentException(pair.Key + " 需同时指定 RTK 和 ROid。");
                var tag = Convert.ToString(values["RTK"]);
                if (pair.Key == "Owner_Org" ? tag != "PLANT" : (tag != "WORK_CENTER" && tag != "SUPPLIER")) throw new ArgumentException("不支持的组织或工作中心类型。");
                var target = (DependencyObject)entity[pair.Key]; target["RTK"] = tag; target["ROid"] = ConvertField(values["ROid"], typeof(Guid));
                continue;
            }
            var property = entity.DependencyObjectType.Properties[pair.Key];
            if (!WritableField(pair.Key) || pair.Key == entity.GetDataEntityType().Name + "_ID" || property == null || property.IsReadOnly)
                throw new ArgumentException("该字段不可直接写入：" + pair.Key);
            var value = ConvertField(pair.Value, property.PropertyType);
            if ((pair.Key == "PLAN_QTY" || pair.Key == "REQUEST_QTY" || pair.Key == "REQUEST_SETS") && Convert.ToDecimal(value) <= 0) throw new ArgumentException(pair.Key + " 必须大于零。");
            entity[pair.Key] = value;
        }
    }

    private Dictionary<string, object> AsMap(object value)
    {
        var result = value as Dictionary<string, object>;
        if (result == null) throw new ArgumentException("参数必须是 JSON 对象。");
        return result;
    }

    private void ApplyLineChanges(string module, DependencyObject entity, object lineChanges)
    {
        if (lineChanges == null) return;
        foreach (var raw in (IEnumerable)lineChanges)
        {
            var change = AsMap(raw);
            var collectionName = Convert.ToString(change["collection"]);
            if (module == "MO" ? (collectionName != "MO_D" && collectionName != "MO_PRODUCT" && collectionName != "MO_DEMAND") : (collectionName != "ISSUE_RECEIPT_REQ_D" && collectionName != "ISSUE_RECEIPT_REQ_MO"))
                throw new ArgumentException("不允许修改该明细集合：" + collectionName);
            var collection = (DependencyObjectCollection)entity[collectionName];
            var action = Convert.ToString(change["action"]);
            DependencyObject row = null;
            if (action == "add")
            {
                row = collection.AddNew();
                var primaryKey = collectionName + "_ID";
                if (row.DependencyObjectType.Properties.IndexOfName(primaryKey) >= 0 && (Guid)row[primaryKey] == Guid.Empty) row[primaryKey] = Guid.NewGuid();
                if (row.DependencyObjectType.Properties.IndexOfName("SequenceNumber") >= 0) row["SequenceNumber"] = collection.Count;
            }
            else if (action == "update")
            {
                var id = Guid.Parse(Convert.ToString(change["row_id"]));
                foreach (DependencyObject item in collection) if ((Guid)item[collectionName + "_ID"] == id) { if (row != null) throw new ArgumentException("明细 ID 重复。"); row = item; }
                if (row == null) throw new ArgumentException("找不到指定明细行。");
            }
            else throw new ArgumentException("明细 action 只能是 add 或 update。");
            ApplyFields(row, AsMap(change["fields"]));
        }
    }

    private void ValidateDraft(string module, DependencyObject entity)
    {
        if (Convert.ToString(((DependencyObject)entity["Owner_Org"])["RTK"]) != "PLANT" || (Guid)((DependencyObject)entity["Owner_Org"])["ROid"] == Guid.Empty)
            throw new ArgumentException("必须指定所属工厂。");
        if ((Guid)entity["DOC_ID"] == Guid.Empty || (DateTime)entity["DOC_DATE"] <= new DateTime(1900, 1, 1))
            throw new ArgumentException("必须指定单据类型和单据日期。");
        if (module == "MO" && ((Guid)entity["ITEM_ID"] == Guid.Empty || (decimal)entity["PLAN_QTY"] <= 0 || (Guid)entity["BUSINESS_UNIT_ID"] == Guid.Empty))
            throw new ArgumentException("工单必须指定产品、预计产量和业务单位。");
        var validator = connector.GetService(typeof(ICommonValidateService), module) as ICommonValidateService;
        if (validator != null) validator.Validate(new object[] { entity }, "Save");
        // SaveAndReturn is the authoritative server validation boundary when no preflight validator is published.
    }

    private string StorePlan(OperationPlan plan)
    {
        plan.Id = Guid.NewGuid().ToString("N"); plan.User = logOnService.CurrentUser.LogOnName; plan.Created = DateTime.UtcNow; plan.State = "prepared";
        if (plan.PreparedUserId == Guid.Empty && logOnService.IsAuthenticated) plan.PreparedUserId = CurrentE10UserId();
        operationPlans.Add(plan.Id, plan);
        if (plan.PlanKind == "workorder_confirm_batch") return json.Serialize(new { ok = true, operation_id = plan.Id,
            state = plan.State, module = plan.Module, action = plan.Action, expires_in_minutes = 30, saved = false,
            ready_to_commit = true, collected_count = plan.CollectedCount, selected_count = plan.ConfirmItems.Count,
            excluded_ids = plan.ExcludedIds, selected_ids = plan.SelectedIds,
            filters = new { planner_name = plan.PlannerName, doc_type_code = plan.DocTypeCode, approval_status = plan.ApprovalStatus, doc_date = plan.DocDateFilter },
            summary = plan.Summary });
        return json.Serialize(new { ok = true, operation_id = plan.Id, state = plan.State, module = plan.Module, action = plan.Action,
            document_no = plan.Number, summary = plan.Summary, expires_in_minutes = 30, saved = false });
    }

    private Guid CurrentE10UserId()
    {
        var value = logOnService.CurrentUserId;
        return value is Guid ? (Guid)value : Guid.Parse(Convert.ToString(value, CultureInfo.InvariantCulture));
    }

    private List<Dictionary<string, object>> ReadAllWorkorderWorkflow(string plannerName, string docCode, string docDate)
    {
        var rows = new List<Dictionary<string, object>>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        var seenNumbers = new HashSet<string>(StringComparer.Ordinal);
        var start = 1;
        while (true)
        {
            var page = json.Deserialize<Dictionary<string, object>>(QueryWorkorderWorkflow(plannerName, docCode, "N", docDate, start, 500));
            if (!page.ContainsKey("ok") || !Convert.ToBoolean(page["ok"]))
                throw new InvalidOperationException("完整读取未审核工单清单失败：" + (page.ContainsKey("error") ? Convert.ToString(page["error"]) : "未知查询错误"));
            if (!page.ContainsKey("filters")) throw new InvalidOperationException("工单查询未返回筛选条件。");
            if (!page.ContainsKey("start_row") || Convert.ToInt32(page["start_row"], CultureInfo.InvariantCulture) != start)
                throw new InvalidOperationException("工单查询分页起始位置不一致。");
            var filters = AsMap(page["filters"]);
            if (Convert.ToString(filters["planner_name"]) != plannerName || Convert.ToString(filters["doc_type_code"]) != docCode || Convert.ToString(filters["approval_status"]) != "N" || Convert.ToString(filters["doc_date"]) != docDate)
                throw new InvalidOperationException("工单查询筛选与冻结条件不一致。");
            var pageRows = (IEnumerable)page["rows"];
            var pageCount = 0;
            foreach (var raw in pageRows)
            {
                var row = AsMap(raw);
                var id = Convert.ToString(row["ID"], CultureInfo.InvariantCulture);
                var number = Convert.ToString(row["DOC_NO"], CultureInfo.InvariantCulture);
                if (String.IsNullOrWhiteSpace(id) || !seen.Add(id) || String.IsNullOrWhiteSpace(number) || !seenNumbers.Add(number))
                    throw new InvalidOperationException("工单查询返回空或重复ID/单号，不能准备批量审核。");
                var rowDate = Convert.ToDateTime(row["DOC_DATE"], CultureInfo.InvariantCulture);
                var day = DateTime.ParseExact(docDate, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None);
                if (Convert.ToString(row["PLANNER_NAME"]) != plannerName || Convert.ToString(row["DOC_CODE"]) != docCode || Convert.ToString(row["APPROVAL_STATUS"]) != "N" || rowDate < day || rowDate >= day.AddDays(1))
                    throw new InvalidOperationException("工单查询分页行不满足固定的生管、单别或未审核条件。");
                rows.Add(row);
                pageCount++;
            }
            var hasNext = Convert.ToBoolean(page["has_next_page"]);
            var next = Convert.ToInt32(page["next_start_row"], CultureInfo.InvariantCulture);
            if (next != start + pageCount) throw new InvalidOperationException("工单查询分页边界不连续。");
            if (!hasNext) break;
            if (pageCount == 0 || rows.Count > 100000) throw new InvalidOperationException("工单分页未前进或超出安全上限，不能准备批量审核。");
            start = next;
        }
        return rows;
    }

    private void ValidateConfirmBatchItem(ConfirmBatchItem item, DependencyObject current)
    {
        if ((Guid)current["MO_ID"] != item.Oid || Convert.ToString(current["DOC_NO"]) != item.Number ||
            (Guid)current["DOC_ID"] != item.DocId || (Guid)current["Owner_Emp"] != item.PlannerId ||
            Convert.ToString(current["ApproveStatus"]) != "N" || Convert.ToInt64(current["Version"]) != item.Version ||
            Convert.ToDateTime(current["DOC_DATE"], CultureInfo.InvariantCulture) != item.DocDate)
            throw new InvalidOperationException("冻结清单中的工单在准备后发生变化：" + item.Number);
    }

    private void ValidateConfirmBatchSnapshot(OperationPlan plan)
    {
        var live = ReadAllWorkorderWorkflow(plan.PlannerName, plan.DocTypeCode, plan.DocDateFilter);
        if (live.Count != plan.ExpectedConfirmItems.Count) throw new InvalidOperationException("未审核完整清单数量已变化，请重新准备。");
        var expected = new Dictionary<string, ConfirmBatchItem>(StringComparer.Ordinal);
        foreach (var item in plan.ExpectedConfirmItems) expected.Add(item.Oid.ToString(), item);
        foreach (var row in live)
        {
            ConfirmBatchItem item;
            var id = Convert.ToString(row["ID"], CultureInfo.InvariantCulture);
            if (!expected.TryGetValue(id, out item) || item.Number != Convert.ToString(row["DOC_NO"]) ||
                item.Version != Convert.ToInt64(row["VERSION"], CultureInfo.InvariantCulture) ||
                item.DocId != Guid.Parse(Convert.ToString(row["DOC_ID"], CultureInfo.InvariantCulture)) ||
                item.PlannerId != Guid.Parse(Convert.ToString(row["PLANNER_ID"], CultureInfo.InvariantCulture)) ||
                item.PlannerName != Convert.ToString(row["PLANNER_NAME"]) || Convert.ToString(row["APPROVAL_STATUS"]) != "N")
                throw new InvalidOperationException("未审核完整清单中的ID、单号、版本、单别或生管归属已变化，请重新准备。");
        }
        foreach (var item in plan.ConfirmItems) ValidateConfirmBatchItem(item, ReadDocument("MO", item.Number));
    }

    public string PrepareWorkorderConfirmationBatch(string expectedItemsJson, string plannerName, string docCode, string docDate,
        string selectionScope, string selectedIdsJson, string excludedIdsJson)
    {
        try
        {
            RequireAction("MO.I01", "CONFIRM"); RequireAction("MO.I01", "BROWSE");
            if (String.IsNullOrWhiteSpace(plannerName) || plannerName.Length > 80) throw new ArgumentException("必须指定完整生管人员姓名。");
            if (docCode != "5101") throw new ArgumentException("此批量审核入口仅支持包装工单单别5101。");
            DateTime requestedDay;
            if (!DateTime.TryParseExact(docDate, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out requestedDay) || requestedDay.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture) != docDate)
                throw new ArgumentException("doc_date必须为ISO格式yyyy-MM-dd。");
            if (selectionScope != "planner_pending" && selectionScope != "generated_only" && selectionScope != "continuation")
                throw new ArgumentException("selection_scope只能为planner_pending、generated_only或continuation。");
            var expected = new Dictionary<string, Dictionary<string, object>>(StringComparer.Ordinal);
            foreach (var raw in (IEnumerable)json.DeserializeObject(expectedItemsJson))
            {
                var row = AsMap(raw);
                var id = Convert.ToString(row["id"], CultureInfo.InvariantCulture);
                if (String.IsNullOrWhiteSpace(id) || expected.ContainsKey(id)) throw new ArgumentException("expected_items包含空或重复ID。");
                expected.Add(id, row);
                if (!row.ContainsKey("document_no") || !row.ContainsKey("version")) throw new ArgumentException("expected_items每项必须包含id、document_no、version。");
            }
            var selectedJson = json.DeserializeObject(selectedIdsJson);
            var selectedRequested = selectedJson == null ? null : new HashSet<string>(StringComparer.Ordinal);
            if (selectedJson != null) foreach (var raw in (IEnumerable)selectedJson)
            {
                var id = Convert.ToString(raw, CultureInfo.InvariantCulture);
                if (String.IsNullOrWhiteSpace(id) || !selectedRequested.Add(id)) throw new ArgumentException("selected_ids包含空或重复ID。");
            }
            var excluded = new HashSet<string>(StringComparer.Ordinal);
            foreach (var raw in (IEnumerable)json.DeserializeObject(excludedIdsJson))
            {
                var id = Convert.ToString(raw, CultureInfo.InvariantCulture);
                if (String.IsNullOrWhiteSpace(id) || !excluded.Add(id)) throw new ArgumentException("excluded_ids包含空或重复ID。");
            }
            if (selectionScope != "planner_pending" && selectedRequested == null)
                throw new ArgumentException(selectionScope + "必须显式提供selected_ids。");
            foreach (var id in excluded) if (!expected.ContainsKey(id)) throw new ArgumentException("excluded_ids不属于expected_items完整清单。");
            if (selectedRequested != null)
                foreach (var id in selectedRequested)
                {
                    if (!expected.ContainsKey(id)) throw new ArgumentException("selected_ids不属于expected_items完整清单。");
                    if (excluded.Contains(id)) throw new ArgumentException("selected_ids与excluded_ids不能交叉。");
                }

            var live = ReadAllWorkorderWorkflow(plannerName, docCode, docDate);
            if (live.Count != expected.Count) throw new InvalidOperationException("准备期间未审核清单数量变化，请重新收集完整清单。");
            foreach (var row in live)
            {
                var id = Convert.ToString(row["ID"], CultureInfo.InvariantCulture);
                Dictionary<string, object> original;
                if (!expected.TryGetValue(id, out original) || Convert.ToString(original["document_no"]) != Convert.ToString(row["DOC_NO"]) ||
                    Convert.ToInt64(original["version"], CultureInfo.InvariantCulture) != Convert.ToInt64(row["VERSION"], CultureInfo.InvariantCulture))
                    throw new InvalidOperationException("准备期间完整清单中的ID、单号或版本变化，请重新收集。");
            }
            if (selectionScope == "planner_pending" && selectedRequested != null)
            {
                var expectedSelected = new HashSet<string>(expected.Keys, StringComparer.Ordinal);
                expectedSelected.ExceptWith(excluded);
                if (!expectedSelected.SetEquals(selectedRequested))
                    throw new ArgumentException("planner_pending的selected_ids必须等于完整expected_items减excluded_ids。");
            }

            var items = new List<ConfirmBatchItem>();
            var expectedItems = new List<ConfirmBatchItem>();
            foreach (var row in live)
            {
                var id = Convert.ToString(row["ID"], CultureInfo.InvariantCulture);
                var item = new ConfirmBatchItem {
                    Oid = Guid.Parse(id), Number = Convert.ToString(row["DOC_NO"]),
                    DocDate = Convert.ToDateTime(row["DOC_DATE"], CultureInfo.InvariantCulture),
                    DocId = Guid.Parse(Convert.ToString(row["DOC_ID"], CultureInfo.InvariantCulture)),
                    DocCode = Convert.ToString(row["DOC_CODE"]), PlannerId = Guid.Parse(Convert.ToString(row["PLANNER_ID"], CultureInfo.InvariantCulture)),
                    PlannerName = Convert.ToString(row["PLANNER_NAME"]), Version = Convert.ToInt64(row["VERSION"], CultureInfo.InvariantCulture)
                };
                expectedItems.Add(item);
                if (!excluded.Contains(id) && (selectedRequested == null || selectedRequested.Contains(id)))
                {
                    var current = ReadDocument("MO", item.Number);
                    ValidateConfirmBatchItem(item, current);
                    item.Draft = EntityData(current, 3);
                    items.Add(item);
                }
            }
            var frozenItems = new List<object>();
            foreach (var item in expectedItems) frozenItems.Add(new { id = item.Oid.ToString(), document_no = item.Number, version = item.Version,
                owner_emp = item.PlannerId, owner_name = item.PlannerName, doc_id = item.DocId, doc_code = item.DocCode, approval_status = "N" });
            if (items.Count == 0) return json.Serialize(new { ok = true, no_changes = true, ready_to_commit = false,
                filters = new { planner_name = plannerName, doc_type_code = docCode, approval_status = "N", doc_date = docDate },
                selection_scope = selectionScope, collected_count = live.Count, excluded_ids = excluded,
                selected_ids = new string[0], selected_count = 0,
                summary = new { filters = new { planner_name = plannerName, doc_type_code = docCode, approval_status = "N", doc_date = docDate },
                    selection_scope = selectionScope, collected_count = live.Count, excluded_ids = excluded,
                    selected_ids = new string[0], selected_count = 0, ready_to_commit = false,
                    frozen_items = frozenItems, items = new object[0] },
                note = "选择结果为空，没有创建可提交操作。" });

            var summaryItems = new List<object>();
            foreach (var item in items) summaryItems.Add(new { id = item.Oid.ToString(), document_no = item.Number, version = item.Version, draft = item.Draft });
            var selectedIds = new string[items.Count];
            for (var i = 0; i < items.Count; i++) selectedIds[i] = items[i].Oid.ToString();
            var excludedIds = new string[excluded.Count]; excluded.CopyTo(excludedIds);
            return StorePlan(new OperationPlan { Module = "MO", Action = "confirm", PlanKind = "workorder_confirm_batch",
                PlannerName = plannerName, DocTypeCode = docCode, ApprovalStatus = "N", SelectionScope = selectionScope, DocDateFilter = docDate,
                PreparedUserId = CurrentE10UserId(), ConfirmItems = items, ExpectedConfirmItems = expectedItems, CollectedCount = live.Count,
                ExcludedIds = excludedIds, SelectedIds = selectedIds,
                Summary = new { filters = new { planner_name = plannerName, doc_type_code = docCode, approval_status = "N", doc_date = docDate },
                    selection_scope = selectionScope, collected_count = live.Count, excluded_ids = excludedIds, selected_ids = selectedIds,
                    selected_count = items.Count, ready_to_commit = true, frozen_items = frozenItems, items = summaryItems,
                    commit_mode = "single MCP call; serial per-document IConfirmService execution; not an E10 atomic batch" } });
        }
        catch (Exception e) { return Error(e); }
    }

    public string PrepareDocument(string module, string action, string number, string payloadJson)
    {
        try
        {
            var program = CheckDocumentModule(module);
            if (action != "create" && action != "update" && action != "confirm") throw new ArgumentException("action 只能是 create、update 或 confirm。");
            RequireAction(program, action == "create" ? "CREATE" : action == "update" ? "MODIFY" : "CONFIRM");
            var payload = json.Deserialize<Dictionary<string, object>>(payloadJson);
            var fields = payload.ContainsKey("fields") ? AsMap(payload["fields"]) : new Dictionary<string, object>();
            var lines = payload.ContainsKey("line_changes") ? payload["line_changes"] : null;
            DependencyObject entity;
            if (action == "create")
            {
                if (!String.IsNullOrEmpty(number)) throw new ArgumentException("新增单号由 E10 单据类型规则产生，请勿填 document_no。");
                if (fields.Count == 0) throw new ArgumentException("新增必须指定单据字段。");
                entity = (DependencyObject)((ICreateService)connector.GetService(typeof(ICreateService), module)).Create();
            }
            else
            {
                RequireAction(program, "BROWSE"); entity = ReadDocument(module, number);
                if (action == "confirm")
                {
                    if (fields.Count != 0 || (lines != null && ((IList)lines).Count != 0)) throw new ArgumentException("审核不接受字段变更。");
                    if (Convert.ToString(entity["ApproveStatus"]) == "Y") return json.Serialize(new { ok = true, no_changes = true, document_no = number, note = "单据已经审核，不重复审核。" });
                }
                else
                {
                    if (fields.Count == 0 && (lines == null || ((IList)lines).Count == 0)) throw new ArgumentException("请指定要修改的字段或明细。");
                    if (Convert.ToString(entity["ApproveStatus"]) == "Y") throw new InvalidOperationException("该单据已审核，需要使用对应变更流程，不能直接覆盖保存。");
                }
            }
            var version = Convert.ToInt64(entity["Version"]);
            if (action != "confirm") { ApplyFields(entity, fields); ApplyLineChanges(module, entity, lines); ValidateDraft(module, entity); }
            return StorePlan(new OperationPlan { Module = module, Action = action, Number = number, Entity = entity,
                Oid = entity[module + "_ID"], Version = version, Summary = new { fields = fields, line_changes = lines, draft = EntityData(entity, 2), validation = "字段类型与必要参数已检查；完整业务规则由正式保存接口校验。" } });
        }
        catch (Exception e) { return Error(e); }
    }

    public string DocumentSchema(string module)
    {
        try
        {
            RequireAction(CheckDocumentModule(module), "CREATE");
            var draft = (DependencyObject)((ICreateService)connector.GetService(typeof(ICreateService), module)).Create();
            return json.Serialize(new { ok = true, module = module, schema = SchemaData(draft.DependencyObjectType, 2),
                note = "引用字段使用已查询确认的 GUID；RTK/ROid 表示组织或来源。新增草稿尚未保存。" });
        }
        catch (Exception e) { return Error(e); }
    }

    private object SchemaData(DependencyObjectType type, int depth)
    {
        var fields = new List<object>();
        var entity = (DependencyObject)type.CreateInstance();
        foreach (var p in type.Properties)
        {
            var complex = entity.GetDataEntityType().ComplexProperties;
            fields.Add(new { name = p.Name, data_type = p.PropertyType.FullName, writable = WritableField(p.Name) && p.Name != type.Name + "_ID" && !p.IsReadOnly });
        }
        var children = new Dictionary<string, object>();
        if (depth > 0)
            foreach (var p in entity.GetDataEntityType().CollectionProperties)
                children[p.Name] = SchemaData(((DependencyObjectCollection)entity[p.Name]).ItemDependencyObjectType, depth - 1);
        return new { entity_type = type.Name, fields = fields, collections = children };
    }

    private void PersistBatchReceipt(string path, Dictionary<string, object> receipt)
    {
        var temporary = path + ".tmp";
        File.WriteAllText(temporary, json.Serialize(receipt), new UTF8Encoding(false));
        if (File.Exists(path)) File.Replace(temporary, path, null);
        else File.Move(temporary, path);
    }

    private string CommitWorkorderConfirmationBatch(OperationPlan plan, string receiptPath, Dictionary<string, object> receipt)
    {
        var entries = (List<Dictionary<string, object>>)receipt["items"];
        var confirmed = new List<string>();
        for (var i = 0; i < plan.ConfirmItems.Count; i++)
        {
            var item = plan.ConfirmItems[i];
            var entry = entries[i];
            var auditDate = DateTime.Now;
            var auditUserId = CurrentE10UserId();
            entry["state"] = "in_flight";
            entry["audit_user_id"] = auditUserId;
            entry["audit_user_name"] = logOnService.CurrentUser.LogOnName;
            entry["audit_date"] = auditDate.ToString("o", CultureInfo.InvariantCulture);
            PersistBatchReceipt(receiptPath, receipt);
            try
            {
                ((IConfirmService)connector.GetService(typeof(IConfirmService), "MO"))
                    .Execute(new ConfirmContext(item.Oid, logOnService.CurrentUserId, auditDate));
                var current = (DependencyObject)((IReadService)connector.GetService(typeof(IReadService), "MO")).Read(item.Oid);
                if (Convert.ToString(current["ApproveStatus"]) != "Y") throw new InvalidOperationException("审核调用已返回，但读回状态不是Y。");
                var returned = EntityData(current, 3);
                entry["state"] = "completed"; entry["result"] = returned;
                confirmed.Add(item.Oid.ToString());
                receipt["confirmed_ids"] = confirmed.ToArray();
                var remainingAfterItem = new List<string>();
                for (var j = i + 1; j < plan.ConfirmItems.Count; j++) remainingAfterItem.Add(plan.ConfirmItems[j].Oid.ToString());
                receipt["remaining_ids"] = remainingAfterItem.ToArray();
                PersistBatchReceipt(receiptPath, receipt);
            }
            catch (Exception e)
            {
                entry["state"] = "unknown";
                entry["error"] = json.DeserializeObject(Error(e));
                var remaining = new List<string>();
                for (var j = i + 1; j < plan.ConfirmItems.Count; j++) remaining.Add(plan.ConfirmItems[j].Oid.ToString());
                plan.State = "unknown";
                receipt["ok"] = false; receipt["state"] = "unknown";
                receipt["confirmed_ids"] = confirmed.ToArray(); receipt["remaining_ids"] = remaining.ToArray();
                receipt["failed_item"] = new { id = item.Oid, document_no = item.Number, state = "unknown", error = entry["error"] };
                receipt["note"] = "批次中一项结果未能确认，后续项目已停止；不得重放此operation_id。请对账后由用户另行指定剩余集合。";
                PersistBatchReceipt(receiptPath, receipt);
                return json.Serialize(new { ok = false, operation_id = plan.Id, user = plan.User, state = "unknown",
                    filters = new { planner_name = plan.PlannerName, doc_type_code = plan.DocTypeCode, approval_status = plan.ApprovalStatus, doc_date = plan.DocDateFilter },
                    result = new { items = entries, confirmed_ids = confirmed.ToArray(), remaining_ids = remaining.ToArray(), failed_item = receipt["failed_item"] },
                    note = receipt["note"] });
            }
        }
        return json.Serialize(new { ok = true, state = "completed",
            filters = new { planner_name = plan.PlannerName, doc_type_code = plan.DocTypeCode, approval_status = plan.ApprovalStatus, doc_date = plan.DocDateFilter },
            result = new { items = entries,
            confirmed_ids = confirmed.ToArray(), remaining_ids = new string[0], failed_item = (object)null } });
    }

    public string CommitOperation(string id, string receiptDirectory)
    {
        OperationPlan plan = null;
        try
        {
            if (!operationPlans.TryGetValue(id, out plan)) throw new ArgumentException("操作草稿不存在或会话已重启，请重新准备。");
            if (plan.User != logOnService.CurrentUser.LogOnName || plan.State != "prepared" || DateTime.UtcNow - plan.Created > TimeSpan.FromMinutes(30)) throw new InvalidOperationException("操作草稿已过期、已执行或不属于当前用户。");
            var program = plan.Module == "ISSUE_RETURN" ? "ISSUE_RECEIPT.I02" : plan.Module == "MO_B002" || plan.Module == "MO_B012" ? plan.Module + ".B01" : CheckDocumentModule(plan.Module);
            RequireAction(program, plan.Action == "confirm" ? "CONFIRM" : plan.Action == "update" ? "MODIFY" : "CREATE");
            if (plan.PlanKind == "workorder_confirm_batch")
            {
                if (CurrentE10UserId() != plan.PreparedUserId || logOnService.CurrentUser.LogOnName != plan.User)
                    throw new InvalidOperationException("当前E10审核身份与prepare时身份不一致，请重新准备。");
                ValidateConfirmBatchSnapshot(plan);
            }
            else if (plan.Action == "update" || plan.Action == "confirm")
            {
                var current = ReadDocument(plan.Module, plan.Number);
                if (Convert.ToInt64(current["Version"]) != plan.Version) throw new InvalidOperationException("单据在准备后已有变化，请重新准备。");
                if (Convert.ToString(current["ApproveStatus"]) == "Y") throw new InvalidOperationException("单据现已审核，请重新读取。");
            }
            if (plan.Module == "ISSUE_RETURN")
            {
                RequireAction("MO.I01", "BROWSE");
                var current = ReadDocument("MO", plan.Number);
                if (Convert.ToInt64(current["Version"]) != plan.Version || ReturnFingerprint(current, ReturnHistory(plan.Number)) != plan.ReturnFingerprint)
                    throw new InvalidOperationException("工单领退料记录已变化，请重新核对，未执行退料。");
            }
            if (plan.Module == "MO" && plan.Action == "create")
            {
                if (((DependencyObjectCollection)plan.Entity["MO_D"]).Count == 0)
                    throw new InvalidOperationException("工单缺少BOM子件，禁止保存；请使用完整BOM准备入口。");
                ValidateManualPlan(plan);
            }
            if (plan.Module == "MO_B002") ValidatePackagingOrderPlan(plan);
            Directory.CreateDirectory(receiptDirectory);
            var receipt = Path.Combine(receiptDirectory, id + ".json");
            if (File.Exists(receipt)) throw new InvalidOperationException("该操作已有执行记录，禁止重复提交。");
            Dictionary<string, object> batchReceipt = null;
            if (plan.PlanKind == "workorder_confirm_batch")
            {
                var entries = new List<Dictionary<string, object>>();
                foreach (var item in plan.ConfirmItems) entries.Add(new Dictionary<string, object> {
                    { "id", item.Oid.ToString() }, { "document_no", item.Number }, { "version", item.Version },
                    { "state", "prepared" }, { "result", null } });
                batchReceipt = new Dictionary<string, object> {
                    { "ok", false }, { "operation_id", id }, { "user", plan.User }, { "prepared_user_id", plan.PreparedUserId.ToString() },
                    { "module", "MO" }, { "action", "confirm_batch" }, { "state", "started" },
                    { "filters", new { planner_name = plan.PlannerName, doc_type_code = plan.DocTypeCode, approval_status = plan.ApprovalStatus, doc_date = plan.DocDateFilter } },
                    { "selection_scope", plan.SelectionScope }, { "started_utc", DateTime.UtcNow.ToString("o") },
                    { "items", entries }, { "confirmed_ids", new string[0] }, { "remaining_ids", plan.SelectedIds }
                };
                using (var file = new FileStream(receipt, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
                using (var writer = new StreamWriter(file, new UTF8Encoding(false))) writer.Write(json.Serialize(batchReceipt));
            }
            else
            {
                using (var file = new FileStream(receipt, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
                using (var writer = new StreamWriter(file, new UTF8Encoding(false))) writer.Write(json.Serialize(new { operation_id = id, user = plan.User, module = plan.Module, action = plan.Action, document_no = plan.Number, state = "started", started_utc = DateTime.UtcNow.ToString("o"), order_source_ids=plan.OrderSourceIds, existing_workorder_ids=plan.OrderExistingIds, order_plant_id=plan.OrderPlantId, order_doc_id=plan.OrderDocId }));
            }
            plan.State = "started";
            object result;
            if (plan.PlanKind == "workorder_confirm_batch")
            {
                var batchResult = json.Deserialize<Dictionary<string, object>>(CommitWorkorderConfirmationBatch(plan, receipt, batchReceipt));
                if (!Convert.ToBoolean(batchResult["ok"])) return json.Serialize(batchResult);
                result = batchResult["result"];
            }
            else if (plan.Module == "ISSUE_RETURN")
            {
                List<GenerateIssueReturnResultInfo> generated;
                if(plan.Action=="create") {
                    var numbering=(Digiwin.ERP.Common.Business.IDocumentNumberGenerateService)connector.GetService(typeof(Digiwin.ERP.Common.Business.IDocumentNumberGenerateService),"ISSUE_RECEIPT");
                    plan.Entity["DOC_NO"]=numbering.NextNumber(plan.ReturnParameters.DOC_ID,DateTime.Today);
                    ValidateDraft("ISSUE_RECEIPT",plan.Entity);
                    var saved=(DependencyObject)((ISaveService)connector.GetService(typeof(ISaveService),"ISSUE_RECEIPT")).SaveAndReturn(plan.Entity);
                    generated=new List<GenerateIssueReturnResultInfo>{new GenerateIssueReturnResultInfo{ISSUE_RECEIPT_ID=saved["ISSUE_RECEIPT_ID"],DOC_NO=Convert.ToString(saved["DOC_NO"])}};
                } else {
                    var service = (IGenerateIssueReturnService)connector.GetService(typeof(IGenerateIssueReturnService), "MO");
                    generated = service.GenerateIssueReturn(plan.ReturnParameters);
                }
                var references = new List<object>();
                if (generated != null) foreach(var item in generated) references.Add(new { id=item.ISSUE_RECEIPT_ID, document_no=item.DOC_NO });
                File.WriteAllText(receipt, json.Serialize(new { operation_id=id, user=plan.User, state="generated_pending_readback", module=plan.Module, documents=references }), new UTF8Encoding(false));
                var documents = new List<object>();
                if (generated != null)
                    foreach(var item in generated)
                    {
                        var document = (DependencyObject)((IReadService)connector.GetService(typeof(IReadService), "ISSUE_RECEIPT")).Read(item.ISSUE_RECEIPT_ID);
                        if(Convert.ToString(document["CATEGORY"]) != "57" || (Guid)document["DOC_ID"] != plan.ReturnParameters.DOC_ID) throw new InvalidOperationException("已生成单据需人工核对方向或类型：" + item.DOC_NO);
                        foreach(DependencyObject line in (DependencyObjectCollection)document["ISSUE_RECEIPT_D"])
                            if((Guid)line["MO_ID"] != (Guid)plan.Oid) throw new InvalidOperationException("已生成单据包含其他工单，需人工核对：" + item.DOC_NO);
                        documents.Add(EntityData(document, 2));
                    }
                if(documents.Count == 0) throw new InvalidOperationException("生成接口未返回单据，请核对 E10 实际结果后处理。");
                result = new { workorder_no=plan.Number, documents=documents };
            }
            else if (plan.Action == "confirm")
            {
                ((IConfirmService)connector.GetService(typeof(IConfirmService), plan.Module)).Execute(new ConfirmContext(plan.Oid, logOnService.CurrentUserId, DateTime.Now));
                result = EntityData((DependencyObject)((IReadService)connector.GetService(typeof(IReadService), plan.Module)).Read(plan.Oid), 1);
            }
            else if (plan.Module == "MO_B002")
            {
                ((IGetDataService)connector.GetService(typeof(IGetDataService), "MO_B002")).DoProcess(plan.Entity);
                File.WriteAllText(receipt, json.Serialize(new {operation_id=id,user=plan.User,module=plan.Module,state="generated_pending_readback",order_source_ids=plan.OrderSourceIds,existing_workorder_ids=plan.OrderExistingIds,returned_utc=DateTime.UtcNow.ToString("o")}), new UTF8Encoding(false));
                result = plan.OrderSourceIds == null ? (object)new { order_no = plan.Number, generated = true } : PackagingOrderResult(plan);
            }
            else if (plan.Module == "MO_B012")
            {
                ((IBatchService)connector.GetService(typeof(IBatchService), "MO_B012")).Process(plan.Batch);
                if (plan.WorkorderNumbers == null)
                    result = new { workorder_no = plan.Number, generated = true, picking_requests = json.DeserializeObject(QueryPickingRequests(plan.Number, 500)) };
                else
                {
                    result = new { workorder_nos = plan.WorkorderNumbers, generated = true,
                        note = "生成接口已正常返回；候选数不等于生成单数，请按本批工单查询关联申请单，与生成前清单比较。" };
                }
            }
            else
            {
                ValidateDraft(plan.Module, plan.Entity);
                if (plan.Module == "MO" && plan.Action == "create")
                {
                    var numbering=(Digiwin.ERP.Common.Business.IDocumentNumberGenerateService)connector.GetService(typeof(Digiwin.ERP.Common.Business.IDocumentNumberGenerateService),"MO");
                    plan.Entity["DOC_NO"]=numbering.NextNumber((Guid)plan.Entity["DOC_ID"],(DateTime)plan.Entity["DOC_DATE"]);
                    plan.Number=Convert.ToString(plan.Entity["DOC_NO"]);
                    File.WriteAllText(receipt,json.Serialize(new {operation_id=id,user=plan.User,module=plan.Module,action=plan.Action,document_no=plan.Number,state="started",entity_id=plan.Oid}),new UTF8Encoding(false));
                }
                var saved = (DependencyObject)((ISaveService)connector.GetService(typeof(ISaveService), plan.Module)).SaveAndReturn(plan.Entity);
                result = EntityData(saved, 2);
            }
            plan.State = "completed";
            var answer = plan.PlanKind == "workorder_confirm_batch"
                ? json.Serialize(new { ok = true, operation_id = id, user = plan.User, state = plan.State, module = plan.Module, action = plan.Action,
                    filters = new { planner_name = plan.PlannerName, doc_type_code = plan.DocTypeCode, approval_status = plan.ApprovalStatus, doc_date = plan.DocDateFilter }, result = result })
                : json.Serialize(new { ok = true, operation_id = id, user = plan.User, state = plan.State, module = plan.Module, action = plan.Action, result = result });
            File.WriteAllText(receipt, answer, new UTF8Encoding(false));
            return answer;
        }
        catch (Exception e)
        {
            if (plan != null && plan.PlanKind == "workorder_confirm_batch" && plan.State == "started")
            {
                plan.State = "unknown";
                var path = Path.Combine(receiptDirectory, id + ".json");
                try
                {
                    if (File.Exists(path))
                    {
                        var record = NormalizeInterruptedConfirmBatch(path, json.Deserialize<Dictionary<string, object>>(File.ReadAllText(path)));
                        record["error"] = json.DeserializeObject(Error(e));
                        PersistBatchReceipt(path, record);
                        return json.Serialize(record);
                    }
                }
                catch { }
                return json.Serialize(new { ok = false, operation_id = id, user = plan.User, state = "unknown",
                    confirmed_ids = new string[0], remaining_ids = plan.SelectedIds,
                    failed_item = new { id = (string)null, document_no = (string)null, state = "unknown", error = json.DeserializeObject(Error(e)) },
                    note = "批量审核已经进入执行阶段但回执读取失败，不能重放此operation_id。" });
            }
            if (plan != null && plan.PlanKind == "workorder_confirm_batch" && plan.State == "prepared")
            {
                plan.State = "rejected";
                return json.Serialize(new { ok = false, operation_id = id, user = plan.User, state = "rejected",
                    confirmed_ids = new string[0], remaining_ids = plan.SelectedIds, failed_item = (object)null,
                    error = json.DeserializeObject(Error(e)), note = "完整批次尚未开始审核，原操作不能自动缩小或提交。" });
            }
            if (plan != null && plan.PlanKind == "workorder_confirm_batch" && plan.State != "started")
                return json.Serialize(new { ok = false, operation_id = id, user = plan.User, state = plan.State,
                    confirmed_ids = (object)null, remaining_ids = (object)null, failed_item = (object)null,
                    error = json.DeserializeObject(Error(e)), note = "该批次已经进入或结束过一次提交阶段，禁止再次执行。" });
            if (plan != null && plan.State == "started")
            {
                plan.State = "unknown";
                var answer = json.Serialize(new { ok = false, operation_id = id, user = plan.User, state = "unknown", document_no = plan.Number, entity_id = plan.Oid, detail = json.DeserializeObject(Error(e)), note = "请求已经进入执行阶段，未自动重试。请查询 E10 实际结果再处理。" });
                try { File.WriteAllText(Path.Combine(receiptDirectory, id + ".json"), answer, new UTF8Encoding(false)); } catch { }
                return answer;
            }
            return Error(e);
        }
    }
}
