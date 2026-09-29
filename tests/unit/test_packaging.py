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
    # Declared in the ml extra, imported lazily by solaris.ml.
    "sklearn",
    "joblib",
    # Declared in the physics extra, imported lazily.
    "pvlib",
    "numpy",
    "pandas",
    # Declared in the firestore extra, imported lazily with a degradation path.
    "google",
}


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

    def test_exempt_packages_are_declared_in_some_extra(self):
        """
        Exempt from the *runtime* requirement, not from being declared at all.
        An optional import still needs an extra that installs it.
        """
        declared = _declared_distributions()
        for root in ("sklearn", "joblib", "pvlib", "numpy", "pandas"):
            distribution = DISTRIBUTION_OF.get(root, root)
            assert distribution.lower() in declared, root


class TestPythonFloor:
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
