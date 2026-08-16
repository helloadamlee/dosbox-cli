"""Locate the dosbox-x binary to launch."""

import os
import platform
import shutil
from pathlib import Path


class BinaryNotFoundError(RuntimeError):
    pass


def _candidate_paths():
    system = platform.system()
    repo_root = Path(__file__).resolve().parents[2]

    if system == "Windows":
        yield repo_root / "bin" / "x64" / "Release SDL2" / "dosbox-x.exe"
        yield (
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
            / "DOSBox-X"
            / "dosbox-x.exe"
        )
        yield (
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "DOSBox-X"
            / "dosbox-x.exe"
        )
    elif system == "Darwin":
        yield Path("/Applications/dosbox-x.app/Contents/MacOS/dosbox-x")
        yield repo_root / "src" / "dosbox-x"
    else:
        yield Path("/usr/bin/dosbox-x")
        yield Path("/usr/local/bin/dosbox-x")
        yield repo_root / "src" / "dosbox-x"


def resolve_dosbox_binary(explicit_path=None):
    """Return a Path to a usable dosbox-x binary.

    Resolution order: explicit_path if given, then known per-platform install
    and in-repo build locations, then PATH.
    """
    if explicit_path is not None:
        path = Path(explicit_path)
        if not path.is_file():
            raise BinaryNotFoundError(f"binary_path does not exist: {path}")
        return path

    for candidate in _candidate_paths():
        if candidate.is_file():
            return candidate

    found = shutil.which("dosbox-x")
    if found is not None:
        return Path(found)

    raise BinaryNotFoundError(
        "could not find a dosbox-x binary; pass binary_path explicitly"
    )
