# Structured Host Control Result Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend host-control `result` events to include `errorlevel`, `drive`, `cwd`, and `duration_ms` for both stdio and socket transports.

**Architecture:** Keep the transport flow unchanged and enrich only the shared request-completion path. The session runner will carry structured command metadata, while host-control snapshots DOS state after each command and serializes it through the existing NDJSON result event.

**Tech Stack:** C++14, Google Test, DOSBox-X shell/DOS helpers, existing host-control session runner

---

### Task 1: Implement the structured result contract

**Files:**
- Modify: `tests/host_control_protocol_tests.cpp`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Write the failing test**

Update the existing result tests so they lock the richer result contract and the shared session-runner ordering.

```cpp
TEST(HostControlProtocolTest, ExecResultLineReportsShellState)
{
	host_control::CommandResult result = {};
	result.shell_exit = false;
	result.errorlevel = 0;
	result.drive = "C";
	result.cwd = "C:\\XCODE101L";
	result.duration_ms = 183;

	EXPECT_EQ(host_control::make_exec_result_json_line("42", true, result),
	          "{\"event\":\"result\",\"id\":\"42\",\"ok\":true,\"shell_exit\":false,\"errorlevel\":0,\"drive\":\"C\",\"cwd\":\"C:\\\\XCODE101L\",\"duration_ms\":183}\n");

	result.shell_exit = true;
	result.errorlevel = 7;
	result.drive = "Z";
	result.cwd = "Z:\\";
	result.duration_ms = 0;

	EXPECT_EQ(host_control::make_exec_result_json_line("43", false, result),
	          "{\"event\":\"result\",\"id\":\"43\",\"ok\":false,\"shell_exit\":true,\"errorlevel\":7,\"drive\":\"Z\",\"cwd\":\"Z:\\\\\",\"duration_ms\":0}\n");
}

TEST(HostControlProtocolTest, ExecResultEscapesStructuredPathFields)
{
	host_control::CommandResult result = {};
	result.shell_exit = false;
	result.errorlevel = 3;
	result.drive = "C";
	result.cwd = "C:\\TMP\\\"QUOTED\"";
	result.duration_ms = 44;

	EXPECT_EQ(host_control::make_exec_result_json_line("77", true, result),
	          "{\"event\":\"result\",\"id\":\"77\",\"ok\":true,\"shell_exit\":false,\"errorlevel\":3,\"drive\":\"C\",\"cwd\":\"C:\\\\TMP\\\\\\\"QUOTED\\\"\",\"duration_ms\":44}\n");
}

TEST(HostControlProtocolTest, SessionRunnerFlushesBufferedOutputBeforeStructuredResult)
{
	std::vector<std::string> writes = {};
	std::vector<std::string> requests = {R"({"id":"9","op":"exec","command":"echo hi"})"};
	std::size_t next_request = 0;

	const auto read_line = [&](std::string &line) {
		if (next_request >= requests.size()) {
			line.clear();
			return false;
		}

		line = requests[next_request++];
		return true;
	};
	const auto write_line = [&](const std::string &line) {
		writes.push_back(line);
		return true;
	};
	const auto exec_request = [&](const host_control::Request &request,
	                              host_control::CommandResult &result) {
		const uint8_t bytes[] = {'h', 'i', '\r', '\n'};
		EXPECT_EQ(request.id, "9");
		EXPECT_EQ(request.command, "echo hi");
		result.shell_exit = false;
		result.errorlevel = 7;
		result.drive = "Z";
		result.cwd = "Z:\\BUILD";
		result.duration_ms = 12;
		host_control::capture_dos_write(
		        DeviceInfoFlags::Device | DeviceInfoFlags::StdOut,
		        "CON",
		        bytes,
		        sizeof(bytes));
		return true;
	};

	const auto result = host_control::run_control_session(
	        host_control::Options{host_control::Transport::Socket, "/tmp/d.sock"},
	        read_line,
	        write_line,
	        exec_request);

	EXPECT_TRUE(result.started);
	EXPECT_FALSE(result.had_io_error);
	ASSERT_EQ(writes.size(), 3u);
	EXPECT_EQ(writes[0],
	          "{\"event\":\"ready\",\"transport\":\"socket\",\"endpoint\":\"/tmp/d.sock\"}\n");
	EXPECT_EQ(writes[1],
	          "{\"event\":\"output\",\"id\":\"9\",\"encoding\":\"base64\",\"data\":\"aGkNCg==\"}\n");
	EXPECT_EQ(writes[2],
	          "{\"event\":\"result\",\"id\":\"9\",\"ok\":true,\"shell_exit\":false,\"errorlevel\":7,\"drive\":\"Z\",\"cwd\":\"Z:\\\\BUILD\",\"duration_ms\":12}\n");
}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src dosbox-x
rtk /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -tests --gtest_filter='HostControlProtocolTest.ExecResultLineReportsShellState:HostControlProtocolTest.SessionRunnerFlushesBufferedOutputBeforeStructuredResult'
```

