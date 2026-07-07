# Host Control CLI Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal host-side CLI client that drives DOSBox-X host control over stdio or Unix sockets, supports `exec`, `status`, and a simple REPL, and prints raw JSON events unchanged.

**Architecture:** Implement a small Python 3 stdlib script under `scripts/` with a shared line-oriented transport abstraction for Unix sockets and spawned stdio subprocesses. Keep stdout as a raw event stream, use stderr for local prompts and errors, and rely on the existing DOSBox-X request lifecycle to detect request completion.

**Tech Stack:** Python 3 stdlib (`argparse`, `json`, `socket`, `subprocess`, `sys`, `unittest`), existing DOSBox-X host-control protocol, existing DOSBox-X debug test binary for live verification

---

### Task 1: Add the client test harness and CLI contract tests

**Files:**
- Create: `tests/host_control_client_tests.py`
- Test: `tests/host_control_client_tests.py`

- [ ] **Step 1: Write the failing test**

Create `tests/host_control_client_tests.py` with socket, spawned-stdio, and command-parsing coverage:

```python
import json
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
    def test_socket_status_one_shot_preserves_raw_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "control.sock")
            requests = []
            lines = [
                '{"event":"ready","transport":"socket","endpoint":"%s"}\n' % sock_path,
                '{"event":"status","id":"1","transport":"socket","session_active":true,"errorlevel":0,"drive":"Z","cwd":"Z:\\\\"}\n',
            ]

            def serve():
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(sock_path)
                server.listen(1)
                conn, _ = server.accept()
                with conn, server:
                    for line in lines:
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

            def serve():
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(sock_path)
                server.listen(1)
                conn, _ = server.accept()
                with conn, server:
                    for line in lines:
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
        import importlib.util

        spec = importlib.util.spec_from_file_location("host_control_client", CLIENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertEqual(module.parse_repl_command("status"), ("status", None))
        self.assertEqual(module.parse_repl_command("exec dir"), ("exec", "dir"))
        self.assertEqual(module.parse_repl_command("quit"), ("quit", None))
        self.assertEqual(module.parse_repl_command("help"), ("help", None))
        self.assertIsNone(module.parse_repl_command(""))
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests -v
```

Expected: FAIL because `scripts/host_control_client.py` does not exist.

- [ ] **Step 3: Write minimal implementation**

Create the minimal script file with only enough structure to satisfy imports and a failing behavior path:

```python
#!/usr/bin/env python3

def main():
    raise SystemExit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify the failure changes shape**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests -v
```

Expected: FAIL because the client exists but does not implement the socket or stdio behavior.

- [ ] **Step 5: Commit**

```bash
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x add -- tests/host_control_client_tests.py scripts/host_control_client.py
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x commit -m "test: add host control client contract coverage"
```

### Task 2: Implement one-shot socket and stdio requests

**Files:**
- Create: `scripts/host_control_client.py`
- Modify: `tests/host_control_client_tests.py`
- Test: `tests/host_control_client_tests.py`

- [ ] **Step 1: Write the failing test**

Fill in the remaining failing tests to pin down one-shot behavior:

```python
def test_socket_exec_waits_for_result_after_output(self):
    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = str(Path(tmpdir) / "control.sock")
        requests = []
        lines = [
            '{"event":"ready","transport":"socket","endpoint":"%s"}\n' % sock_path,
            '{"event":"output","id":"1","encoding":"base64","data":"aGkNCg=="}\n',
            '{"event":"result","id":"1","ok":true,"shell_exit":false,"errorlevel":0,"drive":"Z","cwd":"Z:\\\\","duration_ms":1}\n',
        ]

        def serve():
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)
            conn, _ = server.accept()
            with conn, server:
                for line in lines:
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

        proc = subprocess.run(
            [sys.executable, str(CLIENT), "socket", sock_path, "exec", "echo hi"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        thread.join(timeout=2)

        self.assertEqual(requests, ['{"id":"1","op":"exec","command":"echo hi"}\n'])
        self.assertEqual(proc.stdout, "".join(lines))

def test_stdio_status_spawns_child_and_reads_ready_then_status(self):
    stub = textwrap.dedent(
        \"\"\"
        import sys
        sys.stdout.write('{"event":"ready","transport":"stdio"}\\n')
        sys.stdout.flush()
        request = sys.stdin.readline()
        sys.stdout.write('{"event":"status","id":"1","transport":"stdio","session_active":true,"errorlevel":0,"drive":"Z","cwd":"Z:\\\\\\\\"}\\n')
        sys.stdout.flush()
        sys.stdin.read()
        \"\"\"
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
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(proc.returncode, 0, proc.stderr)
    self.assertIn('{"event":"ready","transport":"stdio"}\n', proc.stdout)
    self.assertIn('{"event":"status","id":"1"', proc.stdout)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_socket_exec_waits_for_result_after_output tests.host_control_client_tests.HostControlClientTest.test_stdio_status_spawns_child_and_reads_ready_then_status -v
```

