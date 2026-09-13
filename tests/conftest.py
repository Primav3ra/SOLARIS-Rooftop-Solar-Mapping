"""
Shared test configuration.

Two suites, deliberately separated:

``tests/unit``        offline. No Earth Engine credentials, no network. A fresh
                      clone with no Google Cloud account must get a green run.
``tests/integration`` marked ``gee``; skipped unless credentials resolve.

The default ``addopts`` in pyproject.toml deselects ``gee`` and ``network``, so
plain ``pytest`` is always the offline suite.
"""

from __future__ import annotations

import os
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = pathlib.Path(__file__).resolve().parent / "data"


def _gee_credentials_available() -> bool:
    """True if some Earth Engine credential source looks present."""
    if os.environ.get("GEE_SA_JSON") or os.environ.get("GEE_SA_KEY_FILE"):
        return True
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return True
    # local `earthengine authenticate` credentials
    return (pathlib.Path.home() / ".config" / "earthengine" / "credentials").exists()


def pytest_collection_modifyitems(config, items):
    """Skip gee-marked tests when no credentials are available."""
    if _gee_credentials_available():
        return
    skip = pytest.mark.skip(
        reason="no Earth Engine credentials (set GEE_SA_JSON or run `earthengine authenticate`)"
    )
    for item in items:
        if "gee" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def spa_reference():
    """
    NREL SPA solar positions, generated once by pvlib and committed.

    Committing the table rather than computing it at test time means the
    offline suite has no pvlib runtime dependency, and the reference cannot
    silently shift under a pvlib upgrade.
    """
    import csv

    path = DATA_DIR / "spa_reference.csv"
    if not path.exists():
        pytest.skip(f"missing reference table: {path}")
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["lat"] = float(r["lat"])
        r["lon"] = float(r["lon"])
        r["elevation_deg"] = float(r["elevation_deg"])
        r["azimuth_deg"] = float(r["azimuth_deg"])
    return rows
