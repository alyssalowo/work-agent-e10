using System;
using System.IO;
using System.Reflection;

public static class E10ClientAssemblyResolver
{
    private static string[] directories;
    public static void Initialize(string clientDirectory)
    {
        if (directories != null) return;
        directories = new[] { clientDirectory, Path.Combine(clientDirectory, "System"), Path.Combine(clientDirectory, "System", "SDK"), Path.Combine(clientDirectory, "Support"), Path.Combine(clientDirectory, "Programs"), Path.Combine(clientDirectory, "Programs", "SDK") };
        AppDomain.CurrentDomain.AssemblyResolve += Resolve;
    }
    private static Assembly Resolve(object sender, ResolveEventArgs args)
    {
        var name = new AssemblyName(args.Name).Name;
        foreach (var directory in directories)
        {
            var candidate = Path.Combine(directory, name + ".dll");
            if (File.Exists(candidate)) return Assembly.LoadFrom(candidate);
        }
        return null;
    }
}
