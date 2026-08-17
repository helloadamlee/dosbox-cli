# Prebuilt Release Bundles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship downloadable Windows and Linux bundles containing the emulator plus the MCP server, so an end user can run the host-control MCP server without a C++ toolchain.

**Architecture:** A new `release.yml` workflow, triggered by `dosbox-cli-v*` tags, builds both platforms, assembles bundles with a shared Python script, smoke-tests each assembled bundle by installing it and running a real DOS command, and only then publishes a GitHub Release. Prerequisite repairs fix the broken CI paths and stop eight inherited workflows from publishing on our tags.

**Tech Stack:** GitHub Actions, MSBuild (Windows), autotools via `build-scripts/build-sdl2` (Linux), Python 3.9+, pytest, setuptools.

**Spec:** `docs/superpowers/specs/2026-08-17-prebuilt-release-bundles-design.md`

## Global Constraints

- **Bundle layout is fixed** and every task must match it exactly: emulator payload under `dosbox-x/`, MCP server under `mcp-server/`, canonical client at `scripts/host_control_client.py`, vendored copy at `mcp-server/dosbox_mcp/_vendor/host_control_client.py`, plus `QUICKSTART.md` and `COPYING` at bundle root. The `dosbox-x/` nesting is mandatory — the emulator ships its own `scripts/` directory which would otherwise collide.
- **Artifact names:** `dosbox-cli-<version>-win64.zip` and `dosbox-cli-<version>-linux-x86_64.tar.gz`. Linux must be `.tar.gz`, not zip, to preserve the executable bit.
- **Tag shape is `dosbox-cli-v<X.Y.Z>`**, deliberately distinct from upstream's `dosbox-x-v<X.Y.Z>`. Version = tag minus the `dosbox-cli-v` prefix.
- **Never widen an inherited workflow's release guard.** Task 3 narrows them; no later task may reintroduce a bare `refs/tags/` guard on an `action-gh-release` step.
- **The smoke gate must install from the assembled bundle**, never from the repo checkout. Installing from the checkout would pass while the bundle is broken, defeating the gate's entire purpose.

**User decisions (already made):**
- "One bundle per OS" — emulator and MCP server ship together, not as a PyPI package plus separate binary download.
- "Windows + Linux" for v1.
- "at this point of the project, a Mac version isn't a concern" — macOS bundles are out of scope even though the CI repair unblocks the macOS build.
- "Repoint workflows" at `build-scripts/` rather than restoring build scripts to the repo root.
- "Assume Python 3.9+" — no PyInstaller freezing.
- "Definitely add those 3 things as they sound critical" — the `pip install` import fix, the release-guard narrowing, and `core.filemode=false` are all in scope, not optional.

---

### Task 1: Defuse the mode-bit hazard

**Goal:** Stop `git status` from reporting 210 phantom mode changes in this clone, and document the setting so anyone cloning on Windows avoids the same trap.

**Files:**
- Modify: `BUILD.md` (append a "Cloning on Windows" note)
- Local config only (not committed): `core.filemode`

**Acceptance Criteria:**
- [ ] `git -c core.filemode=false status --short | wc -l` and `git status --short | wc -l` return the same number
- [ ] `git ls-files -s | awk '{print $1}' | grep -c 100755` still returns `210` (committed exec bits untouched)
- [ ] `BUILD.md` contains the `git config core.filemode false` instruction with its rationale

**Verify:** `git status --short | wc -l` → `13` (was `223`)

**Steps:**

- [ ] **Step 1: Record the before-state**

```bash
git status --short | wc -l                              # expect 223
git ls-files -s | awk '{print $1}' | sort | uniq -c     # expect 7585 x 100644, 210 x 100755
```

- [ ] **Step 2: Apply the setting**

```bash
git config core.filemode false
```

- [ ] **Step 3: Confirm the phantom changes are gone and HEAD is untouched**

```bash
git status --short | wc -l                              # expect 13
git ls-files -s | awk '{print $1}' | grep -c 100755     # expect 210
```

If the second command no longer returns 210, STOP — exec bits were actually committed away and must be restored with `git update-index --chmod=+x` before continuing.

- [ ] **Step 4: Document it in BUILD.md**

Append this section to `BUILD.md`:

```markdown
## Cloning on Windows

Git for Windows normally disables filemode tracking, but if your clone has
`core.filemode=true`, git compares committed permissions against NTFS (which
reports nothing as executable) and invents ~210 phantom `100755 -> 100644`
modifications. Committing those strips the executable bit from every build
script and breaks the Linux and macOS CI jobs with "Permission denied".

Set this once per clone:

    git config core.filemode false

This affects only your local working copy; committed permissions are unchanged.
```

- [ ] **Step 5: Commit**

```bash
git add BUILD.md
git commit -m "docs: note core.filemode for Windows clones"
```

---

### Task 2: Repair the broken CI build-script paths

**Goal:** Repoint the 30 workflow steps that invoke root-level build scripts at their real location under `build-scripts/`, so the Linux build (and four other platforms) stops failing with exit 127.

**Files:**
- Modify: `.github/workflows/linux.yml` (lines 29, 50, 56, 113, 119)
- Modify: `.github/workflows/macos.yml` (lines 46, 53, 117, 125, 186, 193, 257, 264)
- Modify: `.github/workflows/mingw32.yml` (lines 64, 94, 198, 227, 332)
- Modify: `.github/workflows/mingw64.yml` (lines 49, 78, 161, 190, 272, 301)
- Modify: `.github/workflows/vsbuild_xp.yml` (lines 201, 207)
- Modify: `.github/workflows/windows-installers.yml` (lines 302, 311, 361, 370)

**Acceptance Criteria:**
- [ ] No workflow file invokes a root-level `./build*` script that does not exist
- [ ] Every rewritten invocation points at a file that exists in `build-scripts/`
- [ ] Working directory is unchanged (scripts still run from the repo root, because they do `./autogen.sh` and `cd vs/sdl` relative to cwd)

**Verify:** `grep -rE "^\s*\./(build|build-debug|build-debug-sdl2|build-macos|build-macos-sdl2|build-mingw|build-mingw-sdl2|build-mingw-lowend9x)\b" .github/workflows/*.yml` → no matches

**Steps:**

- [ ] **Step 1: Confirm the current breakage count**

```bash
grep -rcE "^\s*\./(build|build-debug|build-debug-sdl2|build-macos|build-macos-sdl2|build-mingw|build-mingw-sdl2|build-mingw-lowend9x)\b" .github/workflows/*.yml | awk -F: '{s+=$2} END{print s}'
```

Expected: `30`

- [ ] **Step 2: Confirm every target exists before rewriting**

