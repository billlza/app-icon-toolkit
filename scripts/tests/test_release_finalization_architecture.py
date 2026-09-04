"""AST contracts for the release-finalization dependency graph."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
MODULE_FILES = {
    "release_finalization_core": SCRIPTS_ROOT / "release_finalization_core.py",
    "release_finalization_source": SCRIPTS_ROOT / "release_finalization_source.py",
    "release_candidate_preparation": SCRIPTS_ROOT
    / "release_candidate_preparation.py",
    "release_notarization_transaction": SCRIPTS_ROOT
    / "release_notarization_transaction.py",
    "release_draft_staging": SCRIPTS_ROOT / "release_draft_staging.py",
    "release_publication_preflight": SCRIPTS_ROOT
    / "release_publication_preflight.py",
    "release_github_publication": SCRIPTS_ROOT / "release_github_publication.py",
    "release_public_acceptance_transaction": SCRIPTS_ROOT
    / "release_public_acceptance_transaction.py",
    "finalize_macos_release": SCRIPTS_ROOT / "finalize_macos_release.py",
}
COMPLETE_RELEASE_MODULE_FILES = {
    path.stem: path for path in sorted(SCRIPTS_ROOT.glob("release_*.py"))
} | {
    "finalize_macos_release": SCRIPTS_ROOT / "finalize_macos_release.py",
    "macos_signing": SCRIPTS_ROOT / "macos_signing.py",
}
ALLOWED_FINALIZATION_DEPENDENCIES = {
    "release_finalization_core": frozenset(),
    "release_finalization_source": frozenset({"release_finalization_core"}),
    "release_candidate_preparation": frozenset({"release_finalization_core"}),
    "release_notarization_transaction": frozenset(
        {"release_finalization_core"}
    ),
    "release_draft_staging": frozenset({"release_finalization_core"}),
    "release_publication_preflight": frozenset(
        {
            "release_finalization_core",
            "release_finalization_source",
            "release_draft_staging",
        }
    ),
    "release_github_publication": frozenset(
        {
            "release_finalization_core",
            "release_draft_staging",
            "release_publication_preflight",
        }
    ),
    "release_public_acceptance_transaction": frozenset(
        {"release_finalization_core"}
    ),
    "finalize_macos_release": frozenset(
        {
            "release_finalization_core",
            "release_finalization_source",
            "release_candidate_preparation",
            "release_notarization_transaction",
            "release_draft_staging",
            "release_github_publication",
            "release_public_acceptance_transaction",
        }
    ),
}


def _parse(module_name: str) -> ast.Module:
    path = MODULE_FILES[module_name]
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_dependencies(
    tree: ast.Module, module_files: dict[str, Path]
) -> frozenset[str]:
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            dependencies.update(
                alias.name.partition(".")[0]
                for alias in node.names
                if alias.name.partition(".")[0] in module_files
            )
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            dependency = node.module.partition(".")[0]
            if dependency in module_files:
                dependencies.add(dependency)
    return frozenset(dependencies)


def _dependency_cycle(
    graph: dict[str, frozenset[str]],
) -> tuple[str, ...] | None:
    visited: set[str] = set()
    active: list[str] = []

    def visit(module_name: str) -> tuple[str, ...] | None:
        if module_name in active:
            cycle_start = active.index(module_name)
            return tuple(active[cycle_start:] + [module_name])
        if module_name in visited:
            return None
        active.append(module_name)
        for dependency in sorted(graph[module_name]):
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        active.pop()
        visited.add(module_name)
        return None

    for module_name in graph:
        cycle = visit(module_name)
        if cycle is not None:
            return cycle
    return None


class ReleaseFinalizationArchitectureTests(unittest.TestCase):
    def test_only_allowed_finalization_dependencies_are_imported(self) -> None:
        observed = {
            module_name: _module_dependencies(_parse(module_name), MODULE_FILES)
            for module_name in MODULE_FILES
        }
        self.assertEqual(observed, ALLOWED_FINALIZATION_DEPENDENCIES)

    def test_finalization_dependency_graph_is_acyclic(self) -> None:
        graph = {
            module_name: _module_dependencies(_parse(module_name), MODULE_FILES)
            for module_name in MODULE_FILES
        }
        cycle = _dependency_cycle(graph)
        self.assertIsNone(
            cycle,
            "finalization dependency cycle: "
            + (" -> ".join(cycle) if cycle is not None else "none"),
        )

    def test_complete_release_dependency_graph_is_acyclic(self) -> None:
        graph = {
            module_name: _module_dependencies(
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
                COMPLETE_RELEASE_MODULE_FILES,
            )
            for module_name, path in COMPLETE_RELEASE_MODULE_FILES.items()
        }
        cycle = _dependency_cycle(graph)
        self.assertIsNone(
            cycle,
            "complete release dependency cycle: "
            + (" -> ".join(cycle) if cycle is not None else "none"),
        )

    def test_imported_finalization_bindings_are_never_shadowed(self) -> None:
        violations: list[str] = []
        for module_name in MODULE_FILES:
            tree = _parse(module_name)
            protected_bindings: set[str] = set()
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.partition(".")[0] in MODULE_FILES:
                            protected_bindings.add(
                                alias.asname or alias.name.partition(".")[0]
                            )
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.module in MODULE_FILES
                ):
                    protected_bindings.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name != "*"
                    )

            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Store)
                    and node.id in protected_bindings
                ):
                    violations.append(f"{module_name}:{node.lineno}:{node.id}")
                elif isinstance(node, ast.arg) and node.arg in protected_bindings:
                    violations.append(f"{module_name}:{node.lineno}:{node.arg}")

        self.assertEqual(
            violations,
            [],
            "stage-local names must not make imported release primitives unusable",
        )

    def test_entrypoint_contains_only_cli_and_orchestration_functions(self) -> None:
        tree = _parse("finalize_macos_release")
        functions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
        self.assertEqual(
            functions,
            {"finalize", "_hosted_validation_input", "build_options", "main"},
        )
        self.assertEqual(classes, [])

        finalization_from_imports = [
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module in MODULE_FILES
        ]
        self.assertEqual(
            finalization_from_imports,
            [],
            "entrypoint must not recreate a compatibility re-export facade",
        )

        entrypoint_lines = MODULE_FILES["finalize_macos_release"].read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertLessEqual(
            len(entrypoint_lines),
            350,
            "the orchestration entrypoint must stay intentionally small",
        )
        entrypoint_test_path = Path(__file__).with_name(
            "test_finalize_macos_release.py"
        )
        entrypoint_test_lines = entrypoint_test_path.read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertLessEqual(
            len(entrypoint_test_lines),
            400,
            "entrypoint tests must not absorb stage-domain behavior",
        )


if __name__ == "__main__":
    unittest.main()
