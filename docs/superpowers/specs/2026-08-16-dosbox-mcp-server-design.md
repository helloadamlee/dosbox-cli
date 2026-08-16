# DOSBox-X Host-Control MCP Server Design

## Goal

Package the host-control pipe protocol as an MCP server so agents can drive a
DOS build or session as typed tool calls instead of hand-rolling NDJSON
framing, base64 decoding, and transport lifecycle. This is a portfolio-facing
v1: usable and demoable now, with known gaps documented rather than blocking
scope creep. Non-critical protocol bugs (the broken `cancel` op, upstream
`[autoexec]`/bad-command fixes) stay tracked in
`docs/host-control-windows-pipe-roadmap.md` and are explicitly out of scope
here.

## Non-goals (v1)

- Multiple concurrent sessions. One DOSBox-X process at a time, matching
  host-control's own one-client-per-process limit.
- A `workflow`/recipe tool. Multi-step sequences are repeated `exec`+`poll`
  calls from the agent's own turn loop; adding a batched tool would reintroduce
  the long-blocking-call problem async-with-polling avoids.
- A `cancel` tool. The protocol's `cancel`/`break` op doesn't actually stop a
  running program (verified this session: output kept growing after
  `cancel_result: ok:true`). `stop_session` (kill the process) is the only
  abort path.
- Fixing the underlying protocol gaps server-side (DOSBox-X `[autoexec]` not
  running under host control, bad-command detection). This package works
  around them client-side; see "Known-gap workarounds" below.

## Architecture

One MCP server process manages at most one DOSBox-X child process. On
`start_session`, the server:

1. Picks a unique pipe/socket endpoint name (uuid4-based, same scheme as
   `tests/host_control_live_tests.py` already uses).
2. Resolves the binary: `binary_path` if given, else the same search-path
   logic as the project's `build.py` (`get_dosbox_path()`) — a platform list of
   known install locations, falling back to `PATH`. Fails clearly if none
   found. `config_path` is optional and simply omitted from the launch command
   when absent, so DOSBox-X falls back to its own default config discovery.
3. Launches `dosbox-x -control-<transport> <endpoint> -headless -conf <config>`
   in the requested working directory.
4. Connects via `host_control_client`'s existing transport classes
   (`PipeTransport` on Windows, `SocketTransport` on Unix).
5. Spawns one background reader thread that continuously drains NDJSON events
   off the transport into an in-memory `SessionState`, guarded by a lock:
   accumulated decoded output (since last poll), last `result` event, current
   request id, drive/cwd, errorlevel/max_errorlevel, done flag.

This is the key structural move: every tool handler is non-blocking on the
pipe. `exec` and `send_input` write a request and return immediately; `poll`
and `status` read the current `SessionState` snapshot. The reader thread
replaces what was, this session, a manual foreground `read_event_line` loop —
moved off the tool-call path so async-with-polling actually works instead of
just relocating the blocking wait into `poll`.

### Session state machine

`idle` → `starting` → `ready` ⇄ `busy` (in-flight `exec`) → `stopped`.

- `exec` requires `ready`, transitions to `busy`.
- `poll` transitions `busy` → `ready` once the in-flight request completes.
- `send_input` requires `busy`.
- `stop_session` works from any state: closes the pipe handle, waits up to 2
  seconds for the process to exit on its own (mirrors the shell-exit path
  fixed this session), then kills it if still alive. `force:true` skips the
  wait and kills immediately.
- A second `start_session` while a session is active fails with a clear error;
  callers must `stop_session` first.

## Tool surface

| Tool | Params | Returns |
|---|---|---|
| `start_session` | `binary_path?`, `config_path?`, `cwd`, `mount?` ({drive, host_path}), `dos_path?`, `env?` (dict of DOS `set` vars) | `{session_active, drive, cwd, pid, setup_results}` |
| `exec` | `command` | `{request_id}` — returns immediately |
| `poll` | `wait_seconds?` (bounded long-poll, default ~2s, max ~10s) | `{running, done, output, errorlevel, max_errorlevel, ok, bad_command, drive, cwd}` |
| `send_input` | `text?` or `key?` (exactly one) | `{queued}` |
| `status` | — | `{session_active, drive, cwd}` |
| `stop_session` | `force?` | `{stopped}` |

`poll.output` is the *new* decoded text (cp437, base64 stripped) since the
last poll, not full history, so an agent can stream output across repeated
calls without re-reading everything. Once `done`, later polls return
`done:true` with no new output until the next `exec`.

## Known-gap workarounds

**`[autoexec]` replacement.** Host control replaces the DOS shell's normal run
loop, so `AUTOEXEC.BAT` — and therefore a config's `[autoexec]` section — never
runs (see `docs/host-control.md`, "The session starts at a bare prompt").
`start_session`'s `mount`/`dos_path`/`env` params are real setup steps, not
just metadata: the server issues them as `exec` requests immediately after
connecting, before returning — `mount c "<host_path>"`, `c:`,
`path <dos_path>` (default `Z:\;C:\;C:\TOOLS`), then one `set VAR=value` per
`env` entry. `setup_results` reports each step's `ok`/`errorlevel` so a bad
mount path fails at session start, not on the first real `exec`.

**Bad-command detection.** DOSBox-X reports a DOS command it can't find as
`ok:true, errorlevel:0` with no distinguishing signal (see roadmap). When
`poll` reports `done`, the server scans the accumulated output for the literal
string `Bad command or filename` and sets `bad_command: true`. This is a
client-side heuristic, not a protocol fix. If a future DOSBox-X version adds a
real `bad_command` field to the `result` event, the server should prefer that
field and only fall back to string-scanning when it's absent, so the
workaround degrades to a no-op rather than a wrong answer.

**Hung/runaway commands.** Since `cancel` doesn't work, a command that never
completes (this session: a linker's `.MOT` re-prompt loop after EOF) just
accumulates output forever. `poll` does not impose its own timeout — the
calling agent decides how many polls is too many — but `stop_session` is the
documented answer: kill the process, start a fresh session.

## Packaging

- **Location:** `mcp-server/`, sibling to `scripts/`, `src/`, `docs/` in
  `upstream-dosbox-x` (which is what `helloadamlee/dosbox-cli` on GitHub
  actually is — there is no separate tooling repo).
- **Package:** `dosbox-x-mcp`, console-script entry point `dosbox-mcp`, built
  on the official `mcp` Python SDK using its FastMCP-style `@mcp.tool()`
  decorator API.
- **Reusing `host_control_client.py`:** a `sys.path` shim at import time
  (`Path(__file__).resolve().parents[1] / "scripts"`) rather than vendoring or
  duplicating transport/framing code, since `mcp-server/` and `scripts/` are
  siblings under the same repo. Promoting `scripts/` to a real installable
  dependency is a viable later refactor, not needed for v1.

## Testing

- Unit tests mock the transport layer (same `FakeWindowsPipeApi`-style pattern
  already in `tests/host_control_client_tests.py`) to cover `SessionState`
  transitions, the autoexec-equivalent setup sequencing, and the bad-command
  scan, without needing a real DOSBox-X process.
- One opt-in integration test, gated like the existing `DOSBOX_X_LIVE_TESTS`
  env var, starts the real MCP server as a subprocess, drives it through the
  `mcp` SDK's client test utilities, and runs a trivial DOS command end-to-end
  against a real binary.
