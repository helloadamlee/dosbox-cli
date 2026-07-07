# Host Control Workflow Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a JSON-only workflow runner to the Python host-control client.

**Architecture:** Keep the runner inside `scripts/host_control_client.py` so it can reuse the existing transports, request encoding, completion detection, and timeout behavior. Add small parser, matcher, transcript, and workflow execution helpers with focused tests in `tests/host_control_client_tests.py`; update `docs/host-control.md` after behavior is covered.

**Tech Stack:** Python 3 standard library, `unittest`, fake Unix socket and stdio servers, existing DOSBox-X host-control JSON protocol.

---

## File Structure

- Modify `scripts/host_control_client.py`: add workflow parse/validation helpers, event recorder, transcript writer, wait matchers, workflow execution, CLI options, and workflow action dispatch.
- Modify `tests/host_control_client_tests.py`: add unit and subprocess tests for recipe parsing, request sequencing, waits, diagnostics, and transcript output.
- Modify `tests/host_control_live_tests.py`: optional gated live workflow smoke if practical after unit coverage.
- Modify `docs/host-control.md`: document JSON recipes, CLI usage, transcript output, stdio limits, and failure behavior.

---

### Task 1: Recipe Parsing And CLI Shape

**Files:**
- Modify: `scripts/host_control_client.py`
- Test: `tests/host_control_client_tests.py`

- [ ] **Step 1: Write failing parser and CLI tests**

Add tests to `HostControlClientTest`:

```python
def test_parse_workflow_recipe_accepts_supported_steps(self):
    module = load_client_module()
    recipe = {
        "steps": [
            {"comment": "mount"},
            {"exec": "mount c /tmp/project"},
            {"wait_for": {"event": "result", "ok": True}},
            {"status": True},
            {"input_text": "dir\n"},
            {"key": "enter"},
            {"wait_for": "input_result"},
            {},
        ]
    }

    steps = module.parse_workflow_recipe(recipe)

    self.assertEqual([step.action for step in steps], [
        "comment",
        "exec",
        "wait_for",
        "status",
        "input_text",
        "key",
        "wait_for",
        "noop",
    ])
    self.assertEqual(steps[1].value, "mount c /tmp/project")
    self.assertEqual(steps[2].value, {"event": "result", "ok": True})

def test_parse_workflow_recipe_rejects_unknown_or_ambiguous_steps(self):
    module = load_client_module()

    with self.assertRaisesRegex(module.WorkflowError, "step 0: unknown action"):
        module.parse_workflow_recipe({"steps": [{"sleep": 1}]})
    with self.assertRaisesRegex(module.WorkflowError, "step 0: multiple actions"):
        module.parse_workflow_recipe({"steps": [{"exec": "dir", "status": True}]})
    with self.assertRaisesRegex(module.WorkflowError, "step 0: expected object"):
        module.parse_workflow_recipe({"steps": ["exec dir"]})

def test_parse_args_accepts_workflow_and_transcript(self):
    module = load_client_module()

    args = module.parse_args([
        "--timeout", "2.5",
        "--transcript", "run.jsonl",
        "socket", "/tmp/d.sock", "workflow", "recipe.json",
    ])

    self.assertEqual(args.timeout, 2.5)
    self.assertEqual(args.transcript, "run.jsonl")
    self.assertEqual(args.transport, "socket")
    self.assertEqual(args.action, "workflow")
    self.assertEqual(args.command, "recipe.json")
```

- [ ] **Step 2: Verify RED**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_parse_workflow_recipe_accepts_supported_steps tests.host_control_client_tests.HostControlClientTest.test_parse_workflow_recipe_rejects_unknown_or_ambiguous_steps tests.host_control_client_tests.HostControlClientTest.test_parse_args_accepts_workflow_and_transcript
```

Expected: tests fail because `WorkflowError`, `parse_workflow_recipe`, and workflow CLI support do not exist.

- [ ] **Step 3: Implement minimal parser and CLI support**

In `scripts/host_control_client.py`, add:

```python
from dataclasses import dataclass
```

Add after `RequestTimeout`:

```python
class WorkflowError(RuntimeError):
    pass


@dataclass
class WorkflowStep:
    action: str
    value: object = None
```

Add `parse_workflow_recipe()` with these validation rules:

```python
WORKFLOW_ACTIONS = {"comment", "exec", "status", "input_text", "key", "wait_for"}


