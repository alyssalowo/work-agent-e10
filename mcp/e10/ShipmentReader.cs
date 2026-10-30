using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using System.Data;
using System.Web.Script.Serialization;
using Digiwin.Common.Services;
using Digiwin.Common.Network.Wcf;
using Digiwin.Common.Torridity;
using Digiwin.Common.Torridity.Query;

public static class ShipmentReader {
 static JavaScriptSerializer J=new JavaScriptSerializer{MaxJsonLength=50000000};
 static WcfConnector Connector(object session,string module) {
  if(module!="SALES_DELIVERY" && module!="BORROW_DOC" && module!="STOCK_REQUISITION")throw new ArgumentException("Unsupported module");
  string program=module+(module=="BORROW_DOC"?".I02":".I01");
  var c=(WcfConnector)session.GetType().GetField("connector",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(session);
  var roles=((IProgramRoleService)c.GetService(typeof(IProgramRoleService),null)).GetCurrentUserProgramAuthorizations();
  bool allowed=roles.IsAdministrator;
  foreach(var role in roles)if(role.ProgramId==program&&(role.IsAdministrator||role.Actions.Contains("BROWSE")))allowed=true;
  if(!allowed)throw new UnauthorizedAccessException("Missing "+program+" BROWSE");
  return c;
 }
 public static string Configuration(object session,string first,string second) {
  var c=Connector(session,first);
  var config=((IAccountSetQueryProjectConfigService)c.GetService(typeof(IAccountSetQueryProjectConfigService),null)).Read(first,second);
  return J.Serialize(new Dictionary<string,object>{{"ok",true},{"configuration",config}});
 }
 public static string Page(object session,string optionsJson) {
  var o=J.Deserialize<Dictionary<string,object>>(optionsJson);
  string module=Convert.ToString(o["module"]),expectedFrom=Convert.ToString(o["expected_from"]),expectedPath=Convert.ToString(o["expected_path"]),statusPath=Convert.ToString(o["status_path"]),statusEnd=Convert.ToString(o["status_end"]);
  int start=Convert.ToInt32(o["start_row"]),count=Convert.ToInt32(o["max_rows"]);
  DateTime parsed;
  if(!DateTime.TryParseExact(expectedFrom,"yyyy-MM-dd",CultureInfo.InvariantCulture,DateTimeStyles.None,out parsed)||count<1||count>1000||start<1)throw new ArgumentException("Invalid query options");
  if(module=="BORROW_DOC" && (!String.IsNullOrEmpty(statusPath) || !String.IsNullOrEmpty(statusEnd)))throw new ArgumentException("Borrow documents have no outbound status; filter detail CLOSE offline.");
  if(module=="STOCK_REQUISITION" && (!String.IsNullOrEmpty(statusPath) || !String.IsNullOrEmpty(statusEnd)))throw new ArgumentException("Stock requisitions must not use sales outbound status.");
  if(module=="SALES_DELIVERY" && (statusPath!="ISSUED_STATUS" || statusEnd!="3"))throw new ArgumentException("Sales status filter required");
  foreach(string path in String.IsNullOrEmpty(statusPath)?new[]{expectedPath}:new[]{expectedPath,statusPath})if(!System.Text.RegularExpressions.Regex.IsMatch(path,@"\A[A-Za-z0-9_]+\z"))throw new ArgumentException("Only verified header filters allowed");
  string fieldsJson=J.Serialize(o["fields"]);
  var c=Connector(session,module);
  var q=new QueryInputData{Id="MCP_SHIPMENT_READONLY",MainEntityPath=module,ProgramId=module+(module=="BORROW_DOC"?".I02":".I01"),SourceTypeKey=module,IsIncludeDetail=true,IsDistinct=true};
  var fields=J.Deserialize<List<Dictionary<string,string>>>(fieldsJson);
  if(fields==null || fields.Count<1 || fields.Count>100)throw new ArgumentException("Expected 1-100 query fields");
  var aliases=new HashSet<string>(StringComparer.Ordinal);
  var paths=new Dictionary<string,string>();
  foreach(var field in fields){
   if(!field.ContainsKey("alias") || !field.ContainsKey("path") || !System.Text.RegularExpressions.Regex.IsMatch(field["alias"],@"\A[A-Za-z_][A-Za-z0-9_]*\z") || field["alias"]=="CREATE_BY" || field["alias"]=="CREATOR_NAME" || !aliases.Add(field["alias"]))throw new ArgumentException("Duplicate or invalid field alias");
   paths[field["alias"]]=field["path"];
  }
  foreach(var required in new Dictionary<string,string>{{"HEAD_ID",module+"_ID"},{"DETAIL_ID",module+"_D."+module+"_D_ID"},{"DOC_NO","DOC_NO"},{"DOC_DATE","DOC_DATE"}})
   if(!paths.ContainsKey(required.Key) || paths[required.Key]!=required.Value)throw new ArgumentException("Missing stable identity/date field: "+required.Key);
  if(!paths.ContainsValue(expectedPath))throw new ArgumentException("Expected shipment date must be included in output fields");
  if(module=="BORROW_DOC" && !paths.ContainsValue("BORROW_DOC_D.CLOSE"))throw new ArgumentException("Borrow detail CLOSE must be included; exclude ended details offline");
  foreach(var f in fields) {
   if(!System.Text.RegularExpressions.Regex.IsMatch(f["path"],@"\A[A-Za-z0-9_\.\(\)]+\z"))throw new ArgumentException("Invalid field path");
   q.QueryColumns.Add(new QueryColumn{Alias=f["alias"],Value=new PropertyValue{Path=f["path"]}});
  }
  q.QueryColumns.Add(new QueryColumn{Alias="CREATE_BY",Value=new PropertyValue{Path="CreateBy"}});
  q.OrderColumns.Add(new OrderByDict(new PropertyValue{Path=module+"_ID"},SortType.Asc,1,"HEAD_ID"));
  q.OrderColumns.Add(new OrderByDict(new PropertyValue{Path=module+"_D."+module+"_D_ID"},SortType.Asc,2,"DETAIL_ID"));
  var filter=new ConditionItemGroup{LogicalOperator=LogicalOperator.And};
  var dateFilter=new ConditionItem{Left=new PropertyValue{Path="DOC_DATE"},CompareOperator=CompareOperator.GreaterEqual};
  dateFilter.Values.Add(new ConstantValue{Value="2026-01-01",DataType=GeneralDBType.DateTime});filter.Items.Add(dateFilter);
  var shipFilter=new ConditionItem{Left=new PropertyValue{Path=expectedPath},CompareOperator=CompareOperator.GreaterEqual};
  shipFilter.Values.Add(new ConstantValue{Value=expectedFrom,DataType=GeneralDBType.DateTime});filter.Items.Add(shipFilter);
  if(!String.IsNullOrEmpty(statusPath)){
  var issuedFilter=new ConditionItem{Left=new PropertyValue{Path=statusPath},CompareOperator=CompareOperator.NotEqual};
  issuedFilter.Values.Add(new ConstantValue{Value=statusEnd,DataType=GeneralDBType.String});filter.Items.Add(issuedFilter);
  }
  if(module=="SALES_DELIVERY")foreach(string prefix in new[]{"2409","2410","2411","2412","2416","2417","2502","2503","2505"}){
   var exclusion=new ConditionItem{Left=new PropertyValue{Path="DOC_NO"},CompareOperator=CompareOperator.NotLike};
   exclusion.Values.Add(new ConstantValue{Value=prefix+"%",DataType=GeneralDBType.String});filter.Items.Add(exclusion);
  }
  if(module=="BORROW_DOC"){
   var direction=new ConditionItem{Left=new PropertyValue{Path="CATEGORY"},CompareOperator=CompareOperator.Equal};
   direction.Values.Add(new ConstantValue{Value="19",DataType=GeneralDBType.String});filter.Items.Add(direction);
  }
  if(module=="BORROW_DOC")foreach(string name in new[]{"内部借出单","借料单(工厂用)"}){
   var exclusion=new ConditionItem{Left=new PropertyValue{Path="DOC_ID.DOC_NAME"},CompareOperator=CompareOperator.NotEqual};
   exclusion.Values.Add(new ConstantValue{Value=name,DataType=GeneralDBType.String});filter.Items.Add(exclusion);
  }
  if(module=="STOCK_REQUISITION"){
   var included=new ConditionItemGroup{LogicalOperator=LogicalOperator.Or};
   foreach(string prefix in new[]{"1018","1008","1012"}){
    var prefixFilter=new ConditionItem{Left=new PropertyValue{Path="DOC_NO"},CompareOperator=CompareOperator.Like};
    prefixFilter.Values.Add(new ConstantValue{Value=prefix+"%",DataType=GeneralDBType.String});included.Items.Add(prefixFilter);
   }
   filter.Items.Add(included);
  }
  var result=((IBrowseService)c.GetService(typeof(IBrowseService),module)).ExecuteQuery(new RowNumberInfo(start,start+count-1),q,filter,new ParameterCollection(),CultureInfo.GetCultureInfo("zh-CN"));
  var rows=new List<Dictionary<string,object>>();
  var creators=new Dictionary<Guid,string>();
  var detailIds=new HashSet<string>(StringComparer.Ordinal);
  foreach(DependencyObject row in (IEnumerable)result.DataSource){var d=new Dictionary<string,object>();foreach(var f in q.QueryColumns){var v=row[f.Alias];d[f.Alias]=v==null||v==DBNull.Value?null:v is DateTime?((DateTime)v).ToString("yyyy-MM-dd HH:mm:ss"):v;}
   if(String.IsNullOrEmpty(Convert.ToString(d["DETAIL_ID"])) || !detailIds.Add(Convert.ToString(d["DETAIL_ID"])))throw new InvalidOperationException("Missing or duplicate detail identity");
   var id=(Guid)row["CREATE_BY"];
   if(!creators.ContainsKey(id)) {
    var profile=((IUserService)c.GetService(typeof(IUserService),null)).GetUserProfile(id);
    if(profile==null||profile.Rows.Count!=1||!profile.Columns.Contains("USER_NAME"))throw new InvalidOperationException("Creator profile name is unavailable; profile columns: "+(profile==null?"null":String.Join(",",Columns(profile))));
    creators[id]=Convert.ToString(profile.Rows[0]["USER_NAME"]);
   }
   d["CREATOR_NAME"]=creators[id];rows.Add(d);
  }
  return J.Serialize(new Dictionary<string,object>{{"ok",true},{"read_only",true},{"module",module},{"filters",new {doc_date_from="2026-01-01",expected_from=expectedFrom,expected_path=expectedPath,status_path=statusPath,status_end=statusEnd,borrow_category=module=="BORROW_DOC"?"19":null,stock_requisition_prefixes=module=="STOCK_REQUISITION"?new[]{"1018","1008","1012"}:null}},{"start_row",start},{"next_start_row",start+rows.Count},{"has_next_page",result.HasNextPage},{"rows",rows}});
 }
 static string[] Columns(DataTable table){var names=new List<string>();foreach(DataColumn column in table.Columns)names.Add(column.ColumnName);return names.ToArray();}
}
