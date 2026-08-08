"""Patch A — stop the Shadeform catalog being frozen for the life of the process.

`common.read_catalog` returns a `LazyDataFrame` whose loader is an
`lru_cache(scope='request')` that the executor clears at the end of every request,
so every cloud gets a per-request re-read for free. Shadeform is the only catalog
that MATERIALISES that lazy frame into a plain DataFrame:

    df = common.read_catalog('shadeform/vms.csv')   # LazyDataFrame
    df = df[df['InstanceType'].notna()]             # __getitem__ -> real frame
    _df = df.reset_index(drop=True)                 # module global, forever

which defeats the refresh, and the module-level `_df` then pins the result for
the process lifetime. Compounding it, `read_catalog` is called without
`pull_frequency_hours`, so `_need_update()` returns False whenever the file
merely exists and the CSV on disk never re-downloads either.

Net effect on a long-running API server: it plans against whatever shipped with
the machine's disk. Ours still offers an H100 at $1.90 in a region where that
instance type no longer exists — the cheapest listing, so the optimizer picks it
every time and it can never be provisioned.

Four parts, all needed:
  A1  give `read_catalog` a pull frequency, so the file on disk can re-download.
  A2  stop materialising: hold ONE LazyDataFrame and re-derive the filtered frame
      whenever the CSV underneath it changes, clearing the shared loader cache.
  A3  do the downloading on a daemon thread, so it is normally off the request path.
  A4  evict the LazyDataFrame's own `_df` on refresh. A1-A3 are not enough on a
      server with several long-lived executor workers: only the worker that wins
      the download race re-reads, and the rest serve a startup snapshot forever.
      See `_evict`.

Freshness is keyed on the FILE's identity — `(mtime_ns, size)` — rather than on a
per-process timer. Two independent clocks (the file's download cycle and each
worker's refresh cycle, phase-offset by process start) put the worst case at just
under twice the interval and let workers disagree in between; one `os.stat` puts
every worker on the newest bytes the moment anyone writes them, whoever that is.
`_STALE_AFTER_S` remains only as a backstop for a file that stops changing at all.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterable

from skypilot_marketplace_fixes import anchors

logger = logging.getLogger(__name__)

#: The catalog this package materialises around. A1 is safe to widen (eleven
#: other catalogs also omit `pull_frequency_hours`, `nebius` most obviously —
#: it defines the constant and never passes it); A2 is deliberately NOT, because
#: holding a frame for a 5 MB catalog like `aws/vms.csv` is a different tradeoff.
_MATERIALISING_CATALOG = 'shadeform/vms.csv'

#: Backstop only. Freshness is decided by the FILE's identity (`_file_key`), not by
#: a clock: any change on disk is picked up on the next call, whoever wrote it. This
#: timer exists for the one case identity cannot see — the file never changing
#: because nothing is downloading it (a dead refresher, or the lost-md5 trap where
#: `is_catalog_modified` pins `_need_update` to False). Re-deriving then re-runs the
#: download check. One hour, matching the refresher, so a process that somehow lost
#: its thread degrades to "as fresh as the file" rather than to hours of drift.
_STALE_AFTER_S = 3600

#: `key` is the (mtime_ns, size) of the CSV the frame was derived from.
_state: dict = {'lazy': None, 'frame': None, 'key': None, 'loaded_at': 0.0}

#: Guards `_state` ONLY. Never held across a read: `_update_catalog` downloads with
#: no timeout under a filelock with no timeout, and holding this lock across that
#: turns one hung connection into a worker whose Shadeform planning never returns.
_lock = threading.Lock()
_refresher: threading.Thread | None = None


def patch(catalog_files: Iterable[str], refresh_hours: float) -> None:
    """Apply A1 + A2. Idempotent."""
    from sky.catalog import common as catalog_common
    from sky.catalog import shadeform_catalog

    _patch_read_catalog(catalog_common, tuple(catalog_files), refresh_hours)
    _patch_get_df(catalog_common, shadeform_catalog)


def start_background_refresh(refresh_hours: float) -> None:
    """A3. Start the off-path refresher. Idempotent; never raises into the caller."""
    global _refresher
    if _refresher is not None and _refresher.is_alive():
        return
    interval = max(60.0, refresh_hours * 3600.0)

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                refresh(force=True)
            except Exception as e:  # noqa: BLE001 - a refresh failure must not kill the thread
                logger.warning('marketplace-fixes: catalog refresh failed: %s', e)

    _refresher = threading.Thread(target=_loop, name='marketplace-fixes-catalog',
                                  daemon=True)
    _refresher.start()


def _patch_read_catalog(catalog_common, catalog_files: tuple, refresh_hours: float) -> None:
    original = anchors.require_attr(
        catalog_common, 'read_catalog',
        'It is where the on-disk catalog TTL is decided.')
    if getattr(original, '_marketplace_fixes_patched', False):
        return
    anchors.require_params(original, 'filename', 'pull_frequency_hours')

    def patched(filename, pull_frequency_hours=None):
        # Only when the caller expressed no opinion. Every catalog that already
        # passes a frequency keeps it; changing those is out of scope.
        if pull_frequency_hours is None and filename in catalog_files:
            pull_frequency_hours = refresh_hours
        return original(filename, pull_frequency_hours)

    patched._marketplace_fixes_patched = True
    # Keep the original reachable: after patching, anything inspecting this name
    # sees OUR wrapper, so the anchor above becomes un-checkable exactly when you
    # still want to check it.
    patched._marketplace_fixes_original = original
    catalog_common.read_catalog = patched


def _patch_get_df(catalog_common, shadeform_catalog) -> None:
    original = anchors.require_attr(
        shadeform_catalog, '_get_df',
        'It is the single reader of the Shadeform offer table.')
    if getattr(original, '_marketplace_fixes_patched', False):
        return
    if not hasattr(shadeform_catalog, '_df'):
        raise anchors.PatchDriftError(
            'sky.catalog.shadeform_catalog no longer has a module-level `_df`. '
            'The caching shape this patch replaces has changed; upstream may '
            'have fixed it, in which case DELETE this patch rather than keep it.')
    lazy_cls = anchors.require_attr(
        catalog_common, 'LazyDataFrame',
        'Its shared loader cache is what gets cleared on expiry.')
    # `_evict` drops `self._df` because `_load_df` short-circuits on it. If that
    # short-circuit ever goes away, the eviction is dead weight and should go too.
    anchors.require_source_contains(
        lazy_cls._load_df, 'self._df is None',
        'LazyDataFrame._load_df no longer re-reads based on `self._df`; the '
        'per-process freeze this eviction works around may be fixed upstream. '
        'Re-check `_evict` and delete it if so.')

    def patched():
        return _frame(catalog_common)

    patched._marketplace_fixes_patched = True
    patched._marketplace_fixes_original = original
    shadeform_catalog._get_df = patched
    # Drop upstream's frozen frame so nothing keeps it alive. `_get_df` is the
    # only reader, so this is safe; it is here to release the memory, not to
    # change behaviour.
    shadeform_catalog._df = None


def _file_key(catalog_common):
    """Identity of the CSV on disk: `(mtime_ns, size)`, or None if it is not there.

    This, not a timer, is what decides whether the cached frame is current. It costs
    one `os.stat` and it converges on ANY writer — this process, another worker, or a
    hand-edited file — which a per-process clock cannot do.
    """
    try:
        st = os.stat(catalog_common.get_catalog_path(_MATERIALISING_CATALOG))
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _frame(catalog_common):
    """The filtered offer table, re-derived when the file underneath it changed."""
    key = _file_key(catalog_common)
    with _lock:
        current = (_state['frame'] is not None and key is not None and
                   _state['key'] == key and
                   time.monotonic() - _state['loaded_at'] < _STALE_AFTER_S)
        if current:
            return _state['frame']
    return refresh(force=False, catalog_common=catalog_common)


def refresh(force: bool, catalog_common=None):
    """Re-derive the filtered frame from the (single) LazyDataFrame.

    The read happens OUTSIDE `_lock`. It can download, and `_update_catalog` gives
    `requests.get` no timeout and its filelock no timeout, so holding the lock across
    it would let one hung connection wedge every caller of `_get_df` in this process —
    trading a stale catalog for a worker that cannot plan at all. Two threads racing
    here cost a duplicate read of a 20 KB CSV and nothing else.
    """
    if catalog_common is None:
        from sky.catalog import common as catalog_common

    with _lock:
        key = _file_key(catalog_common)
        if not force and _state['frame'] is not None and key is not None and \
                _state['key'] == key and \
                time.monotonic() - _state['loaded_at'] < _STALE_AFTER_S:
            return _state['frame']

        lazy = _state['lazy']
        if lazy is None:
            # Built on FIRST USE, not at patch time: `read_catalog` resolves a path
            # that may not exist yet, and a plugin `install()` must not do network
            # I/O — that is the boot-path stall this server has been taken down by
            # before. Constructing it does not read or download; `_load_df` does.
            lazy = _state['lazy'] = catalog_common.read_catalog(_MATERIALISING_CATALOG)
            fresh_instance = True
        else:
            fresh_instance = False

    if not fresh_instance:
        # Shared, class-level lru_cache keyed on the LazyDataFrame instance. Clearing
        # it is what makes the SAME instance re-read; minting a new LazyDataFrame
        # instead would retain a (frame, loader) pair per refresh for up to 128
        # entries. `_evict` is the half that actually matters — see its docstring.
        catalog_common.LazyDataFrame._load_df.cache_clear()
        _evict(lazy)

    try:
        frame = _filtered(lazy)
    except Exception as e:  # noqa: BLE001 - any read failure degrades, never raises
        # Deliberately NOT cached. Upstream caches the empty fallback frame in `_df`
        # forever, so one transient download failure at first read poisons the
        # process for its lifetime.
        logger.warning('marketplace-fixes: could not read %s (%s); serving '
                       'an empty catalog for this call only',
                       _MATERIALISING_CATALOG, e)
        return _empty_frame()

    with _lock:
        _state['frame'] = frame
        # Re-stat rather than reuse the key read above: the read may itself have
        # downloaded a new file, and stamping the OLD key would leave the next caller
        # believing this frame is older than it is, re-deriving it for nothing.
        _state['key'] = _file_key(catalog_common)
        _state['loaded_at'] = time.monotonic()
    return frame


def _evict(lazy) -> None:
    """Drop the LazyDataFrame's OWN copy of the frame, so it must re-read the CSV.

    Clearing the lru_cache alone is not enough. `_load_df` re-reads only when::

        if self._update_if_stale_func() or self._df is None:

    and `_update_if_stale_func` reports whether THIS process performed the
    download — not whether the file changed. The API server runs several
    long-lived executor workers, each with its own LazyDataFrame over the same
    path, so exactly one of them wins the download race and returns True. Every
    other worker sees a file that is already fresh, keeps the `self._df` it read
    at startup, and serves it for the lifetime of the process.

    That is the same freeze this module exists to prevent, one level down, and
    it is worse than the original: the workers disagree, so identical launches
    succeed or fail depending on which one picks them up. Measured on the live
    server (2026-08-08): two of three long workers were planning against a
    20-hour-old snapshot whose cheapest H100, `massedcompute_H100` in
    desmoines-usa-1, Shadeform had since withdrawn — so those launches failed
    on a listing that no longer existed while real H100s sat available.
    """
    lazy._df = None


def _filtered(lazy):
    """Upstream's own filtering, applied to the lazy frame rather than replacing it."""
    df = lazy[lazy['InstanceType'].notna()]
    if 'AcceleratorName' in df.columns:
        df = df[df['AcceleratorName'].notna()]
        df = df.assign(
            AcceleratorName=df['AcceleratorName'].astype(str).str.strip())
    return df.reset_index(drop=True)


def _empty_frame():
    import pandas as pd
    return pd.DataFrame(columns=[
        'InstanceType', 'AcceleratorName', 'AcceleratorCount', 'vCPUs',
        'MemoryGiB', 'Price', 'Region', 'GpuInfo', 'SpotPrice'
    ])
