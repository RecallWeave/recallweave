from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any


def is_link_like(path: Path) -> bool:
    """Return True for symlinks and Windows junction-style reparse points."""

    try:
        info = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse_tag = getattr(info, "st_reparse_tag", 0)
    link_tags = {
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", -1),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -2),
    }
    return reparse_tag in link_tags


def path_identity(path: Path) -> tuple[int, int]:
    info = path.stat(follow_symlinks=False)
    return int(info.st_dev), int(info.st_ino)


def _validate_output_path(
    output: Path,
    protected: Path,
    label: str,
    *,
    protected_target_message: str | None = None,
) -> None:
    if is_link_like(output):
        raise ValueError(f"Refusing to replace a symlink or junction: {output}")

    current = Path(output.anchor)
    for part in output.parent.parts[1:]:
        current /= part
        if is_link_like(current):
            raise ValueError(
                f"Refusing {label.lower()} through a symlinked parent: {current}"
            )

    if output.exists() and os.path.samefile(output, protected):
        if protected_target_message is not None:
            raise ValueError(protected_target_message)
        raise ValueError(f"{label} cannot replace the protected file: {output}")


def prepare_destination(
    output: Path,
    protected: Path,
    *,
    force: bool,
    label: str,
    protected_target_message: str | None = None,
) -> dict[str, Any]:
    _validate_output_path(
        output, protected, label, protected_target_message=protected_target_message
    )
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as error:
        raise ValueError(
            f"{label} parent is not a directory: {output.parent}"
        ) from error
    _validate_output_path(
        output, protected, label, protected_target_message=protected_target_message
    )
    if not output.parent.is_dir():
        raise ValueError(f"{label} parent is not a directory: {output.parent}")

    output_existed = output.exists()
    if output_existed and not output.is_file():
        # An existing destination that is not a regular file is refused even
        # under --force. The replacement path renames whatever is there into a
        # hidden backup, so a mistyped destination naming a DIRECTORY relocated
        # an entire tree and installed the artifact at its former path. --force
        # authorizes replacing an artifact, not moving a directory.
        raise ValueError(
            f"{label} exists and is not a regular file: {output}. Refusing to "
            "replace it; choose a destination that is a file or does not exist."
        )
    if output_existed and not force:
        raise ValueError(
            f"{label} already exists: {output}. Pass --force to replace it."
        )
    return {
        "parent_identity": path_identity(output.parent),
        "output_existed": output_existed,
        "output_identity": path_identity(output) if output_existed else None,
    }


def verify_destination(
    output: Path,
    protected: Path,
    guard: dict[str, Any],
    *,
    label: str,
    protected_target_message: str | None = None,
) -> None:
    _validate_output_path(
        output, protected, label, protected_target_message=protected_target_message
    )
    try:
        parent_identity = path_identity(output.parent)
    except FileNotFoundError as error:
        raise ValueError(
            f"{label} parent changed during export: {output.parent}"
        ) from error
    if parent_identity != guard["parent_identity"]:
        raise ValueError(
            f"{label} parent changed during export: {output.parent}"
        )

    if guard["output_existed"]:
        if not output.exists():
            raise ValueError(f"{label} changed during export: {output}")
        if path_identity(output) != guard["output_identity"]:
            raise ValueError(f"{label} changed during export: {output}")
    elif output.exists() or is_link_like(output):
        raise ValueError(
            f"{label} appeared during export and was not replaced: {output}"
        )


def _install_non_replacing(source: Path, destination: Path) -> None:
    """Move source into an absent destination without a replace window."""

    if os.name == "nt":
        # Windows rename is atomic and refuses an existing target.
        os.rename(source, destination)
    else:
        # POSIX rename replaces, so install with an exclusive hard link.
        os.link(source, destination)
        source.unlink()


def _restore_backup(
    backup: Path, output: Path, installer: Any
) -> bool:
    """Restore without overwriting a late arrival; retain backup on failure."""

    if output.exists() or is_link_like(output):
        return False
    try:
        installer(backup, output)
    except OSError:
        return False
    return True


def _replace_recoverably(
    temporary: Path,
    output: Path,
    expected_identity: tuple[int, int],
    expected_parent_identity: tuple[int, int],
    *,
    label: str,
    installer: Any,
    install_failed_message: str | None = None,
    install_failed_retained_message: str | None = None,
) -> str:
    """Two-phase force replacement retaining every unapproved late arrival."""

    backup_directory = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.backup.", dir=output.parent)
    )
    backup = backup_directory / output.name
    if path_identity(output.parent) != expected_parent_identity:
        try:
            backup_directory.rmdir()
        except OSError:
            pass
        raise ValueError(
            f"{label} parent changed during final replacement: {output.parent}"
        )
    try:
        os.rename(output, backup)
    except OSError:
        try:
            backup_directory.rmdir()
        except OSError:
            pass
        raise

    rotated_identity = path_identity(backup)
    parent_changed = path_identity(output.parent) != expected_parent_identity
    if rotated_identity != expected_identity or parent_changed:
        restored = _restore_backup(backup, output, installer)
        if restored:
            try:
                backup_directory.rmdir()
            except OSError:
                pass
            raise ValueError(
                f"{label} or parent changed during final replacement; "
                "the rotated file was restored and no export was installed."
            )
        raise ValueError(
            f"{label} or parent changed during final replacement. "
            f"Backup retained at: {backup}"
        )

    if path_identity(output.parent) != expected_parent_identity:
        restored = _restore_backup(backup, output, installer)
        if restored:
            try:
                backup_directory.rmdir()
            except OSError:
                pass
            raise ValueError(
                f"{label} parent changed during final replacement; "
                "the previous output was restored."
            )
        raise ValueError(
            f"{label} parent changed during final replacement. "
            f"Backup retained at: {backup}"
        )

    try:
        installer(temporary, output)
    except OSError as error:
        restored = _restore_backup(backup, output, installer)
        if restored:
            try:
                backup_directory.rmdir()
            except OSError:
                pass
            message = (
                install_failed_message
                or f"{label} installation failed; the previous output was restored."
            )
            raise ValueError(message) from error
        message = install_failed_retained_message or (
            f"{label} installation failed and the previous output could not "
            f"be restored without overwriting another file."
        )
        raise ValueError(f"{message} Backup retained at: {backup}") from error

    # Deliberately retain the approved old output. There is no cross-platform
    # compare-and-delete primitive that can prove this path was not swapped
    # between an identity check and unlink. Cleanup is therefore user-directed.
    return str(backup)


def install(
    temporary: Path,
    output: Path,
    guard: dict[str, Any],
    *,
    label: str,
    installer: Any = None,
    install_failed_message: str | None = None,
    install_failed_retained_message: str | None = None,
) -> str | None:
    if installer is None:
        installer = _install_non_replacing
    if guard["output_existed"]:
        return _replace_recoverably(
            temporary,
            output,
            guard["output_identity"],
            guard["parent_identity"],
            label=label,
            installer=installer,
            install_failed_message=install_failed_message,
            install_failed_retained_message=install_failed_retained_message,
        )
    try:
        installer(temporary, output)
    except FileExistsError as error:
        raise ValueError(
            f"{label} appeared during export and was not replaced: {output}"
        ) from error
    return None
