# Unix Socket Host Control Milestone 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first Unix domain socket host-control transport so DOSBox-X can serve the existing NDJSON `exec` protocol over `-control-socket <path>` with single-client local access, buffered raw-byte output events, and socket cleanup on shutdown.

**Architecture:** Reuse the current host-control protocol helpers and buffered DOS output capture by moving the stdio request loop behind a shared session runner with injectable line-read and line-write callbacks. Add a minimal Unix-only socket server path in `src/misc/host_control.cpp` that accepts one client, runs that shared session over the accepted file descriptor, and unlinks the socket path on teardown or setup failure.

**Tech Stack:** C++, POSIX Unix domain sockets, existing DOSBox-X shell/control runtime, Google Test via `./src/dosbox-x -tests`

---

### Task 1: Add failing protocol/session tests for shared transport behavior

**Files:**
- Modify: `tests/host_control_protocol_tests.cpp`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Write the failing tests**

```cpp
TEST(HostControlProtocolTest, SessionRunnerEmitsReadyInvalidRequestAndDisconnectsCleanly)
{
	std::vector<std::string> written = {};
	std::vector<std::string> inputs = {"{\"id\":\"7\",\"op\":\"bogus\"}"};
	std::size_t next_input = 0;

	const host_control::Options options = {
	        host_control::Transport::Socket,
	        "/tmp/dosboxx.sock",
	};

	const auto read_line = [&](std::string &line) {
		if (next_input >= inputs.size()) {
			line.clear();
			return false;
		}
		line = inputs[next_input++];
		return true;
	};
	const auto write_line = [&](const std::string &line) {
		written.push_back(line);
		return true;
	};
	const auto exec_command = [&](const host_control::Request &, bool &shell_exit) {
		shell_exit = false;
		return false;
	};

	const auto session = host_control::run_control_session(
	        options, read_line, write_line, exec_command);

	EXPECT_TRUE(session.started);
	EXPECT_FALSE(session.had_io_error);
	ASSERT_EQ(written.size(), 2u);
	EXPECT_EQ(written[0], "{\"event\":\"ready\",\"transport\":\"socket\",\"endpoint\":\"/tmp/dosboxx.sock\"}\n");
	EXPECT_EQ(written[1], "{\"event\":\"error\",\"id\":\"7\",\"message\":\"unsupported op\"}\n");
}

TEST(HostControlProtocolTest, SessionRunnerExecutesCommandAndFlushesBufferedOutputBeforeResult)
{
	std::vector<std::string> written = {};
	std::vector<std::string> inputs = {
	        "{\"id\":\"42\",\"op\":\"exec\",\"command\":\"echo hi\"}",
	};
	std::size_t next_input = 0;

	const auto read_line = [&](std::string &line) {
		if (next_input >= inputs.size()) {
			line.clear();
			return false;
		}
		line = inputs[next_input++];
		return true;
	};
	const auto write_line = [&](const std::string &line) {
		written.push_back(line);
		return true;
	};
	const auto exec_command = [&](const host_control::Request &request, bool &shell_exit) {
		const uint8_t bytes[] = {'h', 'i', '\r', '\n'};
		shell_exit = false;
		host_control::append_session_output(bytes, sizeof(bytes));
		EXPECT_EQ(request.id, "42");
		EXPECT_EQ(request.command, "echo hi");
		return true;
	};

	const auto session = host_control::run_control_session(
	        host_control::Options{host_control::Transport::Socket, "/tmp/d.sock"},
	        read_line,
	        write_line,
	        exec_command);

	ASSERT_EQ(written.size(), 3u);
	EXPECT_EQ(written[1], "{\"event\":\"output\",\"id\":\"42\",\"encoding\":\"base64\",\"data\":\"aGkNCg==\"}\n");
	EXPECT_EQ(written[2], "{\"event\":\"result\",\"id\":\"42\",\"ok\":true,\"shell_exit\":false}\n");
	EXPECT_TRUE(session.started);
}
```

- [ ] **Step 2: Run the focused test command to verify the new tests fail**

Run:

