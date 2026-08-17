#!/usr/bin/env python3
"""Prove an assembled bundle actually runs a DOS command.

Run this AFTER `pip install <bundle>/mcp-server`, so it exercises the
installed package rather than the repo checkout. That is the point: a check
that imported from the checkout would pass while the bundle was broken.
"""

import os
import sys
import time
from pathlib import Path


def smoke(bundle_root):
    bundle_root = Path(bundle_root).resolve()
    binary = bundle_root / "dosbox-x" / (
        "dosbox-x.exe" if os.name == "nt" else "dosbox-x"
    )
    if not binary.is_file():
        raise SystemExit(f"FAIL: bundled binary missing: {binary}")

    os.environ["DOSBOX_X_BINARY"] = str(binary)

    # Imported here, after the env var is set, and resolved from the
    # installed package rather than any local source tree.
    from dosbox_mcp.session import DosboxSession

    session, setup_results = DosboxSession.launch(cwd=str(bundle_root))
    try:
        for step in setup_results:
            if not step.get("ok"):
                raise SystemExit(f"FAIL: setup step failed: {step}")

        session.exec("ver")
        deadline = time.monotonic() + 60
        result = None
        while time.monotonic() < deadline:
            result = session.poll(wait_seconds=5)
            if result["done"]:
                break
        if result is None or not result["done"]:
            raise SystemExit("FAIL: 'ver' did not complete within 60s")
        if not result["ok"]:
            raise SystemExit(f"FAIL: 'ver' reported failure: {result}")

        print("SMOKE OK")
        print(f"  binary : {binary}")
        print(f"  output : {result['output'].strip()[:200]!r}")
        return 0
    finally:
        session.stop(force=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke_bundle.py <bundle-root>")
    sys.exit(smoke(sys.argv[1]))
