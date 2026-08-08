"""Patch A: the Shadeform catalog stops being frozen for the life of the process."""
import time

import pytest

from skypilot_marketplace_fixes import anchors, catalog_freshness

sky = pytest.importorskip('sky', reason='skypilot not installed')


@pytest.fixture(autouse=True)
def _unpatch():
    """Restore stock SkyPilot and drop this module's cached frame between tests."""
    from sky.catalog import common as catalog_common
    from sky.catalog import shadeform_catalog

    read = catalog_common.read_catalog
    get_df = shadeform_catalog._get_df
    yield
    catalog_common.read_catalog = getattr(read, '_marketplace_fixes_original', read)
    shadeform_catalog._get_df = getattr(get_df, '_marketplace_fixes_original', get_df)
    catalog_freshness._state.update({'lazy': None, 'frame': None, 'key': None,
                                     'loaded_at': 0.0})


# --- A1: the on-disk catalog gains a TTL --------------------------------------------

def test_the_named_catalog_gains_a_pull_frequency():
    from sky.catalog import common as catalog_common

    seen = {}
    catalog_common.read_catalog = lambda filename, pull_frequency_hours=None: \
        seen.setdefault(filename, pull_frequency_hours)
    catalog_freshness._patch_read_catalog(
        catalog_common, ('shadeform/vms.csv',), refresh_hours=1)
    catalog_common.read_catalog('shadeform/vms.csv')
    assert seen['shadeform/vms.csv'] == 1


def test_other_catalogs_are_left_alone():
    # Eleven other catalogs also omit the parameter, but changing their cadence is
    # out of scope — this patch only speaks for the ones it is told about.
    from sky.catalog import common as catalog_common

    seen = {}
    catalog_common.read_catalog = lambda filename, pull_frequency_hours=None: \
        seen.setdefault(filename, pull_frequency_hours)
    catalog_freshness._patch_read_catalog(
        catalog_common, ('shadeform/vms.csv',), refresh_hours=1)
    catalog_common.read_catalog('aws/vms.csv')
    assert seen['aws/vms.csv'] is None


def test_an_explicit_frequency_from_the_caller_wins():
    from sky.catalog import common as catalog_common

    seen = {}
    catalog_common.read_catalog = lambda filename, pull_frequency_hours=None: \
        seen.setdefault(filename, pull_frequency_hours)
    catalog_freshness._patch_read_catalog(
        catalog_common, ('shadeform/vms.csv',), refresh_hours=1)
    catalog_common.read_catalog('shadeform/vms.csv', 24)
    assert seen['shadeform/vms.csv'] == 24


# --- A2: the in-process frame stops being permanent ----------------------------------

def test_the_module_global_is_released_and_get_df_still_works():
    from sky.catalog import shadeform_catalog

    catalog_freshness.patch(('shadeform/vms.csv',), refresh_hours=1)
    assert shadeform_catalog._df is None, 'upstream frame must be released'
    frame = shadeform_catalog._get_df()
    assert list(frame.columns)[:2] == ['InstanceType', 'AcceleratorName']
    assert len(frame) > 0


def _point_catalog_at(monkeypatch, catalog_common, csv):
    """`_file_key` stats whatever `get_catalog_path` resolves to."""
    monkeypatch.setattr(catalog_common, 'get_catalog_path', lambda filename: str(csv))


def test_the_frame_is_re_derived_when_the_file_changes(monkeypatch, tmp_path):
    # The property that matters: freshness follows the FILE, not a clock, so a
    # worker converges on whatever another worker just wrote without waiting out
    # a TTL and without having done the download itself.
    from sky.catalog import common as catalog_common

    before = 'InstanceType,AcceleratorName,Price\nphantom_H100,H100,2.73\n'
    after = 'InstanceType,AcceleratorName,Price\nreal_H100,H100,3.30\n'
    # The key is (mtime_ns, size). Same-length payloads written inside one mtime
    # granule would collide, so this test must not depend on timing alone.
    assert len(before) != len(after), 'keep the two payloads different lengths'

    csv = tmp_path / 'vms.csv'
    csv.write_text(before)
    catalog_freshness._state.update({
        'lazy': catalog_common.LazyDataFrame(str(csv),
                                             update_if_stale_func=lambda: False),
        'frame': None, 'key': None, 'loaded_at': 0.0})
    _point_catalog_at(monkeypatch, catalog_common, csv)

    assert list(catalog_freshness._frame(catalog_common)['InstanceType']) == \
        ['phantom_H100']
    key = catalog_freshness._state['key']
    assert key is not None, 'the frame must record the identity it came from'

    # No change on disk: served from memory, identity unchanged.
    catalog_freshness._frame(catalog_common)
    assert catalog_freshness._state['key'] == key

    csv.write_text(after)
    assert list(catalog_freshness._frame(catalog_common)['InstanceType']) == \
        ['real_H100']
    assert catalog_freshness._state['key'] != key


