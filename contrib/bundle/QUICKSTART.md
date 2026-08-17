# dosbox-cli — Quick Start

This bundle contains a prebuilt DOSBox-X with the **host-control** interface,
plus an MCP server that exposes it as typed tools. No compiler required.

## What's in here

```
dosbox-x/        the emulator and its runtime assets
mcp-server/      the MCP server (install this)
scripts/         the reference Python client, usable standalone
QUICKSTART.md    this file
```

## 1. Requirements

- Python 3.10 or newer (`python --version`)
- An MCP-capable client (Claude Code, Claude Desktop, or any host that reads
  a `command`/`env` server entry)

## 2. Install the server

From inside this unzipped folder:

```bash
pip install ./mcp-server
```

That installs a `dosbox-mcp` console script.

## 3. Point your MCP client at it

Add this to your MCP client's config, replacing the path with the real
location of this folder.

**Windows:**

```json
{
  "mcpServers": {
    "dosbox-x": {
      "command": "dosbox-mcp",
      "env": {
        "DOSBOX_X_BINARY": "C:\\path\\to\\dosbox-cli-0.1.0-win64\\dosbox-x\\dosbox-x.exe"
      }
    }
  }
}
```

**Linux:**

```json
{
  "mcpServers": {
    "dosbox-x": {
      "command": "dosbox-mcp",
      "env": {
        "DOSBOX_X_BINARY": "/path/to/dosbox-cli-0.1.0-linux-x86_64/dosbox-x/dosbox-x"
      }
    }
  }
}
```

`DOSBOX_X_BINARY` matters: without it the server may find a stock DOSBox-X
already installed on your machine, which has no host-control support and will
fail once a session starts.

## 4. Try it

Restart your MCP client, then ask it to run something. Under the hood that is:

| Step | Tool | Arguments |
|---|---|---|
| 1 | `start_session` | `cwd`, optionally `mount` (`{"drive": "c", "host_path": "..."}`) |
| 2 | `exec` | `{"command": "dir"}` — returns immediately |
| 3 | `poll` | repeat until `done: true`; each call returns new output |
| 4 | `stop_session` | ends the session and the emulator process |

The other two tools: `send_input` answers an interactive prompt while a
command is running, and `cancel` interrupts a running batch file.

## Notes and limits

- **One session at a time.** A second `start_session` fails until you call
  `stop_session`.
- **The session starts at a bare prompt.** A config's `[autoexec]` section
  does not run under host control. Use `start_session`'s `mount`, `dos_path`,
  and `env` parameters instead — they are issued as real setup commands.
- **`cancel` stops batch files**, not every program. Something in a tight loop
  with no console reads may ignore it; `stop_session` is the fallback.
- **A DOS command that does not exist** still reports `ok: true` with
  errorlevel 0. The server flags this separately as `bad_command: true` in
  `poll` output — check that field, not just `ok`.

Full protocol reference and the project source:
https://github.com/helloadamlee/dosbox-cli
