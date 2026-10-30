using System;
using System.IO;
using System.Text;
using System.Security.Cryptography;
using System.Web.Script.Serialization;

public sealed class E10SavedCredential
{
    public string UserName { get; set; }
    public string Password { get; set; }
    public string Identity { get; set; }
}

public static class E10CredentialStore
{
    public static void SaveIdentity(string path, string userName, string identity)
    {
        if (String.IsNullOrWhiteSpace(userName) || String.IsNullOrEmpty(identity))
            throw new ArgumentException("Missing local account identity.");
        var json = new JavaScriptSerializer();
        var plain = Encoding.UTF8.GetBytes(json.Serialize(new E10SavedCredential { UserName=userName, Identity=identity }));
        byte[] encrypted;
        try { encrypted = ProtectedData.Protect(plain, null, DataProtectionScope.CurrentUser); }
        finally { Array.Clear(plain, 0, plain.Length); }
        var temporary = path + ".tmp";
        File.WriteAllBytes(temporary, encrypted);
        if (File.Exists(path)) File.Replace(temporary, path, null);
        else File.Move(temporary, path);
    }

    public static void Save(string path, string userName, string password)
    {
        var json = new JavaScriptSerializer();
        var plain = Encoding.UTF8.GetBytes(json.Serialize(new E10SavedCredential { UserName=userName, Password=password }));
        byte[] encrypted;
        try { encrypted = ProtectedData.Protect(plain, null, DataProtectionScope.CurrentUser); }
        finally { Array.Clear(plain, 0, plain.Length); }
        var temporary = path + ".tmp";
        File.WriteAllBytes(temporary, encrypted);
        if (File.Exists(path)) File.Replace(temporary, path, null);
        else File.Move(temporary, path);
    }

    public static E10SavedCredential Load(string path)
    {
        var plain = ProtectedData.Unprotect(File.ReadAllBytes(path), null, DataProtectionScope.CurrentUser);
        try { return new JavaScriptSerializer().Deserialize<E10SavedCredential>(Encoding.UTF8.GetString(plain)); }
        finally { Array.Clear(plain, 0, plain.Length); }
    }
}
