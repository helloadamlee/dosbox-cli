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
                "cancelled": result.get("cancelled"),
                "bad_command": self._bad_command,
                "server_error": (
                    result.get("message") if result.get("event") == "error" else None
                ),
            }
