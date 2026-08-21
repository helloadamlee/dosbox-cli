"""Wire-format tests: the MCP protocol surface as a client actually sees it.

test_server.py calls the tool functions as plain Python, so it cannot observe
anything the MCP layer does -- a renamed tool, a dropped schema field, or a
changed result serialization all pass it. These tests drive a real client over
the SDK's in-memory transport instead, so those failures are visible.

Requires mcp 2.x. server.py imports mcp.server.mcpserver, which does not exist
in the 1.x line, so there is no version of this suite that runs against both.

SDK surface as found in mcp 2.0.0, recorded because it differs from 1.x in two
ways that matter to anyone reading these assertions:

  * mcp.shared.memory.create_connected_server_and_client_session, the 1.x
    in-memory helper, is gone. The replacement is the public mcp.client.Client,
    which takes a Server or MCPServer instance directly and is documented for
    exactly this use. (mcp.client._memory.InMemoryTransport is what backs it,
    but it is private.)
  * The result and tool models renamed their camelCase fields to snake_case:
    inputSchema -> input_schema, outputSchema -> output_schema, isError ->
    is_error, structuredContent -> structured_content. This is a rename of the
    Python attributes only; the values are unchanged.

Two observed conventions are pinned deliberately, because a silent change to
either is exactly what this file exists to catch. Both were verified to be
identical either side of the 1.28.1 -> 2.0.0 port:

  * No tool declares an output_schema, so every result arrives as a single
    JSON-encoded TextContent block and structured_content is None. The tools
    are annotated `-> dict` (bare, unparameterized), which the SDK treats as
    unstructured. If a later version starts emitting structured_content, these
    tests fail -- that is the intended signal, not a defect in the tests.
  * A SessionError raised inside a tool surfaces as a result with is_error set,
    not as an exception on the client. The client call returns normally.
"""

import json

import anyio
import pytest
from mcp.client import Client

from dosbox_mcp import server

# FakeSession is imported rather than copied so the two files cannot drift.
# reset_session is an autouse fixture; importing it registers it in this
# module's namespace too, which is what keeps _session from leaking between
# tests here.
from .test_server import FakeSession, reset_session  # noqa: F401

EXPECTED_TOOL_NAMES = {
    "start_session",
    "exec",
    "poll",
    "send_input",
    "cancel",
    "status",
    "stop_session",
}


def drive(body):
    """Run `body(session)` against a real in-memory MCP client session."""

    async def _main():
        async with Client(server.mcp) as session:
            await body(session)

    anyio.run(_main)


def payload(result):
    """Return a tool result's JSON body, asserting how it was serialized."""
    assert result.is_error is False, f"unexpected tool error: {result.content}"
    assert result.structured_content is None, (
        "structured_content appeared; the result serialization changed"
    )
    assert len(result.content) == 1, f"expected one content block, got {result.content}"
    assert result.content[0].type == "text"
    return json.loads(result.content[0].text)


def test_all_seven_tools_are_advertised():
    async def body(session):
        names = {tool.name for tool in (await session.list_tools()).tools}
        # exec, not exec_command: the function is exec_command but it is
        # registered as @mcp.tool(name="exec"). Losing that alias renames a
        # tool for every client.
        assert names == EXPECTED_TOOL_NAMES

    drive(body)


def test_input_schemas_pin_required_params():
    # required is absent (None) when a tool has no required params.
    expected = {
        "start_session": (
            ["cwd"],
            ["binary_path", "config_path", "cwd", "dos_path", "env", "mount"],
        ),
        "exec": (["command"], ["command"]),
        "poll": (None, ["wait_seconds"]),
        "send_input": (None, ["key", "text"]),
        "cancel": (None, []),
        "status": (None, []),
        "stop_session": (None, ["force"]),
    }

    async def body(session):
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == set(expected)
        for name, (required, properties) in expected.items():
            schema = tools[name].input_schema
            assert schema.get("required") == required, f"{name}: required changed"
            assert sorted(schema.get("properties", {})) == properties, (
                f"{name}: properties changed"
            )

    drive(body)


def test_result_payload_start_session(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(
        server.DosboxSession, "launch", staticmethod(lambda **kw: (fake, []))
    )

    async def body(session):
        result = await session.call_tool("start_session", {"cwd": "C:\\project"})
        assert payload(result) == {
            "session_active": True,
            "drive": "C",
            "cwd": "C:\\",
            "pid": 4242,
            "setup_results": [],
        }

    drive(body)


def test_result_payload_poll():
    server._session = FakeSession()

    async def body(session):
        result = await session.call_tool("poll", {"wait_seconds": 0.1})
        # The widest payload of the seven, so the one most likely to lose a key.
        assert payload(result) == {
            "running": False,
            "done": True,
            "output": "hi\r\n",
            "errorlevel": 0,
            "max_errorlevel": 0,
            "ok": True,
            "bad_command": False,
            "drive": "C",
            "cwd": "C:\\",
        }

    drive(body)


def test_result_payload_status():
    server._session = FakeSession()

    async def body(session):
        result = await session.call_tool("status", {})
        assert payload(result) == {
            "session_active": True,
            "drive": "C",
            "cwd": "C:\\",
        }

    drive(body)


def test_status_without_session_reports_inactive():
    async def body(session):
        result = await session.call_tool("status", {})
        assert payload(result) == {
            "session_active": False,
            "drive": None,
            "cwd": None,
        }

    drive(body)


def test_no_session_surfaces_as_tool_error():
    async def body(session):
        # SessionError must come back as an error *result*, not as an exception
        # raised on the client. Which of the two a port produces is a plausible
        # 2.0 change, so the convention is pinned rather than assumed.
        result = await session.call_tool("exec", {"command": "dir"})
        assert result.is_error is True
        assert result.content[0].type == "text"
        assert "no active session" in result.content[0].text

    drive(body)


def test_results_are_json_text_not_structured_content():
    server._session = FakeSession()

    async def body(session):
        for name, args in (("status", {}), ("cancel", {}), ("send_input", {"text": "y"})):
            result = await session.call_tool(name, args)
            assert result.structured_content is None, f"{name}: now structured"
            assert len(result.content) == 1, f"{name}: content block count changed"
            assert result.content[0].type == "text", f"{name}: not a text block"
            # Must be a JSON object, not a repr or a bare string.
            assert isinstance(json.loads(result.content[0].text), dict), (
                f"{name}: text block is not a JSON object"
            )

    drive(body)
