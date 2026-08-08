"""Patch B — make every feasible offer launchable, not just the cheapest.

`sky/optimizer.py::_fill_in_launchable_resources`:

    cheapest = feasible_resources.resources_list[0]
    launchable[resources].extend(
        resources_utils.make_launchables_for_valid_region_zones(cheapest))

Everything after `[0]` is discarded for provisioning. `blocked_resources` is
applied afterwards and never reaches `get_feasible_launchable_resources`, so the
failover can walk the regions of that one instance type and nothing else; when
they are exhausted the whole cloud is reported as having no capacity.

On a hyperscaler this is usually invisible — an accelerator spec maps to one
instance type per cloud. On a marketplace each instance type is a DIFFERENT
VENDOR, so it means one vendor stands in for the entire market. Measured on
Shadeform: 7 real `H100:1` offers existed, 2 were attempted, both the same
instance type.

The fix folds the other feasible offers back in. Blocking is per-(cloud,
instance_type, region) — `_default_handler` blocks `launchable.copy(zone=…)` and
`should_be_blocked_by` compares instance_type and region — so the extra offers
genuinely survive failover rather than merely lengthening a list.

The fold has to keep running DURING failover, which is the part this patch
originally got wrong. Failover blocks what it just tried and re-optimizes; once
the cheapest instance type is blocked in all of its regions, upstream's
`resources_list[0]` yields [] even though `cloud_candidates` still holds the
cloud and the other vendors are untouched. Skipping the fold on that emptiness
ended failover two vendors in. Simulated against the live catalog, walking the
real optimize/block/re-optimize loop: 3 offers across 2 instance types before,
10 across 5 after — the whole launchable set, exhausted in the right order.

WHY IT RECOMPUTES rather than reusing the `cloud_candidates` the function already
returns: that dict is aggregated per cloud across ALL requested `Resources`, so
folding it into every requested entry leaks candidates between entries. Measured:
an `any_of` entry for `V100:1` with NO feasible AWS resources — where upstream
logs "No resource satisfying …" and returns [] — acquired 107 launchables, and
the AWS optimize path slowed 3.2-5.7x from the duplication. Recomputing per
requested entry is exact by construction and costs ~2 ms warm.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

from skypilot_marketplace_fixes import anchors

logger = logging.getLogger(__name__)

#: Emitted once per process if upstream turns out to have fixed the defect.
_reported_noop: set = set()


def patch(failover_clouds: Iterable[str], max_extra_instance_types: int) -> None:
    """Apply Patch B. Idempotent."""
    from sky import optimizer
    from sky.utils import resources_utils

    allowlist = {c.strip().lower() for c in failover_clouds}

    original = anchors.require_attr(
        optimizer, '_fill_in_launchable_resources',
        'It is where the launchable set is built.')
    if getattr(original, '_marketplace_fixes_patched', False):
        return

    anchors.require_params(original, 'task', 'blocked_resources', 'quiet')
    anchors.require_source_contains(
        original, 'resources_list[0]',
        'That truncation IS the defect this patch works around. Upstream may '
        'have fixed it, in which case this patch should be DELETED rather than '
        'silently kept.')
    filter_blocked = anchors.require_attr(
        optimizer, '_filter_out_blocked_launchable_resources',
        'The folded-in offers must still respect the failover blocklist.')
    make_launchables = anchors.require_attr(
        resources_utils, 'make_launchables_for_valid_region_zones',
        'It expands one feasible resource into its region/zone variants.')

    def patched(task, blocked_resources, quiet: bool = False):
        launchable, cloud_candidates, fuzzy, hints = original(
            task, blocked_resources, quiet)
        # Materialise: the parameter is typed Optional[Iterable], and a consumed
        # iterator would block nothing, silently.
        blocked = list(blocked_resources or [])

        for requested, current in list(launchable.items()):
            if not current and not blocked:
                # Nothing blocked yet and upstream still found nothing feasible
                # for this entry: it genuinely has no offer, and said so in the
                # log. Folding here would make the data disagree with the message.
                #
                # With a blocklist present the SAME empty list means the opposite,
                # and skipping on it was this patch's own worst bug. Failover
                # blocks what it just tried and re-optimizes; upstream keeps only
                # `resources_list[0]`, so once the cheapest instance type is
                # blocked in every one of its regions upstream returns [] — while
                # `cloud_candidates` still holds the cloud and the other vendors
                # are untouched. Declining to fold there is exactly when the fold
                # is needed, and it ended failover early with the market reported
                # exhausted. Measured on the live server: after blocking
                # lyceum/h100.1x and scaleway_H100 in paris + warsaw, upstream
                # returned 0 offers and this patch added 0; folding returns 7,
                # across digitalocean_H100-sxm5, lambdalabs_H100 and
                # lambdalabs_H100-sxm5, all available and none blocked.
                continue
            extra = _extra_launchables(requested, task, current, cloud_candidates,
                                       allowlist, max_extra_instance_types,
                                       make_launchables)
            if not extra:
                continue
            merged = _dedupe(list(current) + extra)
            launchable[requested] = filter_blocked(merged, blocked)
            logger.debug(
                'marketplace-fixes: folded +%d launchables (%d -> %d) for %s',
                len(launchable[requested]) - len(current), len(current),
                len(launchable[requested]), requested)

        return launchable, cloud_candidates, fuzzy, hints

    patched._marketplace_fixes_patched = True
    patched._marketplace_fixes_original = original
    optimizer._fill_in_launchable_resources = patched


def _extra_launchables(requested, task, current, cloud_candidates, allowlist,
                       max_extra, make_launchables) -> list:
    """Feasible offers for THIS requested entry that upstream discarded."""
    extra: list = []
    for cloud in cloud_candidates:
        name = str(cloud).lower()
        if name not in allowlist:
            continue
        if requested.cloud is not None and not cloud.is_same_cloud(requested.cloud):
            continue
        if _already_multi_typed(current, cloud, name):
            continue
        try:
            feasible = cloud.get_feasible_launchable_resources(
                requested, task.num_nodes)
        except Exception as e:  # noqa: BLE001 - one blind cloud must not cost the launch
            # One cloud that cannot be introspected must not cost the launch;
            # without the fold it simply behaves as it does today.
            logger.debug('marketplace-fixes: %s not expandable (%s)', name, e)
            continue
        # resources_list is price-sorted by upstream contract, so the cap keeps
        # the cheapest alternatives.
        for resource in list(feasible.resources_list)[:max_extra]:
            if resource.instance_type is None:
                continue
            extra.extend(make_launchables(resource))
    return extra


def _already_multi_typed(current, cloud, name: str) -> bool:
    """True if upstream already offers >1 instance type here — i.e. it is fixed.

    Loud but never fatal. The boot-time equivalent of this check cannot run:
    `load_plugins(MAIN)` executes before the cluster-state DB is initialised, and
    the function under test reads it. So the drift gate is the static source
    anchor, and this is the runtime companion.
    """
    types = {r.instance_type for r in current
             if r.cloud is not None and r.cloud.is_same_cloud(cloud)}
    if len(types) <= 1:
        return False
    if name not in _reported_noop:
        _reported_noop.add(name)
        logger.warning(
            'marketplace-fixes: %s already yields %d instance types without this '
            'patch — upstream appears fixed. DELETE this patch rather than carry '
            'it.', name, len(types))
    return True


def _dedupe(resources: list) -> list:
    """Structural dedupe.

    `Resources` defines neither `__eq__` nor `__hash__`, so a set() would compare
    by identity and silently keep every duplicate.
    """
    seen = set()
    out = []
    for r in resources:
        key = (str(r.cloud), r.instance_type, r.region, r.zone, r.use_spot,
               str(r.accelerators))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