```bash
for s in build build-debug build-debug-sdl2 build-sdl2 build-macos build-macos-sdl2 build-mingw build-mingw-sdl2 build-mingw-lowend9x; do
  test -f "build-scripts/$s" && echo "OK   $s" || echo "MISS $s"
done
```

Every line must read `OK`. A `MISS` means that script was deleted, not moved — stop and investigate before rewriting its call sites.

- [ ] **Step 3: Rewrite the invocations**

```bash
sed -i -E 's|^(\s*)\./(build-debug-sdl2|build-debug|build-sdl2|build-macos-sdl2|build-macos|build-mingw-lowend9x|build-mingw-sdl2|build-mingw|build)\b|\1./build-scripts/\2|' \
  .github/workflows/linux.yml \
  .github/workflows/macos.yml \
  .github/workflows/mingw32.yml \
  .github/workflows/mingw64.yml \
  .github/workflows/vsbuild_xp.yml \
  .github/workflows/windows-installers.yml
```

Note the alternation order: longer names come first so `build-debug-sdl2` is not partially matched by `build-debug`.

- [ ] **Step 4: Verify no stale invocations and no double-prefixing**

```bash
grep -rE "^\s*\./(build|build-debug|build-debug-sdl2|build-macos|build-macos-sdl2|build-mingw|build-mingw-sdl2|build-mingw-lowend9x)\b" .github/workflows/*.yml
grep -rn "build-scripts/build-scripts" .github/workflows/*.yml
```

Both must produce no output. Then confirm the rewrite landed:

```bash
grep -rcn "\./build-scripts/" .github/workflows/*.yml | awk -F: '{s+=$2} END{print s}'
```

