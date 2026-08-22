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


if __name__ == "__main__":
    unittest.main()
