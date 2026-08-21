# Known-Issues Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out the actionable items in [`docs/known-issues.md`](../../known-issues.md) so that doc shrinks to intentional scope decisions only: repair the three remaining non-macOS stale build paths, build the missing wire-format test coverage and then lift the `mcp<2` pin, and turn the Linux runtime dependency list from "known to work on one distro" into something mechanically checked.

**Architecture:** Four tracks. Track A is three one-line path edits with no CI cost. Track B is a test-first sequence on the MCP server — real-transport tests land against `mcp<2`, the `FastMCP` → `MCPServer` port runs against those tests, and only then does the `pyproject.toml` cap come off. Track C adds a CI step that derives the emulator's runtime library set from `ldd` and asserts `QUICKSTART.md`'s package list actually covers it, plus a container run on a distro that splits packages differently from Debian. Track D updates the doc that motivated all of this.

**Tech Stack:** GNU autotools (`Makefile.am`), GitHub Actions, MSYS/MinGW build scripts, Python 3.10+, `mcp` SDK, pytest, Docker/Podman containers.

**Source doc:** `docs/known-issues.md`

## Global Constraints

- **No macOS work.** The `./appbundledeps.py` reference on `Makefile.am:45` sits inside the `if MACOSX` block and is deliberately left broken — see Out of Scope. Do not "fix it while you're in the file."
- **No glibc floor change.** `release.yml`'s `linux-bundle` job stays on `ubuntu-latest`. The Ubuntu 24.04+ / Debian 13+ requirement remains a documented, intentional constraint. Track C validates the *package list*, not the *glibc baseline*.
- **Track B is strictly ordered.** Tests (Task 5) must be green against `mcp<2` before the port (Task 6) is written, and the cap (Task 7) comes off last. Doing the port first discards the entire point of the exercise — the tests exist to catch a silent response-shape change, and a test written after the port would just codify whatever the port produced.
- **Never widen an inherited workflow's release guard.** Track A touches `hxdos.yml`; the existing narrow `refs/tags/dosbox-x-v*` guards on `action-gh-release` steps stay exactly as they are.
- **All seven tools stay registered under the same names.** `exec` is registered as `@mcp.tool(name="exec")` because the Python function is `exec_command`. Any port must preserve that alias.

**User decisions (already made):**
- "excluding the MacOS items" — the macOS bundle job and the `appbundledeps.py` path fix are both out of scope.
- glibc floor: "Keep documented, don't widen" — no `ubuntu-22.04` rebuild, no investigation task.
- mcp 2.0: "Tests then port then lift cap" — the full sequence, in that order.

---

## Track A — Stale build-script paths

### Task 1: Repair `EXTRA_DIST` in `Makefile.am`

**Goal:** `make dist` stops omitting `autogen.sh`, which currently breaks the distribution tarball.

**Files:**
- Modify: `Makefile.am` (line 3)

**Acceptance Criteria:**
- [x] `Makefile.am:3` reads `EXTRA_DIST = scripts/autogen.sh`
- [x] `grep -n 'appbundledeps' Makefile.am` still shows the unmodified `./appbundledeps.py` line (proving the macOS exclusion held)
- [x] `git diff --stat Makefile.am` shows exactly 1 insertion, 1 deletion

**Verify:** `sed -n '3p' Makefile.am` → `EXTRA_DIST = scripts/autogen.sh`

**Steps:**

- [x] **Step 1: Confirm the before-state**

```bash
sed -n '3p' Makefile.am                 # expect: EXTRA_DIST = autogen.sh
test -f scripts/autogen.sh && echo ok   # expect: ok
test -f autogen.sh || echo "absent at root, as expected"
```

- [x] **Step 2: Apply the fix**

```bash
sed -i 's|^EXTRA_DIST = autogen.sh$|EXTRA_DIST = scripts/autogen.sh|' Makefile.am
```

- [x] **Step 3: Verify, including the exclusion**

```bash
sed -n '3p' Makefile.am
grep -n 'appbundledeps' Makefile.am     # must still show ./appbundledeps.py
git diff --stat Makefile.am             # expect: 1 insertion(+), 1 deletion(-)
```

If `git diff --stat` shows more than one changed line, revert and reapply — `sed` matched something unintended.

---

### Task 2: Repair the HX-DOS workflow's build-script argument

**Goal:** `hxdos.yml` invokes a script path that exists, so the HX-DOS build can run at all.

