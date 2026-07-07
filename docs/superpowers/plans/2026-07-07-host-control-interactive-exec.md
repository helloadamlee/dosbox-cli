# Host Control Interactive Exec Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add socket-only `exec_interactive` workflow support for commands that stream output and need host-control input before completion.

**Architecture:** Extend the existing Python workflow runner. Add recursive recipe parsing for `exec_interactive`, introduce a small workflow runtime that tracks request ids, pending events, and completed requests, and use it to execute nested interactive steps without changing DOSBox-X internals.

**Tech Stack:** Python 3 standard library, `unittest`, fake Unix socket servers, existing DOSBox-X host-control protocol.

---

## File Structure

- Modify `scripts/host_control_client.py`: add `exec_interactive` parsing, recursive nested-step validation, workflow runtime with pending events and request completion tracking, socket-only validation, and interactive execution.
- Modify `tests/host_control_client_tests.py`: add parser, transport validation, request-ordering, out-of-order completion, timeout, and transcript regression tests.
- Modify `tests/host_control_live_tests.py`: add a gated `pause` interactive workflow smoke if stable.
- Modify `docs/host-control.md`: document `exec_interactive`, socket-only behavior, and idle-prompt output limitation.

---

### Task 1: Parse Interactive Exec Recipes

- [ ] Add failing tests for valid `exec_interactive`, missing command, missing steps, and non-list steps.
- [ ] Run the new parser tests and confirm they fail because the action is unsupported.
- [ ] Add `exec_interactive` to supported workflow actions and recursively parse nested steps into `WorkflowStep("exec_interactive", {"command": str, "steps": list[WorkflowStep]})`.
- [ ] Run the parser tests and confirm they pass.
- [ ] Commit parser support.

### Task 2: Socket-Only Validation

- [ ] Add a failing test showing stdio workflow rejects `exec_interactive` before spawning.
- [ ] Run the test and confirm it fails because the child process starts or the wrong error appears.
- [ ] Extend `validate_workflow_for_transport()` to reject `exec_interactive` when `allow_input` is false, including nested input validation.
- [ ] Run the validation test and focused parser tests.
- [ ] Commit validation.

### Task 3: Interactive Runtime And Request Ordering

- [ ] Add a failing fake-socket workflow test for `exec_interactive`: send parent `exec`, receive `output`, send nested `key`, receive `input_result`, then receive parent `result`.
- [ ] Run the test and confirm it fails because execution is not implemented.
- [ ] Add a `WorkflowRuntime` helper that owns `transport`, `timeout`, `recorder`, `allow_input`, `next_request_id`, pending events, request operation mapping, and completed request events.
- [ ] Route existing workflow execution through the runtime without changing existing behavior.
- [ ] Implement `run_exec_interactive()` by sending parent `exec`, running nested steps, and ensuring parent completion.
- [ ] Run the new request-ordering test plus all existing client tests.
- [ ] Commit interactive execution.

### Task 4: Preserve Early Parent Completion

- [ ] Add a failing test where parent `result` arrives while waiting for nested `input_result`, then the nested workflow waits for the parent result.
- [ ] Run the test and confirm it fails if the parent result is lost.
- [ ] Record every request completion by id as events are read, and let request waits return already-recorded completions.
- [ ] Run the new test and full client tests.
- [ ] Commit completion tracking.

### Task 5: Diagnostics And Transcript Regression

- [ ] Add tests for nested timeout diagnostics and transcript output during an interactive workflow.
- [ ] Run the tests and confirm any missing nested context or transcript regression.
- [ ] Include nested step context such as `workflow step 0 exec_interactive nested step 1 key`.
- [ ] Confirm JSONL transcript still writes one event entry per raw event and stdout remains unchanged.
- [ ] Commit diagnostics and transcript coverage.

### Task 6: Docs And Live Smoke

- [ ] Update `docs/host-control.md` with `exec_interactive` recipe examples and the idle-prompt output limitation.
- [ ] Add a gated live workflow smoke for `pause` if it is stable with the current binary.
- [ ] Run `python3 -m unittest tests.host_control_client_tests`.
- [ ] Run `DOSBOX_X_LIVE_TESTS=1 python3 -m unittest tests.host_control_live_tests` if the binary is available.
- [ ] Run `./src/dosbox-x -tests --gtest_filter='*HostControl*'`.
- [ ] Commit docs and live smoke.

### Task 7: Final Verification

- [ ] Run `python3 -m unittest tests.host_control_client_tests`.
- [ ] Run `DOSBOX_X_LIVE_TESTS=1 python3 -m unittest tests.host_control_live_tests`.
- [ ] Run `./src/dosbox-x -tests --gtest_filter='*HostControl*'`.
- [ ] Run `./src/dosbox-x -tests`.
- [ ] Check `git status --short --branch`.
