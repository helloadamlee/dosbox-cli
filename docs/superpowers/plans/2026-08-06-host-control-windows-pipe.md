# Host-Control Windows Named Pipe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add a Windows named-pipe implementation for -control-pipe without changing the host-control NDJSON protocol or Unix FIFO behavior.

**Architecture:** Extract the request queue, writer locking, and protocol loop from the POSIX FIFO implementation into a callback-based duplex-session runner. POSIX FDs and Win32 HANDLEs become thin adapters around the same runner; Windows owns one byte-mode duplex endpoint while Unix retains its two FIFOs.

**Tech Stack:** C++14, Win32 named-pipe API, MinGW, MSVC, Python 3 standard library (ctypes), Google Test, unittest.

**User decisions (already made):**
- Target both MinGW and MSVC through the shared Win32 API surface.
- Use one full-duplex Windows named pipe, retaining Unix .in/.out FIFOs.
- Keep pipe <endpoint> CLI syntax and the existing JSONL protocol unchanged.

---

## File Structure

- include/host_control.h: platform-neutral duplex-session hooks, endpoint normalization, opaque native-handle slot.
- src/misc/host_control.cpp: common runner, POSIX adapter, and Win32 server/adapter.
- tests/host_control_protocol_tests.cpp: normalization, shared session, POSIX regression, and Windows lifecycle tests.
- scripts/host_control_client.py: ctypes-backed Windows named-pipe transport.
- tests/host_control_client_tests.py: fake Win32 API tests on every platform.
- tests/host_control_live_tests.py: opt-in real Windows status recipe.
- docs/host-control.md: platform-specific pipe behavior and commands.

### Task 1: Extract Duplex Pipe Session

**Goal:** Move the request queue and output-ordering loop behind platform-neutral line-I/O callbacks while preserving POSIX FIFO behavior.

**Files:**
- Modify: include/host_control.h:12-170
- Modify: src/misc/host_control.cpp:210-430,727-896
- Test: tests/host_control_protocol_tests.cpp:1130-1260

**Acceptance Criteria:**
- [ ] run_control_pipe_session retains its existing POSIX signature and behavior.
- [ ] Ready, output, status, input-result, and exec-result lines retain their current order.
- [ ] Reader shutdown unblocks and joins without leaking POSIX descriptors.

**Verify:** ./src/dosbox-x -tests --gtest_filter='*HostControl*' -> all host-control protocol tests pass.

**Steps:**

- [ ] **Step 1: Write the failing shared-runner test.**

~~~cpp
TEST(HostControlPipeSession, DuplexRunnerEmitsReadyStatusAndResultInOrder)
{
    std::deque<std::string> input = {
            R"({"id":"1","op":"status"})",
            R"({"id":"2","op":"exec","command":"echo hi"})"};
    std::vector<std::string> output = {};
    const auto session = host_control::run_control_duplex_session(
            {host_control::Transport::Pipe, "test"},
            [&input](std::string &line) {
                if (input.empty()) return false;
                line = input.front(); input.pop_front(); return true;
            },
            [&output](const std::string &line) { output.push_back(line); return true; },
            []() { return false; }, []() {},
            [](const host_control::Request &, host_control::CommandResult &) { return true; });
    EXPECT_TRUE(session.started);
    EXPECT_THAT(output, ::testing::ElementsAre(
            ::testing::HasSubstr("\"event\":\"ready\""),
            ::testing::HasSubstr("\"event\":\"status\""),
            ::testing::HasSubstr("\"event\":\"result\"")));
}
~~~

- [ ] **Step 2: Verify red state.**

Run: ./src/dosbox-x -tests --gtest_filter='HostControlPipeSession.DuplexRunnerEmitsReadyStatusAndResultInOrder'

Expected: compile failure because run_control_duplex_session is undeclared.

- [ ] **Step 3: Add the callback API and adapt the POSIX session.**

~~~cpp
using PeerDisconnectedFn = std::function<bool()>;
using StopReaderFn = std::function<void()>;

SessionResult run_control_duplex_session(const Options &options,
                                         const ReadLineFn &read_line,
                                         const WriteLineFn &write_line,
                                         const PeerDisconnectedFn &peer_disconnected,
                                         const StopReaderFn &stop_reader,
                                         const ExecRequestFn &exec_request);
~~~

Move PipeSessionState queue handling, writer serialization, input request handling, and the request loop into this runner. Make run_control_pipe_session pass its existing read_pipe_line, write_pipe_session_line, is_pipe_session_disconnected, and stop_pipe_session_reader operations as callbacks.

- [ ] **Step 4: Verify green state.**

Run: ./src/dosbox-x -tests --gtest_filter='*HostControl*'

Expected: PASS, including the new in-memory session test and existing FIFO lifecycle/session tests.

- [ ] **Step 5: Commit.**

~~~bash
git add include/host_control.h src/misc/host_control.cpp tests/host_control_protocol_tests.cpp
git commit -m "refactor: share host control duplex pipe session"
~~~

### Task 2: Implement Win32 Named-Pipe Server

