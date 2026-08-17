---
name: dosbox-mcp
description: Use when driving a DOS session through the dosbox-cli MCP server — running commands, mounting a host directory, or automating a DOS program via start_session/exec/poll, especially when a command hangs, an interactive prompt blocks progress, or a batch file needs interrupting.
---

# dosbox-mcp

## Overview

The `dosbox-mcp` server exposes DOSBox-X's host-control protocol as seven
async MCP tools. Every `exec` returns immediately; you poll for output and
completion instead of blocking. The full tool table and parameters are the
source of truth — see [`mcp-server/README.md`](../../../mcp-server/README.md).
This skill covers the parts that aren't obvious from the tool signatures.

## Typical sequence

```
start_session(cwd, mount?)  →  exec(command)  →  poll() [repeat until done]  →  stop_session()
```

`send_input` answers an interactive prompt mid-command. `cancel` interrupts a
running batch file. `status` checks session state without touching output.

## Gotchas that aren't in the tool signatures

- **`[autoexec]` in a `.conf` file never runs.** Host control replaces the
  shell's normal run loop. Use `start_session`'s `mount` / `dos_path` / `env`
  params instead — they're replayed as real setup commands before the call
  returns (check `setup_results` for each step's outcome).
- **`bad_command` is a text heuristic, not a protocol signal.** DOS reports
  an unknown command as a normal `errorlevel 0` exit. `poll`'s `bad_command`
  field scans output for `Bad command or filename` — check it explicitly;
  don't infer failure from `ok` alone.
- **`cancel` only works on batch files / Ctrl-C-aware programs.** A tight
  emulation loop with no console reads can ignore it. If a `cancel` doesn't
  produce `cancelled: true` within a poll or two, escalate to
  `stop_session(force=True)` — it kills the whole DOSBox-X process.
- **One session at a time.** A second `start_session` fails until
  `stop_session` is called; there's no queueing.
- **`DOSBOX_X_BINARY` must point at a host-control-capable build.** A stock
  DOSBox-X install has no host-control support and fails once a session
  starts, not at launch — the error surfaces from inside `start_session`.
- **`poll`'s `output` is incremental, not cumulative.** Each call returns
  only text received since the last poll. Once `done: true`, later polls
  return `done: true` with no new output until the next `exec`.

## Common mistakes

| Symptom | Cause | Fix |
|---|---|---|
| `exec` seems to hang | Treating `exec` as blocking | Call `poll` in a loop; `exec` always returns immediately |
| Command "succeeds" but did nothing | Only checked `ok`, not `bad_command` | Check `bad_command` in the `poll` result explicitly |
| Mounted drive / env missing | Relied on `.conf` `[autoexec]` | Pass `mount`/`dos_path`/`env` to `start_session` instead |
| `cancel` does nothing | Program isn't reading console input | Fall back to `stop_session(force=True)` |
| Second session won't start | Prior session never stopped | Call `stop_session` before starting a new one |

## Further reading

- Full tool table and parameters: [`mcp-server/README.md`](../../../mcp-server/README.md)
- Raw protocol (if not using the MCP layer): [`docs/host-control.md`](../../../docs/host-control.md)