Expected: FAIL because the client does not yet connect, spawn, send requests, or detect request completion.

- [ ] **Step 3: Write minimal implementation**

Implement the shared one-shot client in `scripts/host_control_client.py`:

```python
import argparse
import json
import socket
import subprocess
import sys


def encode_request(request_id, op, command=None):
    payload = {"id": str(request_id), "op": op}
    if command is not None:
        payload["command"] = command
    return json.dumps(payload, separators=(",", ":"))


def event_completes_request(event, request_id, op):
    if str(event.get("id", "")) != str(request_id):
        return False
    if event.get("event") == "error":
        return True
    if op == "status":
        return event.get("event") == "status"
    return event.get("event") == "result"
```

Also add:
- a socket transport class using `socket.socket(AF_UNIX, SOCK_STREAM)`
- a stdio transport class using `subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True)`
- `run_one_shot()` that reads and prints `ready`, sends one request, then reads and prints until terminal event
- CLI parsing for:
  - `socket <path> status`
  - `socket <path> exec <command>`
  - `stdio status -- <dosbox-command>`
  - `stdio exec <command> -- <dosbox-command>`

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_socket_status_one_shot_preserves_raw_lines tests.host_control_client_tests.HostControlClientTest.test_socket_exec_waits_for_result_after_output tests.host_control_client_tests.HostControlClientTest.test_stdio_status_spawns_child_and_reads_ready_then_status -v
```

Expected: all three targeted tests pass.

- [ ] **Step 5: Commit**

```bash
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x add -- scripts/host_control_client.py tests/host_control_client_tests.py
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x commit -m "feat: add one-shot host control client"
```

### Task 3: Add the interactive REPL

**Files:**
- Modify: `scripts/host_control_client.py`
- Modify: `tests/host_control_client_tests.py`
- Test: `tests/host_control_client_tests.py`

- [ ] **Step 1: Write the failing test**

Add parser and REPL helper tests that keep stdout clean:

```python
def test_repl_command_parser(self):
    import scripts.host_control_client as client

    self.assertEqual(client.parse_repl_command("status"), ("status", None))
    self.assertEqual(client.parse_repl_command("exec dir"), ("exec", "dir"))
    self.assertEqual(client.parse_repl_command("quit"), ("quit", None))
    self.assertEqual(client.parse_repl_command("help"), ("help", None))
    self.assertIsNone(client.parse_repl_command(""))
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_repl_command_parser -v
```

Expected: FAIL because `parse_repl_command()` and REPL support do not exist.

- [ ] **Step 3: Write minimal implementation**

In `scripts/host_control_client.py`, add:

```python
def parse_repl_command(text):
    text = text.strip()
    if not text:
        return None
    if text == "status":
        return ("status", None)
    if text == "quit":
        return ("quit", None)
    if text == "help":
        return ("help", None)
    if text.startswith("exec "):
        return ("exec", text[5:])
    raise ValueError("unknown command")
```

Also add `run_repl()` that:
- reads user input from stdin
- writes prompts and local help to stderr
- sends one request at a time through the existing one-shot request helper
- exits on `quit` or EOF

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_repl_command_parser -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x add -- scripts/host_control_client.py tests/host_control_client_tests.py
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x commit -m "feat: add interactive host control repl"
```

### Task 4: Verify against the real DOSBox-X binary

**Files:**
- Modify: `scripts/host_control_client.py`
- Test: `tests/host_control_client_tests.py`

- [ ] **Step 1: Run the client test suite**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests -v
```

Expected: all client tests pass.

- [ ] **Step 2: Run the focused DOSBox-X host-control suite**

Run:

```bash
rtk /home/fld/Projects/dosbox-cli/upstream-dosbox-x/.worktrees/feat-host-control-status-op/src/dosbox-x -tests --gtest_filter='*HostControl*'
```

Expected: the existing 28 host-control tests remain green.

- [ ] **Step 3: Run a live stdio smoke**

Run:

```bash
rtk bash -lc 'cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x/.worktrees/feat-host-control-status-op && python3 scripts/host_control_client.py stdio status -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec'
```

Expected: stdout shows the raw `ready` event followed by a raw `status` event.

- [ ] **Step 4: Run a live socket smoke**

Run:

```bash
rtk bash -lc 'cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x/.worktrees/feat-host-control-status-op
sock=$(mktemp -u /tmp/dosboxx-control-XXXXXX.sock)
./src/dosbox-x -control-socket "$sock" -headless -noconfig -noautoexec >/tmp/dosboxx-client-out.log 2>/tmp/dosboxx-client-err.log &
pid=$!
for _ in $(seq 1 50); do
    [ -S "$sock" ] && break
    sleep 0.1
done
python3 scripts/host_control_client.py socket "$sock" exec "echo hi"
wait $pid'
```

Expected: stdout shows raw `ready`, `output`, and `result` lines in order.

- [ ] **Step 5: Commit**

```bash
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x add -- scripts/host_control_client.py tests/host_control_client_tests.py
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x commit -m "feat: add host control cli client"
```
