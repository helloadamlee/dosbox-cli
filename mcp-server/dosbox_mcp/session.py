"""Owns one DOSBox-X process and its host-control session."""

import json
import subprocess
import threading
import time
import uuid

from ._client_import import (
    PipeTransport,
    RequestTimeout,
    SessionClosed,
    encode_request,
    make_deadline,
    wait_for_readable,
)
from .binary_discovery import BinaryNotFoundError, resolve_dosbox_binary
from .session_state import SessionPhase, SessionState

_READ_LOOP_TIMEOUT = 1.0
_READY_TIMEOUT = 30.0
_SETUP_COMMAND_TIMEOUT = 30.0
DEFAULT_DOS_PATH = r"Z:\;C:\;C:\TOOLS"


class SessionError(RuntimeError):
    pass


class TransportEventSource:
    """Adapts a connected PipeTransport to DosboxSession's narrow interface.

    DosboxSession never touches PipeTransport or host_control_client's
    framing functions directly — only through this adapter — so tests can
    substitute a fake source with no real I/O.

    next_event() is built on the transport's buffered-line primitives rather
    than host_control_client.read_event_line(), which unconditionally echoes
    every raw event to stdout. That echo would corrupt the MCP server's own
    stdio transport, so this adapter reads silently.
    """

    def __init__(self, transport):
        self._transport = transport

    def next_event(self, timeout):
        """Return the next event dict, or None if the session closed cleanly.

        Raises TimeoutError if no event arrived within `timeout` seconds.
        """
        try:
            deadline = make_deadline(timeout)
            while not self._transport.has_buffered_line():
                wait_for_readable(self._transport, deadline, "event")
                if not self._transport.read_available():
                    break
            raw_line = self._transport.pop_line()
            if not raw_line:
                return None
            return json.loads(raw_line.decode("utf-8"))
        except SessionClosed:
            return None
        except RequestTimeout as exc:
            raise TimeoutError(str(exc)) from exc
        except OSError:
            # Includes the read failure our own close() deliberately causes
            # mid-read during stop() — treated the same as a clean end since
            # by that point we've already decided to tear down.
            return None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("received invalid JSON event from dosbox-x") from exc

    def send(self, request_id, op, **kwargs):
        self._transport.writeline(encode_request(request_id, op, **kwargs))

    def close(self):
        self._transport.close()


