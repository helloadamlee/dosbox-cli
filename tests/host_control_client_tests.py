import importlib.util
import io
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT = REPO_ROOT / "scripts" / "host_control_client.py"


def load_client_module():
    spec = importlib.util.spec_from_file_location("host_control_client", CLIENT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HostControlClientTest(unittest.TestCase):
    def _serve_socket_once(self, sock_path, response_lines, requests):
        def serve():
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)
            conn, _ = server.accept()
            with conn, server:
                for line in response_lines:
                    conn.sendall(line.encode("utf-8"))
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                requests.append(data.decode("utf-8"))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return thread

    def _serve_socket_hanging_after_lines(self, sock_path, response_lines, requests):
        ready = threading.Event()

        def serve():
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            with conn, server:
                self.assertGreaterEqual(len(response_lines), 1)
                conn.sendall(response_lines[0].encode("utf-8"))
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                requests.append(data.decode("utf-8"))
                for line in response_lines[1:]:
                    conn.sendall(line.encode("utf-8"))
                threading.Event().wait(0.5)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(2.0))
        return thread

    def test_socket_status_one_shot_preserves_raw_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "control.sock")
            requests = []
            lines = [
                '{"event":"ready","transport":"socket","endpoint":"%s"}\n' % sock_path,
                '{"event":"status","id":"1","transport":"socket","session_active":true,"errorlevel":0,"drive":"Z","cwd":"Z:\\\\"}\n',
            ]
            thread = self._serve_socket_once(sock_path, lines, requests)

            proc = subprocess.run(
                [sys.executable, str(CLIENT), "socket", sock_path, "status"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            thread.join(timeout=2)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "".join(lines))
            self.assertEqual(requests, ['{"id":"1","op":"status"}\n'])

    def test_socket_exec_waits_for_result_after_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "control.sock")
            requests = []
            lines = [
                '{"event":"ready","transport":"socket","endpoint":"%s"}\n' % sock_path,
                '{"event":"output","id":"1","encoding":"base64","data":"aGkNCg=="}\n',
                '{"event":"result","id":"1","ok":true,"shell_exit":false,"errorlevel":0,"drive":"Z","cwd":"Z:\\\\","duration_ms":1}\n',
            ]
            thread = self._serve_socket_once(sock_path, lines, requests)

            proc = subprocess.run(
                [sys.executable, str(CLIENT), "socket", sock_path, "exec", "echo hi"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            thread.join(timeout=2)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "".join(lines))
            self.assertEqual(requests, ['{"id":"1","op":"exec","command":"echo hi"}\n'])

    def test_socket_input_text_sends_request_and_waits_for_input_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "control.sock")
            requests = []
            lines = [
                '{"event":"ready","transport":"socket"}\n',
                '{"event":"input_result","id":"1","ok":true,"queued":4}\n',
            ]
            thread = self._serve_socket_once(sock_path, lines, requests)

            proc = subprocess.run(
                [sys.executable, str(CLIENT), "socket", sock_path, "input-text", "dir\n"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            thread.join(timeout=2)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "".join(lines))
            self.assertEqual(requests, ['{"id":"1","op":"input_text","text":"dir\\n"}\n'])

    def test_socket_key_sends_request_and_waits_for_input_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "control.sock")
            requests = []
            lines = [
                '{"event":"ready","transport":"socket"}\n',
                '{"event":"input_result","id":"1","ok":true,"queued":1}\n',
            ]
            thread = self._serve_socket_once(sock_path, lines, requests)

            proc = subprocess.run(
                [sys.executable, str(CLIENT), "socket", sock_path, "key", "enter"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            thread.join(timeout=2)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "".join(lines))
            self.assertEqual(requests, ['{"id":"1","op":"key","key":"enter"}\n'])

    def test_stdio_status_spawns_child_and_reads_ready_then_status(self):
        stub = textwrap.dedent(
            """
            import sys
            sys.stdout.write('{"event":"ready","transport":"stdio"}\\n')
            sys.stdout.flush()
            request = sys.stdin.readline()
            assert request == '{"id":"1","op":"status"}\\n', request
            sys.stdout.write('{"event":"status","id":"1","transport":"stdio","session_active":true,"errorlevel":0,"drive":"Z","cwd":"Z:\\\\\\\\"}\\n')
            sys.stdout.flush()
            sys.stdin.read()
            """
        )

        proc = subprocess.run(
            [
                sys.executable,
                str(CLIENT),
                "stdio",
                "status",
                "--",
                sys.executable,
                "-c",
                stub,
                "-control-stdio",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            '{"event":"ready","transport":"stdio"}\n'
            '{"event":"status","id":"1","transport":"stdio","session_active":true,"errorlevel":0,"drive":"Z","cwd":"Z:\\\\"}\n',
        )

    def test_stdio_exec_reads_result_buffered_after_output(self):
        stub = textwrap.dedent(
            """
            import os
            import sys
            import time

            os.write(sys.stdout.fileno(), b'{"event":"ready","transport":"stdio"}\\n')
            request = sys.stdin.readline()
            assert request == '{"id":"1","op":"exec","command":"echo hi"}\\n', request
            os.write(
                sys.stdout.fileno(),
                b'{"event":"output","id":"1","encoding":"base64","data":"aGkNCg=="}\\n'
                b'{"event":"result","id":"1","ok":true,"shell_exit":false,"errorlevel":0,"drive":"Z","cwd":"Z:\\\\\\\\","duration_ms":1}\\n',
            )
            sys.stdout.flush()
            time.sleep(1.0)
            """
        )

        proc = subprocess.run(
            [
                sys.executable,
                str(CLIENT),
                "--timeout",
                "0.1",
                "stdio",
                "exec",
                "echo hi",
                "--",
                sys.executable,
                "-c",
                stub,
                "-control-stdio",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            '{"event":"ready","transport":"stdio"}\n'
            '{"event":"output","id":"1","encoding":"base64","data":"aGkNCg=="}\n'
            '{"event":"result","id":"1","ok":true,"shell_exit":false,"errorlevel":0,"drive":"Z","cwd":"Z:\\\\","duration_ms":1}\n',
        )

    def test_repl_command_parser(self):
        module = load_client_module()

        self.assertEqual(module.parse_repl_command("status"), ("status", None))
        self.assertEqual(module.parse_repl_command("  status  "), ("status", None))
        self.assertEqual(module.parse_repl_command("exec dir"), ("exec", "dir"))
        self.assertEqual(module.parse_repl_command("input dir"), ("input_text", "dir"))
        self.assertEqual(module.parse_repl_command("input dir  \n"), ("input_text", "dir  "))
        self.assertEqual(module.parse_repl_command("input  dir"), ("input_text", " dir"))
        self.assertEqual(module.parse_repl_command("key enter"), ("key", "enter"))
        self.assertEqual(module.parse_repl_command("quit"), ("quit", None))
        self.assertEqual(module.parse_repl_command("help"), ("help", None))
        self.assertIsNone(module.parse_repl_command(""))

    def test_parse_timeout_option(self):
        module = load_client_module()

        args = module.parse_args(["--timeout", "2.5", "socket", "/tmp/d.sock", "status"])

        self.assertEqual(args.timeout, 2.5)

    def test_parse_rejects_non_positive_timeout(self):
        proc = subprocess.run(
            [sys.executable, str(CLIENT), "--timeout", "0", "socket", "/tmp/d.sock", "status"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("timeout must be greater than zero", proc.stderr)

    def test_socket_timeout_exits_nonzero_when_request_never_completes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "control.sock")
            requests = []
            lines = ['{"event":"ready","transport":"socket"}\n']
            thread = self._serve_socket_hanging_after_lines(sock_path, lines, requests)

            proc = subprocess.run(
                [sys.executable, str(CLIENT), "--timeout", "0.1", "socket", sock_path, "status"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            thread.join(timeout=2)

            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "".join(lines))
            self.assertIn("timed out waiting for status request 1", proc.stderr)
            self.assertEqual(requests, ['{"id":"1","op":"status"}\n'])

    def test_socket_timeout_preserves_output_before_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "control.sock")
            requests = []
            lines = [
                '{"event":"ready","transport":"socket"}\n',
                '{"event":"output","id":"1","encoding":"base64","data":"aGkNCg=="}\n',
            ]
            thread = self._serve_socket_hanging_after_lines(sock_path, lines, requests)

            proc = subprocess.run(
                [sys.executable, str(CLIENT), "--timeout", "0.1", "socket", sock_path, "exec", "hang"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            thread.join(timeout=2)

            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "".join(lines))
            self.assertIn("timed out waiting for exec request 1", proc.stderr)

    def test_stdio_timeout_terminates_spawned_child(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            marker = Path(tmpdir) / "marker"
            pidfile = Path(tmpdir) / "child.pid"
            stub = textwrap.dedent(
                f"""
                import os
                import pathlib
                import signal
                import sys
                import time

                marker = pathlib.Path({str(marker)!r})
                pidfile = pathlib.Path({str(pidfile)!r})
                pidfile.write_text(str(os.getpid()))

                def handle_term(signum, frame):
                    marker.write_text("terminated")
                    raise SystemExit(0)

                signal.signal(signal.SIGTERM, handle_term)
                sys.stdout.write('{{"event":"ready","transport":"stdio"}}\\n')
                sys.stdout.flush()
                sys.stdin.readline()
                while True:
                    time.sleep(0.1)
                """
            )

            timed_out = False
            proc = None
            try:
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(CLIENT),
                        "--timeout",
                        "0.1",
                        "stdio",
                        "status",
                        "--",
                        sys.executable,
                        "-c",
                        stub,
                        "-control-stdio",
                    ],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=3.0,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                if timed_out and pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text()), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    time.sleep(0.1)

            self.assertFalse(timed_out, "client did not return after request timeout")
            self.assertIsNotNone(proc)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("timed out waiting for status request 1", proc.stderr)
            self.assertEqual(marker.read_text(), "terminated")

    def test_repl_socket_timeout_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "control.sock")
            requests = []
            lines = ['{"event":"ready","transport":"socket"}\n']
            thread = self._serve_socket_hanging_after_lines(sock_path, lines, requests)

            proc = subprocess.run(
                [sys.executable, str(CLIENT), "--timeout", "0.1", "socket", sock_path, "repl"],
                cwd=REPO_ROOT,
                input="status\n",
                capture_output=True,
                text=True,
                check=False,
            )
            thread.join(timeout=2)

            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "".join(lines))
            self.assertIn("timed out waiting for status request 1", proc.stderr)
            self.assertEqual(requests, ['{"id":"1","op":"status"}\n'])

    def test_stdio_repl_rejects_input_commands_without_sending_requests(self):
        module = load_client_module()

        class FakeTransport(module.BufferedLineTransport):
            def __init__(self):
                super().__init__()
                self._read_buffer.extend(b'{"event":"ready","transport":"stdio"}\n')
                self.requests = []

            def read_bytes(self):
                return b""

            def fileno(self):
                return -1

            def writeline(self, line):
                self.requests.append(line)

        class FakeStdout:
            def __init__(self):
                self.buffer = io.BytesIO()

            def flush(self):
                pass

        transport = FakeTransport()
        old_stdin = sys.stdin
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
            sys.stdin = io.StringIO("input hi\nkey enter\nquit\n")
            sys.stdout = FakeStdout()
            sys.stderr = io.StringIO()

            result = module.run_repl(transport, allow_input=False)
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        self.assertEqual(result, 0)
        self.assertEqual(transport.requests, [])

    def test_stdio_requires_control_stdio_flag(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(CLIENT),
                "stdio",
                "status",
                "--",
                sys.executable,
                "-c",
                "print('not a host control server')",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("-control-stdio", proc.stderr)

    def test_stdio_rejects_input_text_as_socket_only(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(CLIENT),
                "stdio",
                "input-text",
                "--",
                sys.executable,
                "-c",
                "",
                "-control-stdio",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("input actions are socket-only", proc.stderr)

    def test_stdio_rejects_key_as_socket_only(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(CLIENT),
                "stdio",
                "key",
                "enter",
                "--",
                sys.executable,
                "-c",
                "",
                "-control-stdio",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("input actions are socket-only", proc.stderr)


if __name__ == "__main__":
    unittest.main()
