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
