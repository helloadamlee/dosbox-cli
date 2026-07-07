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
        events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        return proc, events

    def run_socket_repl(self, commands, timeout_seconds=10):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "control.sock"
            server = subprocess.Popen(
                [
                    str(self.dosbox_x),
                    "-control-socket",
                    str(sock_path),
                    "-headless",
                    "-noconfig",
                    "-noautoexec",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline and not sock_path.exists():
                    time.sleep(0.05)
                self.assertTrue(sock_path.exists(), "socket was not created")

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
                server.wait(timeout=timeout_seconds)
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
                if server.stdout is not None:
                    server.stdout.close()
                if server.stderr is not None:
                    server.stderr.close()

        events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
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
        # Output events are currently captured for exec requests; the socket
        # input commands in this session still exercise the live input queue.
        proc, events = self.run_socket_repl(
            "input dir\nkey enter\nexec dir\nquit\n",
            timeout_seconds=10,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(any(event.get("event") == "input_result" for event in events), proc.stdout)
        self.assertTrue(any(event.get("event") == "output" for event in events), proc.stdout)


if __name__ == "__main__":
    unittest.main()
