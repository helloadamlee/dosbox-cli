# Host Control Status Op Design

## Goal

Add a read-only host-control `status` operation so controllers can query current DOS session state on demand without executing a shell command.

## Scope

This milestone adds a new request operation:

- `{"id":"...","op":"status"}`

Included:
- parsing and accepting the `status` op
- emitting a dedicated `status` event
- reporting current `drive`, `cwd`, and last `errorlevel`
- reporting transport and whether the control session is active
- supporting the op over both stdio and socket transports
- focused protocol and session tests

Not included:
- pipe transport
- interactive stdin injection
- command cancellation
- background process inspection
- multi-client or reconnect behavior
- expanding `status` into a command-execution shortcut

## User-Facing Behavior

A controller may send:

```json
{"id":"42","op":"status"}
```

DOSBox-X replies with a single read-only status event:

```json
{"event":"status","id":"42","transport":"socket","session_active":true,"errorlevel":0,"drive":"Z","cwd":"Z:\\"}
```

Behavioral rules:

- `status` does not execute any DOS command.
- `status` does not emit `output` events.
- `status` does not emit a `result` event.
- `status` snapshots the current DOS state at the moment the request is handled.
- `status` is valid between commands and after commands, as long as the control session is active.
- Invalid or malformed `status` requests still use the existing `error` event format.

## Architecture

The current host-control protocol is centered on a shared request loop that parses a request and dispatches behavior by `op`. This milestone should extend that dispatch point with a small read-only branch for `status`, without changing the `exec` path.

### Components

`include/host_control.h`
- Add a small `StatusSnapshot` struct to represent the read-only fields returned by `status`.
- Add a JSON builder declaration for the `status` event.
- Add any lightweight helper declarations needed to snapshot DOS state outside the `exec` result path.

`src/misc/host_control_protocol.cpp`
- Extend request parsing to accept `op == "status"`.
- Add the `status` event serializer.

`src/misc/host_control.cpp`
- Add a helper that snapshots current DOS state into `StatusSnapshot`.
- Extend the shared session runner to dispatch `status` without running `exec_request()`.
- Preserve existing buffering and request handling behavior for `exec`.

`tests/host_control_protocol_tests.cpp`
- Add parser coverage for `status`.
- Add serializer coverage for the `status` event.
- Add shared-session tests proving `status` emits one `status` event and does not emit `result`.

## Data Flow

### Status Request

1. A transport receives a line-delimited JSON request.
2. The shared session runner parses the request.
3. If `op == "status"`, host-control snapshots:
   - current `errorlevel`
   - current DOS drive
   - current DOS path
   - current transport
   - current session-active flag
4. Host-control emits a single `status` event using the request `id`.
5. The session loop continues waiting for the next request.

### Exec Request

Existing `exec` behavior stays intact:

- raw output buffering remains unchanged
- structured `result` events remain unchanged
- output-before-result ordering remains unchanged

The `status` op must not disturb `exec` state handling.

## Status Fields

Milestone 1 keeps the snapshot intentionally small and cheap:

- `transport`
- `session_active`
- `errorlevel`
- `drive`
- `cwd`

Field meanings:

- `transport` is the active control transport string, such as `stdio` or `socket`.
- `session_active` reports whether the host-control session is currently live.
- `errorlevel` is the current DOS return code.
- `drive` is the current DOS drive letter.
- `cwd` is the current DOS path with drive prefix, such as `Z:\` or `C:\BUILD`.

## Error Handling

- Requests missing `id` or `op` still fail through the existing request parser rules.
- Unsupported operations other than `exec` and `status` still emit `unsupported op`.
- If DOS current-directory lookup fails, the same fallback used for structured `result` metadata should be used here: `<drive>:\`.
- `status` must not require `command`, and the parser should reject any design that keeps `command` mandatory for all ops.

## Testing Strategy

The milestone should be validated in three layers.

### Unit and protocol tests

- `status` requests parse successfully without a `command`.
- unsupported ops still fail.
- `status` event JSON includes the expected fields and escaping.

### Shared session tests

- a `status` request emits exactly `ready` then `status`
- a `status` request does not emit `result`
- `exec` behavior remains unchanged in adjacent tests

### Runtime verification

- run a stdio smoke test with `status`, then `cd \`, then `status`, then `exit`
- verify the second `status` reflects the updated `cwd`
- run the same flow over socket transport

## Future Extensions

This milestone is deliberately narrow but leaves room for later additions:

- timestamps
- mounted-drive summaries
- active-command or busy-state reporting
- memory/CPU emulation state
- a richer host-side client that calls `status` directly

The implementation should stop once `status` is a stable, read-only snapshot op for current transports.
