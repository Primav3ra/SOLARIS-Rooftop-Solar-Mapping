"""
The anti-drift guarantee, asserted rather than trusted.

The Validation and Models pages import the evaluation reports as JSON at build
time, so there are no hand-copied numbers in the frontend. That property is the
site's main credibility claim, and it has one real failure mode: someone
regenerates a report and does not rebuild the site, leaving the deployed pages
showing figures the repository no longer produces.

Nothing about the build prevents that. These tests do: they read the committed
artifacts and assert the built bundle contains those exact values. A stale
build fails here rather than being discovered by a reader comparing the site
against the repository.

Also asserted: that the content routes ship no WebGL. Route-splitting makes
that true; only a check keeps it true, because a stray import in a shared
component would silently pull three.js into the main bundle and nothing about
the rendered site would look wrong.

All of these skip when there is no build output, because a fresh clone with no
``npm install`` must still get a green run.
"""

from __future__ import annotations

import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "src" / "solaris" / "api" / "static"
ASSETS_DIR = STATIC_DIR / "assets"
SITE_SRC = REPO_ROOT / "frontend" / "site" / "src"


def _built_javascript() -> dict[str, str]:
    """Every built JS chunk, by filename."""
    if not ASSETS_DIR.is_dir():
        pytest.skip(
            "no built frontend; run `npm --prefix frontend/site run build` to include these checks"
        )
    chunks = {
        p.name: p.read_text(encoding="utf-8", errors="replace") for p in ASSETS_DIR.glob("*.js")
    }
    if not chunks:
        pytest.skip("no JS chunks in the build output")
    return chunks


def _all_javascript() -> str:
    return "\n".join(_built_javascript().values())


def _report(relative: str) -> dict:
    path = REPO_ROOT / relative
    if not path.exists():
        pytest.skip(f"missing artifact: {relative}")
    return json.loads(path.read_text(encoding="utf-8"))


def _contains_number(bundle: str, value) -> bool:
    """
    Whether a numeric literal appears in minified output.

    Minifiers drop the leading zero from a decimal below one, so ``0.08927``
    ships as ``.08927``. Searching for the Python repr alone misses it and
    produces a confident, wrong failure -- which is worse than no check, since
    it would train a reader to ignore this test.
    """
    text = str(value)
    if text in bundle:
        return True
    if text.startswith("0."):
        return text[1:] in bundle
    if text.startswith("-0."):
        return f"-{text[2:]}" in bundle
    return False


class TestReportsAreEmbedded:
    """
    Values from the committed artifacts must appear in the built bundle.

    Chosen to be figures a reader would actually quote, so a failure means the
    site is showing a stale version of exactly the numbers that matter.
    """

    def test_specific_yield_summary_is_current(self):
        report = _report("evals/reports/latest.json")
        summary = report["suites"]["specific_yield_vs_published"]["summary"]
        bundle = _all_javascript()
        assert _contains_number(bundle, summary["mean"]), (
            "The validation report's mean specific yield is not in the built bundle. "
            "Rebuild the site: `npm --prefix frontend/site run build`."
        )

    def test_reference_spread_is_current(self):
        report = _report("evals/reports/latest.json")
        rows = report["suites"]["irradiance_reference_spread"]["rows"]
        bundle = _all_javascript()
        for row in rows:
            assert _contains_number(bundle, row["spread_pct_of_mean"]), row["city"]

    def test_model_skill_is_current(self):
        manifest = _report("ml/artifacts/decomposition.manifest.json")
        bundle = _all_javascript()
        assert _contains_number(bundle, manifest["skill_vs_erbs"])
        assert _contains_number(bundle, manifest["test_rmse"])
        assert manifest["kind"] in bundle

    def test_the_shipped_model_version_is_current(self):
        """
        Catches the most likely staleness: a retrain that bumped the version
        without a rebuild.
        """
        manifest = _report("ml/artifacts/decomposition.manifest.json")
        assert manifest["version"] in _all_javascript(), (
            f"The site does not reference model version {manifest['version']}. "
            "The model was retrained without rebuilding the frontend."
        )

    def test_soiling_findings_are_current(self):
        report = _report("evals/reports/soiling.json")
        bundle = _all_javascript()
        assert _contains_number(bundle, report["window_spread_x"])
        assert _contains_number(bundle, report["mean_spell_understatement"]["max_x"])

    def test_the_dropped_track_decision_is_embedded(self):
        """
        The Track B decision is a *result*, not commentary, so it is held to the
        same standard as the numbers that support it.
        """
        report = _report("evals/reports/track_b_profile.json")
        bundle = _all_javascript()
        ceiling = report["surrogate_ceiling"]
        assert _contains_number(bundle, ceiling["total_round_trips"])
        assert _contains_number(bundle, ceiling["best_possible_speedup_if_compute_bound"])


