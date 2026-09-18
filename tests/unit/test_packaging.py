"""
Packaging invariants: declared dependencies, and the supported Python floor.

Both checks exist because both failed silently.

Every third-party module imported by ``src/solaris`` must be declared in
``pyproject.toml``. Two were not -- ``pydantic-settings``, which
``core/config.py`` imports, and ``cachetools``, which ``core/cache.py``
imports. Both happened to be present locally, ``cachetools`` because an older
``google-auth`` pulled it in transitively. When ``google-auth`` dropped it,
every clean install lost the in-process cache backend and ``/api/yield``
answered 500 on its first cache lookup. A working developer machine says
nothing about a working install.

The Python floor is ``>=3.11``, but mypy targets 3.12 because numpy's stubs
cannot be parsed at 3.11. The floor is therefore asserted here instead: every
source file must parse against the 3.11 grammar.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "solaris"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: Modules imported under ``src/solaris`` that are deliberately not runtime
#: dependencies, each with the reason.
EXEMPT = {
    # The test fake and MonkeyPatch, imported inside a function in
    # evals/profile_yield.py, which is a development tool and raises a clear
    # message when the dev extra is absent.
    "pytest",
    # Declared in the ml extra, imported lazily by solaris.ml.
    "sklearn",
    "joblib",
    # Declared in the physics extra, imported lazily.
    "pvlib",
    "numpy",
    "pandas",
    # Declared in the firestore extra, imported lazily with a degradation path.
    "google",
    # The test package itself. A shipped module importing the test tree is a
    # layering violation, and exactly one module does it -- see
    # test_only_the_profiler_imports_the_test_package, which keeps it from
    # spreading. profile_yield counts round-trips against the numpy fake, so
    # the fake is its subject rather than an incidental dependency.
    "tests",
}

#: The single module permitted to import the test package.
PROFILER = "evals/profile_yield.py"


def _manifest() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _declared_distributions() -> set[str]:
    """Every distribution named in the project's dependencies or extras."""
    project = _manifest()["project"]
    specs = list(project.get("dependencies", []))
    for extra in (project.get("optional-dependencies") or {}).values():
        specs.extend(extra)

    names = set()
    for spec in specs:
        # "pydantic-settings>=2.3,<3" -> "pydantic-settings"; skip self-refs
        # like "solaris[physics]".
        head = spec.split(";")[0].strip()
        for sep in (">=", "<=", "==", "!=", "~=", ">", "<", "["):
            head = head.split(sep)[0]
        head = head.strip()
        if head and head != "solaris":
            names.add(head.lower().replace("_", "-"))
    return names


def _imported_roots() -> dict[str, set[str]]:
    """Top-level third-party module names imported under src/solaris."""
    stdlib = set(sys.stdlib_module_names)
    roots: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module and node.level == 0 else []
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root and root not in stdlib and root != "solaris":
                    roots.setdefault(root, set()).add(str(path.relative_to(SRC)))
    return roots


#: Import name to distribution name, where they differ.
DISTRIBUTION_OF = {
    "ee": "earthengine-api",
    "pydantic_settings": "pydantic-settings",
    "sklearn": "scikit-learn",
    "starlette": "starlette",
}


class TestDeclaredDependencies:
    def test_every_directly_imported_package_is_declared(self):
        declared = _declared_distributions()
        missing = {}
        for root, files in _imported_roots().items():
            if root in EXEMPT:
                continue
            distribution = DISTRIBUTION_OF.get(root, root).lower().replace("_", "-")
            if distribution not in declared:
                missing[root] = sorted(files)
        assert missing == {}, (
            f"imported but not declared in pyproject.toml: {missing}. A package "
            f"that happens to be installed locally is not a dependency."
        )

    def test_only_the_profiler_imports_the_test_package(self):
        """
        A shipped module importing ``tests`` inverts the dependency direction:
        installing the package would not install what it imports.

        One module does it, deliberately and lazily --
        ``evals/profile_yield.py`` counts Earth Engine round-trips against the
        numpy fake, which is the measurement itself rather than a convenience.
        It raises a clear message when the dev extra is absent. This test
        exists so the exception stays a single, named case.
        """
        offenders = _imported_roots().get("tests", set())
        assert offenders == {PROFILER.replace("/", "\\")} or offenders == {PROFILER}, (
            f"modules importing the test package: {sorted(offenders)}. Only "
            f"{PROFILER} may, and only inside a function."
        )

    def test_the_profiler_imports_the_fake_lazily(self):
        """
        Module-level would make ``import solaris.evals.profile_yield`` fail on
        an install without the dev extra, which would break the CLI entry point
        and anything that merely enumerates the package.
        """
        source = (SRC / "evals" / "profile_yield.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tests"):
                raise AssertionError("tests.* is imported at module scope")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("tests"), "tests.* at module scope"

    def test_the_two_that_were_missing_are_declared(self):
        """
        Named explicitly, so a refactor that drops them fails here rather than
        on somebody's clean install.
        """
        declared = _declared_distributions()
        assert "pydantic-settings" in declared
        assert "cachetools" in declared

    def test_exempt_packages_are_declared_in_some_extra(self):
        """
        Exempt from the *runtime* requirement, not from being declared at all.
        An optional import still needs an extra that installs it.
        """
        declared = _declared_distributions()
        for root in ("sklearn", "joblib", "pvlib", "numpy", "pandas"):
            distribution = DISTRIBUTION_OF.get(root, root)
            assert distribution.lower() in declared, root

    def test_the_linters_are_upper_bounded(self):
        """
        ruff's formatter output is version-sensitive: 0.12 and 0.16 disagree on
        lambda wrapping, so an open bound made `ruff format --check` fail in CI
        on code the developer had just formatted.
        """
        dev = _manifest()["project"]["optional-dependencies"]["dev"]
        for spec in dev:
            if spec.startswith(("ruff", "mypy")):
                assert "<" in spec, f"{spec} needs an upper bound"


class TestPythonFloor:
    def test_requires_python_is_what_we_test(self):
        assert _manifest()["project"]["requires-python"] == ">=3.11"

    def test_every_source_file_parses_against_the_oldest_supported_grammar(self):
        """
        mypy targets 3.12 because numpy's stubs cannot be parsed at 3.11, so the
        3.11 floor is asserted here instead.
        """
        failures = []
        for path in sorted(SRC.rglob("*.py")):
            try:
                ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 11))
            except SyntaxError as exc:
                failures.append(f"{path.relative_to(SRC)}: {exc}")
        assert failures == [], failures

    def test_the_test_suite_also_parses_at_the_floor(self):
        failures = []
        for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
            try:
                ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 11))
            except SyntaxError as exc:
                failures.append(f"{path}: {exc}")
        assert failures == [], failures
