import importlib.util
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT = REPO_ROOT / "scripts" / "host_control_client.py"


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

    def test_repl_command_parser(self):
        spec = importlib.util.spec_from_file_location("host_control_client", CLIENT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertEqual(module.parse_repl_command("status"), ("status", None))
        self.assertEqual(module.parse_repl_command("exec dir"), ("exec", "dir"))
        self.assertEqual(module.parse_repl_command("quit"), ("quit", None))
        self.assertEqual(module.parse_repl_command("help"), ("help", None))
        self.assertIsNone(module.parse_repl_command(""))

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


if __name__ == "__main__":
    unittest.main()
