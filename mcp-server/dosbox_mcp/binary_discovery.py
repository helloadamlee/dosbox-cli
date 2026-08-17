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

    Resolution order: explicit_path, then the DOSBOX_X_BINARY environment
    variable, then known per-platform install and in-repo build locations,
    then PATH.

    DOSBOX_X_BINARY comes before the candidate paths on purpose: a release
    bundle sets it to its own binary, and a machine with stock DOSBox-X
    installed would otherwise match a candidate path first and hand back a
    build with no host-control support.
    """
    if explicit_path is not None:
        path = Path(explicit_path)
        if not path.is_file():
            raise BinaryNotFoundError(f"binary_path does not exist: {path}")
        return path

    from_env = os.environ.get("DOSBOX_X_BINARY")
    if from_env:
        path = Path(from_env)
        if not path.is_file():
            raise BinaryNotFoundError(
                f"DOSBOX_X_BINARY is set to a path that does not exist: {path}"
            )
        return path

    for candidate in _candidate_paths():
        if candidate.is_file():
            return candidate

    found = shutil.which("dosbox-x")
    if found is not None:
        return Path(found)

    raise BinaryNotFoundError(
        "could not find a dosbox-x binary. This MCP server requires a "
        "dosbox-cli build with host-control support — a stock DOSBox-X "
        "install will not work. Set DOSBOX_X_BINARY to the binary shipped "
        "in the release bundle, or pass binary_path explicitly."
    )
