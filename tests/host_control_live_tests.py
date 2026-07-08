import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT = REPO_ROOT / "scripts" / "host_control_client.py"
DEFAULT_DOSBOX_X = REPO_ROOT / "src" / "dosbox-x"


@dataclass
class ReplResult:
    proc: subprocess.CompletedProcess
    events: list
    server_stdout: str = ""
    server_stderr: str = ""

    def diagnostics(self):
        details = [
            f"client return code: {self.proc.returncode}",
            f"client stdout:\n{self.proc.stdout}",
            f"client stderr:\n{self.proc.stderr}",
        ]
        if self.server_stdout or self.server_stderr:
            details.extend(
                [
                    f"server stdout:\n{self.server_stdout}",
                    f"server stderr:\n{self.server_stderr}",
                ]
            )
        return "\n".join(details)


class HostControlLiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("DOSBOX_X_LIVE_TESTS") != "1":
            raise unittest.SkipTest("set DOSBOX_X_LIVE_TESTS=1 to run live DOSBox-X tests")

        cls.dosbox_x = Path(os.environ.get("DOSBOX_X_BINARY", DEFAULT_DOSBOX_X))
        if not cls.dosbox_x.exists():
            raise unittest.SkipTest(f"DOSBox-X binary not found: {cls.dosbox_x}")

    def parse_events(self, proc, server_stdout="", server_stderr=""):
        events = []
        for line_number, line in enumerate(proc.stdout.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                result = ReplResult(proc, events, server_stdout, server_stderr)
                self.fail(
                    "failed to parse JSON event line "
                    f"{line_number}: {line!r}\n"
                    f"parse error: {exc}\n"
                    f"{result.diagnostics()}"
                )
        return events

    def read_server_logs(self, stdout_path, stderr_path):
        server_stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        server_stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        return server_stdout, server_stderr

    def server_diagnostics(self, stdout_path, stderr_path, server):
        server_stdout, server_stderr = self.read_server_logs(stdout_path, stderr_path)
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
        return ReplResult(proc, events)

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
                                + self.server_diagnostics(stdout_path, stderr_path, server)
                            )
                        time.sleep(0.05)
                    if not sock_path.exists():
                        server_stdout.flush()
                        server_stderr.flush()
                        self.fail(
                            "socket was not created\n"
                            + self.server_diagnostics(stdout_path, stderr_path, server)
                        )

                    try:
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
                    except subprocess.TimeoutExpired as exc:
                        server_stdout.flush()
                        server_stderr.flush()
                        self.fail(
                            f"client timed out after {exc.timeout} seconds\n"
                            f"client stdout:\n{exc.stdout or ''}\n"
                            f"client stderr:\n{exc.stderr or ''}\n"
                            + self.server_diagnostics(stdout_path, stderr_path, server)
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
                server_stdout_text, server_stderr_text = self.read_server_logs(
                    stdout_path, stderr_path
                )

        events = self.parse_events(proc, server_stdout_text, server_stderr_text)
        return ReplResult(proc, events, server_stdout_text, server_stderr_text)

    def run_socket_workflow(self, recipe, timeout_seconds=10):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "control.sock"
            recipe_path = Path(tmpdir) / "recipe.json"
            recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
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
                                + self.server_diagnostics(stdout_path, stderr_path, server)
                            )
                        time.sleep(0.05)
                    if not sock_path.exists():
                        server_stdout.flush()
                        server_stderr.flush()
                        self.fail(
                            "socket was not created\n"
                            + self.server_diagnostics(stdout_path, stderr_path, server)
                        )

                    try:
                        proc = subprocess.run(
                            [
                                sys.executable,
                                str(CLIENT),
                                "--timeout",
                                str(timeout_seconds),
                                "socket",
                                str(sock_path),
                                "workflow",
                                str(recipe_path),
                            ],
                            text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=timeout_seconds + 5,
                            check=False,
                        )
                    except subprocess.TimeoutExpired as exc:
                        server_stdout.flush()
                        server_stderr.flush()
                        self.fail(
                            f"client timed out after {exc.timeout} seconds\n"
                            f"client stdout:\n{exc.stdout or ''}\n"
                            f"client stderr:\n{exc.stderr or ''}\n"
                            + self.server_diagnostics(stdout_path, stderr_path, server)
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
                server_stdout_text, server_stderr_text = self.read_server_logs(
                    stdout_path, stderr_path
                )

        events = self.parse_events(proc, server_stdout_text, server_stderr_text)
        return ReplResult(proc, events, server_stdout_text, server_stderr_text)

    def test_exec_mount_returns_result_event(self):
        with tempfile.TemporaryDirectory() as mount_dir:
            result = self.run_stdio_repl(
                f"exec mount c {mount_dir}\nquit\n",
                timeout_seconds=10,
            )

        proc = result.proc
        events = result.events
        self.assertEqual(proc.returncode, 0, proc.stderr)
        results = [event for event in events if event.get("event") == "result"]
        self.assertEqual(len(results), 1, proc.stdout)
        self.assertEqual(results[0].get("id"), "1")
        self.assertTrue(results[0].get("ok"), proc.stdout)

    def test_socket_input_text_runs_dir_after_prompt(self):
        result = self.run_socket_repl(
            "input dir\nkey enter\nquit\n",
            timeout_seconds=10,
        )

        self.assertEqual(result.proc.returncode, 0, result.diagnostics())
        events = result.events
        input_results = [event for event in events if event.get("event") == "input_result"]
        self.assertEqual(len(input_results), 2, result.diagnostics())
        self.assertEqual(
            [event.get("id") for event in input_results], ["1", "2"], result.diagnostics()
        )

    def test_socket_exec_dir_returns_output_event(self):
        result = self.run_socket_repl(
            "exec dir\nquit\n",
            timeout_seconds=10,
        )

        self.assertEqual(result.proc.returncode, 0, result.diagnostics())
        self.assertTrue(
            any(event.get("event") == "output" for event in result.events), result.diagnostics()
        )

    def test_socket_workflow_interactive_dir_streams_output(self):
        result = self.run_socket_workflow(
            {
                "steps": [
                    {
                        "exec_interactive": {
                            "command": "dir",
                            "steps": [
                                {"wait_for": "output"},
                                {"wait_for": {"event": "result", "ok": True}},
                            ],
                        }
                    }
                ]
            },
            timeout_seconds=10,
        )

        self.assertEqual(result.proc.returncode, 0, result.diagnostics())
        self.assertTrue(
            any(event.get("event") == "output" for event in result.events), result.diagnostics()
        )
        self.assertTrue(
            any(event.get("event") == "result" and event.get("ok") for event in result.events),
            result.diagnostics(),
        )


if __name__ == "__main__":
    unittest.main()
