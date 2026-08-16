"""Opt-in end-to-end test against a real dosbox-x binary.

Mirrors the gating pattern in tests/host_control_live_tests.py at the repo
root: skipped unless explicitly enabled, since it needs a real binary and
takes longer than the unit suite.
"""

import os
import tempfile
import unittest

from dosbox_mcp import server

LIVE = os.environ.get("DOSBOX_MCP_LIVE_TESTS") == "1"
BINARY = os.environ.get("DOSBOX_X_BINARY")


@unittest.skipUnless(LIVE and BINARY, "set DOSBOX_MCP_LIVE_TESTS=1 and DOSBOX_X_BINARY")
class LiveIntegrationTest(unittest.TestCase):
    def setUp(self):
        server._session = None

    def tearDown(self):
        if server._session is not None:
            server.stop_session(force=True)

    def test_start_exec_poll_stop_round_trip(self):
        with tempfile.TemporaryDirectory() as cwd:
            start_result = server.start_session(cwd=cwd, binary_path=BINARY)
            self.assertTrue(start_result["session_active"])
            self.assertIsInstance(start_result["pid"], int)

            server.exec_command(command="ver")

            output = ""
            ok = None
            errorlevel = None
            for _ in range(30):  # up to ~30s of bounded polling
                result = server.poll(wait_seconds=1.0)
                output += result["output"]
                if result["done"]:
                    ok = result["ok"]
                    errorlevel = result["errorlevel"]
                    break

            self.assertTrue(ok, f"exec did not complete cleanly; output={output!r}")
            self.assertEqual(errorlevel, 0)
            self.assertTrue(output.strip(), "expected some output from 'ver'")

            stop_result = server.stop_session()
            self.assertEqual(stop_result, {"stopped": True})


if __name__ == "__main__":
    unittest.main()
