"""Patch C: price a cluster whose cloud has no zones.

THE DEFECT. `catalog.common._get_instance_type` filters on an `AvailabilityZone`
column whenever it is handed a zone:

    if zone is not None:
        idx &= df['AvailabilityZone'] == zone

Most marketplace catalogs have no such column — shadeform, verda, lambda,
nebius, paperspace and a dozen others ship `InstanceType,...,Price,Region,
SpotPrice` and nothing else — so this raises `KeyError('AvailabilityZone')`.

Nothing would hand a zoneless cloud a zone, except that Shadeform's provisioner
does. `sky/provision/shadeform/instance.py` returns `ProvisionRecord(...,
zone=region)` on the success path, even though the cloud asserts `zone is None`
in three other places and the early-return path in that same file sets
`zone=None` with the comment "Shadeform doesn't use separate zones".
`Resources.copy()` does not re-validate, so the bogus zone is persisted into
cluster history and detonates later, on the READ side, in `cost_report`.

WHY THAT IS SILENT. `core.py` wraps the pricing call in a bare
`except Exception` and substitutes `0.0`. A cluster that cannot be priced is
therefore indistinguishable, over the API, from a cluster that was free. On the
store this was found in, every SUCCESSFUL Shadeform run reported $0.00 — the
only rows carrying a price were aborted attempts that lasted seconds and never
got far enough to be stamped with a zone.

WHY THE GUARD IS THE RIGHT FIX, AND NOT A WORKAROUND. Three reasons:

  * it is upstream's own idiom. `get_region_zones`, 500 lines below in this same
    module, already writes `if 'AvailabilityZone' in df.columns` twice before
    touching that column. This makes `_get_instance_type` consistent with its
    neighbour rather than introducing a new convention.
  * dropping the filter cannot lose information. A catalog without the column
    describes no zones, so there is nothing for the predicate to select.
  * for Shadeform specifically it is provably lossless twice over: `Price` is
    region-invariant there (verified over the live catalog — 68 instance types,
    zero carrying more than one distinct price across regions), and the zone
    being discarded is a verbatim copy of `region`, which is filtered on one
    line above regardless.

It is deliberately generic rather than Shadeform-specific: verda's provisioner
stamps a zone the same way against an equally zoneless catalog, and Lyceum is
one line from joining them.

SCOPE. This is the pricing path only. `validate_region_zone_impl` in the same
module reads `AvailabilityZone` unguarded too, but on the validation path, which
a zoneless cloud reaches through a different door (`Cloud.validate_region_zone`)
and which already fails loudly rather than silently. Fixing what is silent is
the point; a second patch for a defect that announces itself would be carrying
risk for nothing.

UPSTREAM. Both halves are worth reporting — `zone=region` in the provisioner and
the unguarded column access here. Neither has an issue as of this writing, and
`master` and `v0.13.1rc1` are unchanged, so this patch is not racing a release.
"""
from __future__ import annotations

import functools
import logging

from skypilot_marketplace_fixes import anchors

logger = logging.getLogger(__name__)

#: The literal this patch exists to work around. When upstream guards or removes
#: it, `require_source_contains` fails the boot and this module should be DELETED
#: rather than repaired — see anchors.py.
_DEFECT = "idx &= df['AvailabilityZone'] == zone"

_ZONE_COLUMN = 'AvailabilityZone'


def patch() -> None:
    """Make a zone filter a no-op on catalogs that carry no zone column."""
    from sky.catalog import common as catalog_common

    original = anchors.require_attr(
        catalog_common, '_get_instance_type',
        'Pricing a cluster goes through it, and a zoneless cloud raises '
        f'{_ZONE_COLUMN} there.')
    anchors.require_params(original, 'df', 'instance_type', 'region', 'zone')
    anchors.require_source_contains(
        original, _DEFECT,
        'The unguarded zone filter is gone, so this patch has nothing left to '
        'work around. Delete it rather than carry a patch whose premise has '
        'expired.')

    if getattr(original, '_marketplace_fixes_zoneless', False):
        return                                  # already applied; idempotent

    @functools.wraps(original)
    def _get_instance_type(df, instance_type, region, zone=None):
        if zone is not None and _ZONE_COLUMN not in df.columns:
            # No zone column means the catalog describes no zones, so the filter
            # can only select nothing or raise. Dropping it is what upstream's
            # own `get_region_zones` does with the same test.
            logger.debug(
                'marketplace-fixes: ignoring zone %r for %s — this catalog has '
                'no %s column', zone, instance_type, _ZONE_COLUMN)
            zone = None
        return original(df, instance_type, region, zone)

    _get_instance_type._marketplace_fixes_zoneless = True
    catalog_common._get_instance_type = _get_instance_type
