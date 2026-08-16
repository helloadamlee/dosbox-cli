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