class DosboxSession:
    """A running DOSBox-X host-control session.

    Construct via `DosboxSession.launch(...)`, which spawns the real process
    and connects a real event source. Tests construct instances directly
    with a fake event source and no process, bypassing all real I/O.
    """

    def __init__(self, source, process=None):
        self._source = source
        self.process = process
        self.state = SessionState()
        self._next_request_id = 1
        self._reader_thread = None
        self._stop_reading = threading.Event()

    @classmethod
    def launch(
        cls,
        cwd,
        binary_path=None,
        config_path=None,
        mount=None,
        dos_path=None,
        env=None,
    ):
        try:
            binary = resolve_dosbox_binary(binary_path)
        except BinaryNotFoundError as exc:
            raise SessionError(str(exc)) from exc

        endpoint = f"dosbox-mcp-{uuid.uuid4().hex[:12]}"
        args = [str(binary), "-control-pipe", endpoint, "-headless"]
        if config_path is not None:
            args += ["-conf", str(config_path)]

        process = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        transport = PipeTransport(endpoint, timeout=_READY_TIMEOUT)
        connect_deadline = time.monotonic() + _READY_TIMEOUT
        while True:
            try:
                transport.connect()
                break
            except OSError as exc:
                # On Windows the server creates its named pipe a moment after
                # the process starts; CreateFile reports ERROR_FILE_NOT_FOUND
                # until it does. Retry until the pipe appears, the process
                # dies, or the ready deadline passes.
                if process.poll() is not None:
                    process.wait()
                    raise SessionError(
                        f"dosbox-x exited before the control pipe was ready: {exc}"
                    ) from exc
                if time.monotonic() >= connect_deadline:
                    process.kill()
                    process.wait()
                    raise SessionError(f"failed to connect to dosbox-x: {exc}") from exc
                time.sleep(0.1)

        session = cls(TransportEventSource(transport), process=process)
        session._start_reading()

        deadline = time.monotonic() + _READY_TIMEOUT
        while session.state.snapshot()["phase"] == SessionPhase.STARTING:
            if time.monotonic() >= deadline:
                session.stop(force=True)
                raise SessionError("dosbox-x did not become ready in time")
            time.sleep(0.05)

        if session.state.snapshot()["phase"] == SessionPhase.STOPPED:
            raise SessionError("dosbox-x closed the connection before becoming ready")

        setup_results = session._run_setup(mount, dos_path, env)
        return session, setup_results

    def _start_reading(self):
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self):
        while not self._stop_reading.is_set():
            try:
                event = self._source.next_event(_READ_LOOP_TIMEOUT)
            except TimeoutError:
                continue
            if event is None:
                self.state.mark_stopped()
                return
            self.state.record_event(event)

    def _write_request(self, op, **kwargs):
        request_id = str(self._next_request_id)
        self._next_request_id += 1
        self._source.send(request_id, op, **kwargs)
        return request_id

    def _run_setup(self, mount, dos_path, env):
        results = []
        if mount is not None:
            drive = mount["drive"]
            host_path = mount["host_path"]
            results.append(self._run_setup_command(f'mount {drive} "{host_path}"'))
            results.append(self._run_setup_command(f"{drive}:"))
        results.append(self._run_setup_command(f"path {dos_path or DEFAULT_DOS_PATH}"))
        for key, value in (env or {}).items():
            results.append(self._run_setup_command(f"set {key}={value}"))
        return results

    def _run_setup_command(self, command):
        self.exec(command)
        deadline = time.monotonic() + _SETUP_COMMAND_TIMEOUT
        while True:
            snap = self.state.snapshot()
            if snap["done"]:
                return {
                    "command": command,
                    "ok": snap["ok"],
                    "errorlevel": snap["errorlevel"],
                }
            if time.monotonic() >= deadline:
                raise SessionError(f"setup command timed out: {command}")
            time.sleep(0.02)

    def exec(self, command):
        if self.state.snapshot()["phase"] != SessionPhase.READY:
            raise SessionError("exec requires a ready session (no command in flight)")
        self.state.begin_request()
        return self._write_request("exec", command=command)

    def poll(self, wait_seconds=2.0):
        deadline = time.monotonic() + max(0.0, min(wait_seconds, 10.0))
        while True:
            snap = self.state.snapshot()
            if (
                snap["done"]
                or snap["phase"] != SessionPhase.BUSY
                or time.monotonic() >= deadline
            ):
                return {
                    "running": snap["running"],
                    "done": snap["done"],
                    "output": self.state.drain_output(),
                    "errorlevel": snap["errorlevel"],
                    "max_errorlevel": snap["max_errorlevel"],
                    "ok": snap["ok"],
                    "bad_command": snap["bad_command"],
                    "drive": snap["drive"],
                    "cwd": snap["cwd"],
                }
            time.sleep(0.05)

    def send_input(self, text=None, key=None):
        if (text is None) == (key is None):
            raise SessionError("send_input requires exactly one of text or key")
        if self.state.snapshot()["phase"] != SessionPhase.BUSY:
            raise SessionError("send_input requires a command in flight")
        if text is not None:
            self._write_request("input_text", text=text)
        else:
            self._write_request("key", key=key)
        return {"queued": True}

    def status(self):
        snap = self.state.snapshot()
        return {
            "session_active": snap["phase"] != SessionPhase.STOPPED,
            "drive": snap["drive"],
            "cwd": snap["cwd"],
        }

    def stop(self, force=False):
        self._stop_reading.set()
        self._source.close()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=5)
        if self.process is not None:
            if force:
                self.process.kill()
                self.process.wait()
            else:
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        self.state.mark_stopped()
        return {"stopped": True}