def test_an_evict_landing_mid_read_does_not_surface_a_null_frame(monkeypatch, tmp_path):
    # Reads run off `_lock`, so the refresher can evict while a request thread is
    # inside `_load_df` — upstream assigns `self._df` and returns it as two separate
    # statements. Serving None there would raise out of `_filtered` and drop
    # Shadeform from that plan entirely.
    from sky.catalog import common as catalog_common

    csv = tmp_path / 'vms.csv'
    csv.write_text('InstanceType,AcceleratorName,Price\nscaleway_H100,H100,3.30\n')
    lazy = catalog_common.LazyDataFrame(str(csv), update_if_stale_func=lambda: False)
    _point_catalog_at(monkeypatch, catalog_common, csv)

    real_load = lazy._load_df
    calls = []

    def _evicted_once():
        calls.append(1)
        if len(calls) == 1:
            return None      # exactly what a concurrent _evict produces
        return real_load()

    monkeypatch.setattr(lazy, '_load_df', _evicted_once)
    assert list(catalog_freshness._filtered(lazy)['InstanceType']) == ['scaleway_H100']
    assert len(calls) == 2, 'the retry after an evicted read must happen'


def test_a_file_that_stops_changing_is_still_re_read_eventually(monkeypatch, tmp_path):
    # Backstop for what identity cannot see: nothing is writing the file at all
    # (dead refresher, or the lost-md5 trap that pins `_need_update` to False).
    # Re-deriving is what re-runs the download check.
    from sky.catalog import common as catalog_common

    csv = tmp_path / 'vms.csv'
    csv.write_text('InstanceType,AcceleratorName,Price\nphantom_H100,H100,2.73\n')
    reads = []
    catalog_freshness._state.update({
        'lazy': catalog_common.LazyDataFrame(
            str(csv), update_if_stale_func=lambda: reads.append(1) is None),
        'frame': None, 'key': None, 'loaded_at': 0.0})
    _point_catalog_at(monkeypatch, catalog_common, csv)

    catalog_freshness._frame(catalog_common)
    before = len(reads)
    catalog_freshness._frame(catalog_common)
    assert len(reads) == before, 'an unchanged file must not be re-read'

    loaded = catalog_freshness._state['loaded_at']
    monkeypatch.setattr(time, 'monotonic',
                        lambda: loaded + catalog_freshness._STALE_AFTER_S + 1)
    catalog_freshness._frame(catalog_common)
    assert len(reads) > before, 'the backstop must re-run the download check'


def test_the_read_never_holds_the_state_lock(monkeypatch, tmp_path):
    # The read can download, and upstream `_update_catalog` gives requests.get no
    # timeout and its filelock no timeout. Holding `_lock` across it would turn one
    # hung connection into a worker whose Shadeform planning never returns — a worse
    # failure than the stale catalog this module exists to prevent.
    from sky.catalog import common as catalog_common

    csv = tmp_path / 'vms.csv'
    csv.write_text('InstanceType,AcceleratorName,Price\nphantom_H100,H100,2.73\n')
    catalog_freshness._state.update({
        'lazy': catalog_common.LazyDataFrame(str(csv),
                                             update_if_stale_func=lambda: False),
        'frame': None, 'key': None, 'loaded_at': 0.0})
    _point_catalog_at(monkeypatch, catalog_common, csv)

    observed = []
    real_filtered = catalog_freshness._filtered
    monkeypatch.setattr(catalog_freshness, '_filtered',
                        lambda lazy: (observed.append(catalog_freshness._lock.locked())
                                      or real_filtered(lazy)))
    catalog_freshness.refresh(force=True)

    assert observed == [False], 'the download path ran while holding _lock'


