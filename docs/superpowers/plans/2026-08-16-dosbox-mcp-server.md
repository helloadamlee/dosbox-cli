# DOSBox-X Host-Control MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the DOSBox-X host-control pipe protocol as an MCP server (`mcp-server/`) so agents drive DOS sessions via six typed tools instead of hand-rolling NDJSON framing.

**Architecture:** One MCP server process owns at most one DOSBox-X child process. `start_session` launches it and connects `scripts/host_control_client.py`'s existing `PipeTransport`; a background reader thread continuously drains protocol events into a locked `SessionState`, so every tool call (`exec`, `poll`, `send_input`, `status`, `stop_session`) is non-blocking on the pipe. Two known protocol gaps are worked around client-side: the `[autoexec]` setup is replayed as real commands at session start, and DOS's "Bad command or filename" is detected by scanning output text.

**Tech Stack:** Python 3.9+, official `mcp` SDK (FastMCP decorator API, already installed at 1.28.1 on this machine), `pytest` for tests, reuses `scripts/host_control_client.py` via a `sys.path` bridge (no vendoring).

**Spec:** `docs/superpowers/specs/2026-08-16-dosbox-mcp-server-design.md`

## Global Constraints

- No `src/` layout — package lives directly at `mcp-server/dosbox_mcp/`. (The spec's illustrative `sys.path` snippet assumed a flatter layout; this plan uses `parents[2]` from `dosbox_mcp/<file>.py` to reach the repo root, not the spec's `parents[1]`.)
- Transport is always `PipeTransport` via `-control-pipe` (it already handles both Windows named pipes and Unix FIFO pairs internally — no separate socket-transport code path is needed). This is a plan-level implementation refinement of the spec's architecture section; it changes no tool contract.
- `DosboxSession` depends on a narrow `EventSource`-shaped object (`next_event(timeout)`, `send(request_id, op, **kwargs)`, `close()`), not directly on `PipeTransport` or `host_control_client`'s framing functions. This is what makes it unit-testable without a real DOSBox-X process — tests inject a fake source; `DosboxSession.launch()` is the only place that constructs the real one.
- Every mutation of shared session state goes through `SessionState`, guarded by one lock. The background reader thread is the only writer of protocol-driven state (`record_event`); tool-call code only calls `begin_request()` before sending `exec`, and reads via `snapshot()`/`drain_output()`.
- `send_input`'s returned `{"queued": true}` is a fire-and-forget acknowledgment, not the protocol's exact `queued: <int>` count — correlating a specific `input_result` event with a specific `send_input` call while an unrelated `exec`'s output is concurrently streaming needs per-request-id correlation this v1 doesn't do. Documented in the tool's docstring.
- `status` reads cached local state (last known drive/cwd from the most recent `result`/`status` event already seen), not a fresh protocol round-trip — avoids a second concurrent write path for no behavioral gain, since DOS's drive/cwd cannot change while a command is still running.