Expected: `30`

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/
git commit -m "ci: repoint build scripts at build-scripts/ after repo reorg"
```

---

### Task 3: Stop inherited workflows publishing on our tags

**Goal:** Narrow every inherited `action-gh-release` guard from the bare `refs/tags/` prefix to `refs/tags/dosbox-x-v`, so a `dosbox-cli-v*` tag does not wake eight upstream workflows into dumping their own zips (and their current failures) into our release.

**Files:**
- Modify: `.github/workflows/hxdos.yml`
- Modify: `.github/workflows/macos.yml`
- Modify: `.github/workflows/mingw32.yml`
- Modify: `.github/workflows/mingw64.yml`
- Modify: `.github/workflows/vsbuild32.yml`
- Modify: `.github/workflows/vsbuild64.yml`
- Modify: `.github/workflows/vsbuild_xp.yml`
- Modify: `.github/workflows/windows-installers.yml`

**Acceptance Criteria:**
- [ ] Every `action-gh-release` step is guarded by a condition containing `refs/tags/dosbox-x-v`
- [ ] No `action-gh-release` step remains guarded by a bare `refs/tags/`
- [ ] Guards on non-release steps (build/package/artifact-upload steps that also test `refs/tags/`) are left alone — only release-publish steps change

**Verify:** `grep -n -B4 "action-gh-release" .github/workflows/*.yml | grep "refs/tags/'" ` → no matches

**Steps:**

- [ ] **Step 1: Enumerate the exact guards to change**

```bash
for f in .github/workflows/*.yml; do
  grep -n "action-gh-release" "$f" | cut -d: -f1 | while read ln; do
    sed -n "$((ln-4)),${ln}p" "$f" | grep -n "if:" | sed "s|^|$f (release at line $ln) -> |"
  done
done
```

This prints each release step and its governing `if:`. Record the list; you will verify against it in Step 3.

- [ ] **Step 2: Narrow the guards**

For each release-publish step identified in Step 1 whose guard is exactly:

```yaml
        if: startsWith(github.ref, 'refs/tags/')
```

change it to:

```yaml
        if: startsWith(github.ref, 'refs/tags/dosbox-x-v')
```

Leave alone: guards already disabled (`if: 0`), guards that are commented out (`#if:`), and any guard on a step that is not `uses: softprops/action-gh-release@...`.

Do this with a targeted edit per occurrence rather than a blanket `sed`, because the same guard string also appears on build and packaging steps that must keep firing on any tag.

- [ ] **Step 3: Verify only release steps changed**

```bash
# No release step left on a bare refs/tags/ guard:
grep -n -B4 "action-gh-release" .github/workflows/*.yml | grep "refs/tags/'"
```

Expected: no output.

```bash
# Count of narrowed guards:
grep -rc "refs/tags/dosbox-x-v" .github/workflows/*.yml | awk -F: '{s+=$2} END{print s}'
```

Expected: matches the number of active release steps found in Step 1.

- [ ] **Step 4: Confirm no YAML was broken**

```bash
python -c "
import sys, glob, yaml
for f in sorted(glob.glob('.github/workflows/*.yml')):
    yaml.safe_load(open(f, encoding='utf-8'))
    print('OK', f)
"
```

If `yaml` is missing, install it first: `pip install pyyaml`.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/
git commit -m "ci: scope inherited release steps to upstream dosbox-x-v tags"
```

---

### Task 4: Make the MCP server importable after a normal pip install

**Goal:** Fix the `parents[2]` path escape that breaks `pip install ./mcp-server`, by resolving `host_control_client` from a vendored copy first and falling back to the sibling `scripts/` directory for development checkouts.

**Files:**
- Modify: `mcp-server/dosbox_mcp/_client_import.py`
- Create: `mcp-server/dosbox_mcp/_vendor/__init__.py`
- Modify: `mcp-server/tests/test_client_bridge.py`
- Modify: `.gitignore`

**Acceptance Criteria:**
- [ ] `_load_host_control_client()` returns the vendored module when `dosbox_mcp._vendor.host_control_client` is importable
- [ ] `_load_host_control_client()` falls back to the sibling `scripts/` directory when no vendored copy exists
- [ ] The re-exported names (`PipeTransport`, `SessionClosed`, `RequestTimeout`, `encode_request`, `read_event_line`, `make_deadline`, `remaining_seconds`, `wait_for_readable`) are unchanged
- [ ] The generated vendored copy is gitignored; `_vendor/__init__.py` is committed

**Verify:** `cd mcp-server && python -m pytest tests/test_client_bridge.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `mcp-server/tests/test_client_bridge.py`:

```python
import sys
import types

from dosbox_mcp import _client_import


def test_vendored_copy_wins_when_present(monkeypatch):
    sentinel = types.ModuleType("dosbox_mcp._vendor.host_control_client")
    sentinel.MARKER = "vendored"
    monkeypatch.setitem(
        sys.modules, "dosbox_mcp._vendor.host_control_client", sentinel
    )

    result = _client_import._load_host_control_client()

    assert getattr(result, "MARKER", None) == "vendored"


def test_falls_back_to_sibling_scripts_without_vendored_copy(monkeypatch):
    monkeypatch.setitem(sys.modules, "dosbox_mcp._vendor.host_control_client", None)
    monkeypatch.delitem(
        sys.modules, "dosbox_mcp._vendor.host_control_client", raising=False
    )

    result = _client_import._load_host_control_client()

    assert hasattr(result, "PipeTransport")


def test_reexports_survive_resolution():
    assert _client_import.PipeTransport is not None
    assert _client_import.SessionClosed is not None
    assert _client_import.encode_request is not None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd mcp-server && python -m pytest tests/test_client_bridge.py -v
```

Expected: FAIL with `AttributeError: module 'dosbox_mcp._client_import' has no attribute '_load_host_control_client'`

- [ ] **Step 3: Rewrite `_client_import.py`**

Replace the entire contents of `mcp-server/dosbox_mcp/_client_import.py`:

```python
"""Bridge to host_control_client, resolved from one of two locations.

A built or bundled distribution carries a vendored copy at
dosbox_mcp/_vendor/host_control_client.py, copied in at packaging time. A
development checkout has no vendored copy and instead reaches the canonical
scripts/host_control_client.py sitting alongside mcp-server/.

The vendored copy must win when present: after a normal (non-editable)
pip install, this module lives in site-packages/dosbox_mcp/, so the
sibling-scripts path resolves outside the installed package and is wrong.
That is why a plain `pip install ./mcp-server` failed before this existed.
"""

import importlib
import sys
from pathlib import Path


def _load_host_control_client():
    try:
        return importlib.import_module("dosbox_mcp._vendor.host_control_client")
    except ModuleNotFoundError:
        pass

    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return importlib.import_module("host_control_client")


hc = _load_host_control_client()

PipeTransport = hc.PipeTransport
SessionClosed = hc.SessionClosed
RequestTimeout = hc.RequestTimeout
encode_request = hc.encode_request
read_event_line = hc.read_event_line
make_deadline = hc.make_deadline
remaining_seconds = hc.remaining_seconds
wait_for_readable = hc.wait_for_readable
```

- [ ] **Step 4: Create the vendor package marker**

Create `mcp-server/dosbox_mcp/_vendor/__init__.py`:

```python
"""Holds a copy of scripts/host_control_client.py injected at packaging time.

Empty in a development checkout — _client_import falls back to the sibling
scripts/ directory. The bundle assembly step (scripts/assemble_bundle.py)
copies the real module in here so a plain `pip install` works.
"""
```

- [ ] **Step 5: Gitignore the generated copy**

Append to `.gitignore`:

```gitignore
# Injected at packaging time by scripts/assemble_bundle.py
mcp-server/dosbox_mcp/_vendor/host_control_client.py
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd mcp-server && python -m pytest tests/ -q -k "not live"
```

Expected: all pass (43 existing + 3 new = 46)

- [ ] **Step 7: Commit**

```bash
git add mcp-server/dosbox_mcp/_client_import.py mcp-server/dosbox_mcp/_vendor/__init__.py mcp-server/tests/test_client_bridge.py .gitignore
git commit -m "fix(mcp-server): resolve host_control_client from a vendored copy when installed"
```

---

### Task 5: Honor DOSBOX_X_BINARY and clarify the not-found error

**Goal:** Let the bundle point the MCP server at its own binary via an environment variable, and make the failure message say that a stock DOSBox-X install will not work.

**Files:**
- Modify: `mcp-server/dosbox_mcp/binary_discovery.py`
- Modify: `mcp-server/tests/test_binary_discovery.py`

**Acceptance Criteria:**
- [ ] `DOSBOX_X_BINARY` is consulted after `explicit_path` but before any candidate path or `PATH` lookup
- [ ] A set-but-missing `DOSBOX_X_BINARY` raises `BinaryNotFoundError` naming the variable, rather than silently falling through to a stock install
- [ ] The not-found message states that a host-control-capable build is required and that stock DOSBox-X will not work
- [ ] Existing `explicit_path` / candidate / `PATH` behaviour is unchanged

**Verify:** `cd mcp-server && python -m pytest tests/test_binary_discovery.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `mcp-server/tests/test_binary_discovery.py`:

```python
def test_env_var_used_when_no_explicit_path(monkeypatch, tmp_path):
    fake_binary = tmp_path / "dosbox-x"
    fake_binary.write_text("not a real binary")
    monkeypatch.setenv("DOSBOX_X_BINARY", str(fake_binary))

    result = resolve_dosbox_binary()

    assert result == fake_binary


def test_explicit_path_beats_env_var(monkeypatch, tmp_path):
    from_env = tmp_path / "from-env"
    from_env.write_text("not a real binary")
    explicit = tmp_path / "explicit"
    explicit.write_text("not a real binary")
    monkeypatch.setenv("DOSBOX_X_BINARY", str(from_env))

    result = resolve_dosbox_binary(explicit_path=str(explicit))

    assert result == explicit


def test_env_var_pointing_at_missing_file_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("DOSBOX_X_BINARY", str(tmp_path / "nope"))

    with pytest.raises(BinaryNotFoundError) as excinfo:
        resolve_dosbox_binary()

    assert "DOSBOX_X_BINARY" in str(excinfo.value)


def test_env_var_beats_candidate_paths(monkeypatch, tmp_path):
    candidate = tmp_path / "candidate"
    candidate.write_text("not a real binary")
    from_env = tmp_path / "from-env"
    from_env.write_text("not a real binary")
    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery._candidate_paths", lambda: iter([candidate])
    )
    monkeypatch.setenv("DOSBOX_X_BINARY", str(from_env))

    result = resolve_dosbox_binary()

    assert result == from_env


def test_not_found_message_warns_about_stock_dosbox(monkeypatch):
    monkeypatch.delenv("DOSBOX_X_BINARY", raising=False)
    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery._candidate_paths", lambda: iter([])
    )
    monkeypatch.setattr("dosbox_mcp.binary_discovery.shutil.which", lambda _: None)

    with pytest.raises(BinaryNotFoundError) as excinfo:
        resolve_dosbox_binary()

    message = str(excinfo.value)
    assert "host-control" in message
    assert "DOSBOX_X_BINARY" in message
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd mcp-server && python -m pytest tests/test_binary_discovery.py -v
```

Expected: FAIL — the env var is currently ignored, so `test_env_var_used_when_no_explicit_path` resolves to something else or raises.

- [ ] **Step 3: Implement env-var support**

In `mcp-server/dosbox_mcp/binary_discovery.py`, replace the body of `resolve_dosbox_binary` with:

```python
def resolve_dosbox_binary(explicit_path=None):
    """Return a Path to a usable dosbox-x binary.

    Resolution order: explicit_path, then the DOSBOX_X_BINARY environment
    variable, then known per-platform install and in-repo build locations,
    then PATH.

    DOSBOX_X_BINARY comes before the candidate paths on purpose: a release
    bundle sets it to its own binary, and a machine with stock DOSBox-X
    installed would otherwise match a candidate path first and hand back a
    build with no host-control support.
    """
    if explicit_path is not None:
        path = Path(explicit_path)
        if not path.is_file():
            raise BinaryNotFoundError(f"binary_path does not exist: {path}")
        return path

    from_env = os.environ.get("DOSBOX_X_BINARY")
    if from_env:
        path = Path(from_env)
        if not path.is_file():
            raise BinaryNotFoundError(
                f"DOSBOX_X_BINARY is set to a path that does not exist: {path}"
            )
        return path

    for candidate in _candidate_paths():
        if candidate.is_file():
            return candidate

    found = shutil.which("dosbox-x")
    if found is not None:
        return Path(found)

    raise BinaryNotFoundError(
        "could not find a dosbox-x binary. This MCP server requires a "
        "dosbox-cli build with host-control support — a stock DOSBox-X "
        "install will not work. Set DOSBOX_X_BINARY to the binary shipped "
        "in the release bundle, or pass binary_path explicitly."
    )
```

`os` and `shutil` are already imported at the top of the file; no import changes are needed.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd mcp-server && python -m pytest tests/ -q -k "not live"
```

Expected: all pass (46 + 5 new = 51)

- [ ] **Step 5: Commit**

```bash
git add mcp-server/dosbox_mcp/binary_discovery.py mcp-server/tests/test_binary_discovery.py
git commit -m "feat(mcp-server): honor DOSBOX_X_BINARY and warn that stock DOSBox-X won't work"
```

---

### Task 6: Write the bundle QUICKSTART

**Goal:** Author the getting-started document that ships inside every bundle, so a user who has only unzipped a file knows exactly what to run.

**Files:**
- Create: `contrib/bundle/QUICKSTART.md`

**Acceptance Criteria:**
- [ ] Covers: prerequisites, install command, MCP client config with `DOSBOX_X_BINARY`, and one worked example
- [ ] Shows both Windows and Linux paths (the same file ships in both bundles)
- [ ] Names the six tools so a reader knows the surface without opening another document
- [ ] Contains no repo-relative links — the reader has a bundle, not a checkout

**Verify:** `test -f contrib/bundle/QUICKSTART.md && grep -c DOSBOX_X_BINARY contrib/bundle/QUICKSTART.md` → at least `2`

**Steps:**

- [ ] **Step 1: Create the file**

Create `contrib/bundle/QUICKSTART.md`:

````markdown
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

- Python 3.9 or newer (`python --version`)
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
````

- [ ] **Step 2: Verify**

```bash
test -f contrib/bundle/QUICKSTART.md && echo "exists"
grep -c DOSBOX_X_BINARY contrib/bundle/QUICKSTART.md   # expect >= 2
grep -c "](\./\|](\.\./" contrib/bundle/QUICKSTART.md || echo "no repo-relative links: OK"
```

- [ ] **Step 3: Commit**

```bash
git add contrib/bundle/QUICKSTART.md
git commit -m "docs: add bundle quickstart"
```

---

### Task 7: Bundle tooling — assembly and smoke scripts

**Goal:** Provide two scripts the release workflow calls: one that assembles a platform bundle into the fixed layout, and one that proves an assembled bundle actually runs a DOS command.

**Files:**
- Create: `scripts/assemble_bundle.py`
- Create: `scripts/smoke_bundle.py`
- Create: `tests/assemble_bundle_tests.py`

**Acceptance Criteria:**
- [ ] `assemble_bundle.py` produces the exact layout from Global Constraints: `dosbox-x/`, `mcp-server/`, `scripts/host_control_client.py`, `QUICKSTART.md`, `COPYING`
- [ ] The vendored copy lands at `mcp-server/dosbox_mcp/_vendor/host_control_client.py` and is byte-identical to `scripts/host_control_client.py`
- [ ] `__pycache__` directories and `mcp-server/tests/` are excluded from the bundle
- [ ] `smoke_bundle.py` exits 0 on a working bundle and non-zero with a readable message otherwise
- [ ] Assembly is covered by tests using fixture directories, with no real emulator binary needed

**Verify:** `python -m pytest tests/assemble_bundle_tests.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/assemble_bundle_tests.py`:

```python
import filecmp
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from assemble_bundle import assemble  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "host_control_client.py").write_text("# canonical client\n")
    (repo / "mcp-server" / "dosbox_mcp" / "_vendor").mkdir(parents=True)
    (repo / "mcp-server" / "pyproject.toml").write_text("[project]\nname='x'\n")
    (repo / "mcp-server" / "dosbox_mcp" / "__init__.py").write_text("")
    (repo / "mcp-server" / "dosbox_mcp" / "_vendor" / "__init__.py").write_text("")
    (repo / "mcp-server" / "dosbox_mcp" / "__pycache__").mkdir()
    (repo / "mcp-server" / "dosbox_mcp" / "__pycache__" / "junk.pyc").write_text("x")
    (repo / "mcp-server" / "tests").mkdir()
    (repo / "mcp-server" / "tests" / "test_x.py").write_text("")
    (repo / "contrib" / "bundle").mkdir(parents=True)
    (repo / "contrib" / "bundle" / "QUICKSTART.md").write_text("# quickstart\n")
    (repo / "COPYING").write_text("GPL-2.0\n")

    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "dosbox-x.exe").write_text("MZ fake binary")
    (payload / "shaders").mkdir()
    (payload / "shaders" / "a.glsl").write_text("shader")

    return repo, payload


def test_creates_expected_layout(fake_repo, tmp_path):
    repo, payload = fake_repo

    root = assemble(
        repo_root=repo, payload_dir=payload,
        version="0.1.0", platform="win64", out_dir=tmp_path / "out",
    )

    assert root.name == "dosbox-cli-0.1.0-win64"
    assert (root / "dosbox-x" / "dosbox-x.exe").is_file()
    assert (root / "dosbox-x" / "shaders" / "a.glsl").is_file()
    assert (root / "mcp-server" / "pyproject.toml").is_file()
    assert (root / "scripts" / "host_control_client.py").is_file()
    assert (root / "QUICKSTART.md").is_file()
    assert (root / "COPYING").is_file()


def test_vendors_client_identically(fake_repo, tmp_path):
    repo, payload = fake_repo

    root = assemble(
        repo_root=repo, payload_dir=payload,
        version="0.1.0", platform="win64", out_dir=tmp_path / "out",
    )

    canonical = root / "scripts" / "host_control_client.py"
    vendored = root / "mcp-server" / "dosbox_mcp" / "_vendor" / "host_control_client.py"
    assert vendored.is_file()
    assert filecmp.cmp(canonical, vendored, shallow=False)


def test_excludes_pycache_and_tests(fake_repo, tmp_path):
    repo, payload = fake_repo

    root = assemble(
        repo_root=repo, payload_dir=payload,
        version="0.1.0", platform="win64", out_dir=tmp_path / "out",
    )

    assert not (root / "mcp-server" / "tests").exists()
    assert not list(root.rglob("__pycache__"))
    assert not list(root.rglob("*.pyc"))


def test_missing_payload_is_an_error(fake_repo, tmp_path):
    repo, _ = fake_repo

    with pytest.raises(FileNotFoundError):
        assemble(
            repo_root=repo, payload_dir=tmp_path / "nope",
            version="0.1.0", platform="win64", out_dir=tmp_path / "out",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/assemble_bundle_tests.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'assemble_bundle'`

- [ ] **Step 3: Write `scripts/assemble_bundle.py`**

```python
#!/usr/bin/env python3
"""Assemble a release bundle into the layout documented in the design spec.

Layout (identical on both platforms, modulo the binary's name):

    dosbox-cli-<version>-<platform>/
    |-- QUICKSTART.md
    |-- COPYING
    |-- dosbox-x/      emulator payload, including its own scripts/ directory
    |-- mcp-server/    pip install target, with the vendored client inside
    `-- scripts/host_control_client.py

The emulator payload is nested under dosbox-x/ because it ships its own
scripts/ directory, which would otherwise collide with the repo's
scripts/host_control_client.py at bundle root.
"""

import argparse
import shutil
import sys
from pathlib import Path

_EXCLUDE = shutil.ignore_patterns("__pycache__", "*.pyc", "tests")


def assemble(repo_root, payload_dir, version, platform, out_dir):
    """Build the bundle tree and return the path to its root directory."""
    repo_root = Path(repo_root)
    payload_dir = Path(payload_dir)
    out_dir = Path(out_dir)

    if not payload_dir.is_dir():
        raise FileNotFoundError(f"emulator payload not found: {payload_dir}")

    client = repo_root / "scripts" / "host_control_client.py"
    quickstart = repo_root / "contrib" / "bundle" / "QUICKSTART.md"
    copying = repo_root / "COPYING"
    for required in (client, quickstart, copying):
        if not required.is_file():
            raise FileNotFoundError(f"required file missing: {required}")

    root = out_dir / f"dosbox-cli-{version}-{platform}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    shutil.copytree(payload_dir, root / "dosbox-x", ignore=_EXCLUDE)
    shutil.copytree(repo_root / "mcp-server", root / "mcp-server", ignore=_EXCLUDE)

    (root / "scripts").mkdir()
    shutil.copy2(client, root / "scripts" / "host_control_client.py")

    # The vendored copy is what makes a plain `pip install ./mcp-server`
    # work; without it _client_import falls back to a sibling scripts/
    # directory that does not exist once the package is in site-packages.
    vendor_dir = root / "mcp-server" / "dosbox_mcp" / "_vendor"
    vendor_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(client, vendor_dir / "host_control_client.py")

    shutil.copy2(quickstart, root / "QUICKSTART.md")
    shutil.copy2(copying, root / "COPYING")

    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--payload-dir", required=True,
                        help="directory holding the built emulator and its assets")
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", required=True, choices=["win64", "linux-x86_64"])
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)

    root = assemble(
        repo_root=args.repo_root,
        payload_dir=args.payload_dir,
        version=args.version,
        platform=args.platform,
        out_dir=args.out_dir,
    )
    print(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/assemble_bundle_tests.py -v
```

Expected: 4 passed

- [ ] **Step 5: Write `scripts/smoke_bundle.py`**

```python
#!/usr/bin/env python3
"""Prove an assembled bundle actually runs a DOS command.

Run this AFTER `pip install <bundle>/mcp-server`, so it exercises the
installed package rather than the repo checkout. That is the point: a check
that imported from the checkout would pass while the bundle was broken.
"""

import os
import sys
import time
from pathlib import Path


def smoke(bundle_root):
    bundle_root = Path(bundle_root).resolve()
    binary = bundle_root / "dosbox-x" / (
        "dosbox-x.exe" if os.name == "nt" else "dosbox-x"
    )
    if not binary.is_file():
        raise SystemExit(f"FAIL: bundled binary missing: {binary}")

    os.environ["DOSBOX_X_BINARY"] = str(binary)

    # Imported here, after the env var is set, and resolved from the
    # installed package rather than any local source tree.
    from dosbox_mcp.session import DosboxSession

    session, setup_results = DosboxSession.launch(cwd=str(bundle_root))
    try:
        for step in setup_results:
            if not step.get("ok"):
                raise SystemExit(f"FAIL: setup step failed: {step}")

        session.exec("ver")
        deadline = time.monotonic() + 60
        result = None
        while time.monotonic() < deadline:
            result = session.poll(wait_seconds=5)
            if result["done"]:
                break
        if result is None or not result["done"]:
            raise SystemExit("FAIL: 'ver' did not complete within 60s")
        if not result["ok"]:
            raise SystemExit(f"FAIL: 'ver' reported failure: {result}")

        print("SMOKE OK")
        print(f"  binary : {binary}")
        print(f"  output : {result['output'].strip()[:200]!r}")
        return 0
    finally:
        session.stop(force=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke_bundle.py <bundle-root>")
    sys.exit(smoke(sys.argv[1]))
```

- [ ] **Step 6: Confirm the smoke script runs against a locally built bundle**

This is the one place a real binary is needed. On Windows, with a build already present at `bin/x64/Release SDL2/`:

```bash
python scripts/assemble_bundle.py --payload-dir "bin/x64/Release SDL2" \
  --version 0.0.0-dev --platform win64 --out-dir /tmp/bundletest
pip install "/tmp/bundletest/dosbox-cli-0.0.0-dev-win64/mcp-server"
python scripts/smoke_bundle.py "/tmp/bundletest/dosbox-cli-0.0.0-dev-win64"
```

Expected: `SMOKE OK` followed by the binary path and DOS version output.

If no local build exists, skip this step and note it — Task 10 covers it in CI.

- [ ] **Step 7: Commit**

```bash
git add scripts/assemble_bundle.py scripts/smoke_bundle.py tests/assemble_bundle_tests.py
git commit -m "feat: add bundle assembly and smoke-test scripts"
```

---

### Task 8: Add the release workflow

**Goal:** Add `.github/workflows/release.yml` that builds both platforms on a `dosbox-cli-v*` tag, assembles and verifies each bundle, and publishes a GitHub Release only when both succeed.

**Files:**
- Create: `.github/workflows/release.yml`

**Acceptance Criteria:**
- [ ] Triggers on `push` to tags matching `dosbox-cli-v*` and on `workflow_dispatch`
- [ ] Version is derived by stripping the `dosbox-cli-v` prefix from the tag; `workflow_dispatch` runs use `0.0.0-dispatch`
- [ ] `windows-bundle` builds `Release SDL2|x64` via MSBuild; `linux-bundle` builds via `./build-scripts/build-sdl2`
- [ ] Each bundle job installs the MCP server **from the assembled bundle** and runs `scripts/smoke_bundle.py` against it
- [ ] Linux is packaged as `.tar.gz`; Windows as `.zip`
- [ ] `publish` has `needs: [windows-bundle, linux-bundle]` and runs only for tag pushes
- [ ] The workflow is valid YAML and `actionlint`-clean if available

**Verify:** `python -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml', encoding='utf-8')); print('valid')"` → `valid`

**Steps:**

- [ ] **Step 1: Create the workflow**

Create `.github/workflows/release.yml`:

```yaml
name: Release bundles

on:
  push:
    tags:
      - 'dosbox-cli-v*'
  workflow_dispatch:

permissions:
  contents: write

jobs:
  windows-bundle:
    runs-on: windows-2022
    steps:
      - uses: actions/checkout@v7
      - uses: microsoft/setup-msbuild@v3

      # No setup-python action: windows-2022 runners ship Python 3, and this
      # repo has no precedent pinning a setup-python version to copy.
      - name: Derive version
        shell: bash
        run: |
          if [[ "${GITHUB_REF}" == refs/tags/dosbox-cli-v* ]]; then
            echo "VERSION=${GITHUB_REF##*/dosbox-cli-v}" >> $GITHUB_ENV
          else
            echo "VERSION=0.0.0-dispatch" >> $GITHUB_ENV
          fi

      - name: Build Release SDL2 x64
        shell: pwsh
        run: |
          msbuild -m vs/dosbox-x.sln -t:dosbox-x:Rebuild -p:Configuration="Release SDL2" -p:Platform=x64
          if (-not (Test-Path -Path "bin\x64\Release SDL2\dosbox-x.exe" -PathType Leaf)) { exit 1 }

      - name: Stage emulator payload
        shell: bash
        run: |
          top=`pwd`
          payload="$top/payload"
          mkdir -p "$payload"/{drivez,scripts,shaders,glshaders,languages}
          cp "$top/bin/x64/Release SDL2/dosbox-x.exe" "$payload/"
          cp $top/CHANGELOG "$payload/CHANGELOG.txt"
          cp $top/dosbox-x.reference.conf "$payload/"
          cp $top/dosbox-x.reference.full.conf "$payload/"
          cp $top/contrib/windows/installer/inpoutx64.dll "$payload/"
          cp $top/contrib/fonts/FREECG98.BMP "$payload/"
          cp $top/contrib/fonts/wqy_1?pt.bdf "$payload/"
          cp $top/contrib/fonts/Nouveau_IBM.ttf "$payload/"
          cp $top/contrib/fonts/SarasaGothicFixed.ttf "$payload/"
          cp $top/contrib/windows/installer/drivez_readme.txt "$payload/drivez/readme.txt"
          cp $top/contrib/windows/installer/windows_explorer_context_menu*.bat "$payload/scripts/"
          cp $top/contrib/windows/shaders/* "$payload/shaders/"
          cp $top/contrib/glshaders/* "$payload/glshaders/"
          cp $top/contrib/translations/*/*.lng "$payload/languages/"

      - name: Assemble bundle
        shell: bash
        run: |
          python scripts/assemble_bundle.py \
            --payload-dir payload \
            --version "${VERSION}" \
            --platform win64 \
            --out-dir dist

      - name: Smoke-test the assembled bundle
        shell: bash
        run: |
          bundle="dist/dosbox-cli-${VERSION}-win64"
          python -m pip install --upgrade pip
          python -m pip install "${bundle}/mcp-server"
          python scripts/smoke_bundle.py "${bundle}"

      - name: Package
        shell: pwsh
        run: |
          Compress-Archive -Path "dist\dosbox-cli-$env:VERSION-win64" `
                           -DestinationPath "dosbox-cli-$env:VERSION-win64.zip"

      - uses: actions/upload-artifact@v7.0.1
        with:
          name: bundle-win64
          path: dosbox-cli-${{ env.VERSION }}-win64.zip

  linux-bundle:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7

      # No setup-python action: ubuntu-latest ships python3, and this repo
      # has no precedent pinning a setup-python version to copy.
      - name: Derive version
        run: |
          if [[ "${GITHUB_REF}" == refs/tags/dosbox-cli-v* ]]; then
            echo "VERSION=${GITHUB_REF##*/dosbox-cli-v}" >> $GITHUB_ENV
          else
            echo "VERSION=0.0.0-dispatch" >> $GITHUB_ENV
          fi

      - name: Install build dependencies
        run: |
          sudo apt-get update -y
          sudo apt-get install -y nasm fluidsynth libfluidsynth-dev libpcap-dev \
            libslirp-dev libsdl-net1.2-dev libsdl2-net-dev libglu1-mesa-dev \
            freeglut3-dev mesa-common-dev libpng-dev libavcodec-dev \
            libavformat-dev libavutil-dev libswscale-dev libavdevice-dev \
            libavcodec-extra

      - name: Update build info
        run: |
          export shortsha=`echo ${GITHUB_SHA} | cut -c1-7`
          export copyrightyear=`git show -s --format=%at | xargs -I# date -d @# +'%Y'`
          export updatestr=`git show -s --format=%at | xargs -I# date -d @# +'%b %d, %Y %I:%M:%S%P'`
          echo '/* auto generated */' > include/build_timestamp.h
          echo "#define UPDATED_STR \"${updatestr}\"" >> include/build_timestamp.h
          echo "#define GIT_COMMIT_HASH \"${shortsha}\"" >> include/build_timestamp.h
          echo "#define COPYRIGHT_END_YEAR \"${copyrightyear}\"" >> include/build_timestamp.h

      - name: Build SDL2 release
        run: |
          ./build-scripts/build-sdl2
          test -f src/dosbox-x
          strip -s src/dosbox-x

      - name: Stage emulator payload
        run: |
          top=`pwd`
          payload="$top/payload"
          mkdir -p "$payload"/{shaders,glshaders,languages}
          cp $top/src/dosbox-x "$payload/dosbox-x"
          chmod +x "$payload/dosbox-x"
          cp $top/CHANGELOG "$payload/CHANGELOG.txt"
          cp $top/dosbox-x.reference.conf "$payload/"
          cp $top/dosbox-x.reference.full.conf "$payload/"
          cp $top/contrib/fonts/FREECG98.BMP "$payload/"
          cp $top/contrib/fonts/wqy_1?pt.bdf "$payload/"
          cp $top/contrib/fonts/Nouveau_IBM.ttf "$payload/"
          cp $top/contrib/fonts/SarasaGothicFixed.ttf "$payload/"
          cp $top/contrib/glshaders/* "$payload/glshaders/"
          cp $top/contrib/translations/*/*.lng "$payload/languages/"

      - name: Assemble bundle
        run: |
          python3 scripts/assemble_bundle.py \
            --payload-dir payload \
            --version "${VERSION}" \
            --platform linux-x86_64 \
            --out-dir dist

      - name: Smoke-test the assembled bundle
        run: |
          bundle="dist/dosbox-cli-${VERSION}-linux-x86_64"
          python3 -m pip install --upgrade pip
          python3 -m pip install "${bundle}/mcp-server"
          python3 scripts/smoke_bundle.py "${bundle}"

      - name: Package
        run: |
          tar -czf "dosbox-cli-${VERSION}-linux-x86_64.tar.gz" \
            -C dist "dosbox-cli-${VERSION}-linux-x86_64"

      - uses: actions/upload-artifact@v7.0.1
        with:
          name: bundle-linux-x86_64
          path: dosbox-cli-${{ env.VERSION }}-linux-x86_64.tar.gz

  publish:
    needs: [windows-bundle, linux-bundle]
    if: startsWith(github.ref, 'refs/tags/dosbox-cli-v')
    runs-on: ubuntu-latest
    steps:
      # v7 pairs with the upload-artifact@v7.0.1 used above and elsewhere in
      # this repo — upload and download are versioned in lockstep, and a
      # mismatched major will not find the artifacts.
      - uses: actions/download-artifact@v7
        with:
          path: artifacts
      - name: Publish release
        uses: softprops/action-gh-release@v3
        with:
          files: artifacts/**/*
          generate_release_notes: true
