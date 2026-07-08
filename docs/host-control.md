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

### Workflow recipes

Workflow mode runs a JSON recipe with sequential host-control steps:

```bash
scripts/host_control_client.py --timeout 10 socket /tmp/dosboxx.sock workflow recipe.json
scripts/host_control_client.py --timeout 10 --transcript run.jsonl socket /tmp/dosboxx.sock workflow recipe.json
scripts/host_control_client.py --timeout 10 stdio workflow recipe.json -- ./src/dosbox-x -control-stdio -headless -noconfig -noautoexec
```

Recipe files are JSON only and use a top-level `steps` array:

```json
{
  "steps": [
    {"comment": "Mount and inspect a project"},
    {"exec": "mount c /home/me/project"},
    {"wait_for": {"event": "result", "ok": true}},
    {"status": true},
    {"input_text": "dir\n"},
    {"key": "enter"},
    {"wait_for": "input_result"},
    {}
  ]
}
```

Supported steps:

- `{"exec":"command"}` sends an `exec` request and waits for `result`
- `{"exec_interactive":{"command":"setup.exe","steps":[...]}}` sends an `exec`
  request, runs nested workflow steps while that command is active, and then
  ensures the command reaches `result`
- `{"status":true}` sends a `status` request and waits for `status`
- `{"input_text":"text"}` sends an `input_text` request and waits for
  `input_result`
- `{"key":"enter"}` sends a `key` request and waits for `input_result`
- `{"wait_for":"output"}` waits for the next matching event without sending a
  request
- `{"wait_for":{"event":"result","ok":true}}` waits for an event whose listed
  fields all match exactly
- `{"comment":"text"}` and `{}` are no-ops

String `wait_for` aliases are `ready`, `output`, `result`, `status`, `error`,
and `input_result`. Object matchers are shallow exact field matches. Workflow
mode does not decode output, run regular expressions, or support variables,
conditionals, loops, or parallel steps.

Socket workflows may use every step type. Stdio workflows reject `input_text`
`key`, and `exec_interactive` steps before spawning DOSBox-X because
host-control input injection is socket-only.

Use `exec_interactive` for commands or programs that need input while their
output is being captured:

```json
{
  "steps": [
    {
      "exec_interactive": {
        "command": "setup.exe",
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

Nested `exec_interactive` steps support `wait_for`, `input_text`, `key`,
`status`, comments, and no-ops. Nested request ids continue from the same
monotonic workflow sequence as top-level steps.

Keyboard input queued at the idle DOS prompt can be accepted, but it does not
currently produce host-control `output` events unless an `exec` output-capture
context is active. If a workflow needs to wait on output from a prompt,
installer, or menu, run that program with `exec_interactive` and send input from
inside its nested steps.

When `--transcript <path>` is provided, workflow mode writes one JSON object per
event to the JSONL transcript while preserving stdout exactly as raw server
events:

```json
{"type":"event","raw":"{\"event\":\"ready\",\"transport\":\"socket\"}\n","event":{"event":"ready","transport":"socket"}}
```

Workflow failures exit nonzero and write diagnostics to stderr. Timeout and
server-error diagnostics include the failing step index, action name, the error,
and recent raw events so an agent can report useful context.

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
