# Host Control Client Timeouts Design

## Goal

Add host-side timeout handling to the host-control client so an agent can stop waiting for a hung request and recover its local control flow without changing the DOSBox-X execution model.

## Scope

This milestone adds client-side request timeouts and recovery behavior.

Included:
- a `--timeout <seconds>` option on `scripts/host_control_client.py`
- timeout handling while waiting for the initial `ready` event
- timeout handling while waiting for a one-shot request to complete
- timeout handling for each REPL request
- stdio-spawn recovery by terminating the spawned DOSBox-X process
- socket recovery by closing the client socket
- focused client tests for timeout paths
- documentation of timeout behavior and the current server-side cancel limitation

Not included:
- a server-side `cancel` request
- background shell execution inside DOSBox-X
- a second control channel
- Ctrl-Break/key injection
- pipe transport implementation
- retry or reconnect behavior

## Reasoning

The current host-control server is synchronous. The shared session loop reads one request, executes it with `SHELL_ExecuteHostCommand()`, then emits completion events. While `exec` is running, the same stdio or socket session is not reading another JSON request.

Because of that, a same-connection server-side `cancel` request would not be observed until after the command it is meant to cancel has already returned. A reliable server-side cancel needs a larger design involving a separate control plane, asynchronous shell execution, or carefully integrated keyboard/break injection.

Client-side timeout handling is the right first recovery step. It gives agents a bounded wait and a clear failure mode while preserving the existing protocol and server implementation.

## User-Facing Behavior

The client accepts a global timeout option before the transport:

```bash
scripts/host_control_client.py --timeout 5 socket /tmp/dosboxx.sock exec "build.bat"
scripts/host_control_client.py --timeout 5 stdio status -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec
scripts/host_control_client.py --timeout 10 socket /tmp/dosboxx.sock repl
```

Timeout values are seconds and may be fractional. `--timeout 0` is invalid.

If no timeout is provided, behavior remains unchanged.

## Timeout Semantics

Timeouts apply to each wait for protocol progress:

- waiting for the initial `ready` event
- waiting for a matching `status` or `error` event after a `status` request
- waiting for a matching `result` or `error` event after an `exec` request

Any event received before the timeout is printed to stdout unchanged. If the request does not complete before the timeout expires, the client prints a concise error to stderr and exits non-zero.

For REPL mode, the timeout applies independently to each submitted request. If a REPL request times out, the REPL exits non-zero.

## Recovery Behavior

### Stdio

The stdio transport owns the spawned DOSBox-X process. On timeout, the client:

1. closes the child stdin if possible
2. sends terminate to the child process
3. waits briefly for exit
4. sends kill if the child does not exit

This makes stdio timeout recovery strong enough for local automation.

### Socket

The socket transport does not own DOSBox-X. On timeout, the client closes its socket and exits non-zero.

Closing the socket releases the client, but it does not guarantee that DOSBox-X interrupts the DOS command immediately. The server may keep running until the current command returns and then observe the disconnect.

## Error Handling

- Invalid timeout values fail argument parsing before connecting or spawning.
- Timeout errors include the active operation and request id when applicable.
- Timeout errors go to stderr.
- Raw JSON events received before timeout are still printed to stdout.
- Existing malformed JSON and EOF behavior remains unchanged.

## Client Architecture

Add a small timeout-aware read helper rather than rewriting the transport layer.

The current transports expose `readline()`, `writeline()`, and `close()`. This milestone extends them with:

- `fileno()` so the client can wait for readability
- `abort()` so timeout recovery can differ for stdio and socket

`read_event_line()` should accept an optional timeout deadline. It waits for the transport file descriptor to become readable with `select.select()` before calling `readline()`. This keeps the implementation small and uses Python stdlib only.

The timeout value is passed through:

- `run_one_shot()`
- `run_repl()`
- `run_request()`
- `read_event_line()`

## Testing Strategy

Automated tests should use stubs rather than full DOSBox-X integration.

Add tests for:

- argument parsing rejects zero and negative timeout values
- socket one-shot timeout exits non-zero when the server sends `ready` but no completion event
- socket timeout preserves any output event received before the timeout
- stdio timeout terminates a spawned child that sends `ready` but never completes the request
- existing socket, stdio, and REPL parser tests remain green

Runtime verification should include:

- existing Python client tests
- existing focused host-control tests
- one live stdio or socket smoke with a short timeout and a normal quick command

## Future Work

This milestone intentionally leaves server-side interruption for a later design.

A future server-side cancellation milestone should choose one of these directions:

- a second control connection that can request break while the main request is running
- asynchronous shell execution with carefully bounded DOSBox-X state access
- an explicit Ctrl-Break/key injection primitive built on existing DOSBox-X break and keyboard paths