def test_one_lazy_frame_is_reused_across_refreshes():
    # Minting a new LazyDataFrame per refresh would retain a (loader, frame) pair
    # in a shared lru_cache(maxsize=128) each time — noise for a 17 KB CSV, a real
    # leak if this approach ever touched a 5 MB one.
    from sky.catalog import shadeform_catalog

    catalog_freshness.patch(('shadeform/vms.csv',), refresh_hours=1)
    shadeform_catalog._get_df()
    lazy = catalog_freshness._state['lazy']
    catalog_freshness.refresh(force=True)
    assert catalog_freshness._state['lazy'] is lazy


def test_a_worker_that_did_not_win_the_download_race_still_re_reads(tmp_path):
    # The API server runs several long-lived executor workers over one catalog file.
    # Only the one that performs the download sees `update_if_stale_func() is True`;
    # the others must still pick up what it wrote, or they plan against listings that
    # were withdrawn hours ago while believing the file is fresh.
    from sky.catalog import common as catalog_common

    csv = tmp_path / 'vms.csv'
    csv.write_text('InstanceType,AcceleratorName,Price\n'
                   'massedcompute_H100,H100,2.73\n')
    lazy = catalog_common.LazyDataFrame(str(csv), update_if_stale_func=lambda: False)
    catalog_freshness._state.update({'lazy': lazy, 'frame': None, 'loaded_at': 0.0})
    assert list(catalog_freshness.refresh(force=True)['InstanceType']) == \
        ['massedcompute_H100']

    # Another worker refreshes the file on disk; the cheap H100 is withdrawn.
    csv.write_text('InstanceType,AcceleratorName,Price\n'
                   'scaleway_H100,H100,3.30\n')

    assert list(catalog_freshness.refresh(force=True)['InstanceType']) == \
        ['scaleway_H100'], 'the loser of the download race is frozen on a phantom'


def test_a_load_df_that_no_longer_short_circuits_refuses_to_patch(monkeypatch):
    from sky.catalog import common as catalog_common
    from sky.catalog import shadeform_catalog

    class _AlwaysReReads:
        def _load_df(self):
            return 'no short circuit here'

    monkeypatch.setattr(catalog_common, 'LazyDataFrame', _AlwaysReReads)
    with pytest.raises(anchors.PatchDriftError, match='_evict'):
        catalog_freshness._patch_get_df(catalog_common, shadeform_catalog)


def test_a_download_failure_is_not_cached_for_the_process_lifetime(monkeypatch):
    # Upstream stores the empty fallback in `_df` forever, so one transient failure
    # at first read poisons the process. That is fixed here for free.
    from sky.catalog import shadeform_catalog

    catalog_freshness.patch(('shadeform/vms.csv',), refresh_hours=1)
    monkeypatch.setattr(catalog_freshness, '_filtered',
                        lambda lazy: (_ for _ in ()).throw(OSError('network down')))
    empty = shadeform_catalog._get_df()
    assert len(empty) == 0
    assert catalog_freshness._state['frame'] is None, 'failure must not be cached'

    monkeypatch.undo()
    assert len(shadeform_catalog._get_df()) > 0, 'the next call must recover'


def test_patching_twice_is_a_no_op():
    from sky.catalog import shadeform_catalog

    catalog_freshness.patch(('shadeform/vms.csv',), refresh_hours=1)
    once = shadeform_catalog._get_df
    catalog_freshness.patch(('shadeform/vms.csv',), refresh_hours=1)
    assert shadeform_catalog._get_df is once


def test_a_missing_module_global_refuses_to_patch(monkeypatch):
    from sky.catalog import common as catalog_common
    from sky.catalog import shadeform_catalog

    monkeypatch.delattr(shadeform_catalog, '_df')
    with pytest.raises(anchors.PatchDriftError, match='DELETE'):
        catalog_freshness._patch_get_df(catalog_common, shadeform_catalog)


def test_a_renamed_read_catalog_parameter_refuses_to_patch(monkeypatch):
    from sky.catalog import common as catalog_common

    monkeypatch.setattr(catalog_common, 'read_catalog',
                        lambda name, hours=None: None)
    with pytest.raises(anchors.PatchDriftError, match='pull_frequency_hours'):
        catalog_freshness._patch_read_catalog(
            catalog_common, ('shadeform/vms.csv',), refresh_hours=1)


# --- A3: the refresher is off the request path ----------------------------------------

def test_the_background_refresher_is_a_daemon_and_starts_once():
    catalog_freshness.start_background_refresh(refresh_hours=1)
    first = catalog_freshness._refresher
    assert first is not None and first.daemon and first.is_alive()
    catalog_freshness.start_background_refresh(refresh_hours=1)
    assert catalog_freshness._refresher is first
