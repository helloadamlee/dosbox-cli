import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT = REPO_ROOT / "scripts" / "host_control_client.py"
DEFAULT_DOSBOX_X = REPO_ROOT / "src" / "dosbox-x"


class HostControlLiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("DOSBOX_X_LIVE_TESTS") != "1":
            raise unittest.SkipTest("set DOSBOX_X_LIVE_TESTS=1 to run live DOSBox-X tests")

        cls.dosbox_x = Path(os.environ.get("DOSBOX_X_BINARY", DEFAULT_DOSBOX_X))
        if not cls.dosbox_x.exists():
            raise unittest.SkipTest(f"DOSBox-X binary not found: {cls.dosbox_x}")

    def parse_events(self, proc):
        events = []
        for line_number, line in enumerate(proc.stdout.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                self.fail(
                    "failed to parse JSON event line "
                    f"{line_number}: {line!r}\n"
                    f"client stdout:\n{proc.stdout}\n"
                    f"client stderr:\n{proc.stderr}\n"
                    f"parse error: {exc}"
                )
        return events

    def read_server_diagnostics(self, stdout_path, stderr_path, server):
        server_stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        server_stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        return (
            f"server return code: {server.returncode}\n"
            f"server stdout:\n{server_stdout}\n"
            f"server stderr:\n{server_stderr}"
        )

    def run_stdio_repl(self, commands, timeout_seconds=10):
        proc = subprocess.run(
            [
                sys.executable,
                str(CLIENT),
                "--timeout",
                str(timeout_seconds),
                "stdio",
                "repl",
                "--",
                str(self.dosbox_x),
                "-control-stdio",
                "-headless",
                "-noconfig",
                "-noautoexec",
            ],
            input=commands,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds + 5,
            check=False,
        )
        events = self.parse_events(proc)
        return proc, events

    def run_socket_repl(self, commands, timeout_seconds=10):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "control.sock"
            stdout_path = Path(tmpdir) / "server.stdout"
            stderr_path = Path(tmpdir) / "server.stderr"
            with stdout_path.open("w", encoding="utf-8") as server_stdout, stderr_path.open(
                "w", encoding="utf-8"
            ) as server_stderr:
                server = subprocess.Popen(
                    [
                        str(self.dosbox_x),
                        "-control-socket",
                        str(sock_path),
                        "-headless",
                        "-noconfig",
                        "-noautoexec",
                    ],
                    stdout=server_stdout,
                    stderr=server_stderr,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + timeout_seconds
                    while time.monotonic() < deadline and not sock_path.exists():
                        if server.poll() is not None:
                            server_stdout.flush()
                            server_stderr.flush()
                            self.fail(
                                "server exited before creating socket\n"
                                + self.read_server_diagnostics(stdout_path, stderr_path, server)
                            )
                        time.sleep(0.05)
                    if not sock_path.exists():
                        server_stdout.flush()
                        server_stderr.flush()
                        self.fail(
                            "socket was not created\n"
                            + self.read_server_diagnostics(stdout_path, stderr_path, server)
                        )

                    proc = subprocess.run(
                        [
                            sys.executable,
                            str(CLIENT),
                            "--timeout",
                            str(timeout_seconds),
                            "socket",
                            str(sock_path),
                            "repl",
                        ],
                        input=commands,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=timeout_seconds + 5,
                        check=False,
                    )
                    try:
                        server.wait(timeout=timeout_seconds)
                    except subprocess.TimeoutExpired:
                        server.terminate()
                        try:
                            server.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            server.kill()
                            server.wait()
                        server_stdout.flush()
                        server_stderr.flush()
                        self.fail(
                            "server did not exit after client completed\n"
                            + self.read_server_diagnostics(stdout_path, stderr_path, server)
                        )
                finally:
                    if server.poll() is None:
                        server.terminate()
                        try:
                            server.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            server.kill()
                            server.wait()
                    server_stdout.flush()
                    server_stderr.flush()

        events = self.parse_events(proc)
        return proc, events

    def test_exec_mount_returns_result_event(self):
        with tempfile.TemporaryDirectory() as mount_dir:
            proc, events = self.run_stdio_repl(
                f"exec mount c {mount_dir}\nquit\n",
                timeout_seconds=10,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        results = [event for event in events if event.get("event") == "result"]
        self.assertEqual(len(results), 1, proc.stdout)
        self.assertEqual(results[0].get("id"), "1")
        self.assertTrue(results[0].get("ok"), proc.stdout)

    def test_socket_input_text_runs_dir_after_prompt(self):
        proc, events = self.run_socket_repl(
            "input dir\nkey enter\nquit\n",
            timeout_seconds=10,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        input_results = [event for event in events if event.get("event") == "input_result"]
        self.assertEqual(len(input_results), 2, proc.stdout)
        self.assertEqual([event.get("id") for event in input_results], ["1", "2"], proc.stdout)

    def test_socket_exec_dir_returns_output_event(self):
        proc, events = self.run_socket_repl(
            "exec dir\nquit\n",
            timeout_seconds=10,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(any(event.get("event") == "output" for event in events), proc.stdout)


if __name__ == "__main__":
    unittest.main()
