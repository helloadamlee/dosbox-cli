# Host-Control Windows Pipe Roadmap

## Status

Roadmap item. Host-control pipe transport currently supports Unix-like systems
with a FIFO pair created from `-control-pipe <base-path>`:

- `<base-path>.in` receives client requests.
- `<base-path>.out` sends server events.

Windows named pipes are not implemented yet. On non-Unix platforms,
`-control-pipe` should continue to report a clear unsupported-platform message
until this roadmap item is implemented and validated.

## Goal

Add Windows named-pipe support for the existing host-control JSON protocol so
Windows users can run DOSBox-X with `-control-pipe <name-or-path>` and attach
the existing Python client workflows without Unix sockets, FIFO paths, or
Hangtime-specific setup.

## Proposed User Experience

Accept both short names and full Windows pipe paths:

```bat
dosbox-x.exe -control-pipe dosbox-x-control -headless -noconfig -noautoexec
python scripts\host_control_client.py pipe dosbox-x-control status
python scripts\host_control_client.py pipe dosbox-x-control exec "echo hi"
python scripts\host_control_client.py pipe dosbox-x-control workflow examples\host-control\status.json
python scripts\host_control_client.py pipe dosbox-x-control repl
```

The implementation should normalize the short name `dosbox-x-control` to:

```text
\\.\pipe\dosbox-x-control
```

Full pipe paths such as `\\.\pipe\dosbox-x-control` should also be accepted.

## Recommended Design

Use one full-duplex Windows named pipe instead of mirroring the Unix FIFO pair.
This matches the native Windows API and avoids managing separate inbound and
outbound pipe names.

Server side:

- Create the pipe with `CreateNamedPipeW`.
- Use `PIPE_ACCESS_DUPLEX`.
- Use byte mode or message mode with newline-delimited JSON preserved exactly.
- Accept one client per host-control session, matching socket and Unix pipe
  behavior.
- Pass the connected pipe handle into a transport-neutral session runner that
  preserves the current JSON protocol and event ordering.

Client side:

- Keep the public command shape as `pipe <path-or-name> ...`.
- On Windows, open `\\.\pipe\<name>` with Windows file APIs.
- Preserve current workflow, transcript JSONL, timeout, diagnostics, REPL, and
  stdout/stderr behavior.
- Prefer Python stdlib and `ctypes` over a required `pywin32` dependency unless
  stdlib-only behavior proves too fragile.

## Code Areas

Expected server-side files:

- `include/host_control.h`
- `src/misc/host_control.cpp`
- Any existing platform helper headers if the final design needs a small
  Windows handle wrapper.

Expected client-side file:

- `scripts/host_control_client.py`

Expected tests:

- `tests/host_control_protocol_tests.cpp`
- `tests/host_control_client_tests.py`
- `tests/host_control_live_tests.py`

Expected docs:

- `docs/host-control.md`
- This roadmap file can be replaced or moved into release notes when the work is
  implemented.

## Implementation Tasks

1. Add Windows endpoint normalization.
   - Input `dosbox-x-control` becomes `\\.\pipe\dosbox-x-control`.
   - Input already beginning with `\\.\pipe\` is kept as-is.
   - Empty names are rejected with a clear diagnostic.

2. Split pipe session I/O from Unix file-descriptor assumptions.
   - Keep the current JSON request parser and response builders unchanged.
   - Keep stdout event fidelity and output/result ordering unchanged.
   - Isolate OS-specific read, write, close, disconnect, and timeout behavior.

3. Add a Windows pipe server.
   - Create one duplex named pipe.
   - Wait for one client.
   - Run the existing host-control session over the connected handle.
   - Close the handle cleanly when the session ends or the client disconnects.

4. Add Windows client transport support.
   - Open the named pipe for read/write.
   - Reuse the current `PipeTransport` command shape.
   - Keep missing-endpoint and timeout diagnostics actionable.

5. Add tests before implementation.
   - Parser/client validation for short names and full pipe names.
   - Missing endpoint diagnostics.
   - Fake or deterministic transport tests for workflow behavior.
   - Native lifecycle tests where they can run on Windows.
   - Opt-in live Windows named-pipe smoke test.

6. Update documentation.
   - Document Unix FIFO pair behavior separately from Windows named-pipe
     behavior.
   - Provide Windows command examples.
   - Keep limitations explicit.

## Validation Plan

Minimum non-Windows validation:

```bash
python3 -m unittest tests.host_control_client_tests tests.host_control_live_tests
./src/dosbox-x -tests --gtest_filter='*HostControl*'
./src/dosbox-x -tests
```

Minimum Windows validation:

```bat
python -m unittest tests.host_control_client_tests tests.host_control_live_tests
src\dosbox-x.exe -tests --gtest_filter=*HostControl*
```

Opt-in live Windows smoke:

```bat
set DOSBOX_X_LIVE_TESTS=1
set DOSBOX_X_BINARY=%CD%\src\dosbox-x.exe
python -m unittest tests.host_control_live_tests.HostControlLiveTest.test_pipe_status_recipe_runs
```

The live test should start DOSBox-X with `-control-pipe dosbox-x-control`,
connect the client with `pipe dosbox-x-control`, run a status recipe, and verify
that the transcript preserves the same JSONL event shape as socket and Unix pipe
transports.

## Acceptance Criteria

- `-control-pipe <name>` works on Windows with a named pipe.
- `scripts/host_control_client.py pipe <name> status` works on Windows.
- `exec`, `workflow`, `repl`, `input-text`, and `key` work over the Windows pipe
  transport.
- The JSON protocol is unchanged.
- Workflow transcript JSONL behavior is unchanged.
- Client errors for missing or unavailable pipes are useful.
- Unix FIFO behavior remains unchanged.
- Non-supported platforms still fail clearly instead of silently starting a
  normal shell.

## Open Questions

- Should Windows accept only `\\.\pipe\<name>` and short names, or also reject
  path-like values containing `/` and `\` that are not valid named-pipe paths?
- Should the server use blocking `ConnectNamedPipe` in the same startup flow, or
  an overlapped/nonblocking connection path to make shutdown more responsive?
- Should Windows named pipe support remain one client per DOSBox-X process, or
  should reconnect support become a separate future milestone?
- Is a Windows CI runner available for live named-pipe smoke coverage?

## Estimated Difficulty

Basic support is moderate: roughly one to two focused days with a Windows test
machine. Robust support with native tests, live smoke coverage, and polished
diagnostics is more likely three to five days.

The main risk is not JSON protocol work. The main risk is Windows pipe lifecycle
behavior: connection timing, disconnect during `exec`, timeout handling, and
reliable client I/O without adding a heavy Python dependency.