**Goal:** Serve one Windows named-pipe client through the shared session runner with clear lifecycle diagnostics.

**Files:**
- Modify: include/host_control.h:67-170
- Modify: src/misc/host_control.cpp:1-20,899-1020
- Test: tests/host_control_protocol_tests.cpp:1130-1260

**Acceptance Criteria:**
- [ ] A short endpoint normalizes to \\.\pipe\<name>; an existing full local path remains unchanged.
- [ ] open_pipe_server creates one PIPE_ACCESS_DUPLEX, byte-mode endpoint on Windows.
- [ ] run_pipe_shell accepts one client, runs the common session, then disconnects and closes the handle.
- [ ] Creation, connection, read, and write failures include endpoint and Win32 error text.

**Verify:** src\dosbox-x.exe -tests --gtest_filter=*HostControl* -> all host-control tests pass on Windows.

**Steps:**

- [ ] **Step 1: Write failing normalization and lifecycle tests.**

~~~cpp
TEST(HostControlPipeEndpoint, NormalizesShortWindowsName)
{
    EXPECT_EQ(host_control::normalize_windows_pipe_endpoint("dosbox-control"),
              "\\\\.\\pipe\\dosbox-control");
}

#if defined(WIN32)
TEST(HostControlPipeServer, CreatesDuplexNamedPipeAndCleansUp)
{
    host_control::PipeServer server = {};
    std::string error = {};
    ASSERT_TRUE(host_control::open_pipe_server("dosbox-test-pipe", server, error)) << error;
    EXPECT_NE(server.native_handle, 0u);
    host_control::close_pipe_server(server);
    EXPECT_EQ(server.native_handle, 0u);
}
#endif
~~~

- [ ] **Step 2: Verify red state.**

Run: src\dosbox-x.exe -tests --gtest_filter=*HostControlPipeEndpoint*:*HostControlPipeServer*

Expected: compile failure because the normalizer and native_handle do not exist.

- [ ] **Step 3: Implement endpoint and Win32 lifecycle.**

Add std::uintptr_t native_handle = 0; to PipeServer and a UTF-8-to-UTF-16 conversion helper. Under WIN32, normalize short names and create the server with:

~~~cpp
CreateNamedPipeW(endpoint.c_str(), PIPE_ACCESS_DUPLEX,
                 PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                 1, 4096, 4096, 0, nullptr);
~~~

Treat ConnectNamedPipe success and ERROR_PIPE_CONNECTED as connected. Pass complete-line ReadFile and complete-buffer WriteFile callbacks to run_control_duplex_session; map ERROR_BROKEN_PIPE and ERROR_NO_DATA to peer disconnect. Teardown calls DisconnectNamedPipe when connected and always calls CloseHandle. Preserve the POSIX branch behavior.

- [ ] **Step 4: Verify green state.**

Run: src\dosbox-x.exe -tests --gtest_filter=*HostControl*

Expected: PASS; lifecycle tests pass and Unix FIFO tests conditionally skip on Windows.

- [ ] **Step 5: Commit.**

~~~bash
git add include/host_control.h src/misc/host_control.cpp tests/host_control_protocol_tests.cpp
git commit -m "feat: add Windows host control named pipe server"
~~~

### Task 3: Add Windows Python Pipe Client

**Goal:** Make pipe <endpoint> connect to Windows named pipes using only the Python standard library.

**Files:**
- Modify: scripts/host_control_client.py:1-320,672-750
- Test: tests/host_control_client_tests.py:1-220,578-710

**Acceptance Criteria:**
- [ ] Windows selects a ctypes named-pipe transport while non-Windows retains .in/.out FIFO behavior.
- [ ] ERROR_PIPE_BUSY retries only to the configured timeout; ERROR_FILE_NOT_FOUND fails promptly with the normalized endpoint.
- [ ] All existing actions preserve raw output/event sequencing and transcript behavior.

**Verify:** python -m unittest tests.host_control_client_tests -v -> all client tests pass.

**Steps:**

- [ ] **Step 1: Add failing fake-Win32 API tests.**

~~~python
def test_windows_pipe_short_name_normalizes_and_retries_busy(self):
    module = load_client_module()
    api = FakeWindowsPipeApi([module.ERROR_PIPE_BUSY, module.VALID_HANDLE])
    with mock.patch.object(module, "WindowsPipeApi", return_value=api), \
         mock.patch.object(module.os, "name", "nt"):
        transport = module.PipeTransport("dosbox-control", timeout=0.05)
        transport.connect()
    self.assertEqual(api.opened_paths, [r"\\.\pipe\dosbox-control"] * 2)

def test_windows_pipe_missing_endpoint_reports_normalized_name(self):
    module = load_client_module()
    api = FakeWindowsPipeApi([module.INVALID_HANDLE_VALUE],
                             last_error=module.ERROR_FILE_NOT_FOUND)
    with mock.patch.object(module, "WindowsPipeApi", return_value=api), \
         mock.patch.object(module.os, "name", "nt"):
        with self.assertRaisesRegex(RuntimeError, r"\\\\\.\\pipe\\missing"):
            module.PipeTransport("missing", timeout=0.01).connect()