**Files:**
- Modify: `.github/workflows/hxdos.yml` (line 56)

**Context that makes this a real bug, not a cosmetic one:** the workflow passes `build-mingw-hx-dos` as `$1` to `runbuild.sh`, and `build-scripts/mingw/lowend-bin/runbuild.sh` does:

```sh
repodir=$(cat /mingw/msys/1.0/pwd.txt)
cd "${repodir}" || exit
./"${1}"
```

So `$1` resolves relative to the repo root. After the `3fb7ef870` reorganization there is no `./build-mingw-hx-dos` there — the script lives at `build-scripts/build-mingw-hx-dos`. Every sibling workflow (`mingw32.yml`, `mingw64.yml`) already calls `./build-scripts/build-mingw*`.

**Files to read first:** `.github/workflows/hxdos.yml` lines 30–60, `build-scripts/mingw/lowend-bin/runbuild.sh`

**Acceptance Criteria:**
- [x] Line 56 passes `build-scripts/build-mingw-hx-dos`
- [x] `build-scripts/build-mingw-hx-dos` exists in the tree
- [x] No `action-gh-release` guard anywhere in `hxdos.yml` was touched
- [x] The file still parses as YAML

**Verify:** `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/hxdos.yml'))"` then `grep -n 'runbuild.sh' .github/workflows/hxdos.yml`

**Steps:**

- [x] **Step 1: Record the before-state**

```bash
grep -n 'runbuild.sh' .github/workflows/hxdos.yml
test -f build-scripts/build-mingw-hx-dos && echo "target exists"
test -f build-mingw-hx-dos || echo "root copy absent, as expected"
git grep -n 'action-gh-release' -- .github/workflows/hxdos.yml > /tmp/hxdos-guards-before.txt
```

- [x] **Step 2: Apply the fix**

```bash
sed -i 's|runbuild.sh build-mingw-hx-dos|runbuild.sh build-scripts/build-mingw-hx-dos|' \
  .github/workflows/hxdos.yml
```

- [x] **Step 3: Verify the edit and the untouched guards**

```bash
grep -n 'runbuild.sh' .github/workflows/hxdos.yml
git grep -n 'action-gh-release' -- .github/workflows/hxdos.yml > /tmp/hxdos-guards-after.txt
diff /tmp/hxdos-guards-before.txt /tmp/hxdos-guards-after.txt && echo "guards unchanged"
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/hxdos.yml')); print('yaml ok')"
```

**Note on verification depth:** this fix is *not* proven by CI unless someone dispatches the HX-DOS workflow, which needs a full MSYS/MinGW toolchain bootstrap. Static verification (the target path exists, `runbuild.sh` resolves its argument from the repo root) is the intended stopping point. Say so plainly in the commit message rather than implying a build was exercised.

---

### Task 3: Repair the two OS/2 build scripts

**Goal:** `build-os2-sdl2.cmd` and `build-debug-os2-sdl2.cmd` invoke an `autogen.sh` that exists.

**Files:**
- Modify: `build-scripts/build-os2-sdl2.cmd` (line 3)
- Modify: `build-scripts/build-debug-os2-sdl2.cmd` (line 3)

**Acceptance Criteria:**
- [x] Both files call `bash scripts/autogen.sh`
- [x] Both files retain CRLF line endings if they had them (these are `.cmd` files)
- [x] `git diff --stat` shows 1 insertion / 1 deletion per file

**Verify:** `grep -n autogen build-scripts/build-os2-sdl2.cmd build-scripts/build-debug-os2-sdl2.cmd`

**Steps:**

- [x] **Step 1: Record line endings and content before editing**

```bash
file build-scripts/build-os2-sdl2.cmd build-scripts/build-debug-os2-sdl2.cmd
grep -n autogen build-scripts/build-os2-sdl2.cmd build-scripts/build-debug-os2-sdl2.cmd
```

Note whether `file` reports "CRLF line terminators". If it does, the edit must preserve them.

- [x] **Step 2: Apply the fix**

```bash
sed -i 's|^bash autogen\.sh|bash scripts/autogen.sh|' \
  build-scripts/build-os2-sdl2.cmd build-scripts/build-debug-os2-sdl2.cmd
```

- [x] **Step 3: Verify content and line endings survived**

