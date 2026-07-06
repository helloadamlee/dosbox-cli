import json
import os
import subprocess
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
