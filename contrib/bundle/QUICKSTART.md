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

**Linux only — minimum distro.** The emulator is built on Ubuntu 24.04 and
needs glibc 2.38+ and libstdc++ from GCC 13+, so it runs on **Ubuntu 24.04 or
newer, Debian 13 or newer**, and comparable releases. Newer is always fine —
glibc is backward compatible. Older releases (Ubuntu 22.04, Debian 12) are
not supported and fail at startup with a message naming a `GLIBC_2.xx`
version. Build from source for those; see the project README.

**Linux only — system libraries.** The emulator is dynamically linked, so a
minimal or freshly installed system needs these before it will start:

```bash
sudo apt-get install -y libsdl2-2.0-0 libsdl2-net-2.0-0 libasound2t64 \
    libncurses6 libpcap0.8 libslirp0 libfluidsynth3 libgl1 libpng16-16t64
```

Those names are current for Ubuntu 24.04+ and Debian 13+.

`libgl1` and `libpng16-16t64` are easy to miss: the emulator links both
directly, but nothing else in this list depends on them, so only a minimal
system notices they are absent. `scripts/check_runtime_deps.py` verifies this
list covers every linked library on each release build.

**Fedora, and other non-Debian distros.** Tested on Fedora 43. Install the
equivalents:

```bash
sudo dnf install -y SDL2 SDL2_net alsa-lib ncurses-libs libpcap libslirp fluidsynth-libs mesa-libGL libpng libXrandr
```

Two things there do not follow from translating the Debian list package by
package:

- `libXrandr` has to be named explicitly. Ubuntu's SDL2 package depends on it
  so it arrives on its own there; Fedora's SDL2 does not require it.
- `libpcap` alone is not enough. Debian and Ubuntu ship libpcap under its
  historical soname `libpcap.so.0.8`, Fedora ships the upstream
  `libpcap.so.1`, and no Fedora package provides the Debian name — so the
  emulator does not start at all, failing with `libpcap.so.0.8: cannot open
  shared object file`. Both distros ship the same upstream 1.10.x release, so
  a compatibility symlink is sound:

  ```bash
  sudo ln -sf /usr/lib64/libpcap.so.1 /usr/lib64/libpcap.so.0.8
  sudo ldconfig
  ```

With those in place the emulator runs a real DOS command on a clean Fedora.
`scripts/smoke_bundle_fedora.sh` in the repository reproduces the whole check.

You may also see `no version information available` warnings naming
libncurses or libtinfo. Debian builds ncurses with versioned symbols and
Fedora does not; the warnings are harmless.

If the emulator fails to start, run `ldd dosbox-x/dosbox-x` and look for
`not found` — that names the missing package directly. Windows needs none of
this; everything it requires is in the bundle.

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

The other three tools: `send_input` answers an interactive prompt while a
command is running, `cancel` interrupts a running batch file, and `status`
reports whether a session is active and its current DOS drive/cwd.

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