```bash
grep -n autogen build-scripts/build-os2-sdl2.cmd build-scripts/build-debug-os2-sdl2.cmd
file build-scripts/build-os2-sdl2.cmd build-scripts/build-debug-os2-sdl2.cmd
git diff --stat build-scripts/
```

If `file` no longer reports CRLF where it did before, the `sed` stripped the `\r`. Restore with `git checkout` and redo with an editor that preserves the endings.

---

### Task 4: Commit Track A

**Goal:** One commit covering all three path repairs, with the macOS exclusion stated so a future reader doesn't take it for an oversight.

**Acceptance Criteria:**
- [x] Exactly four files changed: `Makefile.am`, `.github/workflows/hxdos.yml`, both OS/2 `.cmd` scripts
- [x] `git show --stat HEAD` shows 4 insertions, 4 deletions
- [x] The commit body notes that the `appbundledeps.py` reference is deliberately left alone

**Verify:** `git show --stat HEAD`

**Steps:**

- [x] **Step 1: Review the full diff before committing**

```bash
git diff -- Makefile.am .github/workflows/hxdos.yml \
  build-scripts/build-os2-sdl2.cmd build-scripts/build-debug-os2-sdl2.cmd
```

- [x] **Step 2: Stage only these four files**

```bash
git add Makefile.am .github/workflows/hxdos.yml \
  build-scripts/build-os2-sdl2.cmd build-scripts/build-debug-os2-sdl2.cmd
git status --short
```

There is uncommitted C++ work in `src/dos/` and `src/ints/` — confirm none of it got staged.

- [x] **Step 3: Commit** with a message covering all three fixes, the deliberate `appbundledeps.py` omission and why, and an explicit note that verification was static only (no CI job covers HX-DOS or OS/2 here).

---

## Track B — Lift the `mcp<2` pin, test-first

### Task 5: Add real-transport wire-format tests against `mcp<2`

**Goal:** Lock down the *observable protocol surface* — tool names, input schemas, and the shape of each tool's result as it comes back over an actual MCP session — so the 2.0 port has something that can fail.

This is the load-bearing task of Track B. `mcp-server/tests/test_server.py` calls `server.start_session(...)` as a plain Python function; it never touches the MCP layer, so it cannot observe a changed serialization, a dropped schema field, or a renamed tool. Those are exactly the failure modes 2.0's new tool-decorator options could introduce.

**Files:**
- Create: `mcp-server/tests/test_wire_format.py`
- Read first: `mcp-server/dosbox_mcp/server.py` (all 7 tools + `main`), `mcp-server/tests/test_server.py` (the `FakeSession` to reuse), `mcp-server/tests/fakes.py`

**Approach:** the `mcp` Python SDK ships an in-memory client/server transport pair, so a test can drive a real `ClientSession` without spawning a subprocess or opening a socket. In the 1.x line this is `mcp.shared.memory.create_connected_server_and_client_session`. **Confirm the exact import path in the installed version before writing against it** — `python -c "import mcp.shared.memory as m; print(dir(m))"` — rather than trusting this plan's spelling. If that helper is absent, fall back to wiring the SDK's memory object streams to `mcp._mcp_server.run()` directly.

**Acceptance Criteria:**
- [x] A `list_tools` assertion pins all seven names exactly: `start_session`, `exec`, `poll`, `send_input`, `cancel`, `status`, `stop_session`. Note `exec`, not `exec_command` — that alias is the single most likely thing a port breaks.
- [x] For each of the seven, the advertised input schema's required-vs-optional split is asserted (e.g. `start_session` requires `cwd` and nothing else; `poll` requires nothing and defaults `wait_seconds`)
- [x] At least three tools are *called* through the session and their full result payload asserted key-by-key, not just "no exception": `start_session`, `poll` (the widest payload — `running` / `done` / `output` / `errorlevel` / `max_errorlevel` / `ok` / `bad_command` / `drive` / `cwd`), and `status`
- [x] The error path is asserted: calling `exec` with no active session surfaces the `SessionError` to the client as a tool error, and the test pins *how* it surfaces (error flag vs. raised exception) — that convention is a plausible 2.0 change
- [x] Every dict-returning tool's result is asserted against the parsed structured payload, so a change from structured content to a JSON-in-text blob fails the test
  - **Found: this premise is inverted.** No tool declares an `outputSchema` (each is annotated bare `-> dict`, which FastMCP treats as unstructured), so results already arrive as a JSON-in-text blob with `structuredContent` **None** under mcp 1.28.1. The tests pin that observed convention in both directions, so a port that *starts* emitting `structuredContent` fails them. Task 6's guardrail should read that failure as a real 2.0 change to justify, not as a wrong test.
