using Jellyfin.Plugin.ThabitTranslator.Runtime;
using Jellyfin.Plugin.ThabitTranslator.Subtitles;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Plugins;
using MediaBrowser.Controller.Subtitles;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.ThabitTranslator;

/// <summary>
/// DI entry point. Jellyfin instantiates this with a parameterless constructor
/// while it builds the service collection (before the provider exists), so it must
/// not resolve anything here.
/// </summary>
public class PluginServiceRegistrator : IPluginServiceRegistrator
{
    /// <inheritdoc />
    public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
    {
        ThabitRuntime.Initialize(applicationHost);

        serviceCollection.AddSingleton<ThabitRunner>();
        serviceCollection.AddSingleton<ThabitJobQueue>();

        // Jellyfin builds scheduled tasks with ActivatorUtilities (constructor
        // injection works), but registering it as well lets the API controller share
        // one instance and keeps the plugin working if that ever changes.
        serviceCollection.AddSingleton<ThabitScheduledTask>();

        // Consumed by Jellyfin's SubtitleManager through IEnumerable<ISubtitleProvider>.
        serviceCollection.AddSingleton<ISubtitleProvider, ThabitSubtitleProvider>();
    }
}
