"""Patch D: an instance type that has left the catalog must not raise.

The scenario throughout is the real one: a cluster is UP on `latitude_H100`, the
hourly catalog refresh pulls a snapshot that no longer lists it, and every code
path that reprs that cluster's resources goes through these lookups.
"""
from __future__ import annotations

import pandas as pd
import pytest

from skypilot_marketplace_fixes import anchors, vanished_instance_type

#: A Shadeform catalog snapshot AFTER `latitude_H100` was delisted. The columns
#: are the real ones; the point is the absence.
WITHOUT_LATITUDE = pd.DataFrame([
    {'InstanceType': 'scaleway_H100', 'AcceleratorName': 'H100',
     'AcceleratorCount': 1.0, 'vCPUs': 24.0, 'MemoryGiB': 240.0, 'Price': 2.73,
     'Region': 'paris-france-1', 'GpuInfo': '', 'SpotPrice': None},
])

GONE = 'latitude_H100'

#: The exact message `catalog/common.py` raises, and the whole reason this patch
#: exists: "No ... found" does not contain "not found".
VANISHED_ERROR = ValueError(f'No instance type {GONE} found.')


@pytest.fixture
def patched():
    from sky.catalog import shadeform_catalog
    original = shadeform_catalog._is_not_found_error
    vanished_instance_type.patch()
    yield shadeform_catalog
    shadeform_catalog._is_not_found_error = original


def test_the_predicate_misses_the_message_without_the_patch():
    """The defect itself, stated as an assertion.

    If this ever fails, upstream fixed the mismatch and this patch should be
    DELETED rather than kept.
    """
    from sky.catalog import shadeform_catalog
    assert shadeform_catalog._is_not_found_error(VANISHED_ERROR) is False


def test_the_lookup_raises_without_the_patch():
    """What that miss costs: the ValueError escapes `_call_or_default`, and in
    production it took `core.status()` down with it."""
    from sky.catalog import shadeform_catalog
    with pytest.raises(ValueError, match='No instance type'):
        shadeform_catalog._call_or_default(
            lambda: (_ for _ in ()).throw(VANISHED_ERROR), (None, None))


def test_a_delisted_instance_type_is_recognised(patched):
    assert patched._is_not_found_error(VANISHED_ERROR) is True


def test_the_default_is_returned_instead_of_raising(patched):
    """Upstream's own default for this call site, finally reachable."""
    got = patched._call_or_default(
        lambda: (_ for _ in ()).throw(VANISHED_ERROR), (None, None))
    assert got == (None, None)


def test_vcpus_mem_degrades_rather_than_exploding(patched, monkeypatch):
    """The end-to-end shape: this is the call `Resources.memory` makes, and the
    one that broke `sky status` for the whole deployment."""
    monkeypatch.setattr(patched, '_get_df', lambda: WITHOUT_LATITUDE)
    assert patched.get_vcpus_mem_from_instance_type(GONE) == (None, None)


def test_an_instance_type_still_in_the_catalog_is_unaffected(patched,
                                                             monkeypatch):
    """The guard must not blunt a lookup that should succeed."""
    monkeypatch.setattr(patched, '_get_df', lambda: WITHOUT_LATITUDE)
    assert patched.get_vcpus_mem_from_instance_type('scaleway_H100') == (24.0,
                                                                         240.0)


def test_accelerators_lookup_degrades_too(patched, monkeypatch):
    """A second of the five call sites, to show the fix is at the shared gate
    rather than in one caller."""
    monkeypatch.setattr(patched, '_get_df', lambda: WITHOUT_LATITUDE)
    assert patched.get_accelerators_from_instance_type(GONE) is None


def test_upstreams_own_cases_still_pass(patched):
    """Delegation: the original predicate's answers are preserved, not replaced."""
    assert patched._is_not_found_error(ValueError('Region not found')) is True
    assert patched._is_not_found_error(
        ValueError('Spot instances are not supported on Shadeform')) is True


def test_an_unrelated_value_error_still_raises(patched):
    """The regex must not become a catch-all. A ValueError that is NOT a delisted
    instance type has to keep propagating — swallowing it would turn a loud bug
    into a silent wrong answer, which is the failure mode this package exists to
    avoid."""
    assert patched._is_not_found_error(
        ValueError('Cannot determine the number of vCPUs')) is False
    with pytest.raises(ValueError, match='Cannot determine'):
        patched._call_or_default(
            lambda: (_ for _ in ()).throw(
                ValueError('Cannot determine the number of vCPUs')), None)


def test_a_message_merely_containing_found_is_not_swallowed(patched):
    """`'found' in msg` would have been the lazy fix; it would also swallow this."""
    assert patched._is_not_found_error(
        ValueError('Zone eu-west-1a found in more than one region')) is False


def test_applying_twice_does_not_stack_wrappers(patched):
    before = patched._is_not_found_error
    vanished_instance_type.patch()
    assert patched._is_not_found_error is before


def test_drift_in_the_predicate_refuses_to_boot(monkeypatch, patched):
    """Upstream repairing the predicate means the premise is gone; refusing to
    start, loudly, is the intended outcome."""
    def repaired(err):
        msg = str(err).lower()
        return 'no instance type' in msg

    monkeypatch.setattr(patched, '_is_not_found_error', repaired)
    with pytest.raises(anchors.PatchDriftError, match='no longer tests'):
        vanished_instance_type.patch()


def test_drift_in_the_message_refuses_to_boot(monkeypatch):
    """The other half of the mismatch. If the wording changes, the regex is
    matching a message that no longer exists and must be re-derived."""
    from sky.catalog import common as catalog_common

    def reworded(df, instance_type):
        raise ValueError(f'Unknown instance type {instance_type}.')

    monkeypatch.setattr(catalog_common, 'get_vcpus_mem_from_instance_type_impl',
                        reworded)
    with pytest.raises(anchors.PatchDriftError, match='wording changed'):
        vanished_instance_type.patch()
