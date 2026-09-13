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
"""

from __future__ import annotations

import importlib

from tests.fakes import fake_ee

#: Every module that does ``import ee`` at module scope.
EE_CONSUMERS = (
    "solaris.api.app",
    "solaris.api.deps",
    "solaris.gee.datasets",
    "solaris.gee.irradiance",
    "solaris.gee.layers",
    "solaris.gee.penalties",
    "solaris.gee.rooftops",
)


def install_fake_ee(monkeypatch) -> None:
    """
    Point every ``solaris`` module's ``ee`` at the fake, and reset the Earth
    Engine init memo so the session is re-established against it.
    """
    for name in EE_CONSUMERS:
        module = importlib.import_module(name)
        if hasattr(module, "ee"):
            monkeypatch.setattr(module, "ee", fake_ee, raising=True)

    deps = importlib.import_module("solaris.api.deps")
    monkeypatch.setattr(deps, "_EE_INIT_PROJECT", None, raising=False)
    monkeypatch.setattr(deps, "ensure_ee", lambda *_a, **_kw: None)


def assert_all_consumers_patched() -> list[str]:
    """
    Return any module whose ``ee`` is *not* the fake.

    Used by a test so that adding a new ``import ee`` module without adding it
    to EE_CONSUMERS is caught, rather than quietly leaking to the network.
    """
    leaked = []
    for name in EE_CONSUMERS:
        module = importlib.import_module(name)
        if getattr(module, "ee", fake_ee) is not fake_ee:
            leaked.append(name)
    return leaked
