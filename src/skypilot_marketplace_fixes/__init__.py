"""SkyPilot API-server patches for marketplace clouds.

Upstream defects that make a cloud fronting many independent providers unusable
when capacity is scarce, or unaccountable after the fact:

  A  the Shadeform catalog is frozen for the life of the server process, so
     planning happens against listings that may be weeks out of date;
  B  only the cheapest instance type per cloud is ever launchable, so failover
     can reach one vendor and then reports the whole market exhausted;
  C  a zone filter against a catalog that has no zone column raises, and the
     pricing path swallows it as $0.00 — so a cluster nobody could price is
     reported as one that was free.

A and B compound: a stale catalog puts a phantom offer at the top of the price
list, and the truncation means nothing else is ever tried. C is independent, and
silent — it made every successful Shadeform run report zero spend.

See `docs/PLAN.md` for the measurements and the design; `anchors.py` for how
drift is detected.
"""
from __future__ import annotations

import logging
import os

__version__ = '0.4.0.dev0'

logger = logging.getLogger(__name__)

#: Escape hatch for a server that will not boot. `install()` raises on drift by
#: design, and on Fly the plugin config lives inside the image — so without this,
#: recovery from a bad anchor is an image rebuild. With it, it is `fly secrets
#: set SKYPILOT_MARKETPLACE_FIXES_DISABLED=1` and a restart.
DISABLE_ENV = 'SKYPILOT_MARKETPLACE_FIXES_DISABLED'

DEFAULTS = {
    'catalog_refresh_hours': 1.0,
    'catalog_files': ['shadeform/vms.csv'],
    'failover_clouds': ['shadeform', 'lyceum'],
    'max_extra_instance_types': 4,
}


def is_disabled() -> bool:
    return os.environ.get(DISABLE_ENV, '').strip().lower() in ('1', 'true', 'yes')


def apply(parameters: dict | None = None, *, context: str = '') -> dict:
    """Apply both patches. Safe to call repeatedly.

    Returns the resolved configuration so the caller can log what actually took
    effect — a plugin that silently runs with different settings than the ones in
    `plugins.yaml` is its own kind of outage.
    """
    config = {**DEFAULTS, **(parameters or {})}

    if is_disabled():
        logger.warning('marketplace-fixes: disabled via %s; SkyPilot will plan '
                       'against a frozen catalog and one instance type per cloud',
                       DISABLE_ENV)
        return {**config, 'enabled': False}

    from skypilot_marketplace_fixes import catalog_freshness, launchable_offers, zoneless_pricing

    catalog_freshness.patch(config['catalog_files'],
                            config['catalog_refresh_hours'])
    launchable_offers.patch(config['failover_clouds'],
                            config['max_extra_instance_types'])
    # No configuration: the guard is "this catalog has no zone column", which is a
    # property of the data, not a policy. A knob here could only be used to turn
    # correct pricing off.
    zoneless_pricing.patch()
    catalog_freshness.start_background_refresh(config['catalog_refresh_hours'])

    logger.info(
        'marketplace-fixes %s applied%s: catalogs=%s every %sh, failover across '
        '%s (max %d extra instance types)', __version__,
        f' [{context}]' if context else '', config['catalog_files'],
        config['catalog_refresh_hours'], config['failover_clouds'],
        config['max_extra_instance_types'])
    return {**config, 'enabled': True}
