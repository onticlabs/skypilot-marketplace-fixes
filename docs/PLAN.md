# skypilot-marketplace-fixes — implementation plan

A SkyPilot API-server plugin carrying two anchored patches for upstream defects that make a
**marketplace cloud** (one "cloud" fronting many independent providers) unusable when
capacity is scarce. Nothing in it is Lyceum-specific, which is why it is not in
`skypilot-lyceum`.

Target: `skypilot==0.13.*`, the version pinned by `ontic-cli` and running on
`https://skypilot.onticlabs.io`.

> **Revision 2.** Reviewed before implementation. The review corrected two factual claims,
> replaced the Patch A mechanism, replaced the Patch B scoping, and removed the install-time
> behavioural anchors — which could not have run. Measurements are from that review.

---

## 1. The two defects

### Defect A — the Shadeform catalog is frozen for the life of the process

The mechanism is **not** simply a missing parameter. `common.read_catalog` returns a
`LazyDataFrame` whose `_load_df` is an `@annotations.lru_cache(scope='request')`, and the
executor clears that cache at the end of every request
(`sky/server/requests/executor.py:822`). Every other cloud therefore gets a per-request
re-read for free.

`sky/catalog/shadeform_catalog.py:21-47` is the only catalog that **materialises** that lazy
frame into a plain DataFrame:

```python
_df = None

def _get_df():
    global _df
    if _df is None:
        df = common.read_catalog('shadeform/vms.csv')   # LazyDataFrame
        df = df[df['InstanceType'].notna()]             # __getitem__ -> real DataFrame
        _df = df.reset_index(drop=True)                 # frozen for the process
    return _df
```

Materialising defeats the request-scoped refresh, and the module-level `_df` then pins the
result for the process lifetime. Compounding it, `read_catalog` is called without
`pull_frequency_hours`, so `_need_update()` (`sky/catalog/common.py:217-228`) returns
`False` whenever the file merely exists — the CSV on disk never re-downloads either.

**Correction to revision 1:** it is false that "every other cloud passes a value". Twelve
catalogs omit `pull_frequency_hours` — `fluidstack`, `hyperbolic`, `do`, `ibm`, `oci`,
`primeintellect`, `scp`, `paperspace`, `yotta`, `vast`, `shadeform`, and `nebius` (which
defines `_PULL_FREQUENCY_HOURS = 7` and then forgets to pass it). `seeweb` uses 8, not 7.
Those others are largely rescued by the per-request cache clear; Shadeform is not, because
of the materialisation above.

**Measured.** Our API server offers 9 `H100:1` Shadeform offers including
`hyperstack_H100 @ montreal-canada-2 @ $1.90`. That instance type is absent from the last two
upstream publications. It is the cheapest, so the optimizer picks it on every launch and it
can never be provisioned — the direct cause of onticlabs/cli#18 (8 consecutive launches, all
failing on `montreal-canada-2`, byte-identical).

Upstream cadence is healthy: a bot republishes
`skypilot-org/skypilot-catalog/catalogs/v8/shadeform/vms.csv` at 03:47, 10:05, 16:14 and
22:33 UTC daily. The failure is entirely that we never pull it.

**Bonus defect fixed for free:** a transient download failure at first read poisons `_df`
with an empty DataFrame for the life of the process (`shadeform_catalog.py:33-39`).

### Defect B — only the cheapest instance type per cloud is launchable

`sky/optimizer.py:1727-1739`:

```python
if feasible_resources.resources_list:
    cheapest = feasible_resources.resources_list[0]
    launchable[resources].extend(
        resources_utils.make_launchables_for_valid_region_zones(cheapest))
    cloud_candidates[cloud].extend(feasible_resources.resources_list)
```

`blocked_resources` is applied afterwards (line 1789) and never reaches
`get_feasible_launchable_resources`, so failover can only walk the regions of that one
instance type before reporting the cloud exhausted. Verified by driving the real
`RetryingVmProvisioner.provision_with_retries`: 7 offers existed, 2 were attempted, both
`scaleway_H100`.

**Correction to revision 1:** `cloud_candidates` is *not* display-only. It also drives
`Optimizer._find_common_infras` / `_select_best_infra` (`optimizer.py:1261-1370`) for
JobGroup SAME_INFRA optimisation. This plan does not mutate it — and must not.

