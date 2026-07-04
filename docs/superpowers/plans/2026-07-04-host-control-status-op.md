# Host Control Status Op Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only host-control `status` operation that returns current DOS session state over both stdio and socket transports without executing a shell command.

**Architecture:** Extend the shared host-control request loop with a dedicated `status` branch instead of overloading `exec`. Keep transport handling unchanged, parse `status` as a first-class op, snapshot current DOS state from host-control, and emit a dedicated `status` event with stable low-cost fields.

**Tech Stack:** C++14, Google Test, DOSBox-X host-control runtime, DOS state helpers in `dos_inc.h`

---

### Task 1: Add the `status` protocol contract

**Files:**
- Modify: `include/host_control.h`
- Modify: `src/misc/host_control_protocol.cpp`
- Modify: `tests/host_control_protocol_tests.cpp`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Write the failing test**

Add parser and serializer tests for the new `status` op and `status` event contract.

```cpp
TEST(HostControlProtocolTest, ParsesStatusRequestWithoutCommand)
{
	const auto request = host_control::parse_request_line(R"({"id":"42","op":"status"})");

	EXPECT_TRUE(request.ok);
	EXPECT_EQ(request.id, "42");
	EXPECT_EQ(request.op, "status");
	EXPECT_TRUE(request.command.empty());
	EXPECT_TRUE(request.error.empty());
}

TEST(HostControlProtocolTest, StatusLineReportsSessionState)
{
	host_control::StatusSnapshot status = {};
	status.transport = host_control::Transport::Socket;
	status.session_active = true;
	status.errorlevel = 7;
	status.drive = "Z";
	status.cwd = "Z:\\BUILD";

	EXPECT_EQ(host_control::make_status_json_line("42", status),
	          "{\"event\":\"status\",\"id\":\"42\",\"transport\":\"socket\",\"session_active\":true,\"errorlevel\":7,\"drive\":\"Z\",\"cwd\":\"Z:\\\\BUILD\"}\n");
}

TEST(HostControlProtocolTest, StatusLineEscapesPathFields)
{
	host_control::StatusSnapshot status = {};
	status.transport = host_control::Transport::Stdio;
	status.session_active = true;
	status.errorlevel = 0;
	status.drive = "C";
	status.cwd = "C:\\TMP\\\"QUOTED\"";

	EXPECT_EQ(host_control::make_status_json_line("77", status),
	          "{\"event\":\"status\",\"id\":\"77\",\"transport\":\"stdio\",\"session_active\":true,\"errorlevel\":0,\"drive\":\"C\",\"cwd\":\"C:\\\\TMP\\\\\\\"QUOTED\\\"\"}\n");
}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/debug
rtk /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -tests --gtest_filter='HostControlProtocolTest.ParsesStatusRequestWithoutCommand:HostControlProtocolTest.StatusLineReportsSessionState:HostControlProtocolTest.StatusLineEscapesPathFields'
```

Expected: build or test failure because `parse_request_line()` only accepts `exec`, `command` is still mandatory, and `StatusSnapshot` / `make_status_json_line()` do not exist.

- [ ] **Step 3: Write minimal implementation**

In `include/host_control.h`, add the new snapshot type and serializer declaration:

```cpp
struct StatusSnapshot {
	Transport transport = Transport::Disabled;
	bool session_active = false;
	uint32_t errorlevel = 0;
	std::string drive = {};
	std::string cwd = {};
};

std::string make_status_json_line(const std::string &id,
                                  const StatusSnapshot &status);
```

In `src/misc/host_control_protocol.cpp`, relax parsing so `status` is accepted without a `command`:

```cpp
request.op = op_it->second;
if (request.op == "exec") {
	const auto command_it = values.find("command");
	if (command_it == values.end()) {
		request.error = "missing command";
		return request;
	}

	request.command = command_it->second;
	request.ok = true;
	return request;
}

if (request.op == "status") {
	request.ok = true;
	return request;
}

request.error = "unsupported op";
return request;
```

Also in `src/misc/host_control_protocol.cpp`, add the status-event serializer:

```cpp
std::string make_status_json_line(const std::string &id,
                                  const StatusSnapshot &status)
{
	std::string json = "{\"event\":\"status\",\"id\":\"";
	json += json_escape(id);
	json += "\",\"transport\":\"";
	json += transport_to_string(status.transport);
	json += "\",\"session_active\":";
	json += status.session_active ? "true" : "false";
	json += ",\"errorlevel\":";
	json += std::to_string(status.errorlevel);
	json += ",\"drive\":\"";
	json += json_escape(status.drive);
	json += "\",\"cwd\":\"";
	json += json_escape(status.cwd);
	json += "\"}\n";
	return json;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/debug
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/misc
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src dosbox-x
rtk /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -tests --gtest_filter='HostControlProtocolTest.ParsesStatusRequestWithoutCommand:HostControlProtocolTest.StatusLineReportsSessionState:HostControlProtocolTest.StatusLineEscapesPathFields'
```

