# Host Control Client Timeouts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add timeout handling to the host-control client so agents can recover locally from hung requests while preserving raw JSON event output.

**Architecture:** Keep timeout handling entirely in the Python client. Add a global `--timeout` option, wait for transport readability with `select.select()`, and add transport-specific abort behavior so stdio-spawn timeouts terminate the child while socket timeouts close the client connection.

**Tech Stack:** Python 3 stdlib (`argparse`, `json`, `select`, `socket`, `subprocess`, `sys`, `time`, `unittest`), existing DOSBox-X host-control protocol tests

---

### Task 1: Add timeout parser and completion tests

**Files:**
- Modify: `tests/host_control_client_tests.py`
- Modify: `scripts/host_control_client.py`

- [ ] **Step 1: Write failing timeout parser tests**

Add import support and parser tests to `tests/host_control_client_tests.py`:

```python
def load_client_module():
    spec = importlib.util.spec_from_file_location("host_control_client", CLIENT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
```

Update `test_repl_command_parser()` to use `load_client_module()`, then add:

```python
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
```

- [ ] **Step 2: Run parser tests to verify red**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_parse_timeout_option tests.host_control_client_tests.HostControlClientTest.test_parse_rejects_non_positive_timeout -v
```

Expected: failure because `--timeout` is not accepted.

- [ ] **Step 3: Implement minimal parser support**

In `scripts/host_control_client.py`, add `--timeout` before subparsers:

```python
parser.add_argument("--timeout", type=float, default=None,
                    help="seconds to wait for each host-control response")
```

After parsing, reject non-positive values:

```python
if args.timeout is not None and args.timeout <= 0:
    parser.error("timeout must be greater than zero")
```

- [ ] **Step 4: Run parser tests to verify green**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_parse_timeout_option tests.host_control_client_tests.HostControlClientTest.test_parse_rejects_non_positive_timeout -v
```

Expected: both tests pass.

### Task 2: Add timeout-aware reads

**Files:**
- Modify: `tests/host_control_client_tests.py`
- Modify: `scripts/host_control_client.py`

- [ ] **Step 1: Write failing socket timeout tests**

Add a helper that can keep a socket connection open after sending response lines:

```python
def _serve_socket_hanging_after_lines(self, sock_path, response_lines, requests):
    ready = threading.Event()

    def serve():
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        ready.set()
        conn, _ = server.accept()
        with conn, server:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            requests.append(data.decode("utf-8"))
            for line in response_lines:
                conn.sendall(line.encode("utf-8"))
            threading.Event().wait(2.0)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    self.assertTrue(ready.wait(2.0))
    return thread
```

Add tests:

```python
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
```

- [ ] **Step 2: Run socket timeout tests to verify red**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_socket_timeout_exits_nonzero_when_request_never_completes tests.host_control_client_tests.HostControlClientTest.test_socket_timeout_preserves_output_before_timeout -v
```

Expected: failure because the client blocks until the server closes instead of timing out.

- [ ] **Step 3: Implement timeout-aware read path**

In `scripts/host_control_client.py`, import `select` and `time`:

```python
import select
import time
```

Add:

```python
class RequestTimeout(RuntimeError):
    pass
```

Add `fileno()` to each transport:

```python
def fileno(self):
    return self.reader.fileno()
```

For stdio:

```python
def fileno(self):
    assert self.process.stdout is not None
    return self.process.stdout.fileno()
```

Replace `read_event_line(transport)` with timeout-aware helpers:

```python
def make_deadline(timeout):
    if timeout is None:
        return None
    return time.monotonic() + timeout

def remaining_seconds(deadline):
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())

def wait_for_readable(transport, deadline, description):
    remaining = remaining_seconds(deadline)
    if remaining is not None and remaining <= 0:
        raise RequestTimeout(f"timed out waiting for {description}")
    readable, _, _ = select.select([transport.fileno()], [], [], remaining)
    if not readable:
        raise RequestTimeout(f"timed out waiting for {description}")
```

Then:

```python
def read_event_line(transport, deadline=None, description="event"):
    wait_for_readable(transport, deadline, description)
    line = transport.readline()
    if not line:
        raise RuntimeError("unexpected EOF from host control transport")
    sys.stdout.write(line)
    sys.stdout.flush()
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError("received invalid JSON event") from exc
```

Update `run_request()` to create one deadline per request and pass descriptions:

```python
def run_request(transport, request_id, op, command=None, timeout=None):
    deadline = make_deadline(timeout)
    transport.writeline(encode_request(request_id, op, command))
    while True:
        event = read_event_line(transport, deadline, f"{op} request {request_id}")
        if event_completes_request(event, request_id, op):
            return 0
