using System;
using MediaBrowser.Controller;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.ThabitTranslator;

/// <summary>
/// Static service locator for code Jellyfin may construct without DI (it builds
/// scheduled tasks with <c>ActivatorUtilities</c>, which does inject, but the
/// plugin must not depend on that staying true).
///
/// Set once from <see cref="PluginServiceRegistrator.RegisterServices"/>, before
/// the service provider is built.
/// </summary>
public static class ThabitRuntime
{
    /// <summary>The server application host handed to the registrator.</summary>
    public static IServerApplicationHost? ApplicationHost { get; private set; }

    /// <summary>The live service provider, once the host has built it.</summary>
    public static IServiceProvider? Services => ApplicationHost?.ServiceProvider;

    /// <summary>Captures the host. Called from the plugin's service registrator.</summary>
    public static void Initialize(IServerApplicationHost applicationHost)
        => ApplicationHost = applicationHost ?? throw new ArgumentNullException(nameof(applicationHost));

    /// <summary>Resolves a service, or null when the provider is not built yet.</summary>
    public static T? GetService<T>()
        where T : class
        => Services?.GetService(typeof(T)) as T;
}
