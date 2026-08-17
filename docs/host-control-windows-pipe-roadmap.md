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
- Session end is a normal outcome on both sides. `ERROR_BROKEN_PIPE`,
  `ERROR_NO_DATA`, and `ERROR_PIPE_NOT_CONNECTED` are all treated as "the peer
  closed", so a DOS shell exit reports `shell_exit` and a single readable
  `host control session ended` message instead of a raw Win32 pipe error.
- The existing NDJSON wire contract and Unix socket/FIFO behavior are unchanged.

## Fixed after the first end-to-end build run

Driving a real DOS build (NBA Hangtime, `build.bat`) over the pipe found one
shipping defect and two contract gaps:

- The client classified only `ERROR_BROKEN_PIPE` as a closed peer. Windows
  reports `ERROR_PIPE_NOT_CONNECTED` (233) when the server exits after its final
  event, which is exactly what a DOS shell exit does. Every workflow ending in
  `exit` — the normal shape of a DOS build batch — therefore ended in a Python
  traceback. The client now treats all three close codes as end of session, and
  `WorkflowRuntime` refuses further requests after a `shell_exit` result with a
  message that names the cause.
- `result.max_errorlevel` was on the wire but in no document. It is the highest
  exit status seen during a request, which is the field batch-driven builds
  actually need, and it is now documented alongside its `expect_errorlevel`
  caveat.
- `shell_exit` semantics were undocumented. `docs/host-control.md` now has a
  "Session end" section.
- `[autoexec]` never runs under host control, because host control replaces the
  shell run loop that executes `AUTOEXEC.BAT` (`src/shell/shell.cpp`, where
  `run_pipe_shell()` stands in for `first_shell->Run()`). Every other config
  section still applies, so the failure is silent and confusing: a config that
  worked under plain `-conf` loses its mount, `PATH`, and `TEMP`/`TMP` and the
  build fails for unrelated-looking reasons. Documented under "The session
  starts at a bare prompt".
- A command DOS cannot find completes with `"ok":true` and `errorlevel` 0, so a
  missing or misspelled build tool reads as a successful step. Documented; the
  real fix is to report it as a distinct condition (see Deferred).
- `cancel`/`break` now actually stops a running batch. `DOS_Shell::RunInternal`
  checks `host_control::is_cancel_requested()` on every batch-line boundary
  (`src/shell/shell.cpp`) and unwinds the whole `goto`/batch chain instead of
  only setting `ctrlbrk` and queueing an ignored Ctrl-C. The `result` event now
  carries `cancelled: true` when this happens, so a caller sees an honest
  outcome instead of the old `ok:true` false positive. Verified live: a batch
  looping on `goto` (52 KB of output and growing) stopped within ~100 ms of a
  `cancel` request, the `result` reported `cancelled: true`, and the session
  stayed usable for a follow-up `exec` — no `stop_session` needed. It only
  interrupts at that batch-line boundary, so a program in a tight loop with no
  console reads can still ignore it; `stop_session` remains the fallback for
  that case. The MCP server (`mcp-server/`) now exposes this as a `cancel`
  tool.

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
- ~~Server-side cancellation of a running DOS command~~ — fixed; see "Fixed
  after the first end-to-end build run" above. A guest program that loops
  forever printing output but doesn't hit a batch-line boundary (a DOS tool
  re-prompting after its redirected stdin hits EOF, for example) still won't
  respond to `cancel` — the client's `--timeout` / the MCP server's
  `stop_session` remain the way out of that case.
- Reporting "command not found" as a distinct condition. `DoCommand()` in
  `src/shell/shell_cmds.cpp` writes `SHELL_EXECUTE_ILLEGAL_COMMAND` and returns
  without setting an errorlevel or any status a caller can see, so
  `SHELL_ExecuteHostCommand` has nothing to report. The additive fix is a flag
  set at that branch, cleared per host command, surfaced as a new result field
  (for example `bad_command`) so `ok` and `errorlevel` keep their current
  meanings.
- Running `[autoexec]` before handing over to the control session, or a startup
  option that opts into it. Today the commands must be reissued as `exec`
  requests.
- An `expect_max_errorlevel` workflow option. `max_errorlevel` is reported and
  documented, but the client's pass/fail policy still reads `errorlevel` only,
  so a batch that fails midway and ends on a successful command passes.
- The separate ROM/VIDRAM emulator-core defect, which is out of scope for this
  work.

  Note on diagnosing it: `DOS fatal memory error: Corrupt MCB chain` during the
  Hangtime `gmake` run is *usually a config error, not the defect*. The project's
  `build.conf` needs `[cpu] core = full` and `[dos] xms/ems/umb = true`, and the
  MCB workaround key is `[dos] mcb corruption becomes application free memory`
  — under `[dosbox]` it is silently ignored. With the correct config the compile
  stage runs to completion (all 51 `.OBJ`, no errors).

  What does reproduce is at the *link* stage under `-headless`: `vidram on`
  reports `Extra RAM: EGA memory, Memory end: 736K` even though SDL is using the
  dummy video driver, and `gsplnk` then emits garbled symbol names
  (`multiply defined: PLYR.OBJ and PLYR.OBJ`, non-ASCII names) and
  `symbol table overflow`. That is consistent with VIDRAM's extra conventional
  RAM not being backed correctly when there is no real video output. Compare a
  windowed run before blaming host control.

  Open question, not yet resolved: a windowed (non-`-headless`) run launched via
  background automation (no real OS focus ever given to the window) stalled on
  a trivial `exec c:` for the full 37s wait, right after a `mount` that
  completed in 16ms. `[sdl] priority` was left at its default (`higher,normal`),
  which should not fully pause an unfocused window, so the stall does not match
  the known focus-loss pause path in `sdlmain.cpp`. Whether this is a real host
  control defect (request processing blocking on the render/present path) or an
  artifact of spawning the window without real focus is unresolved — it needs
  testing on an interactive desktop to isolate. If it reproduces there too,
  host control blocking on window focus is a bug worth its own investigation.

## Resolved questions

- Endpoint scope: only short names and local `\\.\pipe\<name>` paths are
  accepted; other path-like values are rejected.
- Connection model: the server uses `ConnectNamedPipe` for its single client,
  with cancellation-safe teardown on shutdown.
- Client count: one client per DOSBox-X process; reconnect is a future item.
- CI: a Windows runner runs the same gate script used locally.
