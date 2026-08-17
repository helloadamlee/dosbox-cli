# Prebuilt Release Bundles Design

## Goal

Ship prebuilt, downloadable bundles so an end user can run the host-control
MCP server without a C++ toolchain. Today the only path to a working setup is
cloning the repo and building DOSBox-X from source — roughly 20 minutes and a
full Visual Studio or autotools stack. A bundle removes that barrier: unzip,
one `pip install`, point an MCP client at it.

This is portfolio-facing v1. Two platforms, verified before publish, with
known gaps documented rather than fixed.

## Non-goals (v1)

- **macOS bundles.** The CI repair below unblocks the macOS build too, but
  neither maintainer can test a macOS bundle before shipping it. Not published
  until someone can verify one.
- **Removing the Python dependency.** The MCP server ships as source, installed
  with `pip`. Freezing it (PyInstaller) would drop the Python requirement
  entirely but adds CI complexity, ~15–30 MB, and a second code path that can
  break independently. The 20-minute C++ build is the real barrier; Python 3.9+
  is a low bar for anyone already running an MCP client.
- **Publishing to PyPI.** Would require decoupling the package from the repo
  and coordinating versions across two artifacts. The bundle carries the server.
- **Promoting `scripts/` to a real installable package.** The correct long-term
  fix for the import problem below, but larger than this work. Vendoring is the
  v1 workaround; see "Deferred".

## Decisions

Settled with the maintainer before design:

1. **One bundle per OS**, containing emulator + MCP server together — not a
   PyPI package plus separate binary downloads.
2. **Windows and Linux only** for v1.
3. **Repoint workflows** at `build-scripts/` rather than restoring build
   scripts to the repo root; the clean root from the reorg is deliberate.
4. **Assume Python 3.9+** on the user's machine.

## Current state

Four findings from inspecting the repo and CI, all of which shape the work.

