# DOSBox-X Host Control

DOSBox-X host control is a newline-delimited JSON interface for local automation.
It is intended for solo developer and agent workflows that need to run DOS shell
commands, query basic DOS state, and consume raw JSON events.

## Transports

### Stdio

Start DOSBox-X with `-control-stdio`:

```bash
./src/dosbox-x -control-stdio -headless -noconfig -noautoexec
```

Requests are read from stdin. Events are written to stdout.

### Unix Domain Socket

Start DOSBox-X with `-control-socket <path>`:

```bash
./src/dosbox-x -control-socket /tmp/dosboxx.sock -headless -noconfig -noautoexec
```

DOSBox-X creates the socket, accepts one local client, and removes the socket
path when the session ends.

### Pipe

`-control-pipe <path>` is parsed, but pipe transport is not implemented yet. It exits the host-control startup path with a clear stderr message instead of starting a normal interactive shell.

## Requests

Every request is one JSON object followed by a newline.

Run a DOS shell command:

```json
{"id":"1","op":"exec","command":"dir"}
```

Query current host-control state:

```json
{"id":"2","op":"status"}
```

Queue printable ASCII text for DOS keyboard input:

```json
{"id":"7","op":"input_text","text":"dir\r"}
```

Queue a named key:

```json
{"id":"8","op":"key","key":"enter"}
```

`input_text` and `key` are socket-only in Milestone 4. `input_text` accepts
printable ASCII; `\r` and `\n` are normalized to Enter. Non-ASCII text is
rejected. Supported keys are `enter`, `escape`, `tab`, `backspace`, `up`,
`down`, `left`, and `right`.

Request ids are caller-defined strings. The included client uses monotonically
increasing ids starting at `1`.

## Events

The first event on a new connection is `ready`:

```json
{"event":"ready","transport":"socket","endpoint":"/tmp/dosboxx.sock"}
```

Command output is emitted as raw DOS console bytes encoded with base64:

```json
{"event":"output","id":"1","encoding":"base64","data":"aGkNCg=="}
```

Completed `exec` requests emit `result`:

```json
{"event":"result","id":"1","ok":true,"shell_exit":false,"errorlevel":0,"drive":"Z","cwd":"Z:\\","duration_ms":1}
```

Completed `status` requests emit `status`:

```json
{"event":"status","id":"2","transport":"socket","session_active":true,"errorlevel":0,"drive":"Z","cwd":"Z:\\"}
```

Completed `input_text` and `key` requests emit `input_result`:

```json
{"event":"input_result","id":"7","ok":true,"queued":4}
```

`queued` is the number of BIOS keyboard-buffer entries accepted into the
host-control input queue. Completion means input was accepted by DOSBox-X, not
that the guest has processed it.

Malformed or unsupported requests emit `error`:

```json
{"event":"error","id":"3","message":"unsupported op"}
```

Events are part of the protocol stream. Consumers should preserve their raw
line order and avoid assuming that command output is text.

## Client

The repository includes a small Python 3 stdlib client:

```bash
scripts/host_control_client.py socket /tmp/dosboxx.sock status
scripts/host_control_client.py socket /tmp/dosboxx.sock exec "echo hi"
scripts/host_control_client.py socket /tmp/dosboxx.sock input-text $'dir\n'
scripts/host_control_client.py socket /tmp/dosboxx.sock key enter
scripts/host_control_client.py socket /tmp/dosboxx.sock repl
```

For stdio mode, the client spawns DOSBox-X. The spawned command must include
`-control-stdio`:

```bash
scripts/host_control_client.py stdio status -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec
scripts/host_control_client.py stdio exec "echo hi" -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec
scripts/host_control_client.py stdio repl -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec
```

Use `--timeout <seconds>` before the transport to bound how long the client
waits for each protocol response:

```bash
scripts/host_control_client.py --timeout 5 socket /tmp/dosboxx.sock exec "echo hi"
scripts/host_control_client.py --timeout 5 stdio status -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec
```

Timeouts are a client-side recovery feature. In stdio mode, the client owns the
spawned DOSBox-X process and terminates it on timeout. In socket mode, the client
closes its socket; DOSBox-X may continue the current DOS command until it
returns. Socket input requests remain responsive while an `exec` request is
running.

The client writes raw JSON events to stdout. REPL prompts and local help are
written to stderr so stdout remains machine-readable.

REPL commands:

- `status`
- `exec <command>`
- `input <text>`
- `key <name>`
- `help`
- `quit`

## Limits

Current host control is intentionally small:

- one control client per socket session
- no reconnect loop
- no server-side command cancellation
- input injection is socket-only and limited to printable ASCII text plus a small
  named-key set
- no pipe transport implementation
