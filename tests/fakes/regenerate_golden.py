"""
Regenerate tests/data/api_golden.json from the synthetic world.

Run as ``python -m tests.fakes.regenerate_golden``. Always commit the
regenerated file on its own, so the numeric diff is reviewable in isolation
from whatever change caused it.
"""

from __future__ import annotations

import json
import pathlib
import sys
import warnings


def main() -> int:
    warnings.filterwarnings("ignore")
    from tests.fakes import fake_ee as fake
    from tests.fakes import world

    sys.modules["ee"] = fake
    world.register_world()

    from fastapi.testclient import TestClient
    from tests.unit.test_api_golden import (
        GOLDEN_PATH,
        _strip_volatile,
    )

    import solaris.api.app as app_mod

    app_mod._EE_INIT_PROJECT = None
    client = TestClient(app_mod.app)

    golden: dict[str, object] = {}
    for endpoint in ["/api/baseline", "/api/yield", "/api/series", "/api/buildings"]:
        world.register_world()
        response = client.post(endpoint, json=dict(world.AOI_REQUEST))
        if response.status_code != 200:
            print(f"[FAIL] {endpoint} -> {response.status_code}: {response.text[:200]}")
            return 1
        golden[endpoint] = _strip_volatile(response.json())
        print(f"[ok] {endpoint}")

    path = pathlib.Path(GOLDEN_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwrote {path} ({path.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
