# Host-Control Windows Pipe Roadmap

## Status

Implemented and stabilized. The Windows named-pipe host-control transport ships
behind `-control-pipe <name-or-path>` and is covered by the reproducible Windows
release gate at `tests/run_host_control_windows.ps1`.

The gate rebuilds the x64 SDL2 binary, runs the native C++ protocol tests, the
Python client unit tests, and the live named-pipe smoke tests against the
freshly built binary, then writes a matching `artifact-provenance.json`.

## What shipped

- Endpoint normalization: a short name is expanded to `\\.\pipe\<name>`; a full
  local `\\.\pipe\<name>` path is accepted as-is; empty and remote paths are
  rejected.
- One full-duplex named pipe per session, created with
  `FILE_FLAG_FIRST_PIPE_INSTANCE` and `PIPE_REJECT_REMOTE_CLIENTS`.
- A protected DACL granting access only to the current user and LocalSystem.
- One client per DOSBox-X process with no reconnect.
- Bounded framing: inbound NDJSON request lines are limited to 1 MiB, and
  outbound event streams are written in bounded chunks that survive a client
  disconnect.
- Cancellation-safe connect/disconnect cleanup for the server.
- The existing NDJSON wire contract and Unix socket/FIFO behavior are unchanged.

## Validation

All of the following pass on Windows x64 SDL2:

```bat
powershell -NoProfile -ExecutionPolicy Bypass -File tests\run_host_control_windows.ps1
python -m unittest tests.host_control_client_tests
```

The gate covers:

- The native `*HostControl*` GoogleTest suite: endpoint validation, DACL scope,
  framing, duplicate-instance rejection, and lifecycle behavior.
- Python client unit tests: endpoint validation and errorlevel policy.
- Live pipe smoke tests: status, synchronous batch completion, byte-exact
  high-volume output (12+ MiB), timeout, and client disconnect.

A Windows GitHub Actions job (`host-control-windows`) runs the same gate script
used locally.

## Deferred

- Reconnect and multiple simultaneous clients.
- Server-side cancellation of a running DOS command.
- The separate ROM/VIDRAM emulator-core defect, which is out of scope for this
  work.

## Resolved questions

- Endpoint scope: only short names and local `\\.\pipe\<name>` paths are
  accepted; other path-like values are rejected.
- Connection model: the server uses `ConnectNamedPipe` for its single client,
  with cancellation-safe teardown on shutdown.
- Client count: one client per DOSBox-X process; reconnect is a future item.
- CI: a Windows runner runs the same gate script used locally.
