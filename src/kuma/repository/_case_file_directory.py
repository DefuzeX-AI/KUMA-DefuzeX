"""Pin artifact destination directories while publishing a new local Case."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path


def _windows_pin(path: Path) -> tuple[Callable[[int], int], int]:
    """Open an existing Windows directory without permitting rename or deletion.

    Artifact publication holds these handles for each path ancestor. Reparse
    points are opened themselves, not followed; the caller rechecks canonical
    identity before writing. Return the CloseHandle callable and owned handle.
    Win32 errors are deliberately replaced by OSError without native text.
    """
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # READ_ATTRIBUTES; share read/write but not delete; OPEN_EXISTING;
    # BACKUP_SEMANTICS permits directories, OPEN_REPARSE_POINT avoids following.
    handle = create(str(path), 0x80, 3, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError("Case directory could not be pinned")
    return close, handle


@contextmanager
def pinned_case_directory(root: Path, parent: Path) -> Iterator[int | None]:
    """Anchor a Case publication to an already validated repository directory.

    Args:
        root: Canonical authorized repository root.
        parent: Existing canonical destination parent inside root.
    Yields:
        POSIX directory descriptor for all relative write/link/unlink operations;
        None on Windows, where non-delete-sharing ancestor handles pin the path.
    Raises:
        OSError: Missing, moved, linked, reparse or cross-device directory.
    Postconditions:
        All directory handles close on success and every failure. POSIX uses
        O_NOFOLLOW for every component; Windows never permits a pinned ancestor
        to be renamed during publication. No directories are created.
    """
    relative = parent.relative_to(root)
    with ExitStack() as stack:
        if os.name == "nt":
            for path in [*reversed(parent.parents), parent]:
                close, handle = _windows_pin(path)
                stack.callback(close, handle)
            if parent.resolve(strict=True) != parent:
                raise OSError("Case directory identity changed")
            for path in [
                root,
                *(
                    root.joinpath(*relative.parts[:i])
                    for i in range(1, len(relative.parts) + 1)
                ),
            ]:
                info = path.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400
                    or info.st_dev != root.stat().st_dev
                ):
                    raise OSError("Case directory boundary changed")
            yield None
            return
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(root, flags)
        stack.callback(os.close, descriptor)
        device = os.fstat(descriptor).st_dev
        for component in relative.parts:
            descriptor = os.open(component, flags, dir_fd=descriptor)
            stack.callback(os.close, descriptor)
            if os.fstat(descriptor).st_dev != device:
                raise OSError("Case directory crosses a mount")
        frozen, named = os.fstat(descriptor), parent.stat()
        if (frozen.st_dev, frozen.st_ino) != (named.st_dev, named.st_ino):
            raise OSError("Case directory identity changed")
        yield descriptor
