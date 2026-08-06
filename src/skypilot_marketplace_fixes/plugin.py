"""SkyPilot API-server plugin entry point (`~/.sky/plugins.yaml`)."""
from __future__ import annotations

from typing import ClassVar

from sky.server import plugins

import skypilot_marketplace_fixes


class MarketplaceFixesPlugin(plugins.BasePlugin):
    """Applies the catalog-freshness and multi-offer-failover patches."""

    #: MANAGED JOBS ARE OUT OF SCOPE — `CONTROLLER` is deliberately absent.
    #:
    #: `sky jobs launch` runs `provision_with_retries` on the managed-jobs
    #: controller VM, not here, so Patch B does not reach it. Declaring the
    #: context would not change that: the wheel only arrives on the controller
    #: via `controller_wheel_path` plus a `remote_plugins.yaml`, neither of which
    #: this package ships. Declaring it anyway would make the plugin LOOK like it
    #: covers managed jobs while silently doing nothing, which is worse than the
    #: honest gap. `sky launch` (what `ontic launch` uses) is fully covered.
    #:
    #: The three below are spelled out because `BasePlugin.should_load` compares
    #: against `PluginContext` MEMBERS — a frozenset of strings would silently
    #: disable the plugin.
    #:
    #: * MAIN     — patch before main-process bootstrap reads the catalog.
    #: * UVICORN  — `POST /optimize` and `/validate` plan in the web process.
    #: * EXECUTOR — where `optimize` and provisioning actually run.
    load_contexts: ClassVar[frozenset[plugins.PluginContext]] = frozenset({
        plugins.PluginContext.MAIN,
        plugins.PluginContext.UVICORN,
        plugins.PluginContext.EXECUTOR,
    })

    def __init__(self, **parameters):
        # `load_plugins` constructs plugins as `plugin_cls(**parameters)` from the
        # `parameters:` mapping in plugins.yaml — they arrive as keyword args, not
        # as an attribute.
        super().__init__()
        self._parameters = dict(parameters)
        self._resolved: dict = {}

    @property
    def name(self):
        return 'marketplace-fixes'

    @property
    def version(self):
        """The package version, never a second hand-maintained string. Surfaces
        on `/api/plugins`, which is how a bad launch gets pinned to a wheel."""
        return skypilot_marketplace_fixes.__version__

    def install(self, extension_context: plugins.ExtensionContext):
        """Apply both patches.

        No try/except by design. `load_plugins` does not wrap `install`, so a
        `PatchDriftError` propagates and the server fails to start — which is the
        point: a server that boots clean, reports this plugin as loaded, and then
        plans the old way is far harder to diagnose than one that refuses to boot
        with the reason on stderr. `SKYPILOT_MARKETPLACE_FIXES_DISABLED=1` is the
        escape hatch when that bet goes wrong on a machine you cannot rebuild
        quickly.

        Nothing here touches the network or the cluster-state DB: `install` runs
        before `initialize_and_get_db()`, and the catalog is only read on first
        use, from a background thread or a request.
        """
        del extension_context  # Nothing to register on the FastAPI app.
        self._resolved = skypilot_marketplace_fixes.apply(self._parameters)
