# skypilot-marketplace-fixes — implementation plan

A SkyPilot API-server plugin carrying two anchored patches for upstream defects that make
a **marketplace cloud** (one "cloud" fronting many independent providers) unusable when
capacity is scarce. Nothing in it is Lyceum-specific, which is why it is not in
`skypilot-lyceum`.

Target: `skypilot==0.13.0`, the version pinned by `ontic-cli` and running on
`https://skypilot.onticlabs.io`.

---

## 1. The two defects, with measured evidence

### Defect A — the Shadeform catalog is never refreshed, twice over

`sky/catalog/shadeform_catalog.py`:

```python
_df = None                                             # module-level, process-lifetime

def _get_df():
    global _df
    if _df is None:
        df = common.read_catalog('shadeform/vms.csv')   # no pull_frequency_hours
        ...
        _df = df
    return _df
```

Two independent caches, both unbounded:

1. **On disk.** `common.read_catalog`'s `_need_update()` returns `False` whenever the file
   exists and `pull_frequency_hours is None`. Every other cloud passes a value
   (`_PULL_FREQUENCY_HOURS = 7`, i.e. 7 hours — aws, azure, gcp, cudo); Shadeform passes
   nothing, so the CSV is frozen at first download forever.
2. **In the process.** `_df` is populated once and never invalidated, so even a fresh file
   on disk is ignored for the life of the server process.

Fixing either alone is a no-op. Fixing (1) only helps after a restart; fixing (2) only
re-reads the same stale file.

**Measured.** Our API server currently offers 9 `H100:1` Shadeform offers including
`hyperstack_H100 @ montreal-canada-2 @ $1.90`. That instance type is absent from the last
two upstream catalog publications. It is the cheapest, so the optimizer picks it on every
launch — and it cannot be provisioned. This is the direct cause of onticlabs/cli#18
(8 consecutive launches, all failing on `montreal-canada-2`, byte-identical).

Upstream publication cadence is healthy — a bot updates
`skypilot-org/skypilot-catalog/catalogs/v8/shadeform/vms.csv` roughly every 6 hours
(observed: 03:45, 10:05, 16:14, 22:33 UTC). The failure is entirely that we never pull it.

### Defect B — only the cheapest instance type per cloud is ever launchable

`sky/optimizer.py:1727-1739`:

```python
if feasible_resources.resources_list:
    cheapest = feasible_resources.resources_list[0]
    launchable[resources].extend(
        resources_utils.make_launchables_for_valid_region_zones(cheapest))
    cloud_candidates[cloud].extend(feasible_resources.resources_list)
```

Everything after `[0]` is discarded for provisioning purposes; `cloud_candidates` feeds
only the "Considered resources" display table. `blocked_resources` is applied *after* this
(line 1789), and never reaches `get_feasible_launchable_resources`, so the failover can
only walk the regions of that one instance type and then reports the cloud exhausted.

Verified by driving the real `RetryingVmProvisioner.provision_with_retries`: 7 real offers
existed, 2 were attempted, all of them `scaleway_H100`.

Not marketplace-specific in principle (AWS `T4:1` has 5 feasible instance types and only
`g4dn.xlarge` becomes launchable) but harmless on hyperscalers, where an accelerator spec
usually maps to one instance type per cloud.

**Why A alone is insufficient.** A catalog records *listings*, not live availability;
nothing in SkyPilot queries a provider at plan or provision time. Comparing two
consecutive upstream publications 6 hours apart: **2 of 9 `H100:1` offers vanished**
(~20% churn per window). The cheapest offer is also the most contended. So a listing that
is real when published and gone when provisioned is routine, and without B that is a total
launch failure with no second attempt.

---

## 2. Scope

**In scope:** the two patches above, packaged as a SkyPilot API-server plugin.

