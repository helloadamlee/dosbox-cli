# Unix Socket Host Control Design

## Goal

Add a Unix domain socket transport for DOSBox-X host control so an external agent can drive the existing NDJSON command protocol without sharing the emulator process's `stdin` and `stdout`.

## Scope

This milestone adds a server-owned local socket transport behind the existing `-control-socket <path>` flag.

Included:
- Unix domain socket server creation by DOSBox-X
- Removal of a stale socket file before bind
- Single-client accept loop
- Reuse of the existing request protocol and raw-byte output events
- Clean session shutdown on client disconnect or `exit`
- Clear failure behavior on unsupported platforms or socket setup errors

Not included:
- TCP transport
- Multi-client support
- Reconnect support
- Authentication or remote access controls
- Windows named-pipe implementation
- A broad transport abstraction rewrite

## User-Facing Behavior

When DOSBox-X starts with `-control-socket /tmp/dosboxx.sock`, it will:

1. Create and listen on the Unix socket path.
2. Wait for one client connection.
3. Emit the existing `ready` event over the accepted socket connection.
4. Accept line-delimited JSON requests using the same `exec` protocol as stdio.
5. Emit line-delimited JSON events for output, errors, and results over that same connection.
6. End the control session when the client disconnects or when an `exec` request causes the shell to exit.
7. Remove the socket path during shutdown.

The event payloads stay unchanged. Raw DOS console output remains base64-encoded in `{"event":"output","encoding":"base64",...}` frames, with the same buffered hybrid flush policy already implemented for stdio.

## Architecture

The first socket milestone should reuse the current host-control runtime rather than introduce a large transport abstraction. The existing stdio request loop already owns the important behavior:

- session lifecycle
- request parsing
- command dispatch
- buffered DOS console capture
- result emission

The socket transport should add only the minimum I/O surface needed to drive that loop over file descriptors instead of `stdin` and `stdout`.

### Components

`include/host_control.h`
- Extend the public interface with socket transport entry points and any small reusable I/O helpers needed by both stdio and socket sessions.

`src/misc/host_control.cpp`
- Add Unix socket server setup and teardown.
- Add a shared session runner that reads one line-delimited request at a time from an input stream and writes one NDJSON event at a time to an output stream.
- Keep DOS console capture state scoped to the active control session so buffered output still flushes against the current request ID.

`src/shell/shell.cpp`
- Dispatch to socket host control when `Transport::Socket` is selected.

`tests/host_control_protocol_tests.cpp`
- Add transport-selection tests and protocol-level socket-session tests that do not require a full interactive client.

## Data Flow

### Startup

1. DOSBox-X parses `-control-socket <path>` and stores `Transport::Socket` plus the endpoint path.
2. `shell.cpp` detects socket mode and calls the socket host-control entry point.
3. The socket entry point validates platform support and path length, removes any stale socket file, creates the server socket, binds it, and starts listening.
4. DOSBox-X blocks waiting for a single client connection.

### Active Session

1. After `accept()`, DOSBox-X sends the standard `ready` event with `transport:"socket"` and the configured endpoint.
2. The accepted socket becomes the only control channel for the session.
3. Each newline-terminated JSON request is parsed with the existing parser.
4. Valid `exec` requests run through the same shell execution path as stdio mode.
5. DOS console writes continue to accumulate into the current per-request byte buffer.
6. Buffered output flushes on size threshold, time threshold, request completion, or session shutdown.
7. Each completed request emits a `result` event with existing `ok` and `shell_exit` fields.

### Shutdown

The server tears down in all cases:
- normal `exit`
- EOF or disconnect from the client
- bind/listen/accept failure after partial setup

Teardown closes accepted and listening descriptors, flushes any remaining buffered output for the active request, clears session state, and unlinks the socket path if DOSBox-X created it.

## Error Handling

Unsupported cases should fail fast and clearly.

- On non-Unix platforms, `-control-socket` returns a clear startup error and does not silently fall back to another transport.
- If the socket path is empty, too long for `sockaddr_un`, or cannot be bound, DOSBox-X reports the failure and exits the control startup path.
- If a stale socket file exists and cannot be removed, startup fails.
- If `accept()` fails before a client session begins, startup fails.
- If the connected client disconnects mid-session, DOSBox-X treats it as end-of-session rather than a shell command failure.
- Invalid JSON requests still emit the existing `error` event format when possible.

## Concurrency and Limits

This milestone is intentionally single-client and single-session.

- Only one client may connect.
- DOSBox-X does not accept a second client while the first is active.
- There is no reconnect loop inside the same process.
- The control session is synchronous with the DOS shell, matching current stdio behavior.

These constraints keep the implementation aligned with a solo local automation workflow and minimize changes to emulator control flow.

## Platform Support

Milestone 1 targets Unix-like hosts that support `AF_UNIX`, including Linux. The implementation should be guarded so unsupported hosts fail explicitly instead of compiling dead behavior behind the flag.

Windows named pipes remain a separate future transport.

## Testing Strategy

The first implementation should be proven at three levels.

### Unit and protocol tests

- Socket transport selection still parses correctly from argv.
- Socket-ready events continue to report the configured endpoint.
- Shared session logic handles request parsing, invalid requests, request results, and buffered output identically across transports.

### Focused runtime test

Add a local socket smoke test that:
- starts DOSBox-X in socket mode
- connects one client
- sends `echo hi` and `exit`
- verifies `ready`, base64 `output`, and `result` events in order

This can be driven by a small shell or Python helper during verification even if it is not committed as a permanent automated test.

### Regression verification

- Existing host-control tests remain green.
- Full `./src/dosbox-x -tests` remains green.
- The existing stdio smoke test still works unchanged, proving the shared runtime did not regress.

## Future Extensions

This design intentionally leaves room for later additions without forcing them now:

- Windows named-pipe transport with the same protocol
- reconnect behavior
- client mode instead of server mode
- transport-specific timeouts
- a cleaner transport interface if more than two transports need to share logic

The first socket milestone should stop short of these until the single-client Unix path is working and verified.