**User decisions (already made):**
- Async-with-polling over blocking tool calls for long-running DOS commands.
- Server owns the DOSBox-X process lifecycle (launches it, not attach-only).
- One session at a time, no multi-session support in v1.
- Work around the autoexec and bad-command gaps client-side; don't expose a `cancel` tool since the protocol op is broken — `stop_session` is the only abort path.
- Location: `mcp-server/`, sibling to `scripts/`/`src/`/`docs/` in `upstream-dosbox-x` (published as `github.com/helloadamlee/dosbox-cli`).
- Portfolio-facing v1: usable and demoable now; non-critical protocol bugs (the roadmap's deferred items) stay out of scope.

---

## Task 1: Package scaffold and client bridge

**Goal:** Create the `mcp-server/` package skeleton, installable with `pip install -e`, with a working `sys.path` bridge to `scripts/host_control_client.py`.

**Files:**
- Create: `mcp-server/pyproject.toml`
- Create: `mcp-server/dosbox_mcp/__init__.py`
- Create: `mcp-server/dosbox_mcp/_client_import.py`
- Test: `mcp-server/tests/__init__.py`
- Test: `mcp-server/tests/test_client_bridge.py`

**Acceptance Criteria:**
- [ ] `pip install -e mcp-server/[test]` succeeds from the repo root.
- [ ] `dosbox_mcp._client_import` exposes `PipeTransport`, `SessionClosed`, `RequestTimeout`, `encode_request`, `read_event_line`, `make_deadline`, `remaining_seconds`, all identical objects to the ones in `scripts/host_control_client.py` (same module, not a copy).

**Verify:** `cd mcp-server && python -m pytest tests/test_client_bridge.py -v` → 1 passed

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `mcp-server/tests/__init__.py` (empty file).

Create `mcp-server/tests/test_client_bridge.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-server && python -m pytest tests/test_client_bridge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dosbox_mcp'` (package doesn't exist yet)

- [ ] **Step 3: Write the package scaffold**

Create `mcp-server/pyproject.toml`:

```toml
[project]
name = "dosbox-x-mcp"
version = "0.1.0"
description = "MCP server wrapping DOSBox-X host-control for agentic DOS automation"
requires-python = ">=3.9"
dependencies = [
    "mcp>=1.2.0",
]

[project.optional-dependencies]
test = ["pytest>=7.0"]

[project.scripts]
dosbox-mcp = "dosbox_mcp.server:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["dosbox_mcp*"]
```

Create `mcp-server/dosbox_mcp/__init__.py`:

```python
"""dosbox-x-mcp: MCP server wrapping DOSBox-X host-control for agentic use."""

__version__ = "0.1.0"
```

Create `mcp-server/dosbox_mcp/_client_import.py`:

```python
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
```

- [ ] **Step 4: Install and run test to verify it passes**

Run: `cd mcp-server && pip install -e ".[test]" && python -m pytest tests/test_client_bridge.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add mcp-server/pyproject.toml mcp-server/dosbox_mcp/__init__.py mcp-server/dosbox_mcp/_client_import.py mcp-server/tests/__init__.py mcp-server/tests/test_client_bridge.py
git commit -m "feat(mcp-server): scaffold package with client bridge"
```

---

## Task 2: Binary discovery

**Goal:** Resolve a usable `dosbox-x` binary path: explicit override, then known install/build locations, then `PATH`.

**Files:**
- Create: `mcp-server/dosbox_mcp/binary_discovery.py`
- Test: `mcp-server/tests/test_binary_discovery.py`

**Acceptance Criteria:**
- [ ] An explicit `binary_path` that exists is returned as-is; a nonexistent explicit path raises `BinaryNotFoundError`.
- [ ] When no explicit path is given, a candidate location that exists on disk is returned.
- [ ] When nothing matches on disk, falls back to `shutil.which("dosbox-x")`.
- [ ] When nothing is found anywhere, raises `BinaryNotFoundError` with a message telling the caller to pass `binary_path`.

**Verify:** `cd mcp-server && python -m pytest tests/test_binary_discovery.py -v` → all passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `mcp-server/tests/test_binary_discovery.py`:

```python
from pathlib import Path

import pytest

from dosbox_mcp.binary_discovery import BinaryNotFoundError, resolve_dosbox_binary


def test_explicit_path_returned_when_it_exists(tmp_path):
    fake_binary = tmp_path / "dosbox-x.exe"
    fake_binary.write_text("not a real binary")

    result = resolve_dosbox_binary(explicit_path=str(fake_binary))

    assert result == fake_binary


def test_explicit_path_raises_when_missing(tmp_path):
    missing = tmp_path / "does-not-exist.exe"

    with pytest.raises(BinaryNotFoundError):
        resolve_dosbox_binary(explicit_path=str(missing))


def test_falls_back_to_candidate_location(monkeypatch, tmp_path):
    fake_binary = tmp_path / "dosbox-x"
    fake_binary.write_text("not a real binary")

    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery._candidate_paths",
        lambda: iter([tmp_path / "nope", fake_binary]),
    )

    result = resolve_dosbox_binary()

    assert result == fake_binary


def test_falls_back_to_path_when_no_candidate_matches(monkeypatch, tmp_path):
    on_path = tmp_path / "dosbox-x"
    on_path.write_text("not a real binary")

    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery._candidate_paths",
        lambda: iter([tmp_path / "nope"]),
    )
    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery.shutil.which",
        lambda name: str(on_path),
    )

    result = resolve_dosbox_binary()

    assert result == on_path


def test_raises_when_nothing_found(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery._candidate_paths",
        lambda: iter([tmp_path / "nope"]),
    )
    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery.shutil.which",
        lambda name: None,
    )

    with pytest.raises(BinaryNotFoundError):
        resolve_dosbox_binary()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-server && python -m pytest tests/test_binary_discovery.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dosbox_mcp.binary_discovery'`

- [ ] **Step 3: Write the implementation**

Create `mcp-server/dosbox_mcp/binary_discovery.py`:

```python
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

    Resolution order: explicit_path if given, then known per-platform install
    and in-repo build locations, then PATH.
    """
    if explicit_path is not None:
        path = Path(explicit_path)
        if not path.is_file():
            raise BinaryNotFoundError(f"binary_path does not exist: {path}")
        return path

    for candidate in _candidate_paths():
        if candidate.is_file():
            return candidate

    found = shutil.which("dosbox-x")
    if found is not None:
        return Path(found)

    raise BinaryNotFoundError(
        "could not find a dosbox-x binary; pass binary_path explicitly"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-server && python -m pytest tests/test_binary_discovery.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add mcp-server/dosbox_mcp/binary_discovery.py mcp-server/tests/test_binary_discovery.py
git commit -m "feat(mcp-server): add dosbox-x binary discovery"
```

---

## Task 3: SessionState — in-memory state container

**Goal:** A thread-safe, pure in-memory holder for one session's observable state: phase, drive/cwd, accumulated output, last result, and the bad-command heuristic. No I/O.

**Files:**
- Create: `mcp-server/dosbox_mcp/session_state.py`
- Test: `mcp-server/tests/test_session_state.py`

**Acceptance Criteria:**
- [ ] Starts in `STARTING`; a `ready` event moves it to `READY`.
- [ ] `begin_request()` moves `READY`→`BUSY` and clears the previous result/bad-command flag.
- [ ] A `result` (or `error`) event moves `BUSY`→`READY`, records drive/cwd, and makes `snapshot()["done"]` true.
- [ ] `poll`-style output access (`drain_output()`) returns only text accumulated since the last call, then clears it.
- [ ] `snapshot()["bad_command"]` becomes true only when an `output` event's decoded text contains `Bad command or filename`.
- [ ] `snapshot()["done"]` stays true across repeated calls until the next `begin_request()`.

**Verify:** `cd mcp-server && python -m pytest tests/test_session_state.py -v` → all passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `mcp-server/tests/test_session_state.py`:

```python
import base64

from dosbox_mcp.session_state import SessionPhase, SessionState


def output_event(text):
    data = base64.b64encode(text.encode("cp437")).decode("ascii")
    return {"event": "output", "id": "1", "encoding": "base64", "data": data}


def result_event(ok=True, errorlevel=0, max_errorlevel=0, drive="C", cwd="C:\\"):
    return {
        "event": "result",
        "id": "1",
        "ok": ok,
        "shell_exit": False,
        "errorlevel": errorlevel,
        "max_errorlevel": max_errorlevel,
        "drive": drive,
        "cwd": cwd,
        "duration_ms": 1,
    }


def test_starts_in_starting_phase():
    state = SessionState()
    assert state.snapshot()["phase"] == SessionPhase.STARTING


def test_ready_event_moves_to_ready():
    state = SessionState()
    state.record_event({"event": "ready", "transport": "pipe"})
    assert state.snapshot()["phase"] == SessionPhase.READY


def test_begin_request_moves_to_busy_and_clears_previous_result():
    state = SessionState()
    state.record_event({"event": "ready"})
    state.record_event(result_event())
    assert state.snapshot()["done"] is True

    state.begin_request()

    snap = state.snapshot()
    assert snap["phase"] == SessionPhase.BUSY
    assert snap["done"] is False
    assert snap["bad_command"] is False


def test_result_event_moves_to_ready_and_records_drive_cwd():
    state = SessionState()
    state.record_event({"event": "ready"})
    state.begin_request()

    state.record_event(result_event(drive="Z", cwd="Z:\\GAME"))

    snap = state.snapshot()
    assert snap["phase"] == SessionPhase.READY
    assert snap["done"] is True
    assert snap["drive"] == "Z"
    assert snap["cwd"] == "Z:\\GAME"


def test_drain_output_returns_only_new_text_since_last_call():
    state = SessionState()
    state.record_event({"event": "ready"})
    state.begin_request()

    state.record_event(output_event("hello "))
    assert state.drain_output() == "hello "

    state.record_event(output_event("world"))
    assert state.drain_output() == "world"
    assert state.drain_output() == ""


def test_bad_command_marker_detected_in_output():
    state = SessionState()
    state.record_event({"event": "ready"})
    state.begin_request()

    state.record_event(output_event('Bad command or filename - "NOPE.EXE"\r\n'))

    assert state.snapshot()["bad_command"] is True


def test_done_stays_true_until_next_begin_request():
    state = SessionState()
    state.record_event({"event": "ready"})
    state.begin_request()
    state.record_event(result_event())

    assert state.snapshot()["done"] is True
    assert state.snapshot()["done"] is True  # repeated poll, still true

    state.begin_request()
    assert state.snapshot()["done"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-server && python -m pytest tests/test_session_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dosbox_mcp.session_state'`

- [ ] **Step 3: Write the implementation**

Create `mcp-server/dosbox_mcp/session_state.py`:

```python
"""Thread-safe in-memory state for one DOSBox-X host-control session.

One background reader thread writes to this via record_event(); tool
handlers read from it via snapshot()/drain_output() and call begin_request()
before sending a new exec. All access is guarded by a single lock — this
class has no I/O of its own.
"""

import base64
import enum
import threading

BAD_COMMAND_MARKER = "Bad command or filename"


class SessionPhase(enum.Enum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    STOPPED = "stopped"


class SessionState:
    def __init__(self):
        self._lock = threading.Lock()
        self.phase = SessionPhase.STARTING
        self.drive = None
        self.cwd = None
        self._output_buffer = []
        self._last_result = None
        self._bad_command = False

    def begin_request(self):
        with self._lock:
            self.phase = SessionPhase.BUSY
            self._last_result = None
            self._bad_command = False

    def record_event(self, event):
        with self._lock:
            kind = event.get("event")
            if kind == "ready":
                if self.phase == SessionPhase.STARTING:
                    self.phase = SessionPhase.READY
            elif kind == "output" and event.get("data"):
                decoded = base64.b64decode(event["data"]).decode(
                    "cp437", errors="replace"
                )
                self._output_buffer.append(decoded)
                if BAD_COMMAND_MARKER in decoded:
                    self._bad_command = True
            elif kind in ("result", "error"):
                self._last_result = event
                self.drive = event.get("drive", self.drive)
                self.cwd = event.get("cwd", self.cwd)
                self.phase = SessionPhase.READY
            elif kind == "status":
                self.drive = event.get("drive", self.drive)
                self.cwd = event.get("cwd", self.cwd)
            # "input_result" is intentionally not tracked — send_input's
            # contract is fire-and-forget (see plan Global Constraints).

    def mark_stopped(self):
        with self._lock:
            self.phase = SessionPhase.STOPPED

    def drain_output(self):
        """Return and clear output accumulated since the last call."""
        with self._lock:
            text = "".join(self._output_buffer)
            self._output_buffer = []
            return text

    def snapshot(self):
        with self._lock:
            done = self._last_result is not None
            result = self._last_result or {}
            return {
                "phase": self.phase,
                "drive": self.drive,
                "cwd": self.cwd,
                "done": done,
                "running": self.phase == SessionPhase.BUSY and not done,
                "ok": result.get("ok"),
                "errorlevel": result.get("errorlevel"),
                "max_errorlevel": result.get("max_errorlevel"),
                "bad_command": self._bad_command,
                "server_error": (
                    result.get("message") if result.get("event") == "error" else None
                ),
            }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-server && python -m pytest tests/test_session_state.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add mcp-server/dosbox_mcp/session_state.py mcp-server/tests/test_session_state.py
git commit -m "feat(mcp-server): add thread-safe session state container"
```

---

## Task 4: DosboxSession core — process lifecycle and tool operations

**Goal:** `DosboxSession`, the class owning a connected event source, a background reader thread, and the `exec`/`poll`/`send_input`/`status`/`stop` operations, enforcing the session state machine. Testable without a real DOSBox-X process via an injected fake event source.

**Files:**
- Create: `mcp-server/dosbox_mcp/session.py`
- Test: `mcp-server/tests/fakes.py`
- Test: `mcp-server/tests/test_session.py`

**Acceptance Criteria:**
- [ ] `exec()` raises `SessionError` unless the session is `READY`; succeeds and moves to `BUSY` when ready.
- [ ] `poll()` blocks for at most `wait_seconds`, returns new output since the last poll, and reports `done`/`running`/`errorlevel`/`max_errorlevel`/`bad_command` from the current state.
- [ ] `send_input()` raises `SessionError` unless `BUSY`; raises `SessionError` if given both or neither of `text`/`key`.
- [ ] `stop()` sets phase to `STOPPED`, closes the event source, and joins the reader thread within a bounded timeout.
- [ ] The reader thread transitions `STARTING`→`READY` on a `ready` event without racing any other reader of the event source (there is exactly one).
- [ ] A closed/ended event source (`next_event` returns `None`) makes the reader thread mark the session `STOPPED` and exit.

**Verify:** `cd mcp-server && python -m pytest tests/test_session.py -v` → all passed

**Steps:**

- [ ] **Step 1: Write the shared test fake**

Create `mcp-server/tests/fakes.py`:

```python
"""Shared test doubles for dosbox_mcp tests."""

import base64
import queue


def encode_output_event(text):
    data = base64.b64encode(text.encode("cp437")).decode("ascii")
    return {"event": "output", "id": "1", "encoding": "base64", "data": data}


def encode_result_event(ok=True, errorlevel=0, max_errorlevel=0, drive="C", cwd="C:\\"):
    return {
        "event": "result",
        "id": "1",
        "ok": ok,
        "shell_exit": False,
        "errorlevel": errorlevel,
        "max_errorlevel": max_errorlevel,
        "drive": drive,
        "cwd": cwd,
        "duration_ms": 1,
    }


class FakeEventSource:
    """An EventSource double driven by a queue of pre-scripted events.

    next_event(timeout) blocks on the queue up to timeout seconds; raises
    TimeoutError if nothing arrives; returns None if `close()` was called
    with nothing left queued (simulating a clean session end).
    """

    def __init__(self, events=None):
        self._queue = queue.Queue()
        self.written = []
        self.closed = False
        for event in events or []:
            self._queue.put(event)

    def push(self, event):
        self._queue.put(event)

    def next_event(self, timeout):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            if self.closed:
                return None
            raise TimeoutError("no event within timeout")

    def send(self, request_id, op, **kwargs):
        self.written.append({"id": request_id, "op": op, **kwargs})

    def close(self):
        self.closed = True
        self._queue.put(None)
```

- [ ] **Step 2: Write the failing tests**

Create `mcp-server/tests/test_session.py`:

```python
import time

import pytest

from dosbox_mcp.session import DosboxSession, SessionError
from dosbox_mcp.session_state import SessionPhase

from .fakes import FakeEventSource, encode_output_event, encode_result_event


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def make_running_session(events=None):
    source = FakeEventSource(events or [{"event": "ready"}])
    session = DosboxSession(source, process=None)
    session._start_reading()
    assert wait_until(lambda: session.state.snapshot()["phase"] == SessionPhase.READY)
    return session, source


def test_ready_event_transitions_to_ready_with_single_reader():
    session, source = make_running_session()
    assert session.state.snapshot()["phase"] == SessionPhase.READY
    session.stop(force=True)


def test_exec_requires_ready_session():
    session, source = make_running_session()
    session.exec("dir")

    with pytest.raises(SessionError):
        session.exec("dir")

    session.stop(force=True)


def test_exec_then_poll_reports_output_and_done():
    session, source = make_running_session()

    session.exec("echo hi")
    source.push(encode_output_event("hi\r\n"))
    source.push(encode_result_event(ok=True, errorlevel=0))

    assert wait_until(lambda: session.state.snapshot()["done"])
    result = session.poll(wait_seconds=0.5)

    assert result["done"] is True
    assert result["running"] is False
    assert result["ok"] is True
    assert result["errorlevel"] == 0
    assert "hi" in result["output"]

    session.stop(force=True)


def test_poll_output_is_not_repeated_across_calls():
    session, source = make_running_session()
    session.exec("echo hi")
    source.push(encode_output_event("hi\r\n"))
    source.push(encode_result_event())
    assert wait_until(lambda: session.state.snapshot()["done"])

    first = session.poll(wait_seconds=0.5)
    second = session.poll(wait_seconds=0.2)

    assert "hi" in first["output"]
    assert second["output"] == ""
    assert second["done"] is True

    session.stop(force=True)


def test_send_input_requires_busy_session():
    session, source = make_running_session()

    with pytest.raises(SessionError):
        session.send_input(text="y\r")

    session.exec("del *.bak")
    result = session.send_input(text="y\r")
    assert result == {"queued": True}
    assert source.written[-1] == {"id": "2", "op": "input_text", "text": "y\r"}

    session.stop(force=True)


def test_send_input_requires_exactly_one_of_text_or_key():
    session, source = make_running_session()
    session.exec("del *.bak")

    with pytest.raises(SessionError):
        session.send_input()
    with pytest.raises(SessionError):
        session.send_input(text="y", key="enter")

    session.stop(force=True)


def test_status_reflects_last_known_drive_and_cwd():
    session, source = make_running_session()
    session.exec("c:")
    source.push(encode_result_event(drive="C", cwd="C:\\PROJECT"))
    assert wait_until(lambda: session.state.snapshot()["done"])

    status = session.status()

    assert status["session_active"] is True
    assert status["drive"] == "C"
    assert status["cwd"] == "C:\\PROJECT"

    session.stop(force=True)


def test_stop_marks_stopped_and_closes_source():
    session, source = make_running_session()

    result = session.stop(force=True)

    assert result == {"stopped": True}
    assert source.closed is True
    assert session.state.snapshot()["phase"] == SessionPhase.STOPPED


def test_closed_event_source_marks_session_stopped():
    source = FakeEventSource([{"event": "ready"}])
    session = DosboxSession(source, process=None)
    session._start_reading()
    assert wait_until(lambda: session.state.snapshot()["phase"] == SessionPhase.READY)

    source.close()

    assert wait_until(
        lambda: session.state.snapshot()["phase"] == SessionPhase.STOPPED
    )
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd mcp-server && python -m pytest tests/test_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dosbox_mcp.session'`

- [ ] **Step 4: Write the implementation**

Create `mcp-server/dosbox_mcp/session.py`:

```python
"""Owns one DOSBox-X process and its host-control session."""

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
    read_event_line,
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
    """

    def __init__(self, transport):
        self._transport = transport

    def next_event(self, timeout):
        """Return the next event dict, or None if the session closed cleanly.

        Raises TimeoutError if no event arrived within `timeout` seconds.
        """
        try:
            return read_event_line(self._transport, make_deadline(timeout), "event")
        except SessionClosed:
            return None
        except RequestTimeout as exc:
            raise TimeoutError(str(exc)) from exc
        except OSError:
            # Includes the read failure our own close() deliberately causes
            # mid-read during stop() — treated the same as a clean end since
            # by that point we've already decided to tear down.
            return None

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
        try:
            transport.connect()
        except OSError as exc:
            process.kill()
            process.wait()
            raise SessionError(f"failed to connect to dosbox-x: {exc}") from exc

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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd mcp-server && python -m pytest tests/test_session.py -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add mcp-server/dosbox_mcp/session.py mcp-server/tests/fakes.py mcp-server/tests/test_session.py
git commit -m "feat(mcp-server): add DosboxSession core with background reader thread"
```

---

## Task 5: Setup-command replay (autoexec workaround)

**Goal:** Verify and lock down `DosboxSession.launch()`'s setup-replay behavior — the `mount`/`dos_path`/`env` params issued as real commands before `launch()` returns, since `[autoexec]` never runs under host control.

**Files:**
- Test: `mcp-server/tests/test_session_setup.py`

Note: the implementation (`_run_setup`/`_run_setup_command`) already exists from Task 4 — this task is dedicated test coverage for that behavior in isolation, since Task 4's tests exercise `exec`/`poll`/`send_input` directly and don't yet cover the `.launch()` setup sequence. No new production code should be needed; if these tests reveal a gap, fix `session.py` here.

**Acceptance Criteria:**
- [ ] With `mount` given, the setup sequence issues `mount <drive> "<host_path>"` then `<drive>:` before the caller-visible session is returned as ready-to-use.
- [ ] `dos_path` (or the default `Z:\;C:\;C:\TOOLS` when omitted) is issued as a `path` command.
- [ ] Each `env` entry is issued as a `set KEY=value` command.
- [ ] `setup_results` reports one entry per issued command with its `ok`/`errorlevel`.
- [ ] A failing setup command (`ok: false`) still appears in `setup_results` rather than raising — the caller decides whether to treat it as fatal.

**Verify:** `cd mcp-server && python -m pytest tests/test_session_setup.py -v` → all passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `mcp-server/tests/test_session_setup.py`:

```python
import threading
import time

from dosbox_mcp.session import DosboxSession

from .fakes import FakeEventSource, encode_result_event


def auto_respond(source, stop_event):
    """Answer every exec request in source.written with an immediate result."""
    seen = 0
    while not stop_event.is_set():
        if len(source.written) > seen:
            seen = len(source.written)
            source.push(encode_result_event(ok=True, errorlevel=0))
        time.sleep(0.01)


def launch_with_fake_source(mount=None, dos_path=None, env=None):
    source = FakeEventSource([{"event": "ready"}])
    session = DosboxSession(source, process=None)
    session._start_reading()

    stop_responder = threading.Event()
    responder = threading.Thread(
        target=auto_respond, args=(source, stop_responder), daemon=True
    )
    responder.start()

    deadline = time.monotonic() + 2.0
    while session.state.snapshot()["phase"].name == "STARTING":
        assert time.monotonic() < deadline, "session never became ready"
        time.sleep(0.01)

    setup_results = session._run_setup(mount, dos_path, env)

    stop_responder.set()
    responder.join(timeout=1)
    return session, source, setup_results


def test_mount_issues_mount_and_drive_change():
    session, source, results = launch_with_fake_source(
        mount={"drive": "c", "host_path": r"C:\project"}
    )

    commands = [entry["command"] for entry in source.written]
    assert commands[0] == 'mount c "C:\\project"'
    assert commands[1] == "c:"

    session.stop(force=True)


def test_default_dos_path_used_when_not_given():
    session, source, results = launch_with_fake_source()

    commands = [entry["command"] for entry in source.written]
    assert r"path Z:\;C:\;C:\TOOLS" in commands

    session.stop(force=True)


def test_custom_dos_path_used_when_given():
    session, source, results = launch_with_fake_source(dos_path=r"Z:\;C:\;C:\OTHER")

    commands = [entry["command"] for entry in source.written]
    assert r"path Z:\;C:\;C:\OTHER" in commands

    session.stop(force=True)


def test_env_vars_issued_as_set_commands():
    session, source, results = launch_with_fake_source(
        env={"TEMP": r"C:\", "TMP": r"C:\"}
    )

    commands = [entry["command"] for entry in source.written]
    assert "set TEMP=C:\\" in commands
    assert "set TMP=C:\\" in commands

    session.stop(force=True)


def test_setup_results_report_each_command():
    session, source, results = launch_with_fake_source(
        mount={"drive": "c", "host_path": r"C:\project"},
        env={"TEMP": r"C:\"},
    )

    assert len(results) == 4  # mount, drive-change, path, one env var
    assert all(entry["ok"] is True for entry in results)
    assert all("errorlevel" in entry for entry in results)

    session.stop(force=True)
```

- [ ] **Step 2: Run tests to verify they fail or pass**

Run: `cd mcp-server && python -m pytest tests/test_session_setup.py -v`
Expected: since `_run_setup`/`_run_setup_command` already exist from Task 4, these should PASS immediately. If any fail, fix `session.py`'s `_run_setup`/`_run_setup_command` to match — do not change the test expectations to match a broken implementation.

- [ ] **Step 3: Commit**

```bash
git add mcp-server/tests/test_session_setup.py
git commit -m "test(mcp-server): cover autoexec-replacement setup sequencing"
```

---

## Task 6: MCP server — tool wiring

**Goal:** `server.py` exposing the six tools via FastMCP, enforcing one-session-at-a-time, with a `main()` entry point for the `dosbox-mcp` console script.

**Files:**
- Create: `mcp-server/dosbox_mcp/server.py`
- Test: `mcp-server/tests/test_server.py`

**Acceptance Criteria:**
- [ ] `start_session` fails with a clear error if a session is already active.
- [ ] `start_session` returns `pid` and `setup_results` alongside session status.
- [ ] `exec`, `poll`, `send_input`, `status` raise/return a clear error when no session is active (except `status`, which reports `session_active: false` rather than raising).
- [ ] `stop_session` clears the active-session slot so a subsequent `start_session` succeeds.
- [ ] The tool named `exec` does not shadow Python's builtin at the module level (implemented as `exec_command`, exposed as `exec` via the tool decorator's `name=` argument).

**Verify:** `cd mcp-server && python -m pytest tests/test_server.py -v` → all passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `mcp-server/tests/test_server.py`:

```python
from unittest import mock

import pytest

from dosbox_mcp import server
from dosbox_mcp.session import SessionError


class FakeSession:
    def __init__(self):
        self.process = mock.Mock(pid=4242)
        self.stopped = False

    def status(self):
        return {"session_active": True, "drive": "C", "cwd": "C:\\"}

    def exec(self, command):
        return "1"

    def poll(self, wait_seconds=2.0):
        return {
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

    def send_input(self, text=None, key=None):
        return {"queued": True}

    def stop(self, force=False):
        self.stopped = True
        return {"stopped": True}


@pytest.fixture(autouse=True)
def reset_session():
    server._session = None
    yield
    server._session = None


def test_start_session_launches_and_stores_session(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(
        server.DosboxSession, "launch", staticmethod(lambda **kw: (fake, []))
    )

    result = server.start_session(cwd="C:\\project")

    assert result["session_active"] is True
    assert result["pid"] == 4242
    assert result["setup_results"] == []
    assert server._session is fake


def test_start_session_fails_when_already_active():
    server._session = FakeSession()

    with pytest.raises(SessionError):
        server.start_session(cwd="C:\\project")


def test_exec_requires_active_session():
    with pytest.raises(SessionError):
        server.exec_command(command="dir")


def test_exec_delegates_to_session():
    server._session = FakeSession()

    result = server.exec_command(command="dir")

    assert result == {"request_id": "1"}


def test_poll_delegates_to_session():
    server._session = FakeSession()

    result = server.poll(wait_seconds=1.0)

    assert result["done"] is True
    assert result["output"] == "hi\r\n"


def test_send_input_delegates_to_session():
    server._session = FakeSession()

    result = server.send_input(text="y\r")

    assert result == {"queued": True}


def test_status_reports_inactive_without_raising_when_no_session():
    result = server.status()

    assert result == {"session_active": False, "drive": None, "cwd": None}


def test_stop_session_clears_active_session():
    fake = FakeSession()
    server._session = fake

    result = server.stop_session()

    assert result == {"stopped": True}
    assert fake.stopped is True
    assert server._session is None


def test_stop_session_is_a_no_op_when_no_session():
    result = server.stop_session()

    assert result == {"stopped": True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-server && python -m pytest tests/test_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dosbox_mcp.server'`

- [ ] **Step 3: Write the implementation**

Create `mcp-server/dosbox_mcp/server.py`:

```python
"""FastMCP server exposing DOSBox-X host control as agent tools."""

from typing import Optional

from mcp.server.fastmcp import FastMCP

from .session import DosboxSession, SessionError

mcp = FastMCP("dosbox-x")

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
def status() -> dict:
    """Report whether a session is active and its current DOS drive/cwd."""
    if _session is None:
        return {"session_active": False, "drive": None, "cwd": None}
    return _session.status()


@mcp.tool()
def stop_session(force: bool = False) -> dict:
    """Stop the active session and kill the DOSBox-X process.

    This is the only way to abort a hung command — the protocol's cancel op
    does not work (see docs/host-control-windows-pipe-roadmap.md).
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-server && python -m pytest tests/test_server.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add mcp-server/dosbox_mcp/server.py mcp-server/tests/test_server.py
git commit -m "feat(mcp-server): wire the six MCP tools onto DosboxSession"
```

---

## Task 7: Opt-in live integration test

**Goal:** One end-to-end test against a real DOSBox-X binary, gated behind an env var so it doesn't run by default (matching the main repo's `DOSBOX_X_LIVE_TESTS` pattern).

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in the acceptance criteria has been re-validated independently, with output captured.