**Out of scope:** anything Lyceum-related (`skypilot-lyceum` keeps its three patches);
client-side changes (`ontic-cli` PR #19 is decided separately, see §8); provisioning
behaviour, credentials, and the Shadeform provisioner itself.

**Non-goals:** live availability. This plugin cannot deliver it — there is no live query
path in SkyPilot — and must not pretend to. It narrows a weeks-old snapshot to a
~1-7 hour-old one, and makes a wrong listing survivable.

---

## 3. Package shape

```
skypilot-marketplace-fixes/
  pyproject.toml                  # name: skypilot-marketplace-fixes
  README.md                       # what, why, how to deploy, how to remove
  src/skypilot_marketplace_fixes/
    __init__.py                   # __version__, apply()
    patches.py                    # PatchDriftError, the two patches, anchors
    plugin.py                     # MarketplaceFixesPlugin(BasePlugin)
  tests/
    test_patches.py               # anchors, idempotency, behaviour
    test_plugin.py                # contexts, install()
```

Conventions copied deliberately from `skypilot-lyceum`: a module-level `apply()`, anchored
patches, `PatchDriftError`, idempotency markers, and **no try/except around install** so a
drift failure stops the server rather than producing one that boots clean and misbehaves.

### Plugin registration

`sky/server/plugins.py` loads `~/.sky/plugins.yaml`:

```yaml
plugins:
  - class: skypilot_marketplace_fixes.plugin.MarketplaceFixesPlugin
```

Load contexts — both patches affect planning, which happens in more than one process:

| context | needed | why |
|---|---|---|
| `MAIN` | yes | patch before main-process bootstrap touches the registry/catalog |
| `EXECUTOR` | yes | where `optimize` and provisioning actually run |
| `UVICORN` | yes | `POST /optimize` and `/validate` plan in the web process |
| `CONTROLLER` | yes | managed jobs plan on the controller |

Same set as the Lyceum plugin. Cheap to load; both patches are pure monkeypatching with no
credentials and no network at import.

---

## 4. Patch A — catalog freshness

Two coupled changes, both anchored.

### A1: give `read_catalog` a pull frequency for Shadeform

Wrap `sky.catalog.common.read_catalog`. When `filename == 'shadeform/vms.csv'` and the
caller passed `pull_frequency_hours=None`, substitute `_PULL_FREQUENCY_HOURS` (default 1).

Chosen narrowly rather than globally: other clouds already pass 7, and silently changing
their cadence is out of scope.

**Interval: 1 hour.** Upstream publishes every ~6h, so 1h means we are never more than one
publication behind, at the cost of one small CSV GET per hour per process. `0` (refresh on
every read) is rejected: `_get_df` is called inside optimizer loops.

### A2: bound the in-process `_df` cache

Wrap `sky.catalog.shadeform_catalog._get_df` so that when the cached frame is older than
the TTL it sets `shadeform_catalog._df = None` before delegating, forcing both a re-read
and (via A1) a re-download check.

Uses `time.monotonic()`, so it cannot be confused by clock changes. Thread-safety: the
worst case under a race is two threads both re-reading, which is wasteful but correct;
`read_catalog` already takes a filelock for the download.

### Anchors for A

Refuse to install (raise `PatchDriftError`) unless all hold:

- `sky.catalog.common.read_catalog` exists and its signature has parameters
  `(filename, pull_frequency_hours)`.
- `sky.catalog.shadeform_catalog._get_df` exists and is a zero-argument callable.
- `sky.catalog.shadeform_catalog` has a module-level `_df` attribute.
- A probe call to `_get_df()` returns a DataFrame with the expected columns
  (`InstanceType`, `AcceleratorName`, `AcceleratorCount`, `Price`, `Region`).

### Verification for A

- Unit: with a temp `~/.sky` and a stubbed `read_catalog`, assert the substituted frequency
  is 1 for `shadeform/vms.csv` and untouched for `aws/vms.csv`.
- Unit: freeze/advance a fake clock; assert `_df` is invalidated exactly once per TTL and
  that `_get_df()` still returns a frame.
- Integration (offline, real files): populate the cache with the 3.5-week-old snapshot we
  have archived, install the patch, advance the clock past the TTL, assert the frame now
  contains the *fresh* offers and no longer contains `hyperstack_H100`.

---

## 5. Patch B — make every feasible offer launchable

### Approach: wrapper, not a rewrite

Wrap `sky.optimizer._fill_in_launchable_resources`. It returns

```python
(launchable, cloud_candidates, all_fuzzy_candidates, resource_hints)
```

and `cloud_candidates[cloud]` already contains **every** feasible resource — the same list
`[0]` was taken from. So the wrapper does not need to reimplement the body; it folds the
discarded candidates back into `launchable` and re-applies the blocklist:

```python
for requested in list(launchable):
    extra = [x for cloud, feasibles in cloud_candidates.items()
               for r in feasibles
               for x in resources_utils.make_launchables_for_valid_region_zones(r)]
    merged = dedupe(launchable[requested] + extra)
    launchable[requested] = optimizer._filter_out_blocked_launchable_resources(
        merged, blocked_resources or [])
```

Chosen over copying the ~90-line original because that body would have to be re-verified on
every SkyPilot bump; the wrapper depends only on the return shape, which the anchors check.

### Known imprecision, and the mitigation

`cloud_candidates` is aggregated per cloud across **all** requested `Resources` of the task.
For a task with several `any_of` entries that differ in something the candidate list does
not capture, folding the union into every requested entry could make an entry launchable on
hardware it did not ask for.

Mitigation: only fold candidates into `launchable[requested]` when the task has exactly one
requested `Resources`, OR when the candidate's `accelerators`/`use_spot`/`instance_type`
are consistent with `requested`. **Decision needed** (see §9, Q1) — the reviewer should
weigh "filter per requested entry" against "apply only for single-resource tasks".

Note this interacts with `ontic-cli`: today it submits an `any_of` of N entries, which is
exactly the multi-entry case. If PR #19's expansion is deleted (§8), tasks become
single-resource and the imprecision disappears.

### Ordering is preserved

The optimizer still costs and sorts whatever it is given, so the cheapest offer is still
chosen first. The patch only ensures the others remain reachable on failover.

### Anchors for B

- `sky.optimizer._fill_in_launchable_resources` exists and takes
  `(task, blocked_resources, quiet)`.
- It returns a 4-tuple whose 1st element is a dict and 2nd is a dict keyed by `Cloud`.
- `sky.optimizer._filter_out_blocked_launchable_resources` exists and takes
  `(resources_list, blocked_resources)`.
- `sky.utils.resources_utils.make_launchables_for_valid_region_zones` exists.
- **Behavioural anchor:** on a synthetic task with a stub cloud offering 2 instance types,
  the *unpatched* function returns launchables for exactly one of them. If it already
  returns both, upstream has fixed defect B and the patch refuses to install with a message
  saying so — we must not silently keep a patch whose premise is gone.

### Verification for B

- Unit with a stub cloud: unpatched → 1 instance type launchable; patched → all of them.
- Unit: blocked resources are still excluded after folding.
- Unit: idempotent under repeated `apply()`.
- Integration against the real Shadeform catalog (offline, cached CSV): patched
  `_fill_in_launchable_resources` for `H100:1` yields ≥4 distinct instance types.
- Integration through the real failover loop, with `_retry_zones` stubbed to fail: assert
  every distinct instance type is attempted, not just the cheapest.

---

## 6. Deployment

1. Build the wheel; install into the API server image alongside `skypilot-lyceum`.
2. Add the entry to the server's `~/.sky/plugins.yaml`.
3. Deploy. **Boot is the test**: a drift failure raises out of `install()` and the server
   refuses to start, by design.
4. Post-deploy check: `GET /api/plugins` lists `marketplace-fixes` with its version; then
   `list_accelerators(name_filter="H100", clouds=["shadeform"])` must no longer return
   `hyperstack_H100`, confirming Patch A took effect.

## 7. Rollback

Remove the plugins.yaml entry and restart. No state is written, nothing is persisted, both
patches are pure in-process monkeypatches. Uninstalling the wheel is optional.

## 8. Relationship to ontic-cli PR #19

If both patches land and are verified, the client-side candidate expansion in PR #19 is
redundant and should be **deleted**, leaving the parts that stand on their own: the
`sky.yaml` contract (`infra` + hardware; `region`/`zone`/`instance_type`/`cloud`/`any_of`
refused), the error-message work, `--retry-until-up`, and the stdout/`--json` hygiene.

Sequencing: land these patches first, confirm on the server, then strip PR #19. Do not run
both mechanisms permanently — two systems doing one job, and the client one carries all the
catalog-drift risk for no added coverage.

## 9. Open questions for review

1. **Patch B scoping** (§5): filter folded candidates per requested `Resources`, or apply
   the fold only to single-resource tasks? Which is safer given PR #19 currently submits
   multi-entry `any_of`?
2. **Patch A interval**: 1 hour against a 6-hour upstream cadence — reasonable, or should
   it track the publication schedule more cleverly?
3. **Should Patch A generalise** beyond Shadeform to any cloud whose catalog passes
   `pull_frequency_hours=None`? Today that is only Shadeform, but a future marketplace
   plugin would hit the same trap.
4. **Behavioural anchor for B** (§5): is "refuse to install if upstream already returns
   >1 instance type" right, or too aggressive for a server that must boot?
5. Anything in either patch that could deadlock, leak memory, or slow the optimizer
   materially on a large cloud (AWS: 5 instance types × ~19 regions instead of 19).

## 10. Upstream

Both defects are worth filing regardless, each a small change with a measured
justification:

- `shadeform_catalog.py`: pass `pull_frequency_hours` like every other catalog (and
  consider invalidating `_df`).
- `optimizer.py`: iterate `feasible_resources.resources_list` rather than taking `[0]`.

If either is accepted and released, the corresponding patch here is deleted.
