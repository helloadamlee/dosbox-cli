# dosbox-x-mcp

An MCP server that wraps this repo's DOSBox-X host-control pipe protocol so
an agent can drive a DOS session — running a build, inspecting a mounted
directory, answering an interactive prompt — as typed tool calls instead of
hand-rolled NDJSON.

Design background: `docs/superpowers/specs/2026-08-16-dosbox-mcp-server-design.md`.

## Install

```bash
cd mcp-server
pip install -e .
```

This installs the `dosbox-mcp` console script and depends on the sibling
`scripts/host_control_client.py` in this same repo checkout — it is not
usable installed standalone outside this repo.

## Register with an MCP client

Example config (Claude Code, Claude Desktop, or any MCP-compatible host that
reads a `command`/`args` server entry):

```json
{
  "mcpServers": {
    "dosbox-x": {
      "command": "dosbox-mcp"
    }
  }
}
```

## Tools

One DOSBox-X session at a time. `start_session` launches the process;
`stop_session` is the only way to end it — there is no `cancel` for a
running command (see "Known limitations" below).

| Tool | Parameters | Returns |
|---|---|---|
| `start_session` | `cwd`, `binary_path?`, `config_path?`, `mount?` (`{"drive": "c", "host_path": "..."}`), `dos_path?`, `env?` | `{session_active, drive, cwd, pid, setup_results}` |
| `exec` | `command` | `{request_id}` — returns immediately, does not wait for completion |
| `poll` | `wait_seconds?` (default 2, max 10) | `{running, done, output, errorlevel, max_errorlevel, ok, bad_command, drive, cwd}` |
| `send_input` | `text?` or `key?` (exactly one) | `{queued}` |
| `status` | — | `{session_active, drive, cwd}` |
| `stop_session` | `force?` | `{stopped}` |

Typical sequence: `start_session` → `exec` → repeated `poll` until `done` →
… → `stop_session`.

## Known limitations (by design, v1)

- **`[autoexec]` never runs.** Host control replaces the DOS shell's normal
  run loop, so a config's `[autoexec]` section is skipped. `start_session`'s
  `mount`/`dos_path`/`env` params work around this by replaying the
  equivalent commands before returning — use them instead of relying on your
  `.conf` file's `[autoexec]`.
- **`bad_command` is a heuristic.** DOS reports a command it can't find as a
  normal successful exit (`errorlevel 0`). `poll`'s `bad_command` field is a
  text scan for `Bad command or filename` in the output, not a protocol-level
  signal — it can miss a bad-command message split across two `output`
  events at exactly the wrong byte boundary, though this is rare in
  practice.
- **No `cancel` tool.** The underlying protocol's `cancel`/`break` op exists
  but doesn't actually stop a running program. A hung command must be
  handled with `stop_session`, which kills the whole DOSBox-X process — there
  is no way to cancel just the current command and keep the session alive.
- **One session at a time.** A second `start_session` fails until
  `stop_session` is called.

See `docs/host-control-windows-pipe-roadmap.md` in the repo root for the
underlying protocol's own tracked gaps and deferred work.