**CI is broken on five platforms.** Commit `3fb7ef870` ("Reorganize repo
layout…") moved `build`, `build-debug`, and siblings from the repo root into
`build-scripts/`. Thirty workflow steps still invoke them as `./build-debug`
from the root and fail immediately:

```
./build-debug: No such file or directory   → exit 127
```

Affected: `linux.yml`, `macos.yml`, `mingw32.yml`, `mingw64.yml`,
`vsbuild_xp.yml`, `windows-installers.yml`. The scripts themselves are fine —
they do `./autogen.sh` and `cd vs/sdl` relative to the *working directory*, so
invoking them by path from the root still works.

**Windows CI is healthy.** `vsbuild64.yml` currently reports
`MSBuild64_CI_build => success`, `host-control-windows => success` (which builds
*and* runs the host-control suite), and `MSBuild_ARM64_CI_build => success`. The
Windows bundle needs assembly only, no build repair.

**Eight workflows publish on any tag.** Their `action-gh-release` steps are
guarded by the bare prefix `startsWith(github.ref, 'refs/tags/')` — roughly 14
such guards across `hxdos.yml`, `macos.yml`, `mingw32.yml`, `mingw64.yml`,
`vsbuild32.yml`, `vsbuild64.yml`, `vsbuild_xp.yml`, and
`windows-installers.yml`. Pushing any tag wakes all of them, including
currently-broken jobs, scattering upstream-flavored zips into the release.

**The 210 "mode changes" are a Windows artifact, not damage.** The working tree
shows ~210 files as `100755 → 100644`. HEAD is correct — exactly 210 files are
`100755` as committed. This clone has `core.filemode=true`, so git compares
against NTFS (which reports nothing executable) and invents the difference.
Verified:

```
core.filemode=true  : 223 dirty entries
core.filemode=false :  13 dirty entries   (the real ones)
files at 100755 in HEAD: 210              (matches the phantom count)
```

Nothing is broken yet, but `git add -A` on this machine would commit the
stripped bits and break Linux/macOS CI with "Permission denied".

## Architecture

### Release pipeline

A new `.github/workflows/release.yml` owns distribution end to end. It lives in
a file upstream does not have, so upstream merges cannot conflict on it, and it
keeps per-push CI (the nine inherited workflows) separate from artifact
production.

**Trigger:** `push` on tags matching `dosbox-cli-v*`, plus `workflow_dispatch`
for rehearsing without tagging. Version is the tag minus its prefix:
`dosbox-cli-v0.1.0` → `0.1.0`.

The `dosbox-cli-v*` shape is deliberately distinct from upstream's
`dosbox-x-vX.Y.Z`, which the inherited workflows already parse.

**Three jobs:**

1. `windows-bundle` (`windows-2022`) — MSBuild `Release SDL2|x64`, the
   configuration CI already builds green. Assembles, smoke-tests, uploads.
2. `linux-bundle` (`ubuntu-latest`) — apt dependencies, then
   `./build-scripts/build-sdl2` (release, not the `build-debug-sdl2` that
   `linux.yml` uses for CI), then `strip`. Assembles, smoke-tests, uploads.
3. `publish` (`needs: [windows-bundle, linux-bundle]`) — downloads both
   artifacts and creates the GitHub Release. Runs only if both bundle jobs
   succeed, so a half-broken release is impossible.

**Muzzling the inherited publishers.** Each of the ~14 bare `refs/tags/` guards
on an `action-gh-release` step narrows to `refs/tags/dosbox-x-v`. Upstream
behavior is preserved exactly — upstream's tag shape still triggers them — while
`dosbox-cli-v*` tags leave them dormant. The exact set is enumerated at
implementation time with:

```
grep -n -B3 "action-gh-release" .github/workflows/*.yml
```

### Verification gate

Each bundle job, after assembling and before uploading, runs one DOS command
end to end against the artifact it is about to ship:
`start_session` → `exec ver` → assert the result is `ok` → `stop_session`. A
failure fails the job, and `publish` never runs.

Critically, the gate installs the MCP server *from the assembled bundle*
(`pip install ./<bundle>/mcp-server`), not from the repo checkout, and points it
at the bundled binary. This makes it exercise the shipped artifact exactly as a
user would — which means it also covers the `parents[2]` import bug and the
vendoring that fixes it. A gate that installed from the checkout would pass
while the bundle was broken.

This exists because of a concrete near-miss: a local build silently produced a
corrupt 2 MB `dosbox-x.exe` that Windows refused to execute (`error 1392`), and
nothing detected it until a launch attempt. Checking that the file exists is not
enough; the gate must actually run the artifact it is about to ship.

### Bundle layout

The emulator payload is isolated in its own directory. This is required, not
cosmetic: the emulator's portable package already ships a `scripts/` directory
(Explorer context-menu `.bat` files) that would collide with the repo's
`scripts/host_control_client.py` at bundle root.

```
dosbox-cli-0.1.0-win64/
├── QUICKSTART.md
├── COPYING
├── dosbox-x/                    ← emulator + runtime assets
│   ├── dosbox-x.exe
│   ├── drivez/ glshaders/ languages/ scripts/ shaders/
│   └── *.ttf, *.bdf, dosbox-x.reference.conf
├── mcp-server/                  ← pip install target
│   ├── pyproject.toml
│   └── dosbox_mcp/
│       └── _vendor/host_control_client.py
└── scripts/host_control_client.py
```

Linux is identical with `dosbox-x/dosbox-x`, packaged as `.tar.gz` to preserve
the executable bit (zip does not carry it reliably).

Artifact names: `dosbox-cli-<version>-win64.zip`,
`dosbox-cli-<version>-linux-x86_64.tar.gz`.

### Python packaging

**The bug this fixes.** `mcp-server/dosbox_mcp/_client_import.py` resolves
`Path(__file__).resolve().parents[2] / "scripts"`. From a checkout that is
`repo/scripts/` and works. After a normal `pip install ./mcp-server`, the module
lives in `site-packages/dosbox_mcp/`, so `parents[2]` escapes to
`…/lib/python3.x/scripts` and the import fails. Only `pip install -e` happens to
work — which is why this has never surfaced: every use so far has been from a
checkout. The bundle would hit it immediately.

**Resolution.** `scripts/host_control_client.py` remains canonical: the README
links to it and it is a usable standalone CLI. The bundle assembly step copies
it to `mcp-server/dosbox_mcp/_vendor/host_control_client.py`, and
`_client_import.py` gains two-step resolution:

1. the vendored copy, if present (built and bundled distributions)
2. the sibling `scripts/` directory (development checkouts)

Both paths are covered by tests so the bundle install and checkout install
cannot silently diverge.

**Binary discovery.** `resolve_dosbox_binary()` currently checks hardcoded
install locations and `PATH`. In a bundle it would find a *stock* DOSBox-X —
which has no host-control support — before its own sibling binary, then fail
later with a confusing error. Two changes:

- honor a `DOSBOX_X_BINARY` environment variable ahead of all other candidates
- make the not-found error state that this fork's build is required, not stock
  DOSBox-X

QUICKSTART then documents the MCP client config explicitly, with no guessing:

```json
{ "mcpServers": { "dosbox-x": {
    "command": "dosbox-mcp",
    "env": { "DOSBOX_X_BINARY": "C:\\...\\dosbox-cli-0.1.0-win64\\dosbox-x\\dosbox-x.exe" }
}}}
```

## Prerequisite repairs

**Mode bits.** Set `core.filemode=false` in the working clone. `git status`
drops from 223 entries to 13, and the accidental-`git add -A` hazard is gone.
This cannot be fixed repo-side — `.gitattributes` has no filemode control — so
it also gets a line in the build documentation for anyone cloning on Windows.

**CI paths.** Repoint the 30 broken invocations to `build-scripts/`. Only
`linux.yml` is strictly required for v1, but the same mechanical edit fixes
macOS, MinGW32/64, and HX-DOS, and an all-red Actions tab is poor presentation
for a portfolio repo.

These jobs fail at the *first* `./build*` call, so the path fix is necessary but
possibly not sufficient — further breakage may be hiding behind it. The
commitment is that Windows and Linux go green. If another platform stays broken
after the path repair, it is disabled rather than left failing, and noted.

## Documentation

- **`QUICKSTART.md`** (new, ships in each bundle) — unzip, `pip install
  ./mcp-server`, the MCP client config JSON above, and one worked example:
  `start_session` → `exec dir` → `poll`.
- **`README.md`** — gains a **Download** section above Building: get a bundle
  from Releases, no compiler required. Building from source becomes the fallback
  rather than the default path.
- **`mcp-server/README.md`** — its install section currently states the package
  "is not usable installed standalone outside this repo." Vendoring makes that
  false; it is corrected to describe both the bundle install and the checkout
  install.

## Testing

- Unit tests for both `_client_import.py` resolution paths (vendored present,
  vendored absent → sibling fallback), so the bundle and checkout installs stay
  in step.
- Unit tests for `resolve_dosbox_binary()` env-var precedence and the improved
  not-found message.
- The in-CI smoke gate described above, per platform, as the integration test.
- A manual pre-tag rehearsal via `workflow_dispatch`: confirm both bundles
  assemble and smoke-test green before any tag is pushed. The Linux bundle is
  additionally verified by the maintainer on a real Linux machine, since CI's
  ubuntu-latest is not the only target.

## Deferred

- **Promoting `scripts/` to a proper installable package.** The vendoring step
  is a build-time copy — a workaround. Making `host_control_client` a real
  module that both the CLI and the MCP server import would remove the
  duplication and the fallback logic entirely.
- **macOS bundles**, pending someone who can verify one.
- **A frozen, Python-free MCP server executable**, if the pip step proves to be
  a real barrier for users.
- **Checksums and signing** for release assets.
