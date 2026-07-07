# Host Control Workflow Runner Design

## Goal

Add a practical scriptable workflow runner to the Python host-control client so a solo developer or agent can automate common DOSBox-X project tasks with a small JSON recipe.

The runner should be useful for local DOSBox-X project work, including NBA Hangtime-style rebuild sessions, without changing DOSBox-X internals unless the current protocol proves insufficient.

## Scope

Included:

- JSON-only recipe files parsed with the Python standard library
- sequential workflow execution over existing host-control transports
- actions for `exec`, `status`, `input_text`, and `key`
- `wait_for` steps that consume the raw event stream until a requested event appears
- comment and no-op steps
- optional JSONL transcript capture
- useful nonzero failures for agents, including step context, timeout context, and recent events
- tests for parsing, execution, waiting, transcript output, and CLI validation
- host-control documentation updates

Not included:

- YAML or third-party recipe dependencies
- parallel steps
- variables, templating, conditionals, or loops
- server-side cancellation
- protocol changes in DOSBox-X
- stdio keyboard input injection
- reconnect handling or multi-client socket control

## Branch Strategy

Local `master` contains the Milestone 1-4 host-control series and is ahead of `origin/master`, while also being behind upstream. Milestone 5 should branch from local `master` so it builds on the locally merged Milestone 4 base and does not pull unrelated upstream changes into the feature.

The feature work should live in a linked worktree:

```bash
rtk git worktree add .worktrees/feat-host-control-workflow-runner -b feat/host-control-workflow-runner master
```

No push should happen unless explicitly requested.

## Recipe Format

Recipes are JSON objects with a `steps` array:

```json
{
  "steps": [
    {"comment": "Mount project"},
    {"exec": "mount c /path/to/project"},
    {"wait_for": {"event": "result", "ok": true}},
    {"status": true},
    {"input_text": "dir\n"},
    {"key": "enter"},
    {"wait_for": "input_result"},
    {}
  ]
}
```

Each step must be an object. Empty objects are no-ops. A `comment` step is a no-op and may contain any string. Action steps should contain one workflow action key.

Supported action keys:

- `exec`: string DOS shell command
- `status`: boolean `true`, `null`, or an empty object
- `input_text`: string text queued through the existing socket-only host-control input operation
- `key`: string named key queued through the existing socket-only host-control key operation
- `wait_for`: event matcher as a string alias or object field matcher
- `comment`: string no-op

The parser should reject malformed recipes before execution when practical. It should reject unknown action keys, multiple action keys in one step, non-object steps, and invalid action values.

## CLI

Add a `workflow` action to the existing client:

```bash
scripts/host_control_client.py --timeout 10 socket /tmp/dosboxx.sock workflow recipe.json
scripts/host_control_client.py --timeout 10 --transcript run.jsonl socket /tmp/dosboxx.sock workflow recipe.json
scripts/host_control_client.py --timeout 10 stdio workflow recipe.json -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec
```

Socket workflows may use all Milestone 5 actions. Stdio workflows may use `exec`, `status`, `wait_for`, comments, and no-ops. Stdio workflows that contain `input_text` or `key` should fail validation before sending requests.

Existing one-shot and REPL commands should keep their current behavior.

## Execution Semantics

The runner reads the initial `ready` event before executing steps, just like one-shot and REPL modes.

For request actions:

- assign request ids monotonically starting at `1`
- send the matching host-control request
- read and preserve raw events until the request completion event appears
- treat an `error` event with the matching id as request completion and workflow failure

Completion event mapping:

- `exec` completes on `result` or matching `error`
- `status` completes on `status` or matching `error`
- `input_text` and `key` complete on `input_result` or matching `error`

For `wait_for` steps:

- read and preserve raw events until the matcher succeeds
- use the same per-wait timeout mechanism as requests
- do not send a request

This design intentionally keeps workflows sequential. It does not introduce background waits or concurrent request scheduling.

## Wait Matchers

The simplest matcher is a string alias:

```json
{"wait_for": "result"}
```

String aliases map to exact `event` field checks:

- `output`
- `result`
- `status`
- `error`
- `input_result`
- `ready`

Object matchers require every listed field to compare equal:

```json
{"wait_for": {"event": "result", "ok": true}}
```

Object matchers should be shallow and exact. Nested object matching, regular expressions, substring matching, and decoded output matching are out of scope for Milestone 5.

## Raw Event Fidelity And Transcript

The client should continue writing every raw JSON event line to stdout exactly as received. Workflow mode may parse events internally only for control flow.

When `--transcript <path>` is provided, the client writes JSONL entries for each event:

```json
{"type":"event","raw":"{\"event\":\"ready\",\"transport\":\"socket\"}\n","event":{"event":"ready","transport":"socket"}}
```

Transcript writing must not change stdout. If the transcript path cannot be opened or written, the workflow should fail nonzero with a clear stderr message.

## Failure Behavior

Workflow failures should be useful to agents and local scripts. Failures should exit nonzero and include:

- step index, using zero-based indexes to match JSON array positions
- action name or wait matcher description
- timeout message or protocol/validation reason
- the last recent raw events, defaulting to 10
- spawned stdio process stderr where practical, if the client owns that process in a future extension

Timeouts should abort the transport using the existing timeout behavior. In stdio mode, aborting still terminates the spawned DOSBox-X process. In socket mode, aborting closes the client socket.

## Testing Strategy

Use TDD.

Focused Python unit tests should cover:

- recipe parsing and validation
- CLI parsing for `workflow` and `--transcript`
- socket workflow request sequencing
- stdio workflow request sequencing for supported actions
- stdio validation rejection for input actions
- wait matcher success
- wait timeout diagnostics with recent events
- matching server `error` events as workflow failures
- transcript JSONL capture while preserving stdout

Docs should include a minimal recipe, a socket recipe with input, stdio limitations, transcript usage, and failure behavior.

Live tests are optional for routine execution. If added, they should remain gated by `DOSBOX_X_LIVE_TESTS=1`.
