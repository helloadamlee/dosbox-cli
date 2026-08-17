"""Bridge to host_control_client, resolved from one of two locations.

A built or bundled distribution carries a vendored copy at
dosbox_mcp/_vendor/host_control_client.py, copied in at packaging time. A
development checkout has no vendored copy and instead reaches the canonical
scripts/host_control_client.py sitting alongside mcp-server/.

The vendored copy must win when present: after a normal (non-editable)
pip install, this module lives in site-packages/dosbox_mcp/, so the
sibling-scripts path resolves outside the installed package and is wrong.
That is why a plain `pip install ./mcp-server` failed before this existed.
"""

import importlib
import sys
from pathlib import Path


def _load_host_control_client():
    try:
        return importlib.import_module("dosbox_mcp._vendor.host_control_client")
    except ModuleNotFoundError:
        pass

    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return importlib.import_module("host_control_client")


hc = _load_host_control_client()

PipeTransport = hc.PipeTransport
SessionClosed = hc.SessionClosed
RequestTimeout = hc.RequestTimeout
encode_request = hc.encode_request
read_event_line = hc.read_event_line
make_deadline = hc.make_deadline
remaining_seconds = hc.remaining_seconds
wait_for_readable = hc.wait_for_readable
