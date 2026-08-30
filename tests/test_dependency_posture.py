from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class DependencyPostureTest(unittest.TestCase):
    def test_pyproject_declares_mistletoe_test_extra(self) -> None:
        text = _text("pyproject.toml")
        self.assertIn("[project.optional-dependencies]", text)
        self.assertIn("test", text)
        self.assertIn("mistletoe>=1.6", text)

    def test_pyproject_runtime_dependencies_still_empty(self) -> None:
        text = _text("pyproject.toml")
        self.assertIn("dependencies = []", text)

    def test_ci_python_job_installs_test_extra(self) -> None:
        text = _text(".github/workflows/tests.yml")
        self.assertIn('pip install -e ".[test]"', text)

    def test_ci_matrix_unchanged_otherwise(self) -> None:
        text = _text(".github/workflows/tests.yml")
        self.assertIn('python-version: ["3.11", "3.12", "3.13"]', text)
        self.assertIn("python -m unittest discover -s tests -v", text)

    def test_security_asserts_zero_runtime_deps_and_describes_test_dependency(self) -> None:
        text = _text("SECURITY.md")
        self.assertIn("no runtime dependencies", text)
        self.assertIn("test", text)
        self.assertIn("mistletoe", text)

    def test_readme_core_stdlib_and_test_parser(self) -> None:
        text = _text("README.md")
        self.assertIn("standard library", text)
        self.assertIn("CommonMark", text)
        self.assertIn("mistletoe", text)


class StewardModulePostureTest(unittest.TestCase):
    """Steward stays local and one-shot: no networking, no scheduler machinery.

    These are boundary tripwires, not style rules — a steward module that
    imports a networking or server module is crossing the product boundary,
    however it is used."""

    _FORBIDDEN_IMPORTS = (
        "urllib.request",
        "http.client",
        "http.server",
        "socket",
        "socketserver",
        "ssl",
        "sched",
        "asyncio",
        "requests",
        "httpx",
    )

    def _steward_modules(self) -> list[Path]:
        return sorted((ROOT / "src" / "recallweave").glob("steward_*.py"))

    def test_steward_modules_exist(self) -> None:
        self.assertTrue(self._steward_modules())

    def test_steward_modules_import_no_network_or_scheduler(self) -> None:
        offenders = []
        for module in self._steward_modules():
            text = module.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if not (
                    stripped.startswith("import ") or stripped.startswith("from ")
                ):
                    continue
                for name in self._FORBIDDEN_IMPORTS:
                    bare = name.split(".")[0]
                    if (
                        stripped.startswith(f"import {name}")
                        or stripped.startswith(f"from {name} ")
                        or stripped.startswith(f"import {bare}\n")
                        or stripped == f"import {bare}"
                        or stripped.startswith(f"from {bare} import")
                    ):
                        offenders.append(f"{module.name}: {stripped}")
        self.assertEqual(
            offenders, [],
            f"steward modules must not import networking/scheduler modules: {offenders}",
        )

    def test_apply_module_is_import_isolated(self) -> None:
        """The engine's no-write property stays provable from the static
        import graph: no module imports steward_apply at module level, and
        cli.py references it only inside a function body."""

        import ast

        offenders = []
        for module in sorted((ROOT / "src" / "recallweave").glob("*.py")):
            if module.name == "steward_apply.py":
                continue
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] + [
                        alias.name for alias in node.names
                    ]
                if not any("steward_apply" in name for name in names):
                    continue
                if node.col_offset == 0:
                    offenders.append(f"{module.name}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            "steward_apply may only be imported inside a function body: "
            f"{offenders}",
        )

    def test_steward_modules_do_not_shell_out_except_git_wrapper(self) -> None:
        # subprocess is reserved for the (future) git wrapper module only.
        offenders = []
        for module in self._steward_modules():
            if module.name == "steward_git.py":
                continue
            text = module.read_text(encoding="utf-8")
            if "subprocess" in text:
                offenders.append(module.name)
        self.assertEqual(
            offenders, [],
            f"only the git wrapper may use subprocess: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