```bash
cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x
rtk make -j4
rtk ./src/dosbox-x -tests --gtest_filter=HostControlProtocolTest.SessionRunner*
```

Expected:
- Build succeeds.
- Test binary runs.
- New tests fail because `run_control_session`, `append_session_output`, or equivalent shared-session hooks do not exist yet.

- [ ] **Step 3: Implement the minimal shared-session surface**

```cpp
struct SessionResult {
	bool started = false;
	bool had_io_error = false;
};

using ReadLineFn = std::function<bool(std::string &)>;
using WriteLineFn = std::function<bool(const std::string &)>;
using ExecRequestFn = std::function<bool(const Request &, bool &)>;

SessionResult run_control_session(const Options &options,
                                  const ReadLineFn &read_line,
                                  const WriteLineFn &write_line,
                                  const ExecRequestFn &exec_request);
void append_session_output(const uint8_t *data, std::size_t size);
```

Implementation notes:
- Move the current stdio loop logic in `src/misc/host_control.cpp` into `run_control_session`.
- Keep one active session state in that file: current writer callback, active request id, buffered bytes, and whether a session is active.
- Keep `capture_dos_write(...)` delegating into the same session output path so stdio and socket use identical buffering behavior.

- [ ] **Step 4: Run the focused session-runner tests until they pass**

Run:

```bash
cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x
rtk make -j4
rtk ./src/dosbox-x -tests --gtest_filter=HostControlProtocolTest.SessionRunner*
```

Expected:
- Both new `SessionRunner*` tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x
git add tests/host_control_protocol_tests.cpp include/host_control.h src/misc/host_control.cpp
git commit -m "test: add shared host-control session coverage"
```

### Task 2: Add failing Unix socket runtime tests for single-client setup and cleanup

**Files:**
- Modify: `tests/host_control_protocol_tests.cpp`
- Modify: `include/host_control.h`
- Modify: `src/misc/host_control.cpp`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Write the failing socket tests**

```cpp
#if defined(HAVE_SYS_UN_H)
TEST(HostControlProtocolTest, SocketServerRejectsEmptyEndpoint)
{
	host_control::SocketServer server = {};
	std::string error = {};

	EXPECT_FALSE(host_control::open_socket_server("", server, error));
	EXPECT_NE(error.find("empty"), std::string::npos);
}

TEST(HostControlProtocolTest, SocketServerRemovesStalePathAndCleansUpOnClose)
{
	const auto socket_path = MakeHostControlTempPath("dosboxx-control.sock");
	CreateRegularFile(socket_path);

	host_control::SocketServer server = {};
	std::string error = {};
	ASSERT_TRUE(host_control::open_socket_server(socket_path, server, error)) << error;
	EXPECT_TRUE(PathExists(socket_path));

	host_control::close_socket_server(server);
	EXPECT_FALSE(PathExists(socket_path));
}
#endif
```

- [ ] **Step 2: Run the focused socket test command to verify failure**

Run:

```bash
cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x
rtk make -j4
rtk ./src/dosbox-x -tests --gtest_filter=HostControlProtocolTest.SocketServer*
```

Expected:
- Tests fail because `SocketServer`, `open_socket_server`, or `close_socket_server` are not implemented yet.

- [ ] **Step 3: Implement the minimal Unix socket server helpers and socket session entry point**

```cpp
struct SocketServer {
	int listen_fd = -1;
	std::string path = {};
	bool created_path = false;
};

