using System;
using System.Collections;
using System.Collections.Generic;
using Digiwin.Common.Services;
using Digiwin.Common.Torridity;

public sealed partial class E10AccountSession
{
    private object ProbeEntity(DependencyObject entity, int depth)
    {
        var fields = new Dictionary<string, object>();
        var types = new Dictionary<string, string>();
        var children = new Dictionary<string, object>();
        foreach (var p in entity.GetDataEntityType().SimpleProperties)
        {
            var value = p.GetValue(entity);
            fields[p.Name] = CleanValue(value);
            types[p.Name] = value == null ? "null" : value.GetType().FullName;
        }
        if (depth > 0)
        {
            foreach (var p in entity.GetDataEntityType().ComplexProperties)
            {
                var child = p.GetValue(entity) as DependencyObject;
                if (child != null) children[p.Name] = ProbeEntity(child, depth - 1);
            }
            foreach (var p in entity.GetDataEntityType().CollectionProperties)
            {
                var collection = p.GetValue(entity);
                var rows = new List<object>();
                foreach (DependencyObject row in collection) { if (rows.Count >= 3) break; rows.Add(ProbeEntity(row, depth - 1)); }
                children[p.Name] = new { count = collection.Count, sample = rows };
            }
        }
        return new { entity_type = entity.GetDataEntityType().Name, fields = fields, value_types = types, children = children };
    }

    public string ProbeWriteContracts()
    {
        var results = new List<object>();
        try
        {
            var container = (IDataEntityTypeContainer)connector.GetService(typeof(IDataEntityTypeContainer), "MO_B002");
            var entity = (DependencyObject)container.DataEntityTypes["MO_B002"].CreateInstance();
            results.Add(new { operation = "view_draft", type_key = "MO_B002", ok = true, data = ProbeEntity(entity, 1) });
        }
        catch (Exception e) { results.Add(new { operation = "view_draft", ok = false, detail = Error(e) }); }
        foreach (var typeKey in new[] { "MO", "ISSUE_RECEIPT_REQ", "MO_B002" })
        {
            try
            {
                var create = (ICreateService)connector.GetService(typeof(ICreateService), typeKey);
                var draft = (DependencyObject)create.Create();
                results.Add(new { operation = "unsaved_draft", type_key = typeKey, ok = true, data = ProbeEntity(draft, 1) });
            }
            catch (Exception e) { results.Add(new { operation = "unsaved_draft", type_key = typeKey, ok = false, detail = Error(e) }); }
        }
        foreach (var typeKey in new[] { "MO", "ISSUE_RECEIPT_REQ" })
        {
            try
            {
                bool more;
                var fields = new string[,] { { "ID", typeKey + "_ID" }, { "DOC_NO", "DOC_NO" } };
                var number = typeKey == "MO" ? "5101-260924055" : "5D01-261015114";
                var rows = BrowseExact(typeKey, typeKey + ".I01", typeKey == "MO" ? "QPD-0019" : "QPD_0003", typeKey, "DOC_NO", number, fields, 2, false, out more);
                var read = (IReadService)connector.GetService(typeof(IReadService), typeKey);
                var entity = (DependencyObject)read.Read(rows[0]["ID"]);
                results.Add(new { operation = "read_existing", type_key = typeKey, ok = true, data = ProbeEntity(entity, 1) });
            }
            catch (Exception e) { results.Add(new { operation = "read_existing", type_key = typeKey, ok = false, detail = Error(e) }); }
        }
        return json.Serialize(results);
    }

    public string CheckDraftPersistence(string recordsJson)
    {
        var records = json.Deserialize<List<Dictionary<string, object>>>(recordsJson);
        var results = new List<object>();
        foreach (var record in records)
        {
            var module = Convert.ToString(record["module"]); CheckDocumentModule(module);
            var id = Guid.Parse(Convert.ToString(record["id"]));
            var exists = ((IReadService)connector.GetService(typeof(IReadService), module)).Contains(id);
            results.Add(new { module = module, id = id, exists_in_e10 = exists });
        }
        return json.Serialize(new { ok = true, checked_drafts = results });
    }
}