Expected: all three targeted tests pass.

- [ ] **Step 5: Commit**

```bash
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x add -- include/host_control.h src/misc/host_control_protocol.cpp tests/host_control_protocol_tests.cpp
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x commit -m "feat: add host control status protocol contract"
```

### Task 2: Dispatch `status` through the shared session runner

**Files:**
- Modify: `src/misc/host_control.cpp`
- Modify: `tests/host_control_protocol_tests.cpp`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Write the failing test**

Add shared-session tests proving `status` emits a `status` event, does not emit `result`, and reflects updated state between commands.

```cpp
TEST(HostControlProtocolTest, SessionRunnerEmitsStatusWithoutResult)
{
	std::vector<std::string> writes = {};
	std::vector<std::string> requests = {R"({"id":"7","op":"status"})"};
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
	const auto exec_request = [&](const host_control::Request &, host_control::CommandResult &) {
		ADD_FAILURE() << "status must not execute shell commands";
		return false;
	};

	const auto session = host_control::run_control_session(
	        host_control::Options{host_control::Transport::Socket, "/tmp/d.sock"},
	        read_line,
	        write_line,
	        exec_request);

	EXPECT_TRUE(session.started);
	EXPECT_FALSE(session.had_io_error);
	ASSERT_EQ(writes.size(), 2u);
	EXPECT_EQ(writes[0],
	          "{\"event\":\"ready\",\"transport\":\"socket\",\"endpoint\":\"/tmp/d.sock\"}\n");
	EXPECT_EQ(writes[1],
	          "{\"event\":\"status\",\"id\":\"7\",\"transport\":\"socket\",\"session_active\":true,\"errorlevel\":0,\"drive\":\"Z\",\"cwd\":\"Z:\\\\\"}\n");
}

TEST(HostControlProtocolTest, SessionRunnerStatusReflectsUpdatedStateBetweenCommands)
{
	std::vector<std::string> writes = {};
	std::vector<std::string> requests = {
	        R"({"id":"1","op":"exec","command":"cd \\build"})",
	        R"({"id":"2","op":"status"})",
	};
	std::size_t next_request = 0;
	host_control::StatusSnapshot snapshot = {};
	snapshot.transport = host_control::Transport::Stdio;
	snapshot.session_active = true;
	snapshot.errorlevel = 7;
	snapshot.drive = "Z";
	snapshot.cwd = "Z:\\BUILD";

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
		EXPECT_EQ(request.op, "exec");
		result.shell_exit = false;
		result.errorlevel = 7;
		result.drive = "Z";
		result.cwd = "Z:\\BUILD";
		result.duration_ms = 2;
		return true;
	};

	const auto session = host_control::run_control_session(
	        host_control::Options{host_control::Transport::Stdio, ""},
	        read_line,
	        write_line,
	        exec_request);

	EXPECT_TRUE(session.started);
	EXPECT_FALSE(session.had_io_error);
	ASSERT_EQ(writes.size(), 3u);
	EXPECT_NE(writes[1].find("\"event\":\"result\""), std::string::npos);
	EXPECT_EQ(writes[2],
	          "{\"event\":\"status\",\"id\":\"2\",\"transport\":\"stdio\",\"session_active\":true,\"errorlevel\":7,\"drive\":\"Z\",\"cwd\":\"Z:\\\\BUILD\"}\n");
}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/debug
rtk /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -tests --gtest_filter='HostControlProtocolTest.SessionRunnerEmitsStatusWithoutResult:HostControlProtocolTest.SessionRunnerStatusReflectsUpdatedStateBetweenCommands'
```

Expected: failure because the session runner currently sends all successful requests through `exec_request()` and always emits `result`.

- [ ] **Step 3: Write minimal implementation**

In `src/misc/host_control.cpp`, extract a reusable status snapshot helper:

```cpp
StatusSnapshot snapshot_status(const Options &options)
{
	StatusSnapshot status = {};
	status.transport = options.transport;
	status.session_active = session_active;
	status.errorlevel = dos.return_code;
	status.drive.assign(1, static_cast<char>('A' + DOS_GetDefaultDrive()));
	status.cwd = get_current_dos_path();
	return status;
}
```