**Files:**
- Create: `mcp-server/tests/test_live_integration.py`

**Acceptance Criteria:**
- [ ] Skipped by default; runs only when `DOSBOX_MCP_LIVE_TESTS=1` and `DOSBOX_X_BINARY` are both set.
- [ ] Drives the real `server.py` tool functions directly against a real `DosboxSession.launch()` (no subprocess-of-a-subprocess MCP transport needed — the tool functions are plain Python callables) — starts a session, runs a trivial DOS command, polls to completion, asserts on output and errorlevel, then stops the session.

**Verify:** `cd mcp-server && DOSBOX_MCP_LIVE_TESTS=1 DOSBOX_X_BINARY="<path from earlier session>" python -m pytest tests/test_live_integration.py -v` → 1 passed

**Steps:**

- [ ] **Step 1: Write the test**

Create `mcp-server/tests/test_live_integration.py`:

```python
"""Opt-in end-to-end test against a real dosbox-x binary.

Mirrors the gating pattern in tests/host_control_live_tests.py at the repo
root: skipped unless explicitly enabled, since it needs a real binary and
takes longer than the unit suite.
"""

import os
import tempfile
import unittest

from dosbox_mcp import server

LIVE = os.environ.get("DOSBOX_MCP_LIVE_TESTS") == "1"
BINARY = os.environ.get("DOSBOX_X_BINARY")


@unittest.skipUnless(LIVE and BINARY, "set DOSBOX_MCP_LIVE_TESTS=1 and DOSBOX_X_BINARY")
class LiveIntegrationTest(unittest.TestCase):
    def setUp(self):
        server._session = None

    def tearDown(self):
        if server._session is not None:
            server.stop_session(force=True)

    def test_start_exec_poll_stop_round_trip(self):
        with tempfile.TemporaryDirectory() as cwd:
            start_result = server.start_session(cwd=cwd, binary_path=BINARY)
            self.assertTrue(start_result["session_active"])
            self.assertIsInstance(start_result["pid"], int)

            server.exec_command(command="ver")

            output = ""
            ok = None
            errorlevel = None
            for _ in range(30):  # up to ~30s of bounded polling
                result = server.poll(wait_seconds=1.0)
                output += result["output"]
                if result["done"]:
                    ok = result["ok"]
                    errorlevel = result["errorlevel"]
                    break

            self.assertTrue(ok, f"exec did not complete cleanly; output={output!r}")
            self.assertEqual(errorlevel, 0)
            self.assertTrue(output.strip(), "expected some output from 'ver'")

            stop_result = server.stop_session()
            self.assertEqual(stop_result, {"stopped": True})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it against the binary built earlier this session**

Run:
```bash
cd mcp-server
DOSBOX_MCP_LIVE_TESTS=1 DOSBOX_X_BINARY="../bin/x64/Release SDL2/dosbox-x.exe" python -m pytest tests/test_live_integration.py -v
```
Expected: 1 passed. If it fails, debug `DosboxSession`/`server.py` against the real protocol — do not weaken the test's assertions to match a bug.

- [ ] **Step 3: Confirm it's skipped by default**

Run: `cd mcp-server && python -m pytest tests/test_live_integration.py -v`
Expected: 1 skipped

- [ ] **Step 4: Commit**

```bash
git add mcp-server/tests/test_live_integration.py
git commit -m "test(mcp-server): add opt-in live integration test"
```

---

## Task 8: README and MCP client configuration example

**Goal:** A short README covering install, the tool contract, and how to register the server in an MCP-compatible agent host, so this is actually usable — not just importable.

**Files:**
- Create: `mcp-server/README.md`

**Acceptance Criteria:**
- [ ] Documents install (`pip install -e mcp-server/`), the `dosbox-mcp` entry point, and an example MCP client config JSON block.
- [ ] Documents all six tools with their parameters, in one table.
- [ ] States the two workarounds (autoexec replay, bad-command scan) and the one omission (no `cancel` tool — `stop_session` is the abort path) plainly, so a user of the server isn't surprised by behavior already known and documented at the protocol level in `docs/host-control-windows-pipe-roadmap.md`.

**Verify:** Manual read-through; no automated check for a README.

**Steps:**

- [ ] **Step 1: Write the README**

Create `mcp-server/README.md`:

```markdown
# dosbox-x-mcp

