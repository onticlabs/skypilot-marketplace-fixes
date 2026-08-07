"""Patch C: pricing a cluster on a cloud whose catalog has no zone column."""
from __future__ import annotations

import pandas as pd
import pytest

from skypilot_marketplace_fixes import anchors, zoneless_pricing

# The Shadeform catalog's real columns — no `AvailabilityZone`. Two regions at the
# same price, because Shadeform prices per instance type, not per region.
ZONELESS = pd.DataFrame([
    {'InstanceType': 'massedcompute_L40S', 'Price': 0.88,
     'Region': 'kansascity-usa-1', 'SpotPrice': None},
    {'InstanceType': 'massedcompute_L40S', 'Price': 0.88,
     'Region': 'desmoines-usa-1', 'SpotPrice': None},
])

ZONED = pd.DataFrame([
    {'InstanceType': 'p4d.24xlarge', 'Price': 32.77, 'Region': 'us-east-1',
     'AvailabilityZone': 'us-east-1a', 'SpotPrice': 9.83},
    {'InstanceType': 'p4d.24xlarge', 'Price': 32.77, 'Region': 'us-east-1',
     'AvailabilityZone': 'us-east-1b', 'SpotPrice': 9.83},
])


@pytest.fixture
def patched():
    from sky.catalog import common as catalog_common
    original = catalog_common._get_instance_type
    zoneless_pricing.patch()
    yield catalog_common
    catalog_common._get_instance_type = original


def test_a_zone_on_a_zoneless_catalog_raises_without_the_patch():
    """The defect itself. If this stops raising, upstream fixed it and the patch
    should be deleted, not kept."""
    from sky.catalog import common as catalog_common
    with pytest.raises(KeyError, match='AvailabilityZone'):
        catalog_common._get_instance_type(
            ZONELESS, 'massedcompute_L40S', 'kansascity-usa-1',
            'kansascity-usa-1')


def test_the_price_survives_a_zone_the_cloud_does_not_have(patched):
    """Shadeform's provisioner stamps `zone=region` on the success path, so this
    is the shape every real run carries — and the one that reported $0.00."""
    got = patched._get_instance_type(
        ZONELESS, 'massedcompute_L40S', 'kansascity-usa-1', 'kansascity-usa-1')
    assert list(got['Price']) == [0.88]


def test_the_region_filter_is_still_applied(patched):
    """Dropping the zone must not widen the match — region still selects one row."""
    got = patched._get_instance_type(
        ZONELESS, 'massedcompute_L40S', 'desmoines-usa-1', 'desmoines-usa-1')
    assert list(got['Region']) == ['desmoines-usa-1']


def test_a_real_zone_on_a_zoned_catalog_is_untouched(patched):
    """AWS and friends have the column and mean it. The guard must not reach them."""
    got = patched._get_instance_type(ZONED, 'p4d.24xlarge', 'us-east-1',
                                     'us-east-1b')
    assert list(got['AvailabilityZone']) == ['us-east-1b']


def test_a_wrong_zone_on_a_zoned_catalog_still_matches_nothing(patched):
    """The guard must not turn a genuinely bad zone into a silent success."""
    got = patched._get_instance_type(ZONED, 'p4d.24xlarge', 'us-east-1',
                                     'us-west-2c')
    assert got.empty


def test_no_zone_at_all_is_unchanged(patched):
    got = patched._get_instance_type(ZONELESS, 'massedcompute_L40S', None, None)
    assert len(got) == 2


def test_applying_twice_does_not_stack_wrappers(patched):
    before = patched._get_instance_type
    zoneless_pricing.patch()
    assert patched._get_instance_type is before


def test_drift_in_the_defect_refuses_to_boot(monkeypatch, patched):
    """When upstream guards the column, the premise is gone and the right move is
    to delete this patch — so refusing to start, loudly, is the intended outcome."""
    def guarded(df, instance_type, region, zone=None):
        if zone is not None and 'AvailabilityZone' in df.columns:
            pass
        return df

    monkeypatch.setattr(patched, '_get_instance_type', guarded)
    with pytest.raises(anchors.PatchDriftError, match='has nothing left'):
        zoneless_pricing.patch()