**Correction to revision 1:** "harmless on hyperscalers" is false. AWS `T4:1` has 5 feasible
instance types, and folding them in costs 3–6× optimizer wall time (§3). Harmless in
*semantics*, expensive in *cost*.

**Confirmed, and worth stating:** failover blocking is per-(cloud, instance_type, region) —
`_default_handler` blocks `launchable_resources.copy(zone=…)` and
`Resources.should_be_blocked_by` (`resources.py:2108`) compares `instance_type` and `region`.
Extra instance types genuinely survive the blocklist, so Patch B delivers real failover and
not merely a longer list.

**Also checked, benign:** `sky/clouds/shadeform.py:245` has a second `resources_list[0]`,
inside `make_deploy_resources_variables`. The incoming resources already have `instance_type`
set, so the feasible list has one element. Recorded because it looks exactly like the defect
being fixed.

**Why A alone is insufficient.** A catalog records listings, not live availability; nothing
in SkyPilot queries a provider at plan or provision time. Between two consecutive upstream
publications 6 hours apart, **2 of 9 `H100:1` offers vanished** (~20% churn per window), and
the cheapest offer is the most contended. A listing that is real when published and gone when
provisioned is routine; without B that is a total launch failure with no second attempt.

---

## 2. Scope

**In scope:** the two patches, packaged as a SkyPilot API-server plugin.

