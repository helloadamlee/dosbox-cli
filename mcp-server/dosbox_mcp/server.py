"""MCP server exposing DOSBox-X host control as agent tools."""

from typing import Optional

from mcp.server.mcpserver import MCPServer

from .session import DosboxSession, SessionError

mcp = MCPServer("dosbox-x")

_session: Optional[DosboxSession] = None


def _require_session() -> DosboxSession:
    if _session is None:
        raise SessionError("no active session; call start_session first")
    return _session


@mcp.tool()
def start_session(
    cwd: str,
    binary_path: Optional[str] = None,
    config_path: Optional[str] = None,
    mount: Optional[dict] = None,
    dos_path: Optional[str] = None,
    env: Optional[dict] = None,
) -> dict:
    """Launch a DOSBox-X process and connect a host-control session.

    Fails if a session is already active — call stop_session first. `mount`
    is {"drive": "c", "host_path": "..."}. Because DOSBox-X host control
    skips AUTOEXEC.BAT, mount/dos_path/env are replayed as real setup
    commands before this returns; see setup_results for each step's outcome.
    """
    global _session
    if _session is not None:
        raise SessionError("a session is already active; call stop_session first")

    session, setup_results = DosboxSession.launch(
        cwd=cwd,
        binary_path=binary_path,
        config_path=config_path,
        mount=mount,
        dos_path=dos_path,
        env=env,
    )
    _session = session
    status = session.status()
    return {**status, "pid": session.process.pid, "setup_results": setup_results}


@mcp.tool(name="exec")
def exec_command(command: str) -> dict:
    """Start a DOS command. Returns immediately — call poll() for output."""
    request_id = _require_session().exec(command)
    return {"request_id": request_id}


@mcp.tool()
def poll(wait_seconds: float = 2.0) -> dict:
    """Wait briefly for output/completion of the in-flight command.

    output is only the text received since the last poll() call. Once done
    is true, later polls return done:true with no new output until the next
    exec().
    """
    return _require_session().poll(wait_seconds=wait_seconds)


@mcp.tool()
def send_input(text: Optional[str] = None, key: Optional[str] = None) -> dict:
    """Send keyboard input to the running command. Exactly one of text/key.

    queued acknowledges the input was sent, not the protocol's exact queued
    count — see the plan's Global Constraints for why.
    """
    return _require_session().send_input(text=text, key=key)


@mcp.tool()
def cancel() -> dict:
    """Request cancellation of the in-flight command.

    Stops a running batch file; the exec's result then reports
    cancelled: true. Fire-and-forget acknowledgment, so poll() for the
    outcome. A command that ignores Ctrl-C (e.g. a tight emulation loop
    with no console reads) may not stop — stop_session is the fallback.
    """
    return _require_session().cancel()


@mcp.tool()
def status() -> dict:
    """Report whether a session is active and its current DOS drive/cwd."""
    if _session is None:
        return {"session_active": False, "drive": None, "cwd": None}
    return _session.status()


@mcp.tool()
def stop_session(force: bool = False) -> dict:
    """Stop the active session and kill the DOSBox-X process.

    Use this to abort a command that ignores the cancel tool (e.g. a tight
    emulation loop) — it kills the whole DOSBox-X process.
    """
    global _session
    if _session is None:
        return {"stopped": True}
    result = _session.stop(force=force)
    _session = None
    return result


def main():
    mcp.run()


if __name__ == "__main__":
    main()
