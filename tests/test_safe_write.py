from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recallweave.safe_write import install, prepare_destination, verify_destination

LABEL = "Contract output"


class SafeWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        )
        self.root = Path(self.temporary.name)
        self.protected = self.root / "index.sqlite"
        self.protected.write_bytes(b"sqlite-index")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_symlink_target_refused(self) -> None:
        target = self.root / "elsewhere" / "contract.json"
        output = self.root / "contract.json"
        try:
            output.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")

        with self.assertRaisesRegex(ValueError, "symlink or junction"):
            prepare_destination(output, self.protected, force=True, label=LABEL)
        self.assertFalse(target.exists())
        self.assertTrue(output.is_symlink())

    def test_symlinked_parent_refused(self) -> None:
        real_parent = self.root / "real"
        real_parent.mkdir()
        linked_parent = self.root / "linked"
        try:
            linked_parent.symlink_to(real_parent, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlink creation unavailable: {error}")

        with self.assertRaisesRegex(ValueError, "symlinked parent"):
            prepare_destination(
                linked_parent / "contract.json", self.protected, force=True, label=LABEL
            )
        self.assertFalse((real_parent / "contract.json").exists())

    def test_protected_file_target_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot replace"):
            prepare_destination(self.protected, self.protected, force=True, label=LABEL)

    def test_existing_target_without_force_refused(self) -> None:
        output = self.root / "contract.json"
        output.write_text("existing", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "already exists"):
            prepare_destination(output, self.protected, force=False, label=LABEL)

    def test_non_replacing_install(self) -> None:
        output = self.root / "contract.json"
        guard = prepare_destination(output, self.protected, force=False, label=LABEL)
        verify_destination(output, self.protected, guard, label=LABEL)
        temporary = self.root / "contract.json.tmp"
        temporary.write_text("new content", encoding="utf-8")

        result = install(temporary, output, guard, label=LABEL)

        self.assertIsNone(result)
        self.assertTrue(output.exists())
        self.assertEqual(output.read_text(encoding="utf-8"), "new content")
        self.assertFalse(temporary.exists())

    def test_forced_two_phase_replacement_retains_backup(self) -> None:
        output = self.root / "contract.json"
        output.write_text("approved old output", encoding="utf-8")
        guard = prepare_destination(output, self.protected, force=True, label=LABEL)
        verify_destination(output, self.protected, guard, label=LABEL)
        temporary = self.root / "contract.json.tmp"
        temporary.write_text("new content", encoding="utf-8")

        backup_path = install(temporary, output, guard, label=LABEL)

        self.assertIsNotNone(backup_path)
        backup = Path(backup_path)
        self.assertIn(".backup.", backup.parent.name)
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), "approved old output")
        self.assertEqual(output.read_text(encoding="utf-8"), "new content")
        self.assertFalse(temporary.exists())

    def test_rollback_when_install_fails(self) -> None:
        output = self.root / "contract.json"
        output.write_text("approved old output", encoding="utf-8")
        guard = prepare_destination(output, self.protected, force=True, label=LABEL)
        verify_destination(output, self.protected, guard, label=LABEL)
        temporary = self.root / "contract.json.tmp"
        temporary.write_text("new content", encoding="utf-8")
        original_install = __import__(
            "recallweave.viewer", fromlist=["_install_non_replacing"]
        )._install_non_replacing
        failed = False

        def fail_new_install(source: Path, destination: Path) -> None:
            nonlocal failed
            if not failed and source.suffix == ".tmp":
                failed = True
                raise OSError("injected installation failure")
            original_install(source, destination)

        with patch(
            "recallweave.viewer._install_non_replacing",
            side_effect=fail_new_install,
        ):
            with self.assertRaisesRegex(ValueError, "previous output was restored"):
                install(temporary, output, guard, label=LABEL)
        self.assertEqual(output.read_text(encoding="utf-8"), "approved old output")


if __name__ == "__main__":
    unittest.main()