**Out of scope:** anything Lyceum-specific; client-side changes (`ontic-cli` PR #19, see §7);
provisioning behaviour, credentials, the Shadeform provisioner.

**Non-goal:** live availability. There is no live query path in SkyPilot and this plugin must
not pretend otherwise. It narrows a weeks-old snapshot to a ~1-hour-old one and makes a wrong
listing survivable.

---

## 3. The patches

### Package shape

```
src/skypilot_marketplace_fixes/
  __init__.py                # __version__, apply(), kill switch
  anchors.py                 # PatchDriftError + static anchor checks
  catalog_freshness.py       # Patch A
  launchable_offers.py       # Patch B
  plugin.py                  # MarketplaceFixesPlugin(BasePlugin)
```

Conventions from `skypilot-lyceum`: module-level `apply()`, anchored patches,
`PatchDriftError`, idempotency markers, `_original` kept reachable, and **no try/except
around install** so drift stops the server rather than producing one that boots clean and
misbehaves.

```yaml
# ~/.sky/plugins.yaml
plugins:
  - class: skypilot_marketplace_fixes.plugin.MarketplaceFixesPlugin
    parameters:
      catalog_refresh_hours: 1
      catalog_files: ["shadeform/vms.csv"]
      failover_clouds: ["shadeform", "lyceum"]
      max_extra_instance_types: 4
```

Every tunable is a plugin parameter, not a constant — including the cloud allowlist, so the
package stays generic and the defaults carry the policy. Load contexts: `MAIN`, `UVICORN`,
`EXECUTOR`, `CONTROLLER` — planning happens in all four.

### Patch A — catalog freshness

**A1 — give `read_catalog` a pull frequency.** Wrap `sky.catalog.common.read_catalog`; when
`filename` is in `catalog_files` and the caller passed `pull_frequency_hours=None`,
substitute `catalog_refresh_hours`. Safe to widen later to the other eleven omitting catalogs
(they hold `LazyDataFrame`s and are already rescued per request); **not** to be widened
together with A2 — see the leak note.

**A2 — stop materialising.** Replace `_get_df` so a single module-level `LazyDataFrame` is
created **once** at patch time and the filtered frame is re-derived from it per call, cached
behind a TTL. On expiry call `common.LazyDataFrame._load_df.cache_clear()` — do **not**
construct a new `LazyDataFrame`. `_load_df` is a class-level `lru_cache(maxsize=128)` keyed
on `self`, so minting one per expiry retains a `(LazyDataFrame, DataFrame)` pair each time:
harmless for a 17 KB CSV, a real leak if this ever touched `aws/vms.csv` (5 MB).

Measured re-filter cost: **0.22 ms/call**; `_get_df` is called 8× per `optimize` — 1.8 ms.

**A3 — refresh off the hot path.** `_update_catalog` calls `requests.get` with **no timeout**
and takes a `filelock.FileLock` with no timeout. Today that risk is paid once per process;
hourly expiry would move it onto the optimizer path in every process. Given the 2026-07-30
outage on this VM (53 s import vs uvicorn's 5 s worker ping), that is the wrong direction. So
a daemon thread started in `install()` refreshes on a timer, and the inline TTL is generous
(6 h) so the request path almost never downloads.

`_need_update` is mtime-based and `_update_catalog` calls `os.utime(path, None)` on a failed
fetch — a failed fetch costs a full interval. Multi-process is fine: each process expires
independently, and `_update_catalog` re-checks under the filelock so only one downloads.

### Patch B — make every feasible offer launchable

Wrap `sky.optimizer._fill_in_launchable_resources`, which returns
`(launchable, cloud_candidates, all_fuzzy_candidates, resource_hints)`.

**Do not reuse `cloud_candidates`.** It is aggregated per cloud across *all* requested
`Resources`, and folding that union into every requested entry measured badly:

```
                     launchables      Optimizer.optimize
shadeform H100:1      2 →   7        0.264s → 0.266s   ×1.0
aws T4:1             19 →  95        0.441s → 1.407s   ×3.2
aws T4:1 spot        56 → 280        0.879s → 3.857s   ×4.4
any_of x3 aws        31 → 321        0.589s → 3.369s   ×5.7
```

and it breaks an invariant: a requested `V100:1` entry with **no** feasible AWS resources
(upstream logs `No resource satisfying …` and returns `[]`) acquired 107 launchables, leaving
the log and the data disagreeing.

**Instead, recompute the feasible set per requested `Resources`** inside the wrapper —
`get_feasible_launchable_resources` costs ~2 ms warm. Exact by construction: no cross-entry
leakage, no N× duplication, and `[]` stays `[]`.

Bound the blast radius:
- **Cloud allowlist** (`failover_clouds`, default `shadeform`, `lyceum`). Shadeform measured
  ×1.0; the AWS/spot regressions disappear.
- **Cap extra instance types per cloud** (`max_extra_instance_types`, default 4,
  cheapest-first).
- **Never fold into a requested entry the original returned `[]` for.**

The optimizer still costs and sorts whatever it is given, so the cheapest is still chosen.
Confirmed end-to-end: chosen resource and the "Considered resources" table are identical
before and after, because `_get_resource_group_hash` groups by (cloud, accelerators,
use_spot) and shows the min-cost member — already the folded-in `[0]`.

Interactions checked and clean: the `ordered` path (`optimizer.py:1433-1455`) passes a
single-resource task; `_optimize_by_dp`/`_optimize_by_ilp` see only the flat cost map;
multi-node passes `num_nodes` through; job groups read `launchable_resources`.

**Hazard to guard:** if `resources_utils.need_to_query_reservations()` is ever true,
`get_available_reservations` (`optimizer.py:259-280`) fans a thread pool over
`sum(launchable_resources.values(), [])` — N× the union, each a cloud API call. `False` here
today; log a warning and skip folding if it becomes true.

**Wrapper mechanics — each hit during review:**
1. Parameter **names** are load-bearing: `optimizer.py:298` and `:1450` pass
   `blocked_resources=` as a keyword. Anchor on names via `inspect.signature`, not arity.
2. `Resources` defines neither `__eq__` nor `__hash__` — a `set()` dedupe silently does
   nothing. Use a structural key `(str(cloud), instance_type, region, zone, use_spot,
   str(accelerators))`.
3. Materialise `blocked_resources` (typed `Optional[Iterable]`) to a list before re-filtering;
   a consumed iterator blocks nothing, silently.
4. Idempotency marker + `_original` reachable, per the Lyceum house style.

---

## 4. Anchors — static only

Revision 1 proposed a behavioural anchor ("refuse to install if upstream already returns >1
instance type"). **Dropped: it cannot run.** `load_plugins(MAIN)` executes at
`server/server.py:3548`, *before* `initialize_and_get_db()` at :3556, and
`_fill_in_launchable_resources` calls `get_cached_enabled_clouds_or_refresh(...,
raise_if_no_cloud_access=True)` on its first line. The same objection kills "probe
`_get_df()`" — that is a network download during plugin install.

Static anchors, in the shape `skypilot_lyceum/patches.py:168-179` already uses:
- `inspect.signature(optimizer._fill_in_launchable_resources)` has parameters named `task`,
  `blocked_resources`, `quiet`.
- `inspect.getsource(...)` still contains `resources_list[0]` — **the real drift check**: if
  upstream fixes the bug the substring disappears and we fail loudly, executing nothing.
- `read_catalog` signature has `filename`, `pull_frequency_hours`; `shadeform_catalog` has a
  module-level `_df` and a zero-arg `_get_df`; `_filter_out_blocked_launchable_resources` and
  `make_launchables_for_valid_region_zones` exist.

Plus a **runtime** no-op detector: if the original already returned >1 instance type for a
cloud, log once at WARNING (`upstream appears fixed; delete this patch`) and skip folding for
that cloud. Loud, never stops the server.

---

## 5. Deployment, rollback, observability

**Deploy.** Build the wheel; install into the API server image beside `skypilot-lyceum`; add
the `plugins.yaml` entry; deploy. Boot is the drift test.

**Controller gap — close it or scope it out explicitly.** Declaring
`PluginContext.CONTROLLER` does nothing unless the wheel reaches the managed-jobs controller
VM, which needs `controller_wheel_path` in `plugins.yaml` plus `~/.sky/remote_plugins.yaml`
(`server/plugins.py:363-371, 426-454`). `provision_with_retries` — and the re-optimise Patch
B exists to enable — runs **on the controller** for `sky jobs launch`. Without this, managed
jobs stay unfixed and nobody notices.

**Rollback.** Revision 1 assumed you can edit `plugins.yaml` and restart; on Fly, if
`install()` raises you get a crash loop and the config lives in the image. So: an env-var
kill switch read at the top of `install()` (`SKYPILOT_MARKETPLACE_FIXES_DISABLED=1`), making
recovery `fly secrets set` rather than an image rebuild. This is the missing half of the
"boot is the test" bet.

**Observability.** Today nobody would notice if Patch B stopped working — it degrades to
exactly the current behaviour, and the current behaviour is 8 identical failed launches. So:
- startup log per context: plugin version, anchors passed, active allowlist;
- per-call debug: `folded +N launchables across M instance types for <cloud>`;
- a **post-boot** health check (not an install-time anchor), once enabled clouds exist,
  surfaced on `/api/plugins` or as a log line;
- post-deploy verification of **both** patches:
  `list_accelerators(name_filter="H100", clouds=["shadeform"])` must no longer return
  `hyperstack_H100` (A), **and** a `sky.optimize` dryrun for `shadeform H100:1` must show ≥2
  distinct instance types in the launchable set (B).

**Version pin.** `skypilot==0.13.*`: both patches are pinned to internals of one release, so
`pip install -U skypilot` should fail the build, not the boot.

**Security, acknowledged.** Catalogs are fetched over HTTPS from GitHub with an md5 written of
whatever was downloaded — not a trusted digest. Raising pull frequency slightly widens the
window in which a poisoned catalog could steer jobs. Low risk; recorded rather than claimed
absent.

---

## 6. Tests

Per-patch units, plus: no-op when upstream is fixed; keyword-argument compatibility with all
three call sites; the `ordered` (resources-as-list) path; multi-node; the disabled-cloud /
empty-feasible invariant; and that a transient catalog download failure no longer poisons
`_df` for the process lifetime.

## 7. Relationship to ontic-cli PR #19

Once both patches are verified on the server, the client-side candidate expansion in PR #19 is
redundant and should be **deleted**, leaving the parts that stand alone: the `sky.yaml`
contract, the error-message work, `--retry-until-up`, and the stdout/`--json` hygiene.

Sequencing: land here, confirm on the server, then strip PR #19. Not both permanently. Patch
B is designed to be correct for multi-entry `any_of` on its own merits, so it does **not**
depend on PR #19 being stripped first.

## 8. Upstream

Three one-liners worth filing, cheapest first:
- `nebius_catalog.py` defines `_PULL_FREQUENCY_HOURS` and never passes it — the smallest,
  most obviously-correct PR, and a good trust-builder.
- `shadeform_catalog.py`: pass `pull_frequency_hours`, and stop materialising the
  `LazyDataFrame` so the per-request cache clear works as it does for every other cloud.
- `optimizer.py`: iterate `feasible_resources.resources_list` rather than taking `[0]`.

If any lands in a release, delete the corresponding patch here rather than carrying it.