```

- [ ] **Step 2: Validate the YAML**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml', encoding='utf-8')); print('valid')"
```

Expected: `valid`

- [ ] **Step 3: Lint if actionlint is available**

```bash
command -v actionlint >/dev/null && actionlint .github/workflows/release.yml || echo "actionlint not installed, skipping"
```

- [ ] **Step 4: Confirm the guard invariant still holds**

```bash
grep -n -B4 "action-gh-release" .github/workflows/*.yml | grep "refs/tags/'"
```

Expected: no output. The new `publish` job is guarded by `refs/tags/dosbox-cli-v`, which is intentionally *not* the bare prefix Task 3 removed.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci: add release workflow producing verified win64/linux bundles"
```

---

### Task 9: Update the READMEs for downloadable bundles

**Goal:** Tell readers a prebuilt download exists, and correct the MCP server's install docs, which currently state the package cannot be installed outside a checkout.

**Files:**
- Modify: `README.md` (insert a Download section before `## Building` at line 72)
- Modify: `mcp-server/README.md:10-20` (the Install section)

**Acceptance Criteria:**
- [ ] Root README has a Download section above Building, linking to the Releases page
- [ ] Building is reframed as the fallback for unsupported platforms, not the default path
- [ ] `mcp-server/README.md` no longer claims the package "is not usable installed standalone outside this repo"
- [ ] `mcp-server/README.md` documents both the bundle install and the checkout install, and mentions `DOSBOX_X_BINARY`

