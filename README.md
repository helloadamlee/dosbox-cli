# dosbox-cli

A fork of [DOSBox-X](https://github.com/joncampbell123/dosbox-x) that completes the
**host-control pipe interface** — a newline-delimited JSON transport for driving DOSBox-X
programmatically over stdio, a Unix domain socket, or a named pipe. Built primarily for
agentic and automated workflows that need to run DOS shell commands and consume DOS
output as structured events, rather than screen-scraping a terminal emulator.

DOSBox-X referenced this capability but never finished it. This fork implements it.

## Status

| Transport | Platform | Status |
|---|---|---|
| stdio | Unix-like | ✅ Working |
| Unix domain socket | Unix-like | ✅ Working |
| Named pipe | Unix-like (FIFO) | ✅ Working |
| Named pipe | Windows | ✅ Working — see [`docs/host-control.md`](docs/host-control.md) |

## MCP server

For agentic use, [`mcp-server/`](mcp-server/) packages the protocol as six
typed MCP tools — `start_session`, `exec`, `poll`, `send_input`, `cancel`,
`stop_session` — instead of hand-rolled NDJSON. It's async-with-polling, so
no tool call blocks on a running DOS command. See
[`mcp-server/README.md`](mcp-server/README.md) for install and the full tool
table.

## Quick start

Start DOSBox-X with one of the host-control transports directly, if you'd
rather talk to the raw protocol than use the MCP server above:

```bash
# stdio: requests on stdin, events on stdout
./src/dosbox-x -control-stdio -headless -noconfig -noautoexec

# Unix domain socket
./src/dosbox-x -control-socket /tmp/dosboxx.sock -headless -noconfig -noautoexec

# Named pipe (Unix FIFO pair)
./src/dosbox-x -control-pipe /tmp/dosboxx-control -headless -noconfig -noautoexec
```

Send a request (one JSON object per line):

```json
{"id":"1","op":"exec","command":"dir"}
```

Receive events back, e.g.:

```json
{"event":"ready","transport":"socket","endpoint":"/tmp/dosboxx.sock"}
{"event":"output","id":"1","encoding":"base64","data":"aGkNCg=="}
{"event":"result","id":"1","ok":true,"shell_exit":false,"errorlevel":0,"drive":"Z"}
```

A reference Python client is included at [`scripts/host_control_client.py`](scripts/host_control_client.py).

Full protocol reference — transports, workflow recipes, failure semantics,
and known limitations — lives in [`docs/host-control.md`](docs/host-control.md).

## Why

DOSBox-X is a full-featured DOS emulator, but it has no native stdin/stdout automation
interface — everything assumes an interactive terminal window. That makes it hard to
drive from scripts, test harnesses, or LLM agents that want to run DOS programs and
read structured results back. This fork adds that interface without touching the
emulation core.

## Building

This is a DOSBox-X source fork, so upstream build instructions apply — see
[`BUILD.md`](BUILD.md) and [`INSTALL.md`](INSTALL.md). Platform build scripts live in
[`build-scripts/`](build-scripts/).

## Relationship to upstream

This repo is a fork of [DOSBox-X](https://github.com/joncampbell123/dosbox-x), a
feature-rich DOSBox variant. All emulation functionality, licensing (GPL-2.0), and
general documentation come from upstream — see [`COPYING`](COPYING) and
[`CREDITS.md`](CREDITS.md). The host-control pipe interface is the addition made here.

## License

GPL-2.0, inherited from DOSBox-X.
