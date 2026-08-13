# skypilot-marketplace-fixes

A SkyPilot API-server plugin carrying patches for four upstream defects that make a
**marketplace cloud** — one "cloud" fronting many independent providers, like Shadeform —
unusable when GPU capacity is scarce, unaccountable after the fact, or fatally brittle when
a listing disappears.

Nothing here is provider-specific in intent; it lives outside `skypilot-lyceum` because it
has nothing to do with Lyceum.

Target: `skypilot==0.13.0`.

## What it fixes

**The catalog is never refreshed.** `common.read_catalog` returns a `LazyDataFrame` whose
loader is cleared at the end of every request, so every cloud gets a per-request re-read for
free — except Shadeform, which is the only catalog that *materialises* that lazy frame into a
plain DataFrame and then pins it in a module-level `_df` for the life of the process.
Compounding it, `read_catalog` is called without `pull_frequency_hours`, so the CSV on disk
never re-downloads either. A long-running API server therefore plans against whatever was current when its
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

**A zone filter against a catalog with no zones raises, and pricing swallows it as \$0.00.**
`catalog.common._get_instance_type` filters on an `AvailabilityZone` column that zoneless
catalogs do not have, while Shadeform's provisioner stamps `zone=region` on the success
path. `core.py` turns the resulting error into `0.0`, so a cluster nobody could price is
indistinguishable from one that was free — every successful Shadeform run reported zero
spend.

**A delisted instance type takes down the whole control plane.**
`shadeform_catalog._call_or_default` exists to answer "not in the catalog" with a default,
but its predicate tests for the substring `not found` while the message reads
`No instance type X found.` — "No … found" never contains "not found", so all five defaults
are unreachable for the case they were written for. A cluster still running on an instance
type that has left the catalog therefore makes `repr(handle.launched_resources)` raise,
which kills `core.status()` outright: `sky status`, `ontic cluster list` and the launch
matcher all die together, and **no new job can be submitted anywhere** until somebody
removes the record by hand. Seen 2026-08-13, when `latitude_H100` left the snapshot while a
cluster was up on it — the routine `H100:1` match at the time, not an exotic one.

That last one is the sharp edge of the section below: the churn is expected, and it must be
survivable rather than fatal.

## What it does not fix

Live availability. SkyPilot never queries a provider at plan or provision time; the catalog
is a snapshot published upstream roughly every 6 hours. Measured across two consecutive
publications, ~20% of `H100:1` offers disappeared within one window. This plugin narrows a
weeks-old snapshot to a ~1-hour-old one and makes a wrong listing survivable — it does not
make listings true.

## What is not covered: managed jobs

`sky jobs launch` runs its provisioning retries on the managed-jobs **controller VM**, not
on the API server, so Patch B does not reach it. That is deliberate and explicit: the wheel
only arrives on a controller via `controller_wheel_path` plus a `remote_plugins.yaml`, and
this package ships neither. Declaring `PluginContext.CONTROLLER` without them would make the
plugin look like it covers managed jobs while doing nothing — a worse outcome than an
honest gap.

`sky launch`, which is what `ontic launch` uses, is fully covered.

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