class TestBundleBudget:
    """
    Content routes must not ship WebGL.

    The intro's three.js chunk is about 800 kB. The Validation, Method, Data,
    Limitations and About pages have no use for it, and a reader who never opens
    the landing animation should never download it.
    """

    WEBGL_MARKERS = ("WebGLRenderer", "THREE.")

    def test_three_js_stays_in_its_own_chunk(self):
        offenders = []
        for name, source in _built_javascript().items():
            if "intro-webgl" in name:
                continue
            for marker in self.WEBGL_MARKERS:
                if marker in source:
                    offenders.append((name, marker))
        assert offenders == [], (
            f"three.js leaked out of the intro chunk: {offenders}. Check for a static "
            "import of the intro scene, or a three.js symbol used in a shared component."
        )

    def test_the_webgl_chunk_exists_and_is_separate(self):
        """
        The mirror of the test above. If the chunk vanished, the test above
        would pass trivially while the intro had been statically inlined
        somewhere -- so its existence is asserted too.
        """
        names = _built_javascript().keys()
        assert any("intro-webgl" in name for name in names), list(names)

    def test_the_entry_chunk_stays_small(self):
        """
        A generous ceiling. The point is to catch an order-of-magnitude
        regression -- an accidental map or three.js import -- not to police a
        few kilobytes.
        """
        entry = (
            [path for path in ASSETS_DIR.glob("index-*.js") if path.is_file()]
            if ASSETS_DIR.is_dir()
            else []
        )
        if not entry:
            pytest.skip("no entry chunk found")
        largest = max(path.stat().st_size for path in entry)
        assert largest < 320 * 1024, f"entry chunk is {largest / 1024:.0f} kB"


class TestDataLayerDiscipline:
    """
    Structural checks on the frontend source, not the build.

    These encode the rule rather than its consequence: artifacts are read in
    exactly one module, so there is one place to look when asking where a
    number came from.
    """

    def _sources(self) -> dict[str, str]:
        if not SITE_SRC.is_dir():
            pytest.skip("no frontend source")
        return {
            str(path.relative_to(SITE_SRC)): path.read_text(encoding="utf-8")
            for path in SITE_SRC.rglob("*.jsx")
        }

    def test_artifacts_are_imported_in_exactly_one_module(self):
        offenders = [
            name
            for name, source in self._sources().items()
            if "@reports/" in source or "@mlreports/" in source
        ]
        assert offenders == [], (
            f"These files import artifacts directly: {offenders}. Read them through "
            "data/reports.js instead, so there is one place that knows the schema."
        )

    def test_the_data_module_imports_the_artifacts_that_exist(self):
        module = SITE_SRC / "data" / "reports.js"
        if not module.exists():
            pytest.skip("no data module")
        source = module.read_text(encoding="utf-8")
        alias_roots = {
            "@reports/": REPO_ROOT / "evals" / "reports",
            "@mlreports/": REPO_ROOT / "ml" / "reports",
            "@artifacts/": REPO_ROOT / "ml" / "artifacts",
        }
        missing = []
        for line in source.splitlines():
            if not line.startswith("import "):
                continue
            for alias, root in alias_roots.items():
                if alias in line:
                    filename = line.split(alias)[1].split("'")[0].split('"')[0]
                    if not (root / filename).exists():
                        missing.append(f"{alias}{filename}")
        assert missing == [], f"imports reference artifacts that do not exist: {missing}"

    def test_no_page_hardcodes_the_headline_yield_figure(self):
        """
        A narrow guard against the exact regression this design prevents:
        someone typing the mean specific yield into the page instead of reading
        it. Only the data module and the tests may contain it.
        """
        report = _report("evals/reports/latest.json")
        mean = str(report["suites"]["specific_yield_vs_published"]["summary"]["mean"])
        offenders = [name for name, source in self._sources().items() if mean in source]
        assert offenders == [], f"hardcoded {mean} in {offenders}"
