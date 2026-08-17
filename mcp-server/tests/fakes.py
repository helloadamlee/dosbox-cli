"""Shared test doubles for dosbox_mcp tests."""

import base64
import queue


def encode_output_event(text):
    data = base64.b64encode(text.encode("cp437")).decode("ascii")
    return {"event": "output", "id": "1", "encoding": "base64", "data": data}


def encode_result_event(
    ok=True, errorlevel=0, max_errorlevel=0, drive="C", cwd="C:\\", cancelled=False
):
    return {
        "event": "result",
        "id": "1",
        "ok": ok,
        "shell_exit": False,
        "errorlevel": errorlevel,
        "max_errorlevel": max_errorlevel,
        "cancelled": cancelled,
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