An MCP server that wraps this repo's DOSBox-X host-control pipe protocol so
an agent can drive a DOS session — running a build, inspecting a mounted
directory, answering an interactive prompt — as typed tool calls instead of
hand-rolled NDJSON.

Design background: `docs/superpowers/specs/2026-08-16-dosbox-mcp-server-design.md`.

## Install

```bash
cd mcp-server
pip install -e .
```

This installs the `dosbox-mcp` console script and depends on the sibling
`scripts/host_control_client.py` in this same repo checkout — it is not
usable installed standalone outside this repo.

## Register with an MCP client

Example config (Claude Code, Claude Desktop, or any MCP-compatible host that
reads a `command`/`args` server entry):

```json
{
  "mcpServers": {
    "dosbox-x": {
      "command": "dosbox-mcp"
    }
  }
}
```

## Tools

One DOSBox-X session at a time. `start_session` launches the process;
`stop_session` is the only way to end it — there is no `cancel` for a
running command (see "Known limitations" below).

| Tool | Parameters | Returns |
|---|---|---|
| `start_session` | `cwd`, `binary_path?`, `config_path?`, `mount?` (`{"drive": "c", "host_path": "..."}`), `dos_path?`, `env?` | `{session_active, drive, cwd, pid, setup_results}` |
| `exec` | `command` | `{request_id}` — returns immediately, does not wait for completion |
| `poll` | `wait_seconds?` (default 2, max 10) | `{running, done, output, errorlevel, max_errorlevel, ok, bad_command, drive, cwd}` |
| `send_input` | `text?` or `key?` (exactly one) | `{queued}` |
| `status` | — | `{session_active, drive, cwd}` |
| `stop_session` | `force?` | `{stopped}` |