Expected: build or test failure caused by missing `host_control::CommandResult`, the old `make_exec_result_json_line()` signature, and the old `ExecRequestFn` callback shape.

- [ ] **Step 3: Write minimal implementation**

Introduce the structured result type, update the callback signature, and serialize the new fields.

In `include/host_control.h`, replace the callback shape and result builder declaration with:

```cpp
struct CommandResult {
	bool shell_exit = false;
	uint32_t errorlevel = 0;
	std::string drive = {};
	std::string cwd = {};
	uint64_t duration_ms = 0;
};

using ExecRequestFn = std::function<bool(const Request &, CommandResult &)>;

std::string make_exec_result_json_line(const std::string &id,
                                       bool ok,
                                       const CommandResult &result);
```

In `src/misc/host_control_protocol.cpp`, replace the current serializer with:

```cpp
std::string make_exec_result_json_line(const std::string &id,
                                       const bool ok,
                                       const CommandResult &result)
{
	std::string json = "{\"event\":\"result\",\"id\":\"";
	json += json_escape(id);
	json += "\",\"ok\":";
	json += ok ? "true" : "false";
	json += ",\"shell_exit\":";
	json += result.shell_exit ? "true" : "false";
	json += ",\"errorlevel\":";
	json += std::to_string(result.errorlevel);
	json += ",\"drive\":\"";
	json += json_escape(result.drive);
	json += "\",\"cwd\":\"";
	json += json_escape(result.cwd);
	json += "\",\"duration_ms\":";
	json += std::to_string(result.duration_ms);
	json += "}\n";
	return json;
}
```

In `src/misc/host_control.cpp`, update the session runner’s request loop so it carries `CommandResult` instead of `bool shell_exit`:

```cpp
CommandResult command_result = {};
active_request_id = request.id;
reset_buffered_output(buffered_output, request.id);
const bool ok = exec_request(request, command_result);
if (session_write_failed ||
    !emit_session_line(flush_buffered_output_json_line(buffered_output))) {
	result.had_io_error = true;
	active_request_id.clear();
	break;
}
active_request_id.clear();

if (!emit_session_line(make_exec_result_json_line(request.id, ok, command_result))) {
	result.had_io_error = true;
	break;
}
if (command_result.shell_exit) {
	break;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src dosbox-x
rtk /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -tests --gtest_filter='HostControlProtocolTest.ExecResultLineReportsShellState:HostControlProtocolTest.ExecResultEscapesStructuredPathFields:HostControlProtocolTest.SessionRunnerFlushesBufferedOutputBeforeStructuredResult'
```

Expected: all three targeted tests pass.

- [ ] **Step 5: Commit**

```bash
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x add -- tests/host_control_protocol_tests.cpp
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x add -- include/host_control.h src/misc/host_control_protocol.cpp src/misc/host_control.cpp
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x commit -m "feat: add structured host control result payload"
```

### Task 2: Capture DOS state and duration for real commands

**Files:**
- Modify: `src/misc/host_control.cpp`
- Modify: `tests/host_control_protocol_tests.cpp`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Write the failing test**

Add a session-runner test that verifies real metadata values are present and that `duration_ms` is computed by the runner rather than hard-coded in the callback.

```cpp
TEST(HostControlProtocolTest, SessionRunnerEmitsStructuredResultMetadata)
{
	std::vector<std::string> writes = {};
	std::vector<std::string> requests = {R"({"id":"11","op":"exec","command":"cd \\build"})"};
	std::size_t next_request = 0;

	const auto read_line = [&](std::string &line) {
		if (next_request >= requests.size()) {
			line.clear();
			return false;
		}

		line = requests[next_request++];
		return true;
	};
	const auto write_line = [&](const std::string &line) {
		writes.push_back(line);
		return true;
	};
	const auto exec_request = [&](const host_control::Request &, host_control::CommandResult &result) {
		result.shell_exit = false;
		result.errorlevel = 1;
		result.drive = "Z";
		result.cwd = "Z:\\BUILD";
		return true;
	};

	const auto session = host_control::run_control_session(
	        host_control::Options{host_control::Transport::Stdio, ""},
	        read_line,
	        write_line,
	        exec_request);

	EXPECT_TRUE(session.started);
	EXPECT_FALSE(session.had_io_error);
	ASSERT_EQ(writes.size(), 2u);
	EXPECT_NE(writes[1].find("\"errorlevel\":1"), std::string::npos);
	EXPECT_NE(writes[1].find("\"drive\":\"Z\""), std::string::npos);
	EXPECT_NE(writes[1].find("\"cwd\":\"Z:\\\\BUILD\""), std::string::npos);
	EXPECT_NE(writes[1].find("\"duration_ms\":0"), std::string::npos);
}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src dosbox-x
rtk /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -tests --gtest_filter='HostControlProtocolTest.SessionRunnerEmitsStructuredResultMetadata'
```

Expected: assertion failure because `duration_ms` is still `0` unless the session runner measures it itself.

