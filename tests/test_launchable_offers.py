"""Patch B: every feasible offer becomes launchable, not just the cheapest."""
import logging

import pytest

from skypilot_marketplace_fixes import anchors, launchable_offers

sky = pytest.importorskip('sky', reason='skypilot not installed')


@pytest.fixture(autouse=True)
def _unpatch():
    """Each test starts from stock SkyPilot and leaves it that way.

    The patch mutates a module global in a process the whole suite shares, so
    without this a failure in one test silently changes the others.
    """
    from sky import optimizer
    original = optimizer._fill_in_launchable_resources
    launchable_offers._reported_noop.clear()
    yield
    optimizer._fill_in_launchable_resources = getattr(
        original, '_marketplace_fixes_original', original)


def _launchables(resources: dict, enabled):
    """The (instance_type, region) pairs SkyPilot would be able to provision."""
    from sky import check as sky_check
    from sky import optimizer

    original = sky_check.get_cached_enabled_clouds_or_refresh
    sky_check.get_cached_enabled_clouds_or_refresh = lambda **kw: enabled
    optimizer.sky_check.get_cached_enabled_clouds_or_refresh = lambda **kw: enabled
    try:
        task = sky.Task(run='echo hi')
        task.set_resources(sky.Resources.from_yaml_config(resources))
        launchable, _, _, _ = optimizer._fill_in_launchable_resources(
            task, [], quiet=True)
        return {(r.instance_type, r.region)
                for lst in launchable.values() for r in lst}
    finally:
        sky_check.get_cached_enabled_clouds_or_refresh = original
        optimizer.sky_check.get_cached_enabled_clouds_or_refresh = original


@pytest.fixture(scope='module')
def shadeform():
    from sky import clouds
    cloud = clouds.Shadeform()
    try:
        feasible = cloud.get_feasible_launchable_resources(
            next(iter(sky.Resources.from_yaml_config(
                {'infra': 'shadeform', 'accelerators': 'H100:1'}))), 1)
    except (OSError, ConnectionError, TimeoutError) as e:
        pytest.skip(f'shadeform catalog unreachable: {e}')
    if len(feasible.resources_list) < 2:
        pytest.skip('shadeform lists <2 H100 instance types; nothing to widen')
    return cloud


REQ = {'infra': 'shadeform', 'accelerators': 'H100:1'}


def test_stock_skypilot_reaches_one_instance_type(shadeform):
    # The defect, characterised. If this ever fails, upstream fixed it and this
    # whole package should be deleted rather than carried.
    types = {t for t, _ in _launchables(REQ, [shadeform])}
    assert len(types) == 1


def test_the_patch_reaches_every_feasible_instance_type(shadeform):
    before = _launchables(REQ, [shadeform])
    launchable_offers.patch(['shadeform'], max_extra_instance_types=99)
    after = _launchables(REQ, [shadeform])
    assert {t for t, _ in after} > {t for t, _ in before}
    assert len(after) > len(before)


def test_the_cap_bounds_how_many_extra_types_are_folded(shadeform):
    launchable_offers.patch(['shadeform'], max_extra_instance_types=2)
    assert len({t for t, _ in _launchables(REQ, [shadeform])}) <= 2


def test_a_cloud_outside_the_allowlist_is_untouched(shadeform):
    before = _launchables(REQ, [shadeform])
    launchable_offers.patch(['lyceum'], max_extra_instance_types=99)
    # The allowlist is the blast-radius control: folding on a hyperscaler measured
    # 3.2-5.7x optimizer wall time, so a cloud not named must cost exactly nothing.
    assert _launchables(REQ, [shadeform]) == before


def test_blocked_resources_are_still_excluded_after_folding(shadeform):
    from sky import clouds, optimizer

    launchable_offers.patch(['shadeform'], max_extra_instance_types=99)
    task = sky.Task(run='echo hi')
    task.set_resources(sky.Resources.from_yaml_config(REQ))
    from sky import check as sky_check
    sky_check.get_cached_enabled_clouds_or_refresh = lambda **kw: [shadeform]
    optimizer.sky_check.get_cached_enabled_clouds_or_refresh = lambda **kw: [shadeform]
    everything, _, _, _ = optimizer._fill_in_launchable_resources(task, [], quiet=True)
    victim = next(iter(next(iter(everything.values()))))
    blocked = [sky.Resources(cloud=clouds.Shadeform(),
                             instance_type=victim.instance_type,
                             region=victim.region)]
    kept, _, _, _ = optimizer._fill_in_launchable_resources(task, blocked, quiet=True)
    pairs = {(r.instance_type, r.region) for lst in kept.values() for r in lst}
    assert (victim.instance_type, victim.region) not in pairs


