# Structured Host Control Result Design

## Goal

Extend host-control `result` events so controllers can consume command status directly instead of inferring it from DOS text output.

## Scope

This milestone augments the existing `result` event with structured execution metadata:

- `errorlevel`
- `drive`
- `cwd`
- `duration_ms`

Included:
- Capture result metadata after each host-control `exec` request
- Emit the additional fields for both stdio and socket transports
- Preserve the existing raw output event format
- Preserve existing request and shell-exit semantics
- Add focused tests for result serialization and session behavior

Not included:
- Changes to request format
- Changes to output events
- Text decoding or normalization of DOS output
- Multi-command batching
- Pipe transport implementation

## User-Facing Behavior

After this milestone, each successful host-control command completion emits a richer `result` event. Example:

```json
{"event":"result","id":"42","ok":true,"shell_exit":false,"errorlevel":0,"drive":"C","cwd":"C:\\XCODE101L","duration_ms":183}
```

Behavioral rules:

- `errorlevel` reports the DOS return code after the command finishes.
- `drive` reports the current DOS drive letter after the command finishes.
- `cwd` reports the current DOS path after the command finishes, including drive prefix.
- `duration_ms` reports wall-clock elapsed milliseconds spent executing the request.
- These fields are emitted even when `ok` is `false`, as long as DOSBox-X still has a valid shell context after the request returns.
- Existing `ready`, `output`, `error`, and `result` ordering remains unchanged.

## Architecture

The existing host-control flow already has a single point where request completion is turned into a `result` event. This milestone should extend that point instead of creating transport-specific logic.

### Components

`include/host_control.h`
- Add a small result-metadata struct for structured command status.
- Extend the result JSON builder signature to accept structured metadata.
- Extend the host-control execution callback type so request execution can return both shell-exit state and structured result metadata.

`src/misc/host_control_protocol.cpp`
- Serialize the new result fields into the NDJSON result event.

`src/misc/host_control.cpp`
- Capture command start and stop times around shell execution.
- Use DOS state helpers to snapshot `errorlevel`, drive, and current directory after request execution.
- Pass the structured metadata through the shared session runner for both stdio and socket transports.

`src/shell/shell.cpp`
- Keep command execution behavior unchanged.
- If needed, expose only the minimal shell-side helper required to gather post-command DOS state without duplicating shell logic elsewhere.

`tests/host_control_protocol_tests.cpp`
- Add serialization tests for structured result events.
- Add session-runner tests proving metadata is emitted for command results.

## Data Flow

1. A host-control transport receives an `exec` request.
2. The shared session runner records the monotonic start time.
3. The shell executes the command through the existing host-control execution path.
4. Immediately after command completion, host-control snapshots:
   - `dos.return_code`
   - `DOS_GetDefaultDrive()`
   - `DOS_GetCurrentDir()`
   - monotonic end time
5. Host-control emits any pending buffered console bytes.
6. Host-control emits the enriched `result` event.

The metadata must reflect post-command state. For example, if the command changes directory, the emitted `drive` and `cwd` must show the new location.

## Error Handling

- If the current DOS directory cannot be read, host-control should still emit a result event with the best available values. The simplest acceptable fallback is `cwd` as `<drive>:\`.
- `duration_ms` must never be negative; it should be computed from a monotonic clock and clamped naturally by unsigned arithmetic discipline.
- Result serialization must continue to escape path content correctly.
- If shell execution cannot start because no shell exists, existing request failure behavior remains unchanged.

## Testing Strategy

The milestone should be validated in three layers.

### Unit and protocol tests

- Result serialization includes `errorlevel`, `drive`, `cwd`, and `duration_ms`.
- Result serialization still escapes path content correctly.
- Session runner emits enriched result events after buffered output events.

### Focused host-control tests

- Existing host-control argv and protocol tests stay green.
- New tests exercise both a plain command result and a command that changes directory.

### Runtime verification

- Run a stdio or socket smoke test that issues a command such as `cd \\` or `cd XCODE101L`.
- Verify the returned `result` event reports the updated `cwd`.
- Verify `duration_ms` is present and non-negative.

## Future Extensions

This milestone prepares the protocol for later additions without taking them on now:

- separate guest exit code versus built-in shell command status if DOSBox-X semantics require it
- optional host timestamp fields
- explicit machine-readable failure categories
- a small client utility that surfaces structured results cleanly

The implementation should stop once structured result metadata is emitted reliably for current transports.