- [ ] **Step 3: Write minimal implementation**

In `src/misc/host_control.cpp`, add helpers to snapshot DOS state and measure duration using the existing monotonic clock.

At the top of the file, add:

```cpp
#include "dos_inc.h"
```

Near `get_monotonic_ms()`, add:

```cpp
std::string get_current_dos_path()
{
	const auto drive = static_cast<char>('A' + DOS_GetDefaultDrive());
	char dir[DOS_PATHLENGTH] = {};
	if (!DOS_GetCurrentDir(0, dir, true) || dir[0] == 0) {
		return std::string(1, drive) + ":\\";
	}
	return std::string(1, drive) + ":\\" + dir;
}

void populate_command_result(CommandResult &result)
{
	result.errorlevel = dos.return_code;
	result.drive.assign(1, static_cast<char>('A' + DOS_GetDefaultDrive()));
	result.cwd = get_current_dos_path();
}
```

Inside `run_control_session()`, wrap `exec_request()` with timing:

```cpp
CommandResult command_result = {};
const auto start_ms = get_monotonic_ms();
const bool ok = exec_request(request, command_result);
const auto end_ms = get_monotonic_ms();
command_result.duration_ms = end_ms >= start_ms ? (end_ms - start_ms) : 0;
```

Then update the stdio and socket lambdas to fill the metadata after shell execution:

```cpp
[](const Request &request, CommandResult &result) {
	const bool ok = SHELL_ExecuteHostCommand(request.command, result.shell_exit);
	if (ok) {
		populate_command_result(result);
	}
	return ok;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src dosbox-x
rtk /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -tests --gtest_filter='*HostControl*'
```

Expected: all host-control tests pass, including the new structured result metadata checks.

- [ ] **Step 5: Commit**

```bash
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x add -- src/misc/host_control.cpp tests/host_control_protocol_tests.cpp
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x commit -m "feat: report structured host control command status"
```

### Task 3: Full verification and smoke testing

**Files:**
- Modify: `none`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Write the failing test**

Record the exact result fields that runtime verification must prove.

```text
Required runtime fields in every command result:
- "errorlevel":
- "drive":
- "cwd":
- "duration_ms":
```

- [ ] **Step 2: Run test to verify it fails**

Run the full verification matrix on the completed implementation.

```bash
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src dosbox-x
rtk /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -tests
rtk bash -lc 'tmpdir=$(mktemp -d) && printf "%s\n%s\n" "{\"id\":\"1\",\"op\":\"exec\",\"command\":\"cd \\\\\"}" "{\"id\":\"2\",\"op\":\"exec\",\"command\":\"exit\"}" | timeout 20s /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -control-stdio -headless -noconfig -noautoexec >"$tmpdir/out" 2>"$tmpdir/err" && sed -n "1,20p" "$tmpdir/out"'
rtk bash -lc 'sock=$(mktemp -u /tmp/dosboxx-control-XXXXXX.sock) && out=$(mktemp) && err=$(mktemp) && /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -control-socket "$sock" -headless -noconfig -noautoexec >"$out" 2>"$err" & pid=$!; for _ in $(seq 1 50); do [ -S "$sock" ] && break; sleep 0.1; done; python3 - <<'"'"'PY'"'"' "$sock"
import socket, sys
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(sys.argv[1])
sock.sendall(b'{"id":"1","op":"exec","command":"cd \\\\"}\n')
sock.sendall(b'{"id":"2","op":"exec","command":"exit"}\n')
sock.shutdown(socket.SHUT_WR)
print(sock.recv(4096).decode("utf-8", "replace"))
sock.close()
PY
wait $pid; rm -f "$sock" "$out" "$err"'
```

Expected: if anything still fails, the output will identify whether the gap is build, unit tests, stdio runtime behavior, or socket runtime behavior.

- [ ] **Step 3: Write minimal implementation**

No new implementation changes belong in this task. Inspect the emitted runtime output for the required structured fields.

```bash
rtk bash -lc 'tmpdir=$(mktemp -d) && printf "%s\n%s\n" "{\"id\":\"1\",\"op\":\"exec\",\"command\":\"cd \\\\\"}" "{\"id\":\"2\",\"op\":\"exec\",\"command\":\"exit\"}" | timeout 20s /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -control-stdio -headless -noconfig -noautoexec >"$tmpdir/out" 2>"$tmpdir/err" && rg "\"errorlevel\"|\"drive\"|\"cwd\"|\"duration_ms\"" "$tmpdir/out"'
```

- [ ] **Step 4: Run test to verify it passes**

Expected verification evidence:
- `./src/dosbox-x -tests` reports all tests passed.
- The stdio smoke output includes a `result` event containing `"drive":"Z"`, `"cwd":"Z:\\\\"`, and `"duration_ms":`.
- The socket smoke output includes the same structured fields after the `cd \\` request.

- [ ] **Step 5: Commit**

```bash
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x status --short
```

Expected: no new code changes beyond the feature implementation already committed in prior tasks.