~~~

- [ ] **Step 2: Verify red state.**

Run: python -m unittest tests.host_control_client_tests.HostControlClientTest.test_windows_pipe_short_name_normalizes_and_retries_busy tests.host_control_client_tests.HostControlClientTest.test_windows_pipe_missing_endpoint_reports_normalized_name -v

Expected: FAIL because WindowsPipeApi, timeout-aware transport construction, and Windows normalization are absent.

- [ ] **Step 3: Implement the ctypes boundary.**

Keep PipeTransport as the public class. On Windows, create WindowsPipeApi that loads kernel32, calls CreateFileW with GENERIC_READ | GENERIC_WRITE and OPEN_EXISTING, and exposes full-buffer ReadFile, WriteFile, and CloseHandle methods. Retry only ERROR_PIPE_BUSY using a monotonic deadline and short sleep. Translate ERROR_BROKEN_PIPE to EOF. Pass args.timeout from make_transport. Leave POSIX open order, select loop, and FIFO diagnostics unchanged.

- [ ] **Step 4: Verify green state.**

Run: python -m unittest tests.host_control_client_tests -v

Expected: PASS; fake Windows tests pass everywhere and FIFO tests retain their current pass/skip behavior.

- [ ] **Step 5: Commit.**

~~~bash
git add scripts/host_control_client.py tests/host_control_client_tests.py
git commit -m "feat: add Windows host control pipe client"
~~~

### Task 4: Add Live Coverage and Documentation

**Goal:** Validate a real Windows status recipe and document platform-specific host-control pipe usage.

**Files:**
- Modify: tests/host_control_live_tests.py:640-710
- Modify: docs/host-control.md:180-320

**Acceptance Criteria:**
- [ ] The opt-in Windows live test starts DOSBox-X with -control-pipe, runs the status recipe, and sees ready/status transcript events.
- [ ] Documentation distinguishes Unix FIFO pairs from one duplex Windows named pipe.
- [ ] Documentation includes short and full Windows endpoint commands and the one-client limitation.

**Verify:** set DOSBOX_X_LIVE_TESTS=1 && set DOSBOX_X_BINARY=%CD%\src\dosbox-x.exe && python -m unittest tests.host_control_live_tests.HostControlLiveTest.test_pipe_status_recipe_runs -v -> PASS on Windows.

**Steps:**

- [ ] **Step 1: Replace the Windows skip with a failing branch.**

~~~python
if os.name == "nt":
    pipe_name = f"dosbox-x-live-{os.getpid()}"
    result = self.run_pipe_recipe(pipe_name, recipe_path)
    self.assertEqual(result.proc.returncode, 0, result.diagnostics())
    self.assertEqual([event["event"] for event in result.events[:2]],
                     ["ready", "status"])
    return
~~~

- [ ] **Step 2: Verify red state.**

Run: set DOSBOX_X_LIVE_TESTS=1 && set DOSBOX_X_BINARY=%CD%\src\dosbox-x.exe && python -m unittest tests.host_control_live_tests.HostControlLiveTest.test_pipe_status_recipe_runs -v

Expected: FAIL before implementation because the current Windows branch skips or the transport cannot connect.

- [ ] **Step 3: Implement Windows live helper and docs.**

Add a Windows run_pipe_recipe path that starts DOSBox-X with the short pipe name, lets the client retry until the server accepts, writes the transcript to the existing artifact directory, and reuses process cleanup/log diagnostics. Keep the Unix FIFO helper unchanged. In docs/host-control.md, replace the unsupported-platform note with:

~~~bat
src\dosbox-x.exe -control-pipe dosbox-x-control -headless -noconfig -noautoexec
python scripts\host_control_client.py pipe dosbox-x-control status
python scripts\host_control_client.py pipe \\.\pipe\dosbox-x-control workflow examples\host-control\status.json
~~~

State that the endpoint is local-only and serves one client per DOSBox-X process.

- [ ] **Step 4: Run final validation and commit.**

Run: python -m unittest tests.host_control_client_tests tests.host_control_live_tests -v

Expected: PASS or documented opt-in skips when DOSBOX_X_LIVE_TESTS is unset.

Run: src\dosbox-x.exe -tests --gtest_filter=*HostControl*

Expected: PASS on Windows.

~~~bash
git add tests/host_control_live_tests.py docs/host-control.md
git commit -m "docs: document Windows host control pipes"
~~~

## Final Verification

- [ ] Unix-like host: python3 -m unittest tests.host_control_client_tests tests.host_control_live_tests -v and ./src/dosbox-x -tests --gtest_filter='*HostControl*' pass.
- [ ] Windows MinGW and MSVC builds: python -m unittest tests.host_control_client_tests tests.host_control_live_tests -v and src\dosbox-x.exe -tests --gtest_filter=*HostControl* pass.
- [ ] Windows live smoke: enable DOSBOX_X_LIVE_TESTS=1, set DOSBOX_X_BINARY, and run HostControlLiveTest.test_pipe_status_recipe_runs.