- [x] Tests use the existing `FakeSession` pattern — no real `dosbox-x` binary, no `DOSBOX_MCP_LIVE_TESTS` gating
- [x] `pytest mcp-server/tests/ -v` is fully green with `mcp<2` installed

**Verify:**
```bash
cd mcp-server && python -m pytest tests/ -v
python -c "import mcp; print(mcp.__version__)"   # must be 1.x for this task
```

**Steps:**

- [x] **Step 1: Pin the current environment and confirm the gap**

```bash
cd mcp-server
python -m pip show mcp | head -3
python -m pytest tests/ -v --tb=short 2>&1 | tail -20
grep -rn "ClientSession\|list_tools\|call_tool" tests/ || echo "NO transport-level coverage — confirms the gap"
```

- [x] **Step 2: Discover the SDK's in-memory session helper**

```bash
python -c "import mcp.shared.memory as m; print([n for n in dir(m) if not n.startswith('_')])"
python -c "from mcp.server.fastmcp import FastMCP; print([a for a in dir(FastMCP) if 'tool' in a or 'run' in a])"
```

Record what you find in a comment at the top of the new test file. The next task depends on knowing which of these names 2.0 kept.

- [x] **Step 3: Write `tests/test_wire_format.py`**

Structure it in four blocks so a port failure points at a specific layer:

1. `test_all_seven_tools_are_advertised` — `list_tools`, name-set equality
2. `test_input_schemas_pin_required_params` — per-tool required/optional split
3. `test_result_payloads` — call `start_session`, `poll`, `status` through the session with `FakeSession` patched in, assert every key
4. `test_no_session_surfaces_as_tool_error` — the error convention

Import `FakeSession` from `test_server.py` rather than copying it, and reuse the same `reset_session` autouse fixture so state doesn't leak between tests.

- [x] **Step 4: Confirm the tests actually bite**

Temporarily change `@mcp.tool(name="exec")` to `@mcp.tool()` in `server.py` and re-run. The name-set test **must** fail. Revert immediately. A test that passes against a deliberately broken server is worthless — this step is not optional.

```bash
python -m pytest tests/test_wire_format.py -v   # expect a FAILURE here
git checkout dosbox_mcp/server.py
python -m pytest tests/ -v                      # expect all green again
```

- [x] **Step 5: Commit the tests on their own**

A standalone commit lets `git bisect` later distinguish "the tests were wrong" from "the port was wrong." The message should say what the tests cover and that they deliberately land before the port.

---

### Task 6: Port `FastMCP` → `MCPServer`

**Goal:** `server.py` runs on the 2.0 API with Task 5's tests still green.

**Files:**
- Modify: `mcp-server/dosbox_mcp/server.py`
- Possibly modify: `mcp-server/tests/test_wire_format.py` (only if 2.0 legitimately changes a convention — see the guardrail)

