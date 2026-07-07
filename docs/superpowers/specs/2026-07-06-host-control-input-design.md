# Host Control Input Design

## Goal

Add a minimal host-control input path so a local agent or script can send keyboard input to a running DOSBox-X session after launching or reaching an interactive DOS program.

The first useful target is automation of command prompts, setup programs, installers, and simple menu flows. This milestone should move host control toward full automation without trying to become a full terminal emulator or game input system.

## Scope

This milestone adds socket-based input injection.

Included:
- new host-control request operations for text input and a small set of named keys
- socket transport support while DOSBox-X is running and accepting input
- host-side client commands for sending text and named keys over a socket
- raw JSON event fidelity for all new events
- focused protocol tests for request parsing and response generation
- focused input-queue tests where practical
- at least one live smoke that mounts a real project, starts an interactive command or prompt, sends input, and observes completion

Not included:
- stdio input injection
- full terminal emulation
- screenshots, OCR, or screen scraping
- mouse input
- joystick/game input
- held key timing or key-up/key-down APIs
- multi-client socket control
- authentication or shared-system hardening
- server-side cancellation of long-running commands

## Approaches Considered

### 1. Socket input queue with main-thread injection (recommended)

Keep `exec` and `status` semantics small, add input-specific requests, and enqueue input work for DOSBox-X to apply from its normal execution context.

Pros:
- keeps the protocol simple and local-script friendly
- avoids direct DOSBox-X state mutation from a socket thread
- works while guest code is running if socket polling is decoupled from shell `exec`
- leaves stdio unchanged

Cons:
- requires a small async socket-control path
- requires careful selection of the injection point

### 2. Make `exec` asynchronous

Allow one connection to start an `exec` request and continue accepting other requests while it runs.

Pros:
- one conceptual session can run command plus input
- enables future cancellation and richer lifecycle events

Cons:
- changes the meaning of `exec` substantially
- creates more state-management risk around shell execution
- larger than needed for first input support

### 3. Reuse mapper/autotype directly

Translate host-control input into existing mapper/autotype events.

Pros:
- uses an existing input path
- may already understand named key events

Cons:
- may depend on GUI/event-loop assumptions
- less clear for headless socket automation
- needs extra validation before relying on it as the primary API

Recommendation:
- implement approach 1 first
- borrow existing keyboard/mapping helpers only after verifying they are safe from the chosen execution context

## User-Facing Protocol

Add two request operations.

### `input_text`

Request:

```json
{"id":"7","op":"input_text","text":"dir\r"}
```

Behavior:
- queues the text for keyboard injection
- preserves the request id in the response
- returns a completion event after the text is accepted into the host-control input queue
- does not wait for the guest program to consume the input

Completion event:

```json
{"event":"input_result","id":"7","ok":true,"queued":4}
```

`queued` is the number of ASCII input characters accepted by the host-control queue after newline normalization.

### `key`

Request:

```json
{"id":"8","op":"key","key":"enter"}
```

Behavior:
- queues one named key press
- initially supports a deliberately small key set
- returns a completion event after the key is accepted into the host-control input queue

Initial named keys:
- `enter`
- `escape`
- `tab`
- `backspace`
- `up`
- `down`
- `left`
- `right`

Completion event:

```json
{"event":"input_result","id":"8","ok":true,"queued":1}
```

Unsupported keys return an `error` event with the matching id.

## Client Behavior

Extend `scripts/host_control_client.py` socket mode with:

```bash
scripts/host_control_client.py socket /tmp/dosboxx.sock input-text "dir"
scripts/host_control_client.py socket /tmp/dosboxx.sock key enter
scripts/host_control_client.py socket /tmp/dosboxx.sock repl
```

REPL additions:
- `input <text>` sends `input_text`
- `key <name>` sends `key`

The client still prints raw server JSON lines exactly as received. It may parse events internally only to detect completion.

Stdio mode should reject or omit these input actions for this milestone. Stdio can be revisited later if a real use case needs it.

## Server Architecture

The current host-control session model is synchronous: read one request, execute it, emit completion, then read the next request. That cannot support same-connection input during a running guest program.

Milestone 4 should introduce a socket-specific control path that keeps reading socket requests independently from shell command execution. The design constraint is important:

- the socket reader may parse requests and enqueue work
- the socket reader must not directly mutate DOSBox-X keyboard or DOS state from an unsafe thread
- the emulator/main execution context drains queued input and injects it into the existing keyboard path

`status` and input requests can complete as soon as their response data is available. `exec` should keep its current completion behavior unless a later milestone explicitly designs asynchronous exec lifecycle events.

If this creates too much coupling with the existing `run_control_session()` helper, the implementation should split shared protocol parsing/formatting from transport/session orchestration rather than forcing all transports into one loop.

## Input Injection Semantics

`input_text` is for text and simple control characters:
- printable ASCII should be supported first
- `\r` should map to Enter
- `\n` should be accepted and normalized to Enter, unless implementation evidence shows this is unsafe
- non-ASCII text should return an error in this milestone

`key` is for non-text named keys.

The implementation should prefer DOSBox-X's normal keyboard input path over direct BIOS buffer mutation if practical. Direct BIOS buffer insertion is acceptable only if investigation shows it is the safest narrow path for command prompt and installer automation.

Input completion means "accepted by DOSBox-X host control", not "processed by the guest". Guest-visible completion remains observable through existing output/status mechanisms.

## Error Handling

Protocol errors:
- missing `text` for `input_text` returns `error`
- empty `key` or unsupported named key returns `error`
- invalid JSON behavior remains unchanged from existing host control

Queue errors:
- if the input queue is full, return `error`
- do not silently drop input
- keep error text concise and machine-readable enough for scripts to report

Transport errors:
- socket disconnect should stop that control session cleanly
- queued input already accepted may still be delivered
- queued input not yet accepted should not be reported as accepted

## Testing Strategy

Use TDD.

Protocol tests:
- parse `input_text` with text
- reject `input_text` without text
- parse `key` with supported key name
- reject unsupported key names
- emit `input_result` with id and queued count

Server/input tests:
- socket request path accepts input while a session is active
- queued text is drained in order
- queue-full behavior returns `error`
- unsupported keys do not enqueue input

Client tests:
- socket `input-text` sends `{"op":"input_text","text":...}`
- socket `key` sends `{"op":"key","key":...}`
- REPL accepts `input <text>` and `key <name>`
- raw `input_result` lines are printed unchanged
- stdio rejects input actions if those actions are socket-only

Live smoke:
- start DOSBox-X with `-control-socket`
- mount the supplied real project path
- switch to `C:`
- send text plus Enter to execute a simple command such as `dir`
- verify raw output and `input_result` events are present
- verify DOSBox-X exits cleanly after the socket session closes

## Milestone Boundary

This milestone is complete when a script can:

1. connect to DOSBox-X over a Unix socket
2. run setup/navigation commands with existing `exec`
3. send text or Enter while DOSBox-X is still alive
4. receive raw JSON events in order
5. recover locally with existing client timeout behavior

It is not complete only because arbitrary DOS games can be played or because every keyboard key is supported. The goal is practical automation, not exhaustive input emulation.

## Future Work

Likely follow-on milestones:
- asynchronous `exec` lifecycle events
- server-side cancellation or Ctrl-Break
- richer key support with press/release timing
- screen/screenshot observation
- mouse input
- stdio input support if needed
- multi-client control policy
