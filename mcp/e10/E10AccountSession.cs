using System;
using System.Collections.Generic;
using System.Web.Script.Serialization;
using Digiwin.Common.Advanced;
using Digiwin.Common.Network.Wcf;
using Digiwin.Common.Services;

public sealed partial class E10AccountSession : IDisposable
{
    private readonly WcfConnector connector;
    private readonly ILogOnService logOnService;
    private readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024 };

    public E10AccountSession(string serverUrl)
    {
        connector = new WcfConnector(CommunicationMode.BinaryHttp, new Uri(serverUrl));
        logOnService = (ILogOnService)connector.GetService(typeof(ILogOnService), null);
    }

    public string Status()
    {
        try { return json.Serialize(new { ok = true, authenticated = logOnService.IsAuthenticated }); }
        catch (Exception e) { return Error(e); }
    }

    public string Login(string userName, string password, string language)
    {
        string identity = null;
        try
        {
            if (String.IsNullOrWhiteSpace(userName) || String.IsNullOrEmpty(password))
                return json.Serialize(new { ok = false, error = "请先在本机配置账号密码。" });
            identity = LogOnHelper.GetEncryptIdentity(userName, password);
            return LoginIdentity(userName, identity, language);
        }
        catch (Exception e) { return Error(e, password, identity); }
    }

    public string LoginIdentity(string userName, string identity, string language)
    {
        try
        {
            if (String.IsNullOrWhiteSpace(userName) || String.IsNullOrEmpty(identity))
                return json.Serialize(new { ok = false, error = "请先在本机配置账号。" });
            logOnService.LogOn(new LogOnContext(identity, language, false, userName));
            if (!logOnService.IsAuthenticated)
                return json.Serialize(new { ok = false, authenticated = false, error = "E10 未接受本次登录。" });
            return CurrentUser();
        }
        catch (Exception e) { return Error(e, identity); }
    }

    public string CurrentUser()
    {
        try
        {
            if (!logOnService.IsAuthenticated)
                return json.Serialize(new { ok = false, authenticated = false, error = "尚未登录 E10。" });
            var user = logOnService.CurrentUser;
            return json.Serialize(new { ok = true, authenticated = true, user = new { name = user.LogOnName, display_name = user.DisplayName, language = user.LanguageName } });
        }
        catch (Exception e) { return Error(e); }
    }

    public string Logout()
    {
        try
        {
            if (logOnService.IsAuthenticated) logOnService.LogOff();
            return json.Serialize(new { ok = true, authenticated = false });
        }
        catch (Exception e) { return Error(e); }
    }

    public string ModuleAccess()
    {
        if (!logOnService.IsAuthenticated)
            return json.Serialize(new { ok = false, error = "尚未登录 E10。" });
        var programs = new[] { "MO.I01", "ISSUE_RECEIPT_REQ.I01", "MO_B002.B01", "MO_B012.B01" };
        try
        {
            var roleService = (IProgramRoleService)connector.GetService(typeof(IProgramRoleService), null);
            var authorizations = roleService.GetCurrentUserProgramAuthorizations();
            var matching = new List<object>();
            foreach (var authorization in authorizations)
            {
                if (Array.IndexOf(programs, authorization.ProgramId) < 0) continue;
                matching.Add(new { program_id = authorization.ProgramId, administrator = authorization.IsAdministrator, actions = new List<string>(authorization.Actions) });
            }
            var previews = new List<object>();
            foreach (var typeKey in new[] { "MO_B002", "MO_B012" })
            {
                try
                {
                    var preview = (IBatchPreviewService)connector.GetService(typeof(IBatchPreviewService), typeKey);
                    previews.Add(new { type_key = typeKey, ok = true, can_preview = preview.CanPreview(), query_id = preview.GetCanPreviewQueryId() });
                }
                catch (Exception e)
                {
                    while (e.InnerException != null) e = e.InnerException;
                    previews.Add(new { type_key = typeKey, ok = false, error_type = e.GetType().FullName, error = e.Message });
                }
            }
            return json.Serialize(new { ok = true, administrator = authorizations.IsAdministrator, module_authorizations = matching, batch_preview_interfaces = previews });
        }
        catch (Exception e) { return Error(e); }
    }

    private string Error(Exception e, params string[] secrets)
    {
        while (e.InnerException != null) e = e.InnerException;
        var message = e.Message;
        foreach (var secret in secrets)
            if (!String.IsNullOrEmpty(secret)) message = message.Replace(secret, "[已隐藏]");
        return json.Serialize(new { ok = false, error_type = e.GetType().FullName, error = message });
    }

    public static bool CheckIdentityEncoding()
    {
        // Synthetic input only; does not contact an E10 service.
        return !String.IsNullOrEmpty(LogOnHelper.GetEncryptIdentity("mcp-offline-test", "synthetic-password"));
    }

    public void Dispose() { connector.Dispose(); }
}