**Verify:** `grep -c "not usable installed standalone" mcp-server/README.md` → `0`

**Steps:**

- [ ] **Step 1: Add the Download section to the root README**

Insert immediately before the `## Building` heading in `README.md`:

```markdown
## Download

Prebuilt bundles for Windows and Linux are on the
[Releases page](https://github.com/helloadamlee/dosbox-cli/releases) — each
contains the emulator, the MCP server, and a `QUICKSTART.md`. No compiler
required:

```bash
# unzip, then:
pip install ./mcp-server
```

Then point your MCP client at `dosbox-mcp`, setting `DOSBOX_X_BINARY` to the
bundled binary. Full steps are in the bundle's `QUICKSTART.md`.

```

- [ ] **Step 2: Reframe the Building section**

Replace the body of `## Building` in `README.md` with:

```markdown
Building from source is only necessary for platforms without a prebuilt bundle
(macOS, ARM, BSD), or when working on the emulator itself.

This is a DOSBox-X source fork, so upstream build instructions apply — see
[`BUILD.md`](BUILD.md) and [`INSTALL.md`](INSTALL.md). Platform build scripts live in
[`build-scripts/`](build-scripts/).
```

- [ ] **Step 3: Correct the MCP server install docs**

Replace the `## Install` section of `mcp-server/README.md` (lines 10-20) with:

