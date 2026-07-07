# Host Control CLI Client Design

## Goal

Add a small host-side CLI client so a local agent or script can drive DOSBox-X host control over stdio or a Unix domain socket, send `exec` and `status` requests, and print raw JSON events exactly as received.

## Scope

This milestone adds a committed host-side utility for local developer use.

Included:
- a minimal standalone client implemented as a Python 3 stdlib script
- socket mode that connects to `-control-socket <path>`
- stdio mode that spawns DOSBox-X with `-control-stdio`
- one-shot `status`
- one-shot `exec <command>`
- a simple interactive REPL
- tests for the client transport and request lifecycle where practical

Not included:
- changes to the DOSBox-X host-control protocol
- pipe transport
- multi-client coordination
- reconnect loops
- authentication or hardening for shared systems
- decoding or rewriting raw event payloads
- broad packaging or installer integration

## Approaches Considered

### 1. Python stdlib script in `scripts/` (recommended)

Pros:
- smallest implementation for socket plus subprocess stdio
- no DOSBox-X build-system changes
- easy to inspect and adapt for local automation
- easy to test with a lightweight stub server

Cons:
- depends on Python 3 on the host
- not as self-contained as a compiled binary

### 2. Small compiled C++ utility

Pros:
- single-language repo story
- no Python runtime dependency

Cons:
- more build-system work than the feature needs
- slower iteration for a protocol helper
- higher maintenance cost for REPL and process management

### 3. Shell helper around `nc`/`socat`

Pros:
- tiny footprint

Cons:
- poor portability
- awkward JSON request generation
- awkward REPL behavior
- weak stdio subprocess support

Recommendation:
- use the Python stdlib script now
- keep the protocol boundary narrow so the client could be rewritten later without changing DOSBox-X

## User-Facing Behavior

The client exposes three actions:

- `status`
- `exec <command>`
- `repl`

It exposes two transport families:

- socket: connect to an already-started DOSBox-X `-control-socket <path>` process
- stdio: spawn DOSBox-X with `-control-stdio` and talk over the child process stdin/stdout

Planned command shapes:

```bash
python3 scripts/host_control_client.py socket /tmp/dosboxx.sock status
python3 scripts/host_control_client.py socket /tmp/dosboxx.sock exec "dir"
python3 scripts/host_control_client.py socket /tmp/dosboxx.sock repl

python3 scripts/host_control_client.py stdio status -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec
python3 scripts/host_control_client.py stdio exec "cd \\" -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec
python3 scripts/host_control_client.py stdio repl -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec
```

Behavioral rules:

- every JSON event line from DOSBox-X is written to stdout verbatim, in original order
- the client does not decode base64 output events
- the client does not reserialize or pretty-print server events
- the client may parse event JSON internally only to detect request completion
- any local prompt or usage text for REPL goes to stderr so stdout stays machine-readable
- requests are sent one at a time; the client waits for request completion before sending the next REPL request

## Request and Completion Rules

The client generates a simple monotonically increasing request id sequence starting at `1` per session.

Generated requests:

- `status` sends `{"id":"N","op":"status"}`
- `exec <command>` sends `{"id":"N","op":"exec","command":"<escaped command text>"}`

Completion rules:

- `status` is complete after the first `status` event or `error` event with the matching id
- `exec` is complete after the first `result` event or `error` event with the matching id
- intermediate `output` events are printed and do not complete the request

This keeps the client aligned with the existing synchronous DOSBox-X session model.

## Transport Design

The client should share one small line-oriented transport interface:

- `readline()` returns one newline-terminated NDJSON line from DOSBox-X
- `writeline()` sends one newline-terminated NDJSON request
- `close()` ends the session

### Socket transport

- create an `AF_UNIX` stream socket
- connect to the requested path
- wrap it in text-mode file objects for line I/O

### Stdio transport

- spawn the provided DOSBox-X command with `subprocess.Popen`
- require the caller-provided command to include `-control-stdio`
- use the child stdin/stdout for line I/O
- inherit child stderr so DOSBox-X startup failures remain visible

## Session Lifecycle

Both transports use the same session flow:

1. connect or spawn
2. read and print the initial `ready` event
3. send one request and drain events until that request completes
4. for REPL, repeat step 3 until the user quits or EOF occurs
5. close the transport

Important lifecycle detail:

- DOSBox-X already ends the host-control session on stdin EOF or socket disconnect
- the client therefore does not need a custom shutdown protocol
- for one-shot stdio mode, closing the child stdin after the terminal event is sufficient

## REPL Behavior

The REPL is intentionally small.

Accepted commands:

- `status`
- `exec <command>`
- `quit`
- `help`

Rules:

- prompt is written to stderr
- blank lines are ignored
- unknown commands print a short help line to stderr
- `quit` closes the transport without sending another DOSBox-X request

This keeps the REPL practical for a solo developer while preserving clean stdout event streams.

## Error Handling

- connection failures exit non-zero with a short stderr message
- invalid local REPL commands do not terminate the session
- malformed server JSON still prints verbatim to stdout; the client treats it as a transport error only if request completion can no longer be determined
- premature EOF before the active request completes exits non-zero
- if a spawned stdio child exits early, the client surfaces its return code

## Testing Strategy

Automated tests should focus on the client contract rather than full DOSBox-X integration.

### Python client tests

Use `unittest` with lightweight stubs to prove:

- socket `status` prints raw `ready` plus `status` and exits success
- socket `exec` prints raw `ready`, `output`, and `result` in order
- stdio `status` works against a spawned stub process
- request completion logic stops on `status` versus `result` correctly
- REPL command parsing accepts `status`, `exec <command text>`, `help`, and `quit`

### Runtime verification

Run live smokes against the real DOSBox-X binary:

- stdio one-shot `status`
- stdio REPL with `status`, `exec`, `status`
- socket one-shot `exec`
- socket REPL with `status`, `exec`, `status`

Success criteria:

- raw event lines stay unchanged
- `status` and `exec` both work over both transports
- disconnect cleanly ends the session

## Future Extensions

This design intentionally leaves room for later improvements without taking them on now:

- optional request ids from the CLI
- a raw `send-json` mode
- a socket-spawn convenience mode
- optional event filtering for interactive humans

Milestone 2 should stop once the minimal client is usable for local automation and manual control.
