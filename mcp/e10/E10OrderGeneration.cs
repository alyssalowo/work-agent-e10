using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Xml;
using Digiwin.Common.Services;
using Digiwin.Common.Torridity;
using Digiwin.Common.Torridity.Metadata;
using Digiwin.ERP.MO_B002.Business;

public sealed partial class E10AccountSession
{
    public string PrepareOrderWorkorders(string number, string plantId, string optionsJson, string selectedJson, string clientDirectory)
    {
        try
        {
            ValidateDocumentNumber(number); RequireAction("MO_B002.B01", "CREATE");
            var plant = Guid.Parse(plantId); if (plant == Guid.Empty) throw new ArgumentException("必须指定工厂。");
            var root = NewOrderWizard(clientDirectory);
            ((DependencyObject)root["Owner_Org"])["RTK"] = "PLANT";
            ((DependencyObject)root["Owner_Org"])["ROid"] = plant;
            var range = (DependencyObject)root["SALES_ORDER_DOCS"];
            range["Operator"] = 0;
            var rangeItem = ((DependencyObjectCollection)range["RangeEntityItemDatas"]).AddNew();
            rangeItem["ID"] = Guid.NewGuid(); rangeItem["CODE"] = number; rangeItem["SN"] = 1;
            root["PLAN_SHIP_DATE_S"] = new DateTime(1900, 1, 1);
            root["PLAN_SHIP_DATE_E"] = new DateTime(9998, 12, 31);
            root["DOC_DATE"] = DateTime.Today; root["DOC_DEFAULT_TYPE"] = "1";
            var options = json.Deserialize<Dictionary<string, object>>(optionsJson);
            foreach (var pair in options)
            {
                if (!("|DOC_DATE|DOC_ID|DOC_DEFAULT_TYPE|Owner_Emp|Owner_Dept|CALCULAT_YIELD_RATE|CALCULAT_LOSS_RATE|CUSTOMER_MERGER|SO_MERGER|SHIP_DATE_MERGER|").Contains("|" + pair.Key + "|"))
                    throw new ArgumentException("不支持的向导选项：" + pair.Key);
                root[pair.Key] = ConvertField(pair.Value, root.DependencyObjectType.Properties[pair.Key].PropertyType);
            }
            var service = (IGetDataService)connector.GetService(typeof(IGetDataService), "MO_B002");
            var candidates = service.Read(root, Convert.ToString(root["DOC_DEFAULT_TYPE"]), Convert.ToString(root["DOC_ID"]));
            CopyOrderQueryResult(candidates, root);
            candidates = root;
            var rows = (DependencyObjectCollection)candidates["MO_B002_MO"];
            var selected = new HashSet<string>(json.Deserialize<string[]>(selectedJson), StringComparer.OrdinalIgnoreCase);
            var details = new List<object>();
            foreach (DependencyObject row in rows)
            {
                foreach (DependencyObject source in (DependencyObjectCollection)row["MO_B002_SALES"])
                {
                    var docNo = Convert.ToString(source["DOC_NO"]);
                    if (docNo != number && !docNo.StartsWith(number + "-", StringComparison.Ordinal)) throw new InvalidOperationException("候选结果包含指定订单以外的数据，已停止。");
                }
                details.Add(EntityData(row, 2));
            }
            if (rows.Count == 0) return json.Serialize(new { ok = true, no_changes = true, candidate_count = 0, order_no = number, candidates = details, saved = false });
            if (selected.Count == 0) return json.Serialize(new { ok = true, needs_selection = true, candidate_count = rows.Count, order_no = number, candidates = details, saved = false });
            var found = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            for (var i = rows.Count - 1; i >= 0; i--)
            {
                var row = rows[i]; var id = Convert.ToString(row["MO_B002_MO_ID"]);
                if (!selected.Contains(id)) { rows.RemoveAt(i); continue; }
                found.Add(id); row["SELECT"] = true; row["AUTO_APPROVE"] = false;
                foreach (DependencyObject source in (DependencyObjectCollection)row["MO_B002_SALES"]) source["SELECT"] = true;
            }
            if (found.Count != selected.Count) throw new ArgumentException("选择的候选 ID 已变化，请重新取得候选。");
            return StorePlan(new OperationPlan { Module = "MO_B002", Action = "generate", Number = number, Entity = candidates,
                Summary = new { order_no = number, selected_ids = found, candidates = EntityData(candidates, 3), auto_approve = false } });
        }
        catch (Exception e) { return Error(e); }
    }

    // This view has no ORM Create service. Build its transient type from the installed client metadata.
    // Match QueryDataAction.CopyDataToOtherEntity: retain the original wizard's
    // root settings and copy returned child rows into that wizard before DoProcess.
    private void CopyOrderQueryResult(DependencyObject source, DependencyObject target)
    {
        var type = target.GetDataEntityType();
        if (type.Name != "MO_B002")
        {
            foreach (var property in type.SimpleProperties) target[property.Name] = source[property.Name];
            foreach (var property in type.ComplexProperties)
                CopyOrderQueryResult((DependencyObject)source[property.Name], (DependencyObject)target[property.Name]);
        }
        foreach (var property in type.CollectionProperties)
        {
            var destination = (DependencyObjectCollection)target[property.Name];
            destination.Clear();
            foreach (DependencyObject row in (DependencyObjectCollection)source[property.Name])
                CopyOrderQueryResult(row, destination.AddNew());
        }
    }

