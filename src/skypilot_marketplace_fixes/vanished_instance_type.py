"""Patch D: survive an instance type that has left the catalog.

THE DEFECT. `sky/catalog/shadeform_catalog.py` wraps five catalog lookups in a
guard that is meant to answer "not in the catalog" with a default instead of
raising:

    def _is_not_found_error(err: ValueError) -> bool:
        msg = str(err).lower()
        return 'not found' in msg or 'not supported' in msg

    def _call_or_default(func, default):
        try:
            return func()
        except ValueError as err:
            if _is_not_found_error(err):
                return default
            raise

The message it is trying to recognise is raised four times in
`catalog/common.py` and reads:

    No instance type {instance_type} found.

"No ... found" never contains the substring "not found". So the predicate
returns False for the one error it exists to catch, `_call_or_default` re-raises,
and all five defaults — `(None, None)`, `None`, `None`, `(None, [])`, `[]` — are
unreachable dead code for the case they were written for.

WHY THAT IS AN OUTAGE AND NOT A CURIOSITY. A marketplace catalog is a snapshot of
somebody else's inventory, refreshed hourly here from `HOSTED_CATALOG_DIR_URL`.
Instance types come and go while clusters launched from them are still running.
When that happens `Resources.memory` — reached by `repr(handle.launched_resources)`,
which `core.status()` does for every cluster — raises, and the ValueError takes
out the whole call. Not one cluster's row: the entire response.

Everything that walks the cluster registry then dies together: `sky status`,
`ontic cluster list`, and the launch matcher, so **no new job can be submitted
anywhere on the deployment** until somebody removes the offending record by hand.
One stale row is a full control-plane outage.

Observed 2026-08-13: `latitude_H100` left the hosted catalog while
`ontic-solo-732a63e0` was UP on it. Every `sky.status` failed from 15:41 UTC
until the record was torn down. 135 of 617 rows in `cluster_history` had been
launched on that same instance type, so this was the routine match for `H100:1`,
not an exotic corner — and Shadeform's live API still offered it, so the trigger
is ordinary snapshot drift rather than a vendor withdrawing a product.

WHY THE FIX IS THE PREDICATE AND NOT THE CALLERS. Upstream already decided what
should happen: five call sites, five deliberate defaults, each the natural "no
such instance type" answer. The intent is not in question — only the string test
that gates it. Widening the predicate restores the behaviour upstream wrote and
touches nothing else. Patching the five callers, or the raise sites in
`common.py`, would be inventing a policy where one already exists.

The wrapper delegates to the original predicate first, so a future upstream that
recognises more error shapes keeps its own answer and this patch only ever adds
the one case it names.

SCOPE. Shadeform only, because `_call_or_default` exists only there — no other
catalog in `sky/catalog/`, and not the out-of-tree Lyceum plugin, defines this
guard. When Lyceum grows one it will need the same treatment, and the anchors
below will not notice, because they check the module this patch actually edits.

REPLACING A MODULE-LEVEL NAME IS ENOUGH. `_call_or_default` resolves
`_is_not_found_error` as a module global on each call rather than capturing it,
so rebinding the attribute reaches every existing caller without touching the
five wrappers.

UPSTREAM. Worth reporting: the predicate cannot match the message that
`catalog/common.py` raises. Unfixed as of skypilot 0.13.0.
"""
from __future__ import annotations

import functools
import logging
import re

from skypilot_marketplace_fixes import anchors

logger = logging.getLogger(__name__)

#: The broken test this patch exists to widen. When upstream repairs it, the
#: substring changes and `require_source_contains` fails the boot — at which
#: point this module should be DELETED, not repaired. See anchors.py.
_DEFECT = "return 'not found' in msg or 'not supported' in msg"

#: The message the predicate above fails to recognise, anchored at its raise site
#: so drift on EITHER side of the mismatch is caught. A patch whose premise is a
#: mismatch has two premises.
_MESSAGE = 'No instance type {instance_type} found.'

#: Deliberately not `'found' in msg`: that would swallow unrelated ValueErrors
#: (`'Zone {z} not found in region'`-style messages from other code paths) into a
#: silent default. This matches the one sentence `common.py` actually raises.
_VANISHED = re.compile(r'no instance type\b.*\bfound')


def patch() -> None:
    """Let a vanished instance type answer with upstream's default, not a raise."""
    from sky.catalog import common as catalog_common
    from sky.catalog import shadeform_catalog

    original = anchors.require_attr(
        shadeform_catalog, '_is_not_found_error',
        'It gates every `_call_or_default` fallback in the Shadeform catalog, '
        'which is what keeps a cluster on a delisted instance type from taking '
        'down `sky status` for the whole deployment.')
    anchors.require_params(original, 'err')
    anchors.require_source_contains(
        original, _DEFECT,
        'The predicate no longer tests for that substring, so either upstream '
        'fixed the mismatch or rewrote the guard. Re-read it before shipping '
        'this patch again, and delete it if the case is now handled.')
    anchors.require_source_contains(
        anchors.require_attr(
            catalog_common, 'get_vcpus_mem_from_instance_type_impl',
            'It raises the message the predicate has to recognise.'),
        _MESSAGE,
        'The "No instance type ... found." wording changed, so the regex here is '
        'matching against a message that no longer exists. Re-derive it from the '
        'new raise site.')

    if getattr(original, '_marketplace_fixes_vanished', False):
        return                                  # already applied; idempotent

    @functools.wraps(original)
    def _is_not_found_error(err) -> bool:
        # Upstream first: if it ever learns to recognise this, we add nothing.
        if original(err):
            return True
        if _VANISHED.search(str(err).lower()) is None:
            return False
        logger.debug(
            'marketplace-fixes: treating %r as a delisted instance type; '
            'falling back to the catalog default rather than raising', str(err))
        return True

    _is_not_found_error._marketplace_fixes_vanished = True
    shadeform_catalog._is_not_found_error = _is_not_found_error
