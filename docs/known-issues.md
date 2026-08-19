# Known issues

Loose ends from shipping the v0.1.0 / v0.1.1 release bundles. Nothing
here blocks the current release — these are the things worth knowing
about before touching that area again.

For the host-control protocol's own open questions (reconnect support,
the ROM/VIDRAM MCB defect, a windowed-focus stall that's still
unreproduced), see [`host-control-windows-pipe-roadmap.md`](host-control-windows-pipe-roadmap.md)
instead — this doc is just the release-bundle side of things.

## A handful of build scripts still point at the old paths

When the repo got reorganized (`3fb7ef870`), a pile of root-level scripts
moved into `build-scripts/`, `scripts/`, and `docs/`. Two of the leftover
references broke the actual Windows/Linux release build and are fixed now.
Four more are still broken, but none of them sit in the release path, so
they've been low priority:

- `Makefile.am` still points `EXTRA_DIST` at `autogen.sh` instead of
  `scripts/autogen.sh` — breaks `make dist`, not CI.
- `Makefile.am` also calls `./appbundledeps.py` instead of
  `scripts/appbundledeps.py` — only matters for macOS app bundling, which
  isn't part of this project right now anyway.
- `hxdos.yml` calls `build-mingw-hx-dos` without the `build-scripts/`
  prefix.
- Both OS/2 build scripts (`build-os2-sdl2.cmd`, `build-debug-os2-sdl2.cmd`)
  still `bash autogen.sh` instead of `bash scripts/autogen.sh`.

Same one-line fix as the two that already got patched. Just hasn't been
worth a CI run for build targets nobody's using yet.

## The MCP server is pinned below mcp 2.0

`mcp-server/pyproject.toml` caps the `mcp` dependency at `<2` because
`server.py` is built on `mcp.server.fastmcp.FastMCP`, which got removed in
2.0 in favor of `mcp.server.mcpserver.MCPServer`.

The good news: the actual rename is trivial. I tried it in a throwaway
venv against mcp 2.0.0 and all seven tools registered and worked fine.
The bad news is what's *not* tested — `test_server.py` calls the tool
functions directly rather than going through a real MCP transport, so
nothing would catch it if the port quietly changed a response shape (2.0
adds some new tool-decorator options that could do exactly that). Worth
writing those tests before actually lifting the cap, not after.

## The Linux dependency list is "known to work," not "proven minimal"

`QUICKSTART.md` tells Linux users to install seven packages
(`libsdl2-2.0-0`, `libsdl2-net-2.0-0`, `libasound2t64`, `libncurses6`,
`libpcap0.8`, `libslirp0`, `libfluidsynth3`), and that's genuinely enough —
tested it on a fresh WSL2 Ubuntu 26.04 install and the emulator ran a real
DOS command with just those seven.

But the binary actually links a longer list than that
(`libGL`, `libX11`, `libXrandr`, `libz`, `libpng16`, `libtinfo`, `libpulse`,
`libsamplerate`, `libXext` all show up too) — they just happened to arrive
for free as dependencies of the seven packages above. That held on Debian/
Ubuntu. A distro that splits its packages differently could hit a gap this
one test wouldn't have caught.

## Two things left out on purpose, not by accident

- **No macOS bundle.** The build-script fix accidentally unblocked macOS
  compiling again, but there's no macOS job in `release.yml`. Not a
  priority for this project right now.
- **Linux bundle needs Ubuntu 24.04+ / Debian 13+.** It's built against
  glibc 2.38+, so anything older won't run it. Building on `ubuntu-22.04`
  instead of `ubuntu-latest` would widen that, at the cost of an older
  toolchain — a real option, just not one I've taken yet.