    private DependencyObject NewOrderWizard(string clientDirectory)
    {
        var definitions = new Dictionary<string, XmlElement>();
        var types = new Dictionary<string, DependencyObjectType>();
        var source = new XmlDocument();
        source.Load(Path.Combine(clientDirectory, "BatchObjects", "MO_B002", "MetadataContainer.dcxml"));
        foreach (XmlElement element in source.SelectNodes("//*[local-name()='DataEntityType']"))
            definitions[XmlText(element, "Name")] = element;
        var rootType = BuildWizardType("MO_B002", clientDirectory, definitions, types);
        if (rootType.Properties.IndexOfName("Owner_Org") < 0)
            rootType.RegisterComplexProperty("Owner_Org", BuildWizardType("ReferToEntity", clientDirectory, definitions, types));
        foreach (var name in new[] { "Owner_Emp", "Owner_Dept" })
            if (rootType.Properties.IndexOfName(name) < 0) rootType.RegisterSimpleProperty(name, typeof(Guid), Guid.Empty, false, new Attribute[0]);
        return (DependencyObject)rootType.CreateInstance();
    }

    private string XmlText(XmlElement node, string name)
    {
        var value = node.SelectSingleNode("*[local-name()='" + name + "']");
        return value == null ? "" : value.InnerText;
    }

    private DependencyObjectType BuildWizardType(string name, string directory,
        Dictionary<string, XmlElement> definitions, Dictionary<string, DependencyObjectType> types)
    {
        if (types.ContainsKey(name)) return types[name];
        if (name == "ReferToEntity")
        {
            var reference = new DependencyObjectType(name);
            reference.RegisterSimpleProperty("RTK", typeof(string), "", false, new Attribute[0]);
            reference.RegisterSimpleProperty("ROid", typeof(Guid), Guid.Empty, false, new Attribute[0]);
            types[name] = reference; return reference;
        }
        if (!definitions.ContainsKey(name))
        {
            var shared = new XmlDocument();
            shared.Load(Path.Combine(directory, "BusinessObjects", "MetadataContainerElement", "DataEntityType." + name + ".dcxml"));
            definitions[name] = shared.DocumentElement;
        }
        var definition = definitions[name];
        var type = new DependencyObjectType(name);
        types[name] = type;
        foreach (XmlElement property in definition.SelectNodes("*[local-name()='Properties']/*"))
        {
            var field = XmlText(property, "Name");
            if (property.LocalName == "ComplexProperty")
                type.RegisterComplexProperty(field, BuildWizardType(XmlText(property, "DataEntityTypeName"), directory, definitions, types));
            else if (property.LocalName == "CollectionProperty")
                type.RegisterCollectionProperty(field, BuildWizardType(XmlText(property, "ItemDataEntityTypeName"), directory, definitions, types));
            else
            {
                var dbType = XmlText(property, "DbType");
                var business = XmlText(property, "BusinessTypeName");
                if (dbType == "" && business != "")
                {
                    var businessPath = Path.Combine(directory, "BusinessObjects", "MetadataContainerElement", "BusinessType." + business + ".dcxml");
                    if (File.Exists(businessPath))
                    {
                        var businessXml = new XmlDocument(); businessXml.Load(businessPath);
                        dbType = XmlText(businessXml.DocumentElement, "DbType");
                    }
                }
                Type valueType = typeof(string);
                if (property.LocalName == "ReferenceProperty" || dbType == "Guid" || business == "PrimaryKey") valueType = typeof(Guid);
                else if (dbType == "Boolean") valueType = typeof(bool);
                else if (dbType == "Int32") valueType = typeof(int);
                else if (dbType == "Int64") valueType = typeof(long);
                else if (dbType == "Decimal" || business == "Quantity") valueType = typeof(decimal);
                else if (dbType == "Date" || dbType == "DateTime" || business == "Date") valueType = typeof(DateTime);
                object initial = valueType == typeof(string) ? "" : valueType == typeof(DateTime) ? (object)new DateTime(1900, 1, 1) : Activator.CreateInstance(valueType);
                var explicitDefault = XmlText(property, "DefaultValue");
                if (explicitDefault != "") initial = valueType == typeof(bool) ? (object)Boolean.Parse(explicitDefault) : ConvertField(explicitDefault, valueType);
                type.RegisterSimpleProperty(field, valueType, initial, false, new Attribute[0]);
            }
        }
        foreach (XmlElement reference in definition.SelectNodes("*[local-name()='InterfaceReferences']/*"))
        {
            var inherited = XmlText(reference, "Name");
            if (inherited == "ISequenceNumber" && type.Properties.IndexOfName("SequenceNumber") < 0)
                type.RegisterSimpleProperty("SequenceNumber", typeof(int), 0, false, new Attribute[0]);
            if (inherited == "IOwner")
            {
                if(type.Properties.IndexOfName("Owner_Org")<0) type.RegisterComplexProperty("Owner_Org",BuildWizardType("ReferToEntity",directory,definitions,types));
                foreach(var ownerField in new[] {"Owner_Emp","Owner_Dept"})
                    if(type.Properties.IndexOfName(ownerField)<0) type.RegisterSimpleProperty(ownerField,typeof(Guid),Guid.Empty,false,new Attribute[0]);
            }
        }
        return type;
    }
}
