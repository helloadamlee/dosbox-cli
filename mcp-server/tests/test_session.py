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