bool open_socket_server(const std::string &path, SocketServer &server, std::string &error);
void close_socket_server(SocketServer &server);
bool run_socket_shell();
```

Implementation notes:
- Keep the implementation Unix-only with compile guards around `<sys/socket.h>`, `<sys/un.h>`, `bind`, `listen`, `accept`, `close`, and `unlink`.
- Fail clearly for unsupported platforms with a stderr message and a `false` return from `run_socket_shell()`.
- Validate empty path and `sockaddr_un.sun_path` length before bind.
- Remove a stale socket path before bind and fail if unlink fails.
- Accept one client, wrap the client fd in simple line-based `read_line` and `write_line` lambdas, then call `run_control_session`.
- Flush buffered output and close both accepted and listening fds on every exit path.

- [ ] **Step 4: Run the focused socket tests until they pass**

Run:

```bash
cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x
rtk make -j4
rtk ./src/dosbox-x -tests --gtest_filter=HostControlProtocolTest.SocketServer*
```

Expected:
- `SocketServer*` tests pass on Unix-like hosts.

- [ ] **Step 5: Commit**

```bash
cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x
git add tests/host_control_protocol_tests.cpp include/host_control.h src/misc/host_control.cpp
git commit -m "feat: add unix socket host-control server"
```

### Task 3: Wire shell dispatch, add end-to-end protocol coverage, and verify the milestone

**Files:**
- Modify: `src/shell/shell.cpp`
- Modify: `tests/host_control_protocol_tests.cpp`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Write the failing dispatch/runtime test**

```cpp
TEST_F(DOSBox_HostControlArgvTest, ParsesControlSocketPath)
{
	ParseArgs("-control-socket /tmp/dosboxx.sock");
	EXPECT_EQ(control->opt_host_control.transport, host_control::Transport::Socket);
	EXPECT_EQ(control->opt_host_control.endpoint, "/tmp/dosboxx.sock");
}
```

And add one Unix-only protocol smoke-style unit around a connected socket pair or temporary listener that asserts:
- first frame is `ready`
- an `exec` request emits base64 `output`
- the matching `result` frame follows

- [ ] **Step 2: Run the focused host-control suite to verify red state**

Run:

```bash
cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x
rtk make -j4
rtk ./src/dosbox-x -tests --gtest_filter=HostControlProtocolTest.*:DOSBox_HostControlArgvTest.*
```

Expected:
- The new runtime/dispatch assertions fail until shell dispatch and live socket plumbing are complete.

- [ ] **Step 3: Implement the shell dispatch and live socket path**

```cpp
if (host_control::is_stdio_enabled(control->opt_host_control))
	host_control::run_stdio_shell();
else if (control->opt_host_control.transport == host_control::Transport::Socket)
	host_control::run_socket_shell();
else
	first_shell->Run();
```

Implementation notes:
- Keep stdio behavior unchanged except for routing through the new shared session runner.
- Do not add pipe transport behavior in this milestone.
- Return to `first_shell` cleanup paths exactly as the current `try` / `catch` block does.

- [ ] **Step 4: Run the required verification commands**

Run focused host-control tests:

```bash
cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x
rtk make -j4
rtk ./src/dosbox-x -tests --gtest_filter=HostControlProtocolTest.*:DOSBox_HostControlArgvTest.*
```

Run full test suite:

```bash
cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x
rtk ./src/dosbox-x -tests
```

Run live socket smoke test:

```bash
cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x
SOCK="$(mktemp -u /tmp/dosboxx-control.XXXXXX.sock)"
rtk bash -lc './src/dosbox-x -headless -control-socket "$0" >/tmp/dosboxx-socket-server.log 2>&1 &' "$SOCK"
rtk python3 - <<'PY' "$SOCK"
import json, socket, sys
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(sys.argv[1])
f = sock.makefile("rwb", buffering=0)
print(json.loads(f.readline()))
f.write(b'{"id":"1","op":"exec","command":"echo hi"}\n')
print(json.loads(f.readline()))
print(json.loads(f.readline()))
f.write(b'{"id":"2","op":"exec","command":"exit"}\n')
print(json.loads(f.readline()))
print(json.loads(f.readline()))
sock.close()
PY
```

Expected:
- Focused host-control tests pass.
- Full `-tests` suite passes.
- Smoke test shows `ready`, then `output` and `result` for `echo hi`, then `result` for `exit`.

- [ ] **Step 5: Commit**

```bash
cd /home/fld/Projects/dosbox-cli/upstream-dosbox-x
git add src/shell/shell.cpp tests/host_control_protocol_tests.cpp include/host_control.h src/misc/host_control.cpp
git commit -m "feat: add unix socket host-control milestone 1"
```
