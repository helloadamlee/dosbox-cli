import sys
import types
from pathlib import Path

from dosbox_mcp import _client_import


def test_bridge_resolves_sibling_scripts_module():
    expected = (
        Path(__file__).resolve().parents[2] / "scripts" / "host_control_client.py"
    )
    assert Path(_client_import.hc.__file__).resolve() == expected


def test_bridge_reexports_expected_names():
    for name in (
        "PipeTransport",
        "SessionClosed",
        "RequestTimeout",
        "encode_request",
        "read_event_line",
        "make_deadline",
        "remaining_seconds",
    ):
        assert hasattr(_client_import, name), f"missing re-export: {name}"


def test_vendored_copy_wins_when_present(monkeypatch):
    sentinel = types.ModuleType("dosbox_mcp._vendor.host_control_client")
    sentinel.MARKER = "vendored"
    monkeypatch.setitem(
        sys.modules, "dosbox_mcp._vendor.host_control_client", sentinel
    )

    result = _client_import._load_host_control_client()

    assert getattr(result, "MARKER", None) == "vendored"


def test_falls_back_to_sibling_scripts_without_vendored_copy(monkeypatch):
    monkeypatch.setitem(sys.modules, "dosbox_mcp._vendor.host_control_client", None)
    monkeypatch.delitem(
        sys.modules, "dosbox_mcp._vendor.host_control_client", raising=False
    )

    result = _client_import._load_host_control_client()

    assert hasattr(result, "PipeTransport")


def test_reexports_survive_resolution():
    assert _client_import.PipeTransport is not None
    assert _client_import.SessionClosed is not None
    assert _client_import.encode_request is not None