```

Update `run_one_shot()` and `run_repl()` to pass `timeout`.

- [ ] **Step 4: Run socket timeout tests to verify green**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_socket_timeout_exits_nonzero_when_request_never_completes tests.host_control_client_tests.HostControlClientTest.test_socket_timeout_preserves_output_before_timeout -v
```

Expected: both tests pass.

### Task 3: Add stdio abort behavior

**Files:**
- Modify: `tests/host_control_client_tests.py`
- Modify: `scripts/host_control_client.py`

- [ ] **Step 1: Write failing stdio timeout test**

Add:

```python
def test_stdio_timeout_terminates_spawned_child(self):
    marker = Path(tempfile.gettempdir()) / "dosboxx-host-control-timeout-marker"
    if marker.exists():
        marker.unlink()
    stub = textwrap.dedent(
        f"""
        import pathlib
        import signal
        import sys
        import time

        marker = pathlib.Path({str(marker)!r})

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
    )

    self.assertNotEqual(proc.returncode, 0)
    self.assertIn("timed out waiting for status request 1", proc.stderr)
    self.assertEqual(marker.read_text(), "terminated")
    marker.unlink()
```

- [ ] **Step 2: Run stdio timeout test to verify red**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_stdio_timeout_terminates_spawned_child -v
```

Expected: failure because the child is not explicitly terminated on timeout.

- [ ] **Step 3: Implement transport aborts**

In `scripts/host_control_client.py`, add `abort()` methods.

Socket:

```python
def abort(self):
    self.close()
```

Stdio:

```python
def abort(self):
    try:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
    except BrokenPipeError:
        pass
    if self.process.poll() is None:
        self.process.terminate()
        try:
            self.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
```

Make `StdioTransport.close()` avoid blocking forever:

```python
def close(self):
    try:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
    finally:
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.abort()
        if self.process.stdout is not None and not self.process.stdout.closed:
            self.process.stdout.close()
```

Update `main()` so timeout errors abort the transport:

```python
    except RequestTimeout as exc:
        print(str(exc), file=sys.stderr)
        transport.abort()
        return 1
```

Ensure the `finally` block only calls `close()` when the transport has not already been aborted, for example with a local `aborted = False` flag.

- [ ] **Step 4: Run stdio timeout test to verify green**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_stdio_timeout_terminates_spawned_child -v
```

Expected: test passes.

### Task 4: Wire timeout through CLI modes and docs

**Files:**
- Modify: `scripts/host_control_client.py`
- Modify: `tests/host_control_client_tests.py`
- Modify: `docs/host-control.md`

- [ ] **Step 1: Write or update REPL timeout expectations**

Add:

```python
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
```

- [ ] **Step 2: Run REPL timeout test to verify behavior**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_repl_socket_timeout_exits_nonzero -v
```

Expected: pass after Task 2, or fail if `run_repl()` was not wired correctly. If it fails, pass `args.timeout` through `run_repl()`.

- [ ] **Step 3: Update docs**

In `docs/host-control.md`, add examples:

```bash
scripts/host_control_client.py --timeout 5 socket /tmp/dosboxx.sock exec "echo hi"
scripts/host_control_client.py --timeout 5 stdio status -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec
```

Add timeout behavior to the limits section:

```markdown
`--timeout` is a client-side recovery feature. In stdio mode the client owns the spawned DOSBox-X process and terminates it on timeout. In socket mode the client closes its socket; DOSBox-X may continue the current DOS command until it returns because the server currently executes requests synchronously.
```

- [ ] **Step 4: Run full client tests**

Run:

```bash
rtk python3 -m unittest tests/host_control_client_tests.py -v
```

Expected: all client tests pass.

### Task 5: Final verification and commit

**Files:**
- Modify: `scripts/host_control_client.py`
- Modify: `tests/host_control_client_tests.py`
- Modify: `docs/host-control.md`

- [ ] **Step 1: Run focused host-control protocol tests**

Run:

```bash
rtk ./src/dosbox-x -tests --gtest_filter='*HostControl*'
```

Expected: 28 host-control tests pass.

- [ ] **Step 2: Run live timeout-compatible smoke**

Run:

```bash
rtk bash -lc 'cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x/.worktrees/feat-host-control-status-op && scripts/host_control_client.py --timeout 5 stdio status -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec'
```

Expected: raw `ready` and `status` JSON lines print and the command exits 0.

- [ ] **Step 3: Check worktree**

Run:

```bash
rtk git status --short
```

Expected: only intended files are modified.

- [ ] **Step 4: Commit implementation**

Run:

```bash
rtk git add scripts/host_control_client.py tests/host_control_client_tests.py docs/host-control.md
rtk git commit -m "feat: add host control client timeouts"
```

Expected: commit succeeds.