```markdown
## Install

**From a release bundle** (recommended — no compiler needed):

```bash
pip install ./mcp-server
```

The bundle carries a vendored copy of `host_control_client.py`, so this works
as a normal install.

**From a repo checkout** (for development):

```bash
cd mcp-server
pip install -e .
```

An editable install keeps the package next to its sibling `scripts/` directory,
which is where it finds `host_control_client.py` in a checkout.

Either way, set `DOSBOX_X_BINARY` to a host-control-capable `dosbox-x` binary —
one from a release bundle or your own build of this fork. A stock DOSBox-X
install will not work; it has no host-control support.
```

- [ ] **Step 4: Verify**

```bash
grep -c "not usable installed standalone" mcp-server/README.md   # expect 0
grep -n "^## Download" README.md                                  # expect a line number < the Building line
grep -n "^## Building" README.md
grep -c "DOSBOX_X_BINARY" mcp-server/README.md                    # expect >= 1
```

- [ ] **Step 5: Commit**

```bash
git add README.md mcp-server/README.md
git commit -m "docs: document prebuilt bundle downloads and corrected install paths"
```

---

### Task 10: Rehearse the release and verify both bundles

**Goal:** **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

Run the release workflow via `workflow_dispatch` (no tag) and confirm both the Windows and Linux jobs assemble a bundle, install the MCP server from that bundle, and get `SMOKE OK` from a real `ver` command — capturing the run URL and both jobs' smoke output as evidence before any tag is pushed.

