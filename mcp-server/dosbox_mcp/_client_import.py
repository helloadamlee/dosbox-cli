"""Bridge to the sibling scripts/host_control_client.py module.

mcp-server/ and scripts/ are siblings under the same repo checkout. This adds
scripts/ to sys.path at import time so the rest of dosbox_mcp can reuse
host_control_client's transport and framing code directly, instead of
vendoring or reimplementing it.
"""

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import host_control_client as hc  # noqa: E402

PipeTransport = hc.PipeTransport
SessionClosed = hc.SessionClosed
RequestTimeout = hc.RequestTimeout
encode_request = hc.encode_request
read_event_line = hc.read_event_line
make_deadline = hc.make_deadline
remaining_seconds = hc.remaining_seconds
