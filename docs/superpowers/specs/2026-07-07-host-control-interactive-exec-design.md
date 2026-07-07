# Host Control Interactive Exec Design

## Goal

Formalize interactive host-control automation for commands or DOS programs that run under an `exec` request, stream output, and need keyboard input before they complete.

Milestone 5 added sequential JSON workflows. Milestone 6 extends that workflow runner so agents can express an interactive command in one recipe without changing DOSBox-X internals.

## Scope

Included:

- a JSON workflow action named `exec_interactive`
- nested workflow steps that run while the parent `exec` request is still active
- support for nested `wait_for`, `input_text`, `key`, `status`, `comment`, and no-op steps
- raw stdout event fidelity and JSONL transcript behavior unchanged
- diagnostics that identify nested interactive steps
- tests proving request ordering for output, input, and final `result`
- documentation of the idle-prompt output limitation

Not included:

- YAML or third-party dependencies
- server-side cancellation
- parallel workflow branches
- screen scraping, OCR, or decoded-output matching
- changing DOSBox-X output capture outside an active `exec` request
- stdio interactive input; interactive exec is socket-only

## Recipe Shape

`exec_interactive` is an object with a command and nested steps:

```json
{
  "steps": [
    {
      "exec_interactive": {
        "command": "pause",
        "steps": [
          {"wait_for": "output"},
          {"key": "enter"},
          {"wait_for": {"event": "result", "ok": true}}
        ]
      }
    }
  ]
}
```

The parent `exec` request gets the next workflow request id. Nested `input_text`,
`key`, and `status` steps continue incrementing request ids from the same sequence.

The runner should wait for the parent `exec` completion after nested steps finish
if the nested steps did not already observe it.

## Execution Semantics

For `exec_interactive`:

1. Send `{"op":"exec","command":...}` with the next request id.
2. Run nested steps while continuing to read from the same event stream.
3. Allow nested `wait_for` steps to match events from the parent `exec`.
4. Allow nested `input_text` and `key` steps to send socket input requests while
   the parent `exec` is running.
5. Treat a parent `error` event as a workflow failure.
6. Before the `exec_interactive` step completes, ensure the parent `exec` has
   completed with `result` or matching `error`.

Events read while waiting for one request may belong to another active request.
The workflow runner needs a small pending-event buffer plus request-completion
tracking so nested waits can still observe events that arrived during another
nested request wait.

## Transport Rules

`exec_interactive` is socket-only. Stdio host control executes one request at a
time and cannot accept `input_text` or `key` while a command is running.

Socket workflows may combine `exec_interactive` with existing sequential steps.

## Output Limitation

Keyboard input at an idle DOS prompt can be accepted by DOSBox-X host control,
but it does not currently produce host-control `output` events unless an
`exec` output-capture context is active. This milestone should document the
limitation rather than broaden server-side capture.

The practical pattern is:

- use `exec_interactive` for programs or commands that need host-control output
  while they wait for input
- use plain `input_text` or `key` at the idle prompt only when the caller does
  not need output events for that input

## Error Handling

Failures should include:

- top-level step index
- nested step index for interactive failures
- action name
- timeout or server error context
- recent raw events

Timeouts should keep the existing abort behavior so stdio-owned processes are
terminated promptly and socket clients disconnect promptly.

## Testing Strategy

Use TDD.

Python client tests should cover:

- parsing valid `exec_interactive` recipes
- rejecting malformed interactive specs
- rejecting `exec_interactive` on stdio before spawn
- socket request ordering for `exec`, output wait, input request, input result,
  and final result
- final result observed before an input request completes does not get lost
- interactive timeout diagnostics include nested context and recent events
- transcript output remains unchanged

Live tests should stay gated behind `DOSBOX_X_LIVE_TESTS=1`. A practical live
smoke is `exec_interactive` with `pause`, wait for output, send `enter`, and
observe `result`.