def test_blocked_resources_may_be_a_consumed_iterator(shadeform):
    # The parameter is typed Optional[Iterable]. A generator read once would block
    # nothing on the second read — silently.
    from sky import optimizer

    launchable_offers.patch(['shadeform'], max_extra_instance_types=99)
    task = sky.Task(run='echo hi')
    task.set_resources(sky.Resources.from_yaml_config(REQ))
    from sky import check as sky_check
    sky_check.get_cached_enabled_clouds_or_refresh = lambda **kw: [shadeform]
    optimizer.sky_check.get_cached_enabled_clouds_or_refresh = lambda **kw: [shadeform]
    optimizer._fill_in_launchable_resources(task, iter([]), quiet=True)  # must not raise


def test_patching_twice_is_a_no_op(shadeform):
    from sky import optimizer

    launchable_offers.patch(['shadeform'], max_extra_instance_types=99)
    once = optimizer._fill_in_launchable_resources
    launchable_offers.patch(['shadeform'], max_extra_instance_types=99)
    assert optimizer._fill_in_launchable_resources is once


def test_the_original_stays_reachable_for_the_drift_check(shadeform):
    from sky import optimizer

    launchable_offers.patch(['shadeform'], max_extra_instance_types=99)
    original = optimizer._fill_in_launchable_resources._marketplace_fixes_original
    anchors.require_source_contains(original, 'resources_list[0]', 'anchor')


def test_an_entry_with_no_feasible_resources_stays_empty():
    # Upstream logs "No resource satisfying ..." and returns []. Folding anything
    # in would leave the log and the data disagreeing.
    from sky import clouds

    launchable_offers.patch(['shadeform'], max_extra_instance_types=99)
    launchable, _, _, _ = _fill(
        {'infra': 'shadeform', 'accelerators': 'NOSUCHGPU:1'}, [clouds.Shadeform()])
    assert all(not v for v in launchable.values())


def _fill(resources, enabled):
    from sky import check as sky_check
    from sky import optimizer

    sky_check.get_cached_enabled_clouds_or_refresh = lambda **kw: enabled
    optimizer.sky_check.get_cached_enabled_clouds_or_refresh = lambda **kw: enabled
    task = sky.Task(run='echo hi')
    task.set_resources(sky.Resources.from_yaml_config(resources))
    return optimizer._fill_in_launchable_resources(task, [], quiet=True)


def test_a_moved_anchor_refuses_to_patch(monkeypatch):
    from sky import optimizer

    def impostor(task, blocked_resources, quiet=False):
        # Deliberately does not contain the truncation the anchor greps for.
        # (Do not name it here either — inspect.getsource sees this comment.)
        return {}, {}, [], {}

    monkeypatch.setattr(optimizer, '_fill_in_launchable_resources', impostor)
    with pytest.raises(anchors.PatchDriftError, match='DELETED'):
        launchable_offers.patch(['shadeform'], max_extra_instance_types=4)


def test_a_renamed_parameter_refuses_to_patch(monkeypatch):
    # Two call sites pass `blocked_resources=` by keyword; a rename would break
    # every launch at request time after a clean boot.
    from sky import optimizer

    def renamed(task, blocked, quiet=False):
        """resources_list[0]"""

    monkeypatch.setattr(optimizer, '_fill_in_launchable_resources', renamed)
    with pytest.raises(anchors.PatchDriftError, match='blocked_resources'):
        launchable_offers.patch(['shadeform'], max_extra_instance_types=4)


def test_upstream_looking_fixed_warns_but_never_blocks(caplog):
    class FakeCloud:
        def is_same_cloud(self, other):
            return True
        def __str__(self):
            return 'shadeform'

    class R:
        def __init__(self, it):
            self.instance_type, self.cloud = it, FakeCloud()

    with caplog.at_level(logging.WARNING):
        skipped = launchable_offers._already_multi_typed(
            [R('a'), R('b')], FakeCloud(), 'shadeform')
    assert skipped is True
    assert 'upstream appears fixed' in caplog.text


def test_dedupe_is_structural_not_by_identity():
    # `Resources` defines neither __eq__ nor __hash__, so a set() would compare by
    # identity and silently keep every duplicate. Stubs rather than real Resources:
    # the point is the KEY, and two real ones would need a live catalog to build.
    class Stub:
        cloud, instance_type, region = 'Shadeform', 'scaleway_H100', 'paris-france-1'
        zone, use_spot, accelerators = None, False, {'H100': 1}

    one, two = Stub(), Stub()
    assert one is not two
    assert len(launchable_offers._dedupe([one, two])) == 1
    two.region = 'warsaw-poland-1'
    assert len(launchable_offers._dedupe([one, two])) == 2