Typical sequence: `start_session` → `exec` → repeated `poll` until `done` →
… → `stop_session`.

## Known limitations (by design, v1)

- **`[autoexec]` never runs.** Host control replaces the DOS shell's normal
  run loop, so a config's `[autoexec]` section is skipped. `start_session`'s
  `mount`/`dos_path`/`env` params work around this by replaying the
  equivalent commands before returning — use them instead of relying on your
  `.conf` file's `[autoexec]`.
- **`bad_command` is a heuristic.** DOS reports a command it can't find as a
  normal successful exit (`errorlevel 0`). `poll`'s `bad_command` field is a
  text scan for `Bad command or filename` in the output, not a protocol-level
  signal — it can miss a bad-command message split across two `output`
  events at exactly the wrong byte boundary, though this is rare in
  practice.
- **No `cancel` tool.** The underlying protocol's `cancel`/`break` op exists
  but doesn't actually stop a running program. A hung command must be
  handled with `stop_session`, which kills the whole DOSBox-X process — there
  is no way to cancel just the current command and keep the session alive.
- **One session at a time.** A second `start_session` fails until
  `stop_session` is called.

See `docs/host-control-windows-pipe-roadmap.md` in the repo root for the
underlying protocol's own tracked gaps and deferred work.
```

- [ ] **Step 2: Commit**

```bash
git add mcp-server/README.md
git commit -m "docs(mcp-server): add README with tool table and known limitations"
```