**Files:**
- No file changes; this task produces evidence.

**Acceptance Criteria:**
- [ ] A `workflow_dispatch` run of `release.yml` completes with `windows-bundle` = success, captured as the job's conclusion from `gh run view`
- [ ] The same run has `linux-bundle` = success, captured the same way
- [ ] The Windows job's log contains the literal string `SMOKE OK`, captured verbatim
- [ ] The Linux job's log contains the literal string `SMOKE OK`, captured verbatim
- [ ] Both uploaded artifacts exist on the run (`bundle-win64`, `bundle-linux-x86_64`) with non-zero size
- [ ] The `publish` job did NOT run (it is tag-gated, and this is a dispatch)
- [ ] The maintainer has downloaded the Linux tarball onto a real Linux machine and gotten `SMOKE OK` there, with that terminal output captured — CI's ubuntu-latest is one distro, not proof of the target

**Verify:** `gh run view <run-id> --json jobs --jq '.jobs[] | "\(.name) => \(.conclusion)"'` → both bundle jobs report `success`

**Steps:**

- [ ] **Step 1: Trigger the rehearsal**

```bash
GH="/c/Program Files/GitHub CLI/gh.exe"
"$GH" workflow run release.yml --repo helloadamlee/dosbox-cli --ref main
```

Then find the run:

