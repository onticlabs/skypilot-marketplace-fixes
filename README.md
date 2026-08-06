# skypilot-marketplace-fixes

A SkyPilot API-server plugin carrying patches for two upstream defects that make a
**marketplace cloud** — one "cloud" fronting many independent providers, like Shadeform —
unusable when GPU capacity is scarce.

Nothing here is provider-specific in intent; it lives outside `skypilot-lyceum` because it
has nothing to do with Lyceum.

Target: `skypilot==0.13.0`.

## What it fixes

**The catalog is never refreshed.** `sky/catalog/shadeform_catalog.py` caches the offer
table twice, and neither cache expires: `read_catalog` is called without
`pull_frequency_hours` (so the CSV on disk is frozen at first download — every other cloud
passes a value), and the parsed frame is held in a module-level `_df` for the life of the
process. A long-running API server therefore plans against whatever was current when its
disk was created. Ours still offers an H100 at \$1.90 in a region where that instance type
no longer exists; the optimizer picks it on every launch because it is the cheapest, and it
can never be provisioned.

**Only the cheapest instance type per cloud is launchable.** `sky/optimizer.py` keeps
`feasible_resources.resources_list[0]` and discards the rest, so provisioning failover can
walk the regions of that one offer and nothing else. When those are exhausted the whole
cloud is reported as having no capacity. On a hyperscaler this is invisible — an
accelerator spec usually maps to one instance type per cloud — but on a marketplace each
instance type is a different vendor, so it means one vendor stands in for the entire
market.

The two compound: a stale catalog puts a phantom offer at the top of the price list, and
the truncation means nothing else is ever tried.

## What it does not fix

Live availability. SkyPilot never queries a provider at plan or provision time; the catalog
is a snapshot published upstream roughly every 6 hours. Measured across two consecutive
publications, ~20% of `H100:1` offers disappeared within one window. This plugin narrows a
weeks-old snapshot to a ~1-hour-old one and makes a wrong listing survivable — it does not
make listings true.

## Install

Build and install the wheel into the API server image, then register it:

```yaml
# ~/.sky/plugins.yaml
plugins:
  - class: skypilot_marketplace_fixes.plugin.MarketplaceFixesPlugin
```

Boot is the test. Every patch is anchored to a specific upstream shape, and a moved anchor
raises `PatchDriftError` out of `install()`, which stops the server from starting. That is
deliberate: a server that boots clean, reports the plugin as loaded, and quietly plans the
old way is worse than one that refuses to start.

## Remove

Delete the `plugins.yaml` entry and restart. Nothing is persisted; both patches are
in-process monkeypatches.

## Upstream

Both defects are small and worth fixing at the source. If either lands in a SkyPilot
release, the corresponding patch here should be deleted rather than carried:

- pass `pull_frequency_hours` in `shadeform_catalog.py` like every other catalog, and
  invalidate `_df` with it;
- iterate `feasible_resources.resources_list` in `optimizer.py` instead of taking `[0]`.

See [`docs/PLAN.md`](docs/PLAN.md) for the full design, the measurements behind it, and the
verification strategy.
