"""Static checks that the upstream shapes these patches rely on are still there.

Two rules make monkeypatching a long-running server survivable:

  * Check the anchor FIRST, mutate second. A patch that discovers drift halfway
    through leaves the process in a state neither the patched nor the unpatched
    path expects, which is worse than not starting.
  * Fail loudly, never degrade to a warning. A server that boots "healthy",
    reports the plugin as loaded, and then plans the old way is far harder to
    diagnose than one that refuses to boot with the reason on stderr.

Every check here is STATIC — a signature, an attribute, a substring of source.
Nothing is executed. An earlier draft proposed a behavioural anchor ("call the
function and see whether upstream already returns more than one instance type"),
which cannot work: `load_plugins(MAIN)` runs before `initialize_and_get_db()`,
and the function under test reads the cluster-state DB on its first line. The
runtime equivalent lives in `launchable_offers` as a warning, not a boot gate.
"""
from __future__ import annotations

import inspect
from typing import Any


class PatchDriftError(RuntimeError):
    """An upstream anchor no longer matches. Refuse to run rather than guess."""


def require_attr(obj: Any, name: str, why: str) -> Any:
    """Return `obj.name`, or raise with what it was needed for."""
    value = getattr(obj, name, None)
    if value is None:
        raise PatchDriftError(
            f'{_label(obj)}.{name} no longer exists. {why} Refusing to guess '
            'where it moved to.')
    return value


def require_params(func: Any, *names: str) -> None:
    """Assert `func` accepts these parameter NAMES.

    Names, not arity: SkyPilot calls `_fill_in_launchable_resources` with
    `blocked_resources=` as a keyword from two call sites, so a wrapper that
    renames the parameter type-errors at request time — a clean boot followed by
    every launch failing, which is the failure mode this package exists to avoid.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError) as e:  # pragma: no cover - builtins only
        raise PatchDriftError(f'cannot inspect {func!r}: {e}') from e
    missing = [n for n in names if n not in params]
    if missing:
        raise PatchDriftError(
            f'{getattr(func, "__qualname__", func)} no longer takes '
            f'{", ".join(missing)} (it takes: {", ".join(params)}). The wrapper '
            'passes these through by keyword and would break at request time.')


def require_source_contains(func: Any, needle: str, remedy: str) -> None:
    """Assert the defect this patch works around is still present in `func`.

    This is the check that matters most. When upstream fixes the bug the
    substring disappears, and the right response is to DELETE the patch rather
    than carry one whose premise is gone — so refusing to boot with a message
    saying so is the intended outcome, not an accident.
    """
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError) as e:  # pragma: no cover - source ships with sky
        raise PatchDriftError(
            f'cannot read the source of {getattr(func, "__qualname__", func)}: '
            f'{e}. The drift anchor cannot be verified.') from e
    if needle not in source:
        raise PatchDriftError(
            f'{getattr(func, "__qualname__", func)} no longer contains '
            f'{needle!r}. {remedy}')


def _label(obj: Any) -> str:
    return getattr(obj, '__name__', None) or type(obj).__name__