```bash
"$GH" run list --repo helloadamlee/dosbox-cli --workflow release.yml --limit 1 \
  --json databaseId,status,url
```

Record the `databaseId` as `<run-id>` and the URL as evidence.

- [ ] **Step 2: Wait for completion**

```bash
"$GH" run watch <run-id> --repo helloadamlee/dosbox-cli
```

The Windows job includes a full MSBuild rebuild; expect 20-40 minutes.

- [ ] **Step 3: Capture per-job conclusions**

```bash
"$GH" run view <run-id> --repo helloadamlee/dosbox-cli \
  --json jobs --jq '.jobs[] | "\(.name) => \(.conclusion)"'
```

Both `windows-bundle` and `linux-bundle` must read `success`. Paste this output verbatim as evidence. `publish` must be absent or `skipped`.

- [ ] **Step 4: Capture the smoke evidence from BOTH platforms**

```bash
"$GH" run view <run-id> --repo helloadamlee/dosbox-cli --log \
  | grep -E "SMOKE OK|binary :|output :|FAIL:"
```

There must be a `SMOKE OK` from the Windows job and a separate one from the Linux job. One `SMOKE OK` is not sufficient — both platforms must be independently proven. Paste both, with their surrounding `binary :` / `output :` lines.

- [ ] **Step 5: Confirm the artifacts exist**

```bash
"$GH" api repos/helloadamlee/dosbox-cli/actions/runs/<run-id>/artifacts \
  --jq '.artifacts[] | "\(.name) \(.size_in_bytes) bytes"'
```

Both `bundle-win64` and `bundle-linux-x86_64` must be listed with non-zero sizes.

- [ ] **Step 6: Verify the Linux bundle on real Linux hardware**

CI proves the bundle works on `ubuntu-latest`. That is one distro on one
runner image — not proof it works on the maintainer's actual Linux machines,
which are the primary target. Download the rehearsal artifact and run it there:

```bash
gh run download <run-id> --repo helloadamlee/dosbox-cli \
  --name bundle-linux-x86_64 --dir ~/relcheck
cd ~/relcheck && tar -xzf dosbox-cli-*-linux-x86_64.tar.gz
cd dosbox-cli-*-linux-x86_64
python3 -m pip install --user ./mcp-server
python3 ../../scripts/smoke_bundle.py "$PWD"   # or the repo's copy of the script
```

Expected: `SMOKE OK` with the binary path and DOS version output. Capture that
terminal output as evidence. If the emulator fails to start here but passed in
CI, the likely cause is a shared-library difference between the runner image
and the local distro — record the `ldd dosbox-x/dosbox-x` output before
deciding whether to fix or to document a minimum-glibc requirement.

This step needs the maintainer's own machine; hand it to them rather than
substituting the CI result.

- [ ] **Step 7: If either platform failed, fix and re-run**

Do not proceed to Task 11 on a partial pass. Pull the failing job's log:

```bash
"$GH" run view <run-id> --repo helloadamlee/dosbox-cli --log-failed
```

Fix, commit, and return to Step 1. The gate closes only when both platforms are green in a single run.

---

### Task 11: Tag and publish v0.1.0

**Goal:** **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

Push the `dosbox-cli-v0.1.0` tag that triggers the real release, and confirm the published release carries exactly the two expected assets — `dosbox-cli-0.1.0-win64.zip` and `dosbox-cli-0.1.0-linux-x86_64.tar.gz` — captured from `gh release view --json assets`.

**Files:**
- No file changes; this task publishes.

**Acceptance Criteria:**
- [ ] The user has explicitly confirmed the release should go public before the tag is pushed
- [ ] The release contains exactly two assets: `dosbox-cli-0.1.0-win64.zip` and `dosbox-cli-0.1.0-linux-x86_64.tar.gz`
- [ ] No upstream-flavored assets appear (proving Task 3's guard narrowing worked)
- [ ] Downloading and unzipping the Windows asset yields the documented layout

**Verify:** `gh release view dosbox-cli-v0.1.0 --json assets --jq '.assets[].name'` → exactly the two expected names

**Steps:**

- [ ] **Step 1: Confirm with the user**

Publishing creates a public, externally-visible release. Ask the user to confirm before pushing the tag. Do not push on the strength of Task 10 passing.

- [ ] **Step 2: Push the tag**

```bash
git tag dosbox-cli-v0.1.0
git push publish dosbox-cli-v0.1.0
```

- [ ] **Step 3: Watch the release run**

```bash
GH="/c/Program Files/GitHub CLI/gh.exe"
"$GH" run list --repo helloadamlee/dosbox-cli --workflow release.yml --limit 1 --json databaseId
"$GH" run watch <run-id> --repo helloadamlee/dosbox-cli
```

- [ ] **Step 4: Verify the published assets**

```bash
"$GH" release view dosbox-cli-v0.1.0 --repo helloadamlee/dosbox-cli \
  --json assets --jq '.assets[] | "\(.name) \(.size)"'
```

Expected exactly:

```
dosbox-cli-0.1.0-win64.zip <size>
dosbox-cli-0.1.0-linux-x86_64.tar.gz <size>
```

Any additional asset means an inherited workflow still publishes on our tag shape — return to Task 3.

- [ ] **Step 5: Verify the downloaded bundle**

```bash
"$GH" release download dosbox-cli-v0.1.0 --repo helloadamlee/dosbox-cli \
  --pattern "*win64.zip" --dir /tmp/relcheck
cd /tmp/relcheck && unzip -q dosbox-cli-0.1.0-win64.zip
ls dosbox-cli-0.1.0-win64/
test -f dosbox-cli-0.1.0-win64/dosbox-x/dosbox-x.exe && echo "binary OK"
test -f dosbox-cli-0.1.0-win64/QUICKSTART.md && echo "quickstart OK"
test -f dosbox-cli-0.1.0-win64/mcp-server/dosbox_mcp/_vendor/host_control_client.py && echo "vendored client OK"
```

All three checks must print OK.
