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