def parse_workflow_recipe(recipe):
    if not isinstance(recipe, dict):
        raise WorkflowError("recipe: expected object")
    steps = recipe.get("steps")
    if not isinstance(steps, list):
        raise WorkflowError("recipe.steps: expected array")

    parsed = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise WorkflowError(f"step {index}: expected object")
        if not step:
            parsed.append(WorkflowStep("noop"))
            continue

        actions = [key for key in step if key in WORKFLOW_ACTIONS]
        unknown = [key for key in step if key not in WORKFLOW_ACTIONS]
        if unknown:
            raise WorkflowError(f"step {index}: unknown action {unknown[0]}")
        if len(actions) != 1:
            raise WorkflowError(f"step {index}: multiple actions")

        action = actions[0]
        value = step[action]
        if action == "comment":
            if not isinstance(value, str):
                raise WorkflowError(f"step {index}: comment must be a string")
        elif action == "exec":
            if not isinstance(value, str) or not value:
                raise WorkflowError(f"step {index}: exec must be a non-empty string")
        elif action == "status":
            if value not in (True, None) and value != {}:
                raise WorkflowError(f"step {index}: status must be true, null, or object")
        elif action == "input_text":
            if not isinstance(value, str):
                raise WorkflowError(f"step {index}: input_text must be a string")
        elif action == "key":
            if not isinstance(value, str) or not value:
                raise WorkflowError(f"step {index}: key must be a non-empty string")
        elif action == "wait_for":
            if not isinstance(value, (str, dict)):
                raise WorkflowError(f"step {index}: wait_for must be a string or object")
        parsed.append(WorkflowStep(action, value))
    return parsed
```

Extend `parse_args()`:

- add top-level `--transcript`
- include `"workflow"` in socket and stdio action choices
- allow workflow to use the existing `command` positional as the recipe path
- require a recipe path for workflow
- keep existing stdio spawn-command normalization for workflow

- [ ] **Step 4: Verify GREEN**

Run the three tests from Step 2 again.

Expected: all three tests pass.

- [ ] **Step 5: Commit**

```bash
rtk git add scripts/host_control_client.py tests/host_control_client_tests.py
rtk git commit -m "feat: parse host control workflow recipes"
```

---

### Task 2: Workflow Request Execution

**Files:**
- Modify: `scripts/host_control_client.py`
- Test: `tests/host_control_client_tests.py`

- [ ] **Step 1: Write failing socket workflow test**

Add a helper that writes a temporary recipe and a subprocess test:

```python
def _write_recipe(self, tmpdir, recipe):
    path = Path(tmpdir) / "recipe.json"
    path.write_text(json.dumps(recipe), encoding="utf-8")
    return path

