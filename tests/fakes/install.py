"""
Installing the fake Earth Engine into the modules under test.

An earlier version of this assigned ``sys.modules["ee"] = fake_ee`` before
importing the app. That is fragile and was silently wrong: every ``solaris``
module does ``import ee`` at module scope, so once any of them has been
imported -- by an earlier test file, say -- its ``ee`` attribute is already
bound to the real client library and replacing ``sys.modules`` afterwards has no
effect. The golden tests then reached the real Earth Engine and failed with a
permissions error only when run *after* another module, which is the worst kind
of test bug.

So patch the bound attribute on each module instead. That is explicit,
order-independent, and undone automatically by ``monkeypatch``.

Why the consumer list is discovered, not written down
-----------------------------------------------------
It used to be a hand-maintained tuple, and the "is everything patched?" test
checked that tuple against itself -- so it passed no matter what. Adding
``solaris.gee.precipitation`` proved the point: the new module's ``ee`` was
never patched, the guard reported success, and eleven tests failed with a live
Earth Engine authentication error instead.

The list is now obtained by walking the ``solaris`` package and asking each
module whether it has an ``ee`` attribute. A new Earth Engine consumer is
therefore covered the moment it exists, and cannot be forgotten.
"""

from __future__ import annotations

import importlib
import pkgutil

from tests.fakes import fake_ee


def discover_ee_consumers() -> tuple[str, ...]:
    """
    Every importable ``solaris`` module that binds ``ee`` at module scope.

    Import failures are skipped rather than raised: an optional-dependency
    module that cannot import is not an Earth Engine consumer in this process
    and is not this function's problem.
    """
    import solaris

    found: list[str] = []
    for info in pkgutil.walk_packages(solaris.__path__, prefix="solaris."):
        try:
            module = importlib.import_module(info.name)
        except Exception:
            continue
        if hasattr(module, "ee"):
            found.append(info.name)
    return tuple(sorted(found))


def install_fake_ee(monkeypatch) -> None:
    """
    Point every ``solaris`` module's ``ee`` at the fake, and reset the Earth
    Engine init memo so the session is re-established against it.
    """
    for name in discover_ee_consumers():
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "ee", fake_ee, raising=True)

    deps = importlib.import_module("solaris.api.deps")
    monkeypatch.setattr(deps, "_EE_INIT_PROJECT", None, raising=False)
    monkeypatch.setattr(deps, "ensure_ee", lambda *_a, **_kw: None)


def assert_all_consumers_patched() -> list[str]:
    """
    Return any module whose ``ee`` is *not* the fake.

    Discovery-based, so this now genuinely catches a new ``import ee`` module
    rather than confirming a list against itself.
    """
    return [
        name
        for name in discover_ee_consumers()
        if getattr(importlib.import_module(name), "ee", fake_ee) is not fake_ee
    ]
