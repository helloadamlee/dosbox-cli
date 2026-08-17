#!/usr/bin/env python3
"""Assemble a release bundle into the layout documented in the design spec.

Layout (identical on both platforms, modulo the binary's name):

    dosbox-cli-<version>-<platform>/
    |-- QUICKSTART.md
    |-- COPYING
    |-- dosbox-x/      emulator payload, including its own scripts/ directory
    |-- mcp-server/    pip install target, with the vendored client inside
    `-- scripts/host_control_client.py

The emulator payload is nested under dosbox-x/ because it ships its own
scripts/ directory, which would otherwise collide with the repo's
scripts/host_control_client.py at bundle root.
"""

import argparse
import shutil
import sys
from pathlib import Path

_EXCLUDE = shutil.ignore_patterns("__pycache__", "*.pyc", "tests")


def assemble(repo_root, payload_dir, version, platform, out_dir):
    """Build the bundle tree and return the path to its root directory."""
    repo_root = Path(repo_root)
    payload_dir = Path(payload_dir)
    out_dir = Path(out_dir)

    if not payload_dir.is_dir():
        raise FileNotFoundError(f"emulator payload not found: {payload_dir}")

    client = repo_root / "scripts" / "host_control_client.py"
    quickstart = repo_root / "contrib" / "bundle" / "QUICKSTART.md"
    copying = repo_root / "COPYING"
    for required in (client, quickstart, copying):
        if not required.is_file():
            raise FileNotFoundError(f"required file missing: {required}")

    root = out_dir / f"dosbox-cli-{version}-{platform}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    shutil.copytree(payload_dir, root / "dosbox-x", ignore=_EXCLUDE)
    shutil.copytree(repo_root / "mcp-server", root / "mcp-server", ignore=_EXCLUDE)

    (root / "scripts").mkdir()
    shutil.copy2(client, root / "scripts" / "host_control_client.py")

    # The vendored copy is what makes a plain `pip install ./mcp-server`
    # work; without it _client_import falls back to a sibling scripts/
    # directory that does not exist once the package is in site-packages.
    vendor_dir = root / "mcp-server" / "dosbox_mcp" / "_vendor"
    vendor_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(client, vendor_dir / "host_control_client.py")

    shutil.copy2(quickstart, root / "QUICKSTART.md")
    shutil.copy2(copying, root / "COPYING")

    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--payload-dir", required=True,
                        help="directory holding the built emulator and its assets")
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", required=True, choices=["win64", "linux-x86_64"])
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)

    root = assemble(
        repo_root=args.repo_root,
        payload_dir=args.payload_dir,
        version=args.version,
        platform=args.platform,
        out_dir=args.out_dir,
    )
    print(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