Then branch inside `run_control_session()` before `exec_request()`:

```cpp
if (request.op == "status") {
	if (!emit_session_line(make_status_json_line(request.id, snapshot_status(options)))) {
		result.had_io_error = true;
		break;
	}
	continue;
}
```

Leave the existing `exec` path unchanged below that branch.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/debug
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/misc
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src dosbox-x
rtk /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -tests --gtest_filter='*HostControl*'
```

Expected: all host-control tests pass, including the new `status` session tests.

- [ ] **Step 5: Commit**

```bash
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x add -- src/misc/host_control.cpp tests/host_control_protocol_tests.cpp
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x commit -m "feat: dispatch host control status requests"
```

### Task 3: End-to-end verification for stdio and socket `status`

**Files:**
- Modify: `none`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Write the failing test**

Record the runtime fields that the smoke tests must prove:

```text
Required status fields:
- "event":"status"
- "transport":
- "session_active":true
- "errorlevel":
- "drive":
- "cwd":
```

- [ ] **Step 2: Run test to verify it fails**

Run the full verification matrix on the completed implementation:

```bash
rtk make -j3 -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src dosbox-x
rtk /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -tests
rtk bash -lc 'tmpdir=$(mktemp -d) && printf "%s\n%s\n%s\n%s\n" "{\"id\":\"1\",\"op\":\"status\"}" "{\"id\":\"2\",\"op\":\"exec\",\"command\":\"cd \\\\\"}" "{\"id\":\"3\",\"op\":\"status\"}" "{\"id\":\"4\",\"op\":\"exec\",\"command\":\"exit\"}" | timeout 20s /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -control-stdio -headless -noconfig -noautoexec >"$tmpdir/out" 2>"$tmpdir/err" && sed -n "1,20p" "$tmpdir/out"'
rtk python3 - <<'PY'
import os, socket, subprocess, tempfile, time
sock = tempfile.mktemp(prefix="dosboxx-control-", suffix=".sock", dir="/tmp")
proc = subprocess.Popen(
    ["/home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x",
     "-control-socket", sock, "-headless", "-noconfig", "-noautoexec"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    for _ in range(50):
        if os.path.exists(sock):
            break
        time.sleep(0.1)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(sock)
    client.sendall(b'{"id":"1","op":"status"}\n')
    client.sendall(b'{"id":"2","op":"exec","command":"cd \\\\"}\n')
    client.sendall(b'{"id":"3","op":"status"}\n')
    client.sendall(b'{"id":"4","op":"exec","command":"exit"}\n')
    client.shutdown(socket.SHUT_WR)
    data = client.recv(8192).decode("utf-8", "replace")
    print(data)
    client.close()
finally:
    proc.wait(timeout=20)
    if os.path.exists(sock):
        os.unlink(sock)
PY
```

Expected: if anything still fails, the output will identify whether the gap is build, unit tests, stdio runtime behavior, or socket runtime behavior.

- [ ] **Step 3: Write minimal implementation**

No code changes belong in this verification task. Inspect the output for the required `status` fields and the updated post-`cd` path.

```bash
rtk bash -lc 'tmpdir=$(mktemp -d) && printf "%s\n%s\n%s\n%s\n" "{\"id\":\"1\",\"op\":\"status\"}" "{\"id\":\"2\",\"op\":\"exec\",\"command\":\"cd \\\\\"}" "{\"id\":\"3\",\"op\":\"status\"}" "{\"id\":\"4\",\"op\":\"exec\",\"command\":\"exit\"}" | timeout 20s /home/fld/Projects/dosbox-cli/upstream-dosbox-x/src/dosbox-x -control-stdio -headless -noconfig -noautoexec >"$tmpdir/out" 2>"$tmpdir/err" && rg "\"event\":\"status\"|\"transport\"|\"session_active\"|\"errorlevel\"|\"drive\"|\"cwd\"" "$tmpdir/out"'
```

- [ ] **Step 4: Run test to verify it passes**

Expected verification evidence:
- `./src/dosbox-x -tests` reports all tests passed.
- The first stdio `status` event reports `cwd` before `cd \\`.
- The second stdio `status` event reports `cwd` after `cd \\`, typically `Z:\\`.
- The socket run shows the same `status` behavior and fields.
- `result` events still only appear for the `exec` requests.

- [ ] **Step 5: Commit**

```bash
rtk git -C /home/fld/Projects/dosbox-cli/upstream-dosbox-x status --short
```

Expected: no new repo changes beyond the implementation commits from Tasks 1 and 2.
