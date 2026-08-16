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
        env={"TEMP": "C:\\", "TMP": "C:\\"}
    )

    commands = [entry["command"] for entry in source.written]
    assert "set TEMP=C:\\" in commands
    assert "set TMP=C:\\" in commands

    session.stop(force=True)


def test_setup_results_report_each_command():
    session, source, results = launch_with_fake_source(
        mount={"drive": "c", "host_path": r"C:\project"},
        env={"TEMP": "C:\\"},
    )

    assert len(results) == 4  # mount, drive-change, path, one env var
    assert all(entry["ok"] is True for entry in results)
    assert all("errorlevel" in entry for entry in results)

    session.stop(force=True)