**Acceptance Criteria:**
- [x] `server.py` imports from `mcp.server.mcpserver` (confirm the exact module path against the installed 2.0 package; do not trust this plan's spelling)
- [x] All seven tools still register with identical names, `exec` alias included
- [x] `pytest tests/ -v` green against mcp 2.0 in a scratch venv
- [x] `pytest tests/ -v` **still green against mcp 1.x** if the API surface allows both, or the incompatibility documented explicitly in the commit message if it does not
  - **Resolved as: not possible, documented.** `mcp.server.mcpserver` does not exist in 1.x and neither does `mcp.client.Client`, so no single version of `server.py` or of the tests covers both lines. The port is one-way; recorded in the commit message.
  - **Also found:** 2.0 removed `mcp.shared.memory.create_connected_server_and_client_session` (replacement: the public `mcp.client.Client`, which takes an `MCPServer` directly) and renamed the SDK models' camelCase fields to snake_case (`inputSchema` → `input_schema`, `isError` → `is_error`, `structuredContent` → `structured_content`). Attribute renames only — every asserted *value* was identical either side of the port.
- [x] `main()` still works — the `dosbox-mcp` console script starts and responds to `list_tools`

**Guardrail:** if a Task 5 test fails after the port, the default assumption is that the *port* is wrong, not the test. Only relax an assertion after finding the 2.0 changelog entry or SDK source that justifies it, and quote that justification in the commit message. Silently editing a test to match new behavior turns this whole track into theater.

**Verify:**
```bash
python -m venv /tmp/mcp2 && /tmp/mcp2/bin/pip install "mcp>=2,<3" pytest
/tmp/mcp2/bin/pip install -e ./mcp-server --no-deps
cd mcp-server && /tmp/mcp2/bin/python -m pytest tests/ -v
```

**Steps:**

- [x] **Step 1: Build a scratch 2.0 venv and inventory the new API**

```bash
python -m venv /tmp/mcp2
/tmp/mcp2/bin/pip install "mcp>=2,<3" pytest
/tmp/mcp2/bin/python -c "import mcp; print(mcp.__version__)"
/tmp/mcp2/bin/python -c "import mcp.server.mcpserver as s; print([n for n in dir(s) if not n.startswith('_')])"
/tmp/mcp2/bin/python -c "from mcp.server.mcpserver import MCPServer; import inspect; print(inspect.signature(MCPServer.tool))"
```

Write down the `tool()` signature — the new decorator options are the specific thing that could change a response shape.

- [x] **Step 2: Confirm the tests fail before the port**

```bash
/tmp/mcp2/bin/pip install -e ./mcp-server --no-deps
cd mcp-server && /tmp/mcp2/bin/python -m pytest tests/ -v 2>&1 | tail -20
```

Expect an ImportError on `mcp.server.fastmcp`. That is the baseline.

- [x] **Step 3: Apply the port** — change the import and the constructor; leave every decorator name and every function body alone. Keep `@mcp.tool(name="exec")` explicit.

- [x] **Step 4: Run both suites**

```bash
cd mcp-server
/tmp/mcp2/bin/python -m pytest tests/ -v          # 2.0
python -m pytest tests/ -v                        # 1.x, if still compatible
```

- [x] **Step 5: Exercise the console script end to end**

Unit tests do not cover `main()` / `mcp.run()`, which is where a stdio transport change would land.

```bash
/tmp/mcp2/bin/dosbox-mcp < /dev/null; echo "exit=$?"
```

A clean exit or a clean EOF-on-stdin shutdown is a pass; a traceback is a fail.

- [x] **Step 6: Commit the port separately from the cap lift.**

---

### Task 7: Lift the version cap

**Goal:** `pyproject.toml` allows mcp 2.x, and the stale comment explaining the cap is replaced rather than left to mislead.

**Files:**
- Modify: `mcp-server/pyproject.toml`

**Acceptance Criteria:**
- [x] `dependencies` reads `mcp>=2,<3` — a floor of 2, not an unbounded `>=1.2.0`, since the ported `server.py` cannot run on 1.x
- [x] The multi-line comment above `dependencies` explaining the FastMCP removal is **deleted**, not left contradicting the new constraint
- [x] `requires-python` re-checked against mcp 2.0's own `Requires-Python`, and raised if 2.0 raised its floor
- [x] A clean `pip install ./mcp-server` in a fresh venv resolves and the console script imports
  - **Resolves and imports — but only from an assembled bundle, and that is pre-existing.** A raw-checkout `pip install ./mcp-server` resolves mcp 2.0.0 correctly, then fails to import: `dosbox_mcp._client_import` needs a vendored `host_control_client` that only `assemble_bundle.py` copies in (`scripts/assemble_bundle.py:56-58`). Documented in `_client_import.py`'s own docstring and unrelated to the mcp version. Installing from a locally assembled bundle imports and runs.
- [x] Version bumped to `0.2.0` — a dependency floor move is not a patch

**Verify:**
```bash
python -m venv /tmp/mcpclean && /tmp/mcpclean/bin/pip install ./mcp-server
/tmp/mcpclean/bin/python -c "from dosbox_mcp import server; print('import ok')"
/tmp/mcpclean/bin/pip show mcp | head -2
```

**Steps:**

- [x] **Step 1: Check 2.0's Python floor before writing anything**

```bash
/tmp/mcp2/bin/python -c "import importlib.metadata as m; print(m.metadata('mcp')['Requires-Python'])"
```

If it exceeds `>=3.10`, raise `requires-python` to match and note it in the commit.

- [x] **Step 2: Edit `pyproject.toml`** — replace the cap, delete the now-false comment, bump the version.

- [x] **Step 3: Clean-room install**

```bash
rm -rf /tmp/mcpclean
python -m venv /tmp/mcpclean
/tmp/mcpclean/bin/pip install ./mcp-server
/tmp/mcpclean/bin/pip show mcp | head -2         # expect 2.x
/tmp/mcpclean/bin/python -c "from dosbox_mcp import server; print('import ok')"
```

- [x] **Step 4: Re-run the full suite from the clean install, not the editable one**

```bash
/tmp/mcpclean/bin/pip install pytest
cd mcp-server && /tmp/mcpclean/bin/python -m pytest tests/ -v
```

- [x] **Step 5 — DONE LOCALLY, not deferred to CI.** Bundle assembled from the existing win64 build, `mcp-server` pip-installed out of it, `scripts/smoke_bundle.py` run against the real emulator: `SMOKE OK`. **Step 5: Check the bundle path still works.** `release.yml` pip-installs `mcp-server` from the assembled bundle and runs `scripts/smoke_bundle.py`. If a real `dosbox-x` binary is available locally, run that script against a locally assembled bundle before pushing. If not, say explicitly in the commit that the bundle smoke gate was left to CI.

- [x] **Step 6: Commit.**

---

## Track C — Prove the Linux dependency list

### Task 8: Derive the runtime library set mechanically in CI

**Goal:** Replace "these seven packages happened to be enough on one WSL install" with a check that fails the build when the emulator links something `QUICKSTART.md` doesn't account for.

**Files:**
- Create: `scripts/check_runtime_deps.py`
- Modify: `.github/workflows/release.yml` (`linux-bundle` job, right after the existing `ldd` step)
- Modify: `contrib/bundle/QUICKSTART.md` (only if the check finds a genuine gap)

**Context:** the `Build SDL2 release` step already runs `ldd src/dosbox-x` and already greps that output to fail on FFmpeg. That is the hook — the same output feeds this check. `QUICKSTART.md:32` lists seven packages; the binary links a longer set (`libGL`, `libX11`, `libXrandr`, `libz`, `libpng16`, `libtinfo`, `libpulse`, `libsamplerate`, `libXext`, and more) that arrive transitively on Debian/Ubuntu.

**Approach:** the script takes `ldd` output plus an explicit, committed mapping of `soname → owning package`, and fails if any linked non-glibc library is unmapped. The mapping being *committed and explicit* is the real deliverable — it is the artifact a non-Debian packager needs, and the thing that makes the next unmapped library a build failure instead of a user bug report.

**Acceptance Criteria:**
- [x] `scripts/check_runtime_deps.py` reads `ldd` output on stdin (or takes a binary path) and exits non-zero on any linked library not present in its mapping
- [x] glibc-provided sonames (`libc`, `libm`, `libpthread`, `libdl`, `librt`, `ld-linux*`, `libgcc_s`, `libstdc++`) are excluded via an explicit allowlist, commented to say they are covered by the documented glibc floor rather than by a package
- [x] The mapping records, per entry, whether the package is one of QUICKSTART's seven or arrives transitively — that distinction is the actual finding
- [x] The script is wired into `release.yml`'s `linux-bundle` job and fails the job on an unmapped library
- [x] A deliberately removed mapping entry makes the check fail (proven locally, not assumed)
- [x] Any real gap found is fixed in `QUICKSTART.md` in the same change
  - **A real gap was found.** Installing exactly the seven leaves four sonames unresolved: `libGL.so.1` → `libgl1`, `libGLX.so.0` → `libglx0`, `libGLdispatch.so.0` → `libglvnd0`, `libpng16.so.16` → `libpng16-16t64`. The emulator links libGL and libpng16 **directly**, but nothing among the seven depends on either, so their closure never reaches them — a desktop already has both, a minimal system does not. Verified against the noble archive: closure(seven) = 115 packages, misses those four; closure(seven + libgl1 + libpng16-16t64) = 130 packages, covers all 71. `QUICKSTART.md` gained `libgl1` and `libpng16-16t64`.
  - **Also noted, left as-is:** QUICKSTART installs `libpcap0.8`, which on 24.04 is a *virtual* package with exactly one provider (`libpcap0.8t64`). apt resolves it, and the virtual name is the more portable spelling, so only the script's mapping comment records it.
  - **Mapping provenance:** derived from the Ubuntu 24.04 (noble) archive indices directly (`dists/noble/Contents-amd64.gz` + main/universe `Packages`), **not** from the local WSL, which is 26.04 and would have produced different package names. 24.04 is what `ubuntu-latest` resolved to for the v0.1.1 run.

**Verify:**
```bash
ldd /path/to/dosbox-x | python3 scripts/check_runtime_deps.py; echo "exit=$?"
```

**Steps:**

- [x] **Step 1: Get real `ldd` output to work from**

Either build locally, or pull it from the last successful release run — the `Build SDL2 release` step already prints it under `--- runtime shared libraries ---`.

```bash
gh run list --workflow=release.yml --limit 5
gh run view <id> --log | sed -n '/runtime shared libraries/,/^$/p'
```

Save it to `/tmp/ldd-baseline.txt`. Note `gh` lives at `C:\Program Files\GitHub CLI\gh.exe` and is not on PATH.

- [x] **Step 2: Build the soname → package mapping**

On an Ubuntu 24.04 host or container:

```bash
for so in $(awk '{print $1}' /tmp/ldd-baseline.txt | grep '^lib'); do
  path=$(ldconfig -p | awk -v s="$so" '$1==s {print $NF; exit}')
  [ -n "$path" ] && echo "$so -> $(dpkg -S "$path" 2>/dev/null | cut -d: -f1)"
done
```

Record the result verbatim in the script as a data literal, with a comment naming the distro and date it was derived on. That provenance matters — it is what tells the next reader how far to trust it.

- [x] **Step 3: Write the script**, stdlib-only. `release.yml` runs it with the runner's system `python3` and installs nothing extra.

- [x] **Step 4: Prove it fails**

```bash
python3 scripts/check_runtime_deps.py < /tmp/ldd-baseline.txt; echo "clean exit=$?"   # expect 0
# delete one mapping entry, rerun:
python3 scripts/check_runtime_deps.py < /tmp/ldd-baseline.txt; echo "broken exit=$?"  # expect non-zero
# restore the entry
```

- [x] **Step 5: Wire it into `release.yml`** as a step immediately after the existing FFmpeg grep, reusing the same `ldd` invocation.

- [x] **Step 6: Update `QUICKSTART.md` only if the mapping found a genuine gap** — a library owned by a package outside the seven and outside their transitive closure.

---

### Task 9: Smoke-test the bundle on a non-Debian distro

**Goal:** Actually test the hypothesis known-issues.md raises — "a distro that splits its packages differently could hit a gap" — instead of continuing to speculate about it.

**Files:**
- Create: `scripts/smoke_bundle_fedora.sh` (or extend an existing smoke harness)
- Modify: `contrib/bundle/QUICKSTART.md` (the non-Debian paragraph, with whatever this run actually finds)

**Acceptance Criteria:**
- [x] A released Linux bundle is unpacked in a clean Fedora container, its equivalent packages installed, and a real DOS command executed through the emulator
- [x] The exact `dnf install` line that worked is recorded
- [x] Any library needing a package outside the direct translation of QUICKSTART's seven is named explicitly
  - **`libXrandr`** — Ubuntu's `libsdl2-2.0-0` depends on `libxrandr2`; Fedora's SDL2 lists it as neither a Requires nor a Recommends. This is the packaging-split case the issue predicted.
  - **`libpcap` — not fixable by any package.** Debian/Ubuntu ship the historical soname `libpcap.so.0.8`; Fedora ships upstream `libpcap.so.1`, and *nothing* in Fedora provides the Debian name, so the emulator does not start. Both are upstream 1.10.x (noble 1.10.4, Fedora 43 1.10.6), so the soname difference is a Debian convention rather than an ABI break and a compat symlink is sound — verified end to end.
  - **`libGL` / `libpng16`** — missing on bare Fedora too, independently confirming Task 8's finding by a different route.
  - **Environment note:** no Docker/Podman on this machine. Used `wsl --import` of Fedora's official container base rootfs instead — 66 MB, no daemon, no packages installed on Windows or in the Ubuntu distro, removable with `wsl --unregister Fedora43`.
- [x] `QUICKSTART.md`'s non-Debian paragraph goes from generic advice to a concrete, tested package list
- [x] If the run *fails*, the failure and its cause are written down — a negative result closes this issue just as well as a positive one

**Note on the glibc floor:** pick a Fedora release new enough to satisfy glibc 2.38+ (Fedora 39+). Testing on an older one just re-demonstrates the documented floor and proves nothing about package splitting.

**Verify:** the container run exits 0 and its log shows real DOS output.

**Steps:**

- [x] **Step 1: Fetch a released bundle**

```bash
gh release download dosbox-cli-v0.1.1 --pattern '*linux-x86_64.tar.gz' --dir /tmp/bundle
```

- [x] **Step 2: Run it in a clean Fedora container**

```bash
docker run --rm -v /tmp/bundle:/bundle:ro fedora:40 bash -c '
  set -x
  dnf install -y SDL2 SDL2_net alsa-lib ncurses-libs libpcap libslirp fluidsynth-libs python3-pip
  cd /tmp && tar xzf /bundle/*.tar.gz && cd dosbox-cli-*
  ldd dosbox-x/dosbox-x | grep "not found" && echo "MISSING LIBS ABOVE"
  pip install ./mcp-server
  python3 scripts/smoke_bundle.py .
'
```

The `ldd | grep "not found"` line is the real payload — it names any gap directly, whether or not the smoke script then passes.

- [x] **Step 3: Iterate on the package list** until `ldd` is clean, recording every package added beyond the direct seven-package translation.

- [x] **Step 4: Rewrite QUICKSTART's non-Debian paragraph** with the tested command and, if applicable, a note on which packages Fedora splits differently.

- [x] **Step 5: Commit**, summarizing the container log in the body.

---

## Track D — Close the loop

### Task 10: Update `docs/known-issues.md`

**Goal:** The doc reflects reality after Tracks A–C, so it stays trustworthy instead of becoming a stale list people learn to skim past.

**Files:**
- Modify: `docs/known-issues.md`

**Acceptance Criteria:**
- [x] The "handful of build scripts" section is reduced to the single remaining macOS `appbundledeps.py` item, reframed as an intentional exclusion rather than a backlog item
- [x] The "pinned below mcp 2.0" section is removed, replaced by a line in the changelog following the project's existing convention
  - **Section removed. The changelog half has nowhere to go: there is no such convention.** `./CHANGELOG` is upstream DOSBox-X's, the README has no changelog section, and v0.1.1's release notes were GitHub's auto-generated compare link. Rather than invent a file, the change stays recorded where it already is — the three Track B commits and the `mcp-server` bump to 0.2.0.
- [x] The Linux dependency section is rewritten around what Tasks 8–9 actually established, keeping any caveat that survived
- [x] The "Two things left out on purpose" section keeps **both** entries — macOS and the glibc floor are both still true and still deliberate
- [x] Tone matches the existing doc: maintainer notes, not an audit report. No "verified on [date]" scaffolding.
- [x] The cross-link to `host-control-windows-pipe-roadmap.md` is preserved

**Verify:** read the rewritten doc start to finish and check every remaining claim against the tree.

**Steps:**

- [x] **Step 1: Re-read the doc against the post-Track-A/B/C tree**, marking each claim true / false / changed.
- [x] **Step 2: Rewrite**, deleting resolved sections outright rather than annotating them as done. A known-issues doc that accumulates struck-through entries stops being readable.
- [x] **Step 3: Verify every surviving claim** with an actual command — no claim survives on memory.
- [x] **Step 4: Commit and push.**

---

## Out of Scope

- **`Makefile.am:45` `./appbundledeps.py`.** Inside the `if MACOSX` conditional; excluded per the macOS decision. If it is ever wanted, it is `s|\./appbundledeps\.py|scripts/appbundledeps.py|` on that one line — but nobody can test it here, so leaving it broken is the honest state.
- **A macOS release job.** `release.yml` stays Windows + Linux.
- **Lowering the glibc floor.** `linux-bundle` stays on `ubuntu-latest`; the Ubuntu 24.04+ / Debian 13+ requirement remains documented and deliberate.
- **The host-control protocol's open questions** — reconnect support, the ROM/VIDRAM MCB defect, the unreproduced windowed-focus stall. Those live in `docs/host-control-windows-pipe-roadmap.md` and are a separate effort.
- **The uncommitted C++ work** in `src/dos/` and `src/ints/`. Present in the working tree, unrelated to these issues, and needing its own decision.

## Suggested execution order

Tracks A, B, and C touch disjoint files and can run in parallel. Track A is roughly an hour including review. Track B's Task 5 is the substantial one and gates Tasks 6–7. Track C's Task 9 needs a container runtime and a published release to download. Track D runs last, after whichever tracks actually land — and should describe what landed, not what was planned.