def test_socket_workflow_runs_requests_in_sequence(self):
    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = str(Path(tmpdir) / "control.sock")
        recipe_path = self._write_recipe(tmpdir, {
            "steps": [
                {"comment": "run dir"},
                {"exec": "dir"},
                {"status": True},
                {"input_text": "dir\n"},
                {"key": "enter"},
                {},
            ]
        })
        requests = []
        lines = [
            '{"event":"ready","transport":"socket"}\n',
            '{"event":"result","id":"1","ok":true,"shell_exit":false,"errorlevel":0,"drive":"Z","cwd":"Z:\\\\","duration_ms":1}\n',
            '{"event":"status","id":"2","transport":"socket","session_active":true,"errorlevel":0,"drive":"Z","cwd":"Z:\\\\"}\n',
            '{"event":"input_result","id":"3","ok":true,"queued":4}\n',
            '{"event":"input_result","id":"4","ok":true,"queued":1}\n',
        ]
        thread = self._serve_socket_workflow(sock_path, lines, requests, expected_requests=4)

        proc = subprocess.run(
            [sys.executable, str(CLIENT), "socket", sock_path, "workflow", str(recipe_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        thread.join(timeout=2)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "".join(lines))
        self.assertEqual(requests, [
            '{"id":"1","op":"exec","command":"dir"}\n',
            '{"id":"2","op":"status"}\n',
            '{"id":"3","op":"input_text","text":"dir\\n"}\n',
            '{"id":"4","op":"key","key":"enter"}\n',
        ])
```

Implement `_serve_socket_workflow()` in the test class so it sends ready, then alternates reading one request and sending the next response line.

- [ ] **Step 2: Verify RED**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_socket_workflow_runs_requests_in_sequence
```

Expected: fails because workflow execution is not implemented.

- [ ] **Step 3: Implement workflow request execution**

Add helpers:

```python
def load_workflow_recipe(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return parse_workflow_recipe(json.load(handle))
    except OSError as exc:
        raise WorkflowError(f"failed to read workflow recipe: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"failed to parse workflow recipe: {exc}") from exc


def run_workflow(transport, steps, timeout=None, allow_input=True, transcript=None):
    recorder = EventRecorder(transcript=transcript)
    read_event_line(transport, make_deadline(timeout), "ready event", recorder=recorder)
    next_request_id = 1

    for index, step in enumerate(steps):
        if step.action in ("noop", "comment"):
            continue
        try:
            if step.action == "exec":
                run_request(transport, next_request_id, "exec", command=step.value, timeout=timeout, recorder=recorder)
                next_request_id += 1
            elif step.action == "status":
                run_request(transport, next_request_id, "status", timeout=timeout, recorder=recorder)
                next_request_id += 1
            elif step.action == "input_text":
                if not allow_input:
                    raise WorkflowError("input_text actions are socket-only")
                run_request(transport, next_request_id, "input_text", text=step.value, timeout=timeout, recorder=recorder)
                next_request_id += 1
            elif step.action == "key":
                if not allow_input:
                    raise WorkflowError("key actions are socket-only")
                run_request(transport, next_request_id, "key", key=step.value, timeout=timeout, recorder=recorder)
                next_request_id += 1
            elif step.action == "wait_for":
                wait_for_workflow_event(transport, step.value, timeout=timeout, recorder=recorder)
        except (RequestTimeout, RuntimeError, WorkflowError) as exc:
            raise WorkflowError(format_workflow_failure(index, step, exc, recorder)) from exc
    return 0
```

Update `read_event_line()` and `run_request()` to accept `recorder=None`, and call `recorder.record(raw_line, event)` after successful JSON parsing.

Add minimal `EventRecorder`, `format_workflow_failure()`, and this explicit temporary `wait_for_workflow_event()` implementation:

```python
def wait_for_workflow_event(transport, matcher, timeout=None, recorder=None):
    raise WorkflowError("wait_for is not implemented yet")
```

Task 3 replaces this function with event matching behavior.

Dispatch workflow in `main()` by loading the recipe and calling `run_workflow()`.

- [ ] **Step 4: Verify GREEN**

Run the socket workflow test from Step 2.

Expected: pass.

- [ ] **Step 5: Commit**

```bash
rtk git add scripts/host_control_client.py tests/host_control_client_tests.py
rtk git commit -m "feat: run host control workflow requests"
```

---

### Task 3: Wait Matchers And Timeout Diagnostics

**Files:**
- Modify: `scripts/host_control_client.py`
- Test: `tests/host_control_client_tests.py`

- [ ] **Step 1: Write failing wait and timeout tests**

Add tests:

```python
def test_socket_workflow_wait_for_matches_output_event(self):
    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = str(Path(tmpdir) / "control.sock")
        recipe_path = self._write_recipe(tmpdir, {"steps": [{"wait_for": "output"}]})
        requests = []
        lines = [
            '{"event":"ready","transport":"socket"}\n',
            '{"event":"output","id":"99","encoding":"base64","data":"aGkNCg=="}\n',
        ]
        thread = self._serve_socket_workflow(sock_path, lines, requests, expected_requests=0)

        proc = subprocess.run(
            [sys.executable, str(CLIENT), "--timeout", "1", "socket", sock_path, "workflow", str(recipe_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        thread.join(timeout=2)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "".join(lines))
        self.assertEqual(requests, [])

def test_socket_workflow_timeout_reports_step_and_recent_events(self):
    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = str(Path(tmpdir) / "control.sock")
        recipe_path = self._write_recipe(tmpdir, {"steps": [{"wait_for": {"event": "result", "ok": True}}]})
        requests = []
        lines = [
            '{"event":"ready","transport":"socket"}\n',
            '{"event":"output","id":"1","encoding":"base64","data":"aGkNCg=="}\n',
        ]
        thread = self._serve_socket_hanging_after_lines(sock_path, lines, requests)

        proc = subprocess.run(
            [sys.executable, str(CLIENT), "--timeout", "0.1", "socket", sock_path, "workflow", str(recipe_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        thread.join(timeout=2)

        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "".join(lines))
        self.assertIn("workflow step 0 wait_for", proc.stderr)
        self.assertIn("timed out waiting for workflow event", proc.stderr)
        self.assertIn('"event":"output"', proc.stderr)
```

- [ ] **Step 2: Verify RED**

Run both tests.

Expected: fails because wait matching and diagnostics are incomplete.

- [ ] **Step 3: Implement wait matchers and diagnostics**

Add:

```python
WAIT_EVENT_ALIASES = {"ready", "output", "result", "status", "error", "input_result"}


def event_matches(event, matcher):
    if isinstance(matcher, str):
        if matcher not in WAIT_EVENT_ALIASES:
            raise WorkflowError(f"unsupported wait_for event {matcher}")
        return event.get("event") == matcher
    if not isinstance(matcher, dict) or not matcher:
        raise WorkflowError("wait_for object must not be empty")
    return all(event.get(key) == value for key, value in matcher.items())


def wait_for_workflow_event(transport, matcher, timeout=None, recorder=None):
    deadline = make_deadline(timeout)
    while True:
        event = read_event_line(transport, deadline, "workflow event", recorder=recorder)
        if event_matches(event, matcher):
            return event
```

Update `format_workflow_failure()` to include:

- `workflow step {index} {step.action} failed: {exc}`
- `recent events:`
- up to the last 10 raw event lines from `EventRecorder`

- [ ] **Step 4: Verify GREEN**

Run both tests.

Expected: pass.

- [ ] **Step 5: Commit**

```bash
rtk git add scripts/host_control_client.py tests/host_control_client_tests.py
rtk git commit -m "feat: wait for host control workflow events"
```

---

### Task 4: Server Error Events And Stdio Input Rejection

**Files:**
- Modify: `scripts/host_control_client.py`
- Test: `tests/host_control_client_tests.py`

- [ ] **Step 1: Write failing tests**

Add tests:

```python
def test_socket_workflow_fails_on_matching_error_event(self):
    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = str(Path(tmpdir) / "control.sock")
        recipe_path = self._write_recipe(tmpdir, {"steps": [{"exec": "bad"}]})
        requests = []
        lines = [
            '{"event":"ready","transport":"socket"}\n',
            '{"event":"error","id":"1","message":"failed"}\n',
        ]
        thread = self._serve_socket_workflow(sock_path, lines, requests, expected_requests=1)

        proc = subprocess.run(
            [sys.executable, str(CLIENT), "socket", sock_path, "workflow", str(recipe_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        thread.join(timeout=2)

        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "".join(lines))
        self.assertIn("workflow step 0 exec failed", proc.stderr)
        self.assertIn("server error for request 1: failed", proc.stderr)

def test_stdio_workflow_rejects_input_actions_before_spawn(self):
    with tempfile.TemporaryDirectory() as tmpdir:
        recipe_path = self._write_recipe(tmpdir, {"steps": [{"input_text": "dir\n"}]})
        proc = subprocess.run(
            [
                sys.executable,
                str(CLIENT),
                "stdio",
                "workflow",
                str(recipe_path),
                "--",
                sys.executable,
                "-c",
                "raise SystemExit('should not spawn')",
                "-control-stdio",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("input_text actions are socket-only", proc.stderr)
        self.assertNotIn("should not spawn", proc.stderr)
```

- [ ] **Step 2: Verify RED**

Run both tests.

Expected: fail because matching server error is not converted to workflow failure and stdio validates after transport creation.

- [ ] **Step 3: Implement error and validation behavior**

Update `run_request()` so a matching `error` completion raises:

```python
if event.get("event") == "error" and str(event.get("id", "")) == str(request_id):
    raise WorkflowError(f"server error for request {request_id}: {event.get('message', '')}")
```

Keep one-shot behavior compatible by making this behavior opt-in through a `fail_on_error=False` argument. Workflow calls use `fail_on_error=True`; existing one-shot and REPL calls keep `False`.

Add:

```python
def validate_workflow_for_transport(steps, allow_input):
    if allow_input:
        return
    for index, step in enumerate(steps):
        if step.action in ("input_text", "key"):
            raise WorkflowError(f"step {index}: {step.action} actions are socket-only")
```

Call validation in `main()` after loading workflow steps and before `make_transport(args)`.

- [ ] **Step 4: Verify GREEN**

Run both tests.

Expected: pass.

- [ ] **Step 5: Commit**

```bash
rtk git add scripts/host_control_client.py tests/host_control_client_tests.py
rtk git commit -m "fix: report host control workflow failures"
```

---

### Task 5: JSONL Transcript

**Files:**
- Modify: `scripts/host_control_client.py`
- Test: `tests/host_control_client_tests.py`

- [ ] **Step 1: Write failing transcript test**

Add:

```python
def test_socket_workflow_writes_jsonl_transcript_without_changing_stdout(self):
    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = str(Path(tmpdir) / "control.sock")
        recipe_path = self._write_recipe(tmpdir, {"steps": [{"status": True}]})
        transcript_path = Path(tmpdir) / "run.jsonl"
        requests = []
        lines = [
            '{"event":"ready","transport":"socket"}\n',
            '{"event":"status","id":"1","transport":"socket","session_active":true,"errorlevel":0,"drive":"Z","cwd":"Z:\\\\"}\n',
        ]
        thread = self._serve_socket_workflow(sock_path, lines, requests, expected_requests=1)

        proc = subprocess.run(
            [
                sys.executable,
                str(CLIENT),
                "--transcript",
                str(transcript_path),
                "socket",
                sock_path,
                "workflow",
                str(recipe_path),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        thread.join(timeout=2)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "".join(lines))
        entries = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([entry["type"] for entry in entries], ["event", "event"])
        self.assertEqual(entries[0]["raw"], lines[0])
        self.assertEqual(entries[1]["event"]["event"], "status")
```

- [ ] **Step 2: Verify RED**

Run the transcript test.

Expected: fails because transcript writing is not implemented.

- [ ] **Step 3: Implement transcript writing**

Implement `EventRecorder`:

```python
class EventRecorder:
    def __init__(self, transcript=None, recent_limit=10):
        self.recent = []
        self.recent_limit = recent_limit
        self.transcript = transcript

    def record(self, raw_line, event):
        raw_text = raw_line.decode("utf-8", errors="replace")
        self.recent.append(raw_text)
        if len(self.recent) > self.recent_limit:
            self.recent = self.recent[-self.recent_limit:]
        if self.transcript is not None:
            self.transcript.write(json.dumps(
                {"type": "event", "raw": raw_text, "event": event},
                separators=(",", ":"),
            ) + "\n")
            self.transcript.flush()
```

In `main()`, open `args.transcript` only for workflow mode and close it in `finally`.

- [ ] **Step 4: Verify GREEN**

Run the transcript test.

Expected: pass.

- [ ] **Step 5: Commit**

```bash
rtk git add scripts/host_control_client.py tests/host_control_client_tests.py
rtk git commit -m "feat: capture host control workflow transcripts"
```

---

### Task 6: Documentation And Optional Live Smoke

**Files:**
- Modify: `docs/host-control.md`
- Modify: `tests/host_control_live_tests.py` if adding a gated smoke

- [ ] **Step 1: Update documentation**

Add a "Workflow recipes" section to `docs/host-control.md` covering:

```markdown
## Workflow Recipes

Workflow mode runs a JSON recipe with sequential host-control steps:

```bash
scripts/host_control_client.py --timeout 10 socket /tmp/dosboxx.sock workflow recipe.json
scripts/host_control_client.py --timeout 10 --transcript run.jsonl socket /tmp/dosboxx.sock workflow recipe.json
```
```

Include the recipe example from the design doc, the supported step table, wait matcher rules, stdio input limitations, and failure diagnostics.

- [ ] **Step 2: Add optional live smoke only if it stays small**

If adding a live smoke, keep it behind `DOSBOX_X_LIVE_TESTS=1` and run a simple socket workflow:

```json
{"steps":[{"exec":"dir"},{"status":true}]}
```

Do not require the NBA Hangtime project for the automated test. Reserve the Hangtime project for manual smoke.

- [ ] **Step 3: Run focused verification**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests
```

Expected: all client tests pass.

- [ ] **Step 4: Commit**

```bash
rtk git add docs/host-control.md tests/host_control_live_tests.py
rtk git commit -m "docs: document host control workflow recipes"
```

---

### Task 7: Final Verification

**Files:**
- No planned source changes.

- [ ] **Step 1: Run focused Python tests**

```bash
rtk python3 -m unittest tests.host_control_client_tests
```

Expected: all tests pass.

- [ ] **Step 2: Run live tests if binary and opt-in are available**

```bash
rtk env DOSBOX_X_LIVE_TESTS=1 python3 -m unittest tests.host_control_live_tests
```

Expected: all live tests pass if `./src/dosbox-x` is present and usable.

- [ ] **Step 3: Run C++ host-control tests if the binary is present**

```bash
rtk ./src/dosbox-x -tests --gtest_filter='*HostControl*'
```

Expected: all HostControl gtests pass. If the binary is missing or stale, report that explicitly instead of claiming C++ verification.

- [ ] **Step 4: Check git status**

```bash
rtk git status --short --branch
```

Expected: clean feature branch with commits only on `feat/host-control-workflow-runner`.
