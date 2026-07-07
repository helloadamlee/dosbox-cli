# Host Control Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add socket-based host-control keyboard input so scripts can send text and simple named keys to a running DOSBox-X session.

**Architecture:** Extend the host-control protocol with `input_text`, `key`, and `input_result`. Implement a small thread-safe input queue in `src/misc/host_control.cpp`; socket reader code accepts input requests and queues BIOS keyboard codes while the main DOSBox-X thread drains the queue from `Normal_Loop()` in `src/dosbox.cpp`. Keep `exec` completion semantics synchronous, keep stdio input unsupported for this milestone, and serialize socket writes so output and input responses preserve JSON-line fidelity.

**Tech Stack:** C++14, existing gtest test binary via `./src/dosbox-x -tests`, Python 3 stdlib client/tests, Unix domain sockets.

---

## File Structure

- Modify `include/host_control.h`: add request fields, input queue result type, input result JSON helper declaration, input translation/queue/drain APIs.
- Modify `src/misc/host_control_protocol.cpp`: parse `input_text` and `key`, validate request fields, generate `input_result`.
- Modify `src/misc/host_control.cpp`: implement ASCII/named-key translation, input queue, socket write locking, async socket reader handling for input requests, and queue draining.
- Modify `src/dosbox.cpp`: call `host_control::drain_queued_input()` from `Normal_Loop()` so input is injected on the emulator thread.
- Modify `scripts/host_control_client.py`: add socket-only `input-text` and `key` actions plus REPL commands.
- Modify `tests/host_control_protocol_tests.cpp`: protocol, input translation, queue/drain, and session behavior tests.
- Modify `tests/host_control_client_tests.py`: client request/completion tests for `input-text`, `key`, and REPL parsing.
- Modify `tests/host_control_live_tests.py`: opt-in live socket smoke for input.
- Modify `docs/host-control.md`: document the new socket-only input operations and limits.

---

### Task 1: Protocol Parsing And `input_result`

**Files:**
- Modify: `include/host_control.h`
- Modify: `src/misc/host_control_protocol.cpp`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Write failing protocol tests**

Add these tests near the existing request parsing tests in `tests/host_control_protocol_tests.cpp`:

```cpp
TEST(HostControlProtocolTest, ParsesInputTextRequest)
{
	const auto request = host_control::parse_request_line(R"({"id":"7","op":"input_text","text":"dir\r"})");

	EXPECT_TRUE(request.ok);
	EXPECT_EQ(request.id, "7");
	EXPECT_EQ(request.op, "input_text");
	EXPECT_EQ(request.text, "dir\r");
	EXPECT_TRUE(request.error.empty());
}

TEST(HostControlProtocolTest, RejectsInputTextWithoutText)
{
	const auto request = host_control::parse_request_line(R"({"id":"7","op":"input_text"})");

	EXPECT_FALSE(request.ok);
	EXPECT_EQ(request.id, "7");
	EXPECT_EQ(request.error, "missing text");
}

TEST(HostControlProtocolTest, ParsesKeyRequest)
{
	const auto request = host_control::parse_request_line(R"({"id":"8","op":"key","key":"enter"})");

	EXPECT_TRUE(request.ok);
	EXPECT_EQ(request.id, "8");
	EXPECT_EQ(request.op, "key");
	EXPECT_EQ(request.key, "enter");
	EXPECT_TRUE(request.error.empty());
}

TEST(HostControlProtocolTest, RejectsKeyWithoutKeyName)
{
	const auto request = host_control::parse_request_line(R"({"id":"8","op":"key"})");

	EXPECT_FALSE(request.ok);
	EXPECT_EQ(request.id, "8");
	EXPECT_EQ(request.error, "missing key");
}

TEST(HostControlProtocolTest, InputResultReportsQueuedCount)
{
	EXPECT_EQ(host_control::make_input_result_json_line("9", true, 4),
	          "{\"event\":\"input_result\",\"id\":\"9\",\"ok\":true,\"queued\":4}\n");
}
```

- [ ] **Step 2: Verify RED**

Run:

```bash
rtk touch src/debug/debug.cpp
rtk make -j3 -C src/debug
rtk make -j3 -C src dosbox-x
rtk ./src/dosbox-x -tests --gtest_filter='*HostControl*'
```

Expected: compile fails because `Request` has no `text`/`key` members and `make_input_result_json_line()` does not exist.

- [ ] **Step 3: Add request fields and helper declaration**

In `include/host_control.h`, update `Request`:

```cpp
struct Request {
	bool ok = false;
	std::string id = {};
	std::string op = {};
	std::string command = {};
	std::string text = {};
	std::string key = {};
	std::string error = {};
};
```

Add this declaration after `make_status_json_line()`:

```cpp
std::string make_input_result_json_line(const std::string &id, bool ok, std::size_t queued);
```

- [ ] **Step 4: Implement parsing and result JSON**

In `src/misc/host_control_protocol.cpp`, replace the supported-op check inside `parse_request_line()` with:

```cpp
	if (request.op != "exec" && request.op != "status" &&
	    request.op != "input_text" && request.op != "key") {
		request.error = "unsupported op";
		return request;
	}
```

After the existing `exec` command block, add:

```cpp
	if (request.op == "input_text") {
		const auto text_it = values.find("text");
		if (text_it == values.end()) {
			request.error = "missing text";
			return request;
		}

		request.text = text_it->second;
	}

	if (request.op == "key") {
		const auto key_it = values.find("key");
		if (key_it == values.end() || key_it->second.empty()) {
			request.error = "missing key";
			return request;
		}

		request.key = key_it->second;
	}
```

Add the result helper after `make_status_json_line()`:

```cpp
std::string make_input_result_json_line(const std::string &id,
                                        const bool ok,
                                        const std::size_t queued)
{
	std::string json = "{\"event\":\"input_result\",\"id\":\"";
	json += json_escape(id);
	json += "\",\"ok\":";
	json += ok ? "true" : "false";
	json += ",\"queued\":";
	json += std::to_string(queued);
	json += "}\n";
	return json;
}
```

- [ ] **Step 5: Verify GREEN**

Run:

```bash
rtk touch src/debug/debug.cpp
rtk make -j3 -C src/debug
rtk make -j3 -C src dosbox-x
rtk ./src/dosbox-x -tests --gtest_filter='*HostControl*'
```

Expected: HostControl tests pass.

- [ ] **Step 6: Commit**

```bash
rtk git add include/host_control.h src/misc/host_control_protocol.cpp tests/host_control_protocol_tests.cpp
rtk git commit -m "feat: parse host control input requests"
```

---

### Task 2: Input Translation And Queue

**Files:**
- Modify: `include/host_control.h`
- Modify: `src/misc/host_control.cpp`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Write failing input translation tests**

Add these tests in `tests/host_control_protocol_tests.cpp` after the input parsing tests:

```cpp
TEST(HostControlProtocolTest, BuildsAsciiInputCodesForText)
{
	std::vector<uint16_t> codes = {};
	std::string error = {};

	EXPECT_TRUE(host_control::build_input_codes_for_text("dir\n", codes, error));
	ASSERT_EQ(codes.size(), 4u);
	EXPECT_EQ(codes[0], static_cast<uint16_t>('d'));
	EXPECT_EQ(codes[1], static_cast<uint16_t>('i'));
	EXPECT_EQ(codes[2], static_cast<uint16_t>('r'));
	EXPECT_EQ(codes[3], 0x1c0d);
	EXPECT_TRUE(error.empty());
}

TEST(HostControlProtocolTest, RejectsNonAsciiInputText)
{
	std::vector<uint16_t> codes = {};
	std::string error = {};

	EXPECT_FALSE(host_control::build_input_codes_for_text(std::string("\xC3\xA9", 2), codes, error));
	EXPECT_TRUE(codes.empty());
	EXPECT_EQ(error, "input_text supports ASCII only");
}

TEST(HostControlProtocolTest, BuildsNamedKeyCodes)
{
	std::vector<uint16_t> codes = {};
	std::string error = {};

	EXPECT_TRUE(host_control::build_input_codes_for_key("enter", codes, error));
	ASSERT_EQ(codes.size(), 1u);
	EXPECT_EQ(codes[0], 0x1c0d);

	codes.clear();
	EXPECT_TRUE(host_control::build_input_codes_for_key("up", codes, error));
	ASSERT_EQ(codes.size(), 1u);
	EXPECT_EQ(codes[0], 0x4800);
}

TEST(HostControlProtocolTest, RejectsUnsupportedNamedKey)
{
	std::vector<uint16_t> codes = {};
	std::string error = {};

	EXPECT_FALSE(host_control::build_input_codes_for_key("f1", codes, error));
	EXPECT_TRUE(codes.empty());
	EXPECT_EQ(error, "unsupported key");
}
```

- [ ] **Step 2: Verify RED**

Run:

```bash
rtk touch src/debug/debug.cpp
rtk make -j3 -C src/debug
```

Expected: compile fails because `build_input_codes_for_text()` and `build_input_codes_for_key()` are undeclared.

- [ ] **Step 3: Add declarations**

In `include/host_control.h`, add `#include <vector>` and these declarations after the socket functions:

```cpp
bool build_input_codes_for_text(const std::string &text,
                                std::vector<uint16_t> &codes,
                                std::string &error);
bool build_input_codes_for_key(const std::string &key,
                               std::vector<uint16_t> &codes,
                               std::string &error);
```

- [ ] **Step 4: Implement translation**

In `src/misc/host_control.cpp`, add `#include <map>` and `#include <vector>`.

Add these functions before `capture_dos_write()`:

```cpp
bool build_input_codes_for_text(const std::string &text,
                                std::vector<uint16_t> &codes,
                                std::string &error)
{
	codes.clear();
	error.clear();

	for (const auto ch : text) {
		const auto byte = static_cast<unsigned char>(ch);
		if (byte == '\r' || byte == '\n') {
			codes.push_back(0x1c0d);
			continue;
		}
		if (byte < 0x20 || byte > 0x7e) {
			error = "input_text supports ASCII only";
			codes.clear();
			return false;
		}
		codes.push_back(static_cast<uint16_t>(byte));
	}

	return true;
}

bool build_input_codes_for_key(const std::string &key,
                               std::vector<uint16_t> &codes,
                               std::string &error)
{
	static const std::map<std::string, uint16_t> named_keys = {
	        {"enter", 0x1c0d},
	        {"escape", 0x011b},
	        {"tab", 0x0f09},
	        {"backspace", 0x0e08},
	        {"up", 0x4800},
	        {"down", 0x5000},
	        {"left", 0x4b00},
	        {"right", 0x4d00},
	};

	codes.clear();
	error.clear();

	const auto it = named_keys.find(key);
	if (it == named_keys.end()) {
		error = "unsupported key";
		return false;
	}

	codes.push_back(it->second);
	return true;
}
```

- [ ] **Step 5: Verify GREEN**

Run:

```bash
rtk touch src/debug/debug.cpp
rtk make -j3 -C src/debug
rtk make -j3 -C src dosbox-x
rtk ./src/dosbox-x -tests --gtest_filter='*HostControl*'
```

Expected: HostControl tests pass.

- [ ] **Step 6: Write failing queue tests**

Add this test to `tests/host_control_protocol_tests.cpp`:

```cpp
TEST(HostControlProtocolTest, InputQueueAcceptsAndDrainsCodesInOrder)
{
	host_control::clear_queued_input();

	std::vector<uint16_t> codes = {static_cast<uint16_t>('d'), static_cast<uint16_t>('i')};
	const auto queued = host_control::queue_input_codes(codes);

	EXPECT_TRUE(queued.ok);
	EXPECT_EQ(queued.queued, 2u);
	EXPECT_TRUE(queued.error.empty());

	std::vector<uint16_t> drained = {};
	EXPECT_EQ(host_control::drain_queued_input_codes_for_test(drained, 8), 2u);
	ASSERT_EQ(drained.size(), 2u);
	EXPECT_EQ(drained[0], static_cast<uint16_t>('d'));
	EXPECT_EQ(drained[1], static_cast<uint16_t>('i'));

	host_control::clear_queued_input();
}
```

- [ ] **Step 7: Verify RED**

Run:

```bash
rtk touch src/debug/debug.cpp
rtk make -j3 -C src/debug
```

Expected: compile fails because queue APIs and `InputQueueResult` are undeclared.

- [ ] **Step 8: Add queue declarations**

In `include/host_control.h`, add:

```cpp
struct InputQueueResult {
	bool ok = false;
	std::size_t queued = 0;
	std::string error = {};
};

InputQueueResult queue_input_codes(const std::vector<uint16_t> &codes);
void clear_queued_input();
std::size_t drain_queued_input();
std::size_t drain_queued_input_codes_for_test(std::vector<uint16_t> &codes, std::size_t max_codes);
```

- [ ] **Step 9: Implement queue and test drain**

In `src/misc/host_control.cpp`, add `#include <deque>` and `#include <mutex>`.

Inside the anonymous namespace, add:

```cpp
constexpr std::size_t input_queue_max_codes = 1024;
std::deque<uint16_t> pending_input_codes = {};
std::mutex pending_input_mutex = {};
```

After the translation functions, add:

```cpp
InputQueueResult queue_input_codes(const std::vector<uint16_t> &codes)
{
	InputQueueResult result = {};
	std::lock_guard<std::mutex> lock(pending_input_mutex);

	if (pending_input_codes.size() + codes.size() > input_queue_max_codes) {
		result.error = "input queue full";
		return result;
	}

	for (const auto code : codes) {
		pending_input_codes.push_back(code);
	}

	result.ok = true;
	result.queued = codes.size();
	return result;
}

void clear_queued_input()
{
	std::lock_guard<std::mutex> lock(pending_input_mutex);
	pending_input_codes.clear();
}

std::size_t drain_queued_input_codes_for_test(std::vector<uint16_t> &codes, const std::size_t max_codes)
{
	std::lock_guard<std::mutex> lock(pending_input_mutex);
	std::size_t drained = 0;
	while (drained < max_codes && !pending_input_codes.empty()) {
		codes.push_back(pending_input_codes.front());
		pending_input_codes.pop_front();
		++drained;
	}
	return drained;
}
```

Do not implement `drain_queued_input()` yet beyond a stub:

```cpp
std::size_t drain_queued_input()
{
	return 0;
}
```

- [ ] **Step 10: Verify GREEN**

Run:

```bash
rtk touch src/debug/debug.cpp
rtk make -j3 -C src/debug
rtk make -j3 -C src dosbox-x
rtk ./src/dosbox-x -tests --gtest_filter='*HostControl*'
```

Expected: HostControl tests pass.

- [ ] **Step 11: Commit**

```bash
rtk git add include/host_control.h src/misc/host_control.cpp tests/host_control_protocol_tests.cpp
rtk git commit -m "feat: add host control input queue"
```

---

### Task 3: Drain Input From The Emulator Loop

**Files:**
- Modify: `src/misc/host_control.cpp`
- Modify: `src/dosbox.cpp`

- [ ] **Step 1: Implement real drain with BIOS keyboard buffer**

In `src/misc/host_control.cpp`, add:

```cpp
#include "bios.h"
```

Replace the `drain_queued_input()` stub with:

```cpp
std::size_t drain_queued_input()
{
	std::size_t drained = 0;

	for (;;) {
		uint16_t code = 0;
		{
			std::lock_guard<std::mutex> lock(pending_input_mutex);
			if (pending_input_codes.empty()) {
				return drained;
			}
			code = pending_input_codes.front();
		}

		if (!BIOS_AddKeyToBuffer(code)) {
			return drained;
		}

		{
			std::lock_guard<std::mutex> lock(pending_input_mutex);
			if (!pending_input_codes.empty() && pending_input_codes.front() == code) {
				pending_input_codes.pop_front();
			}
		}
		++drained;
	}
}
```

- [ ] **Step 2: Add main-loop drain call**

In `src/dosbox.cpp`, add:

```cpp
#include "host_control.h"
```

Inside `Normal_Loop()`, at the top of the `while (1)` loop before `if (PIC_RunQueue())`, add:

```cpp
            (void)host_control::drain_queued_input();
```

- [ ] **Step 3: Build and run host-control tests**

Run:

```bash
rtk make -j3 -C src/misc
rtk make -j3 -C src dosbox-x
rtk ./src/dosbox-x -tests --gtest_filter='*HostControl*'
```

Expected: HostControl tests pass.

- [ ] **Step 4: Commit**

```bash
rtk git add src/misc/host_control.cpp src/dosbox.cpp
rtk git commit -m "feat: drain host control input in emulator loop"
```

---

### Task 4: Client Socket Input Commands

**Files:**
- Modify: `scripts/host_control_client.py`
- Modify: `tests/host_control_client_tests.py`

- [ ] **Step 1: Write failing client tests**

Add these tests in `tests/host_control_client_tests.py`:

```python
    def test_socket_input_text_sends_request_and_waits_for_input_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "control.sock")
            requests = []
            lines = [
                '{"event":"ready","transport":"socket"}\n',
                '{"event":"input_result","id":"1","ok":true,"queued":4}\n',
            ]
            thread = self._serve_socket_once(sock_path, lines, requests)

            proc = subprocess.run(
                [sys.executable, str(CLIENT), "socket", sock_path, "input-text", "dir\n"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            thread.join(timeout=2)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "".join(lines))
            self.assertEqual(requests, ['{"id":"1","op":"input_text","text":"dir\\n"}\n'])

    def test_socket_key_sends_request_and_waits_for_input_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = str(Path(tmpdir) / "control.sock")
            requests = []
            lines = [
                '{"event":"ready","transport":"socket"}\n',
                '{"event":"input_result","id":"1","ok":true,"queued":1}\n',
            ]
            thread = self._serve_socket_once(sock_path, lines, requests)

            proc = subprocess.run(
                [sys.executable, str(CLIENT), "socket", sock_path, "key", "enter"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            thread.join(timeout=2)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "".join(lines))
            self.assertEqual(requests, ['{"id":"1","op":"key","key":"enter"}\n'])
```

- [ ] **Step 2: Verify RED**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests.HostControlClientTest.test_socket_input_text_sends_request_and_waits_for_input_result
```

Expected: argument parser rejects `input-text`.

- [ ] **Step 3: Update request encoding and completion**

In `scripts/host_control_client.py`, change `encode_request()` to accept arbitrary payload fields:

```python
def encode_request(request_id, op, command=None, text=None, key=None):
    payload = {"id": str(request_id), "op": op}
    if command is not None:
        payload["command"] = command
    if text is not None:
        payload["text"] = text
    if key is not None:
        payload["key"] = key
    return json.dumps(payload, separators=(",", ":"))
```

Change `event_completes_request()`:

```python
    if op == "status":
        return event.get("event") == "status"
    if op in ("input_text", "key"):
        return event.get("event") == "input_result"
    return event.get("event") == "result"
```

Change `run_request()` signature and write call:

```python
def run_request(transport, request_id, op, command=None, text=None, key=None, timeout=None):
    deadline = make_deadline(timeout)
    transport.writeline(encode_request(request_id, op, command, text, key))
```

Update all existing calls to pass `timeout=` by name where needed:

```python
return run_request(transport, 1, op, command, timeout=timeout)
run_request(transport, next_request_id, op, command, timeout=timeout)
```

- [ ] **Step 4: Add parser actions**

In `parse_args()`, change socket choices:

```python
    socket_parser.add_argument("action", choices=("status", "exec", "input-text", "key", "repl"))
```

After existing exec validation, add:

```python
    if args.transport == "stdio" and args.action in ("input-text", "key"):
        parser.error("input actions are socket-only")
    if args.action in ("input-text", "key") and not args.command:
        parser.error(f"{args.action} requires a value")
```

In `main()`, map one-shot actions:

```python
        if args.action == "input-text":
            return run_one_shot(transport, "input_text", text=args.command, timeout=args.timeout)
        if args.action == "key":
            return run_one_shot(transport, "key", key=args.command, timeout=args.timeout)
```

Update `run_one_shot()`:

```python
def run_one_shot(transport, op, command=None, text=None, key=None, timeout=None):
    read_event_line(transport, make_deadline(timeout), "ready event")
    return run_request(transport, 1, op, command, text, key, timeout)
```

- [ ] **Step 5: Verify GREEN**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests
```

Expected: all client tests pass.

- [ ] **Step 6: Add REPL parser tests and implementation**

Add parser assertions:

```python
        self.assertEqual(module.parse_repl_command("input dir"), ("input_text", "dir"))
        self.assertEqual(module.parse_repl_command("key enter"), ("key", "enter"))
```

Update `parse_repl_command()`:

```python
    if text.startswith("input "):
        return ("input_text", text[6:])
    if text.startswith("key "):
        return ("key", text[4:])
```

Update `run_repl()` dispatch:

```python
        if op == "input_text":
            run_request(transport, next_request_id, op, text=command, timeout=timeout)
        elif op == "key":
            run_request(transport, next_request_id, op, key=command, timeout=timeout)
        else:
            run_request(transport, next_request_id, op, command, timeout=timeout)
```

Update REPL help text to:

```python
"commands: status | exec <command> | input <text> | key <name> | help | quit\n"
```

- [ ] **Step 7: Verify and commit**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests
```

Expected: all client tests pass.

Commit:

```bash
rtk git add scripts/host_control_client.py tests/host_control_client_tests.py
rtk git commit -m "feat: add host control client input commands"
```

---

### Task 5: Socket Input Request Handling

**Files:**
- Modify: `src/misc/host_control.cpp`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Write failing session tests for input requests**

Add these tests near the existing `SessionRunner*` tests:

```cpp
TEST(HostControlProtocolTest, SessionRunnerQueuesInputText)
{
	host_control::clear_queued_input();
	std::vector<std::string> writes = {};
	std::vector<std::string> requests = {R"({"id":"7","op":"input_text","text":"dir\n"})"};
	std::size_t next_request = 0;

	const auto read_line = [&](std::string &line) {
		if (next_request >= requests.size()) {
			return false;
		}
		line = requests[next_request++];
		return true;
	};

	const auto write_line = [&](const std::string &line) {
		writes.push_back(line);
		return true;
	};

	const auto result = host_control::run_control_session(
	        {host_control::Transport::Socket, "/tmp/test.sock"},
	        read_line,
	        write_line,
	        [](const host_control::Request &, host_control::CommandResult &) { return false; });

	EXPECT_TRUE(result.started);
	ASSERT_EQ(writes.size(), 2u);
	EXPECT_EQ(writes[1], "{\"event\":\"input_result\",\"id\":\"7\",\"ok\":true,\"queued\":4}\n");

	std::vector<uint16_t> drained = {};
	EXPECT_EQ(host_control::drain_queued_input_codes_for_test(drained, 8), 4u);
	host_control::clear_queued_input();
}

TEST(HostControlProtocolTest, SessionRunnerRejectsUnsupportedInputKey)
{
	std::vector<std::string> writes = {};
	std::vector<std::string> requests = {R"({"id":"8","op":"key","key":"f1"})"};
	std::size_t next_request = 0;

	const auto read_line = [&](std::string &line) {
		if (next_request >= requests.size()) {
			return false;
		}
		line = requests[next_request++];
		return true;
	};

	const auto write_line = [&](const std::string &line) {
		writes.push_back(line);
		return true;
	};

	(void)host_control::run_control_session(
	        {host_control::Transport::Socket, "/tmp/test.sock"},
	        read_line,
	        write_line,
	        [](const host_control::Request &, host_control::CommandResult &) { return false; });

	ASSERT_EQ(writes.size(), 2u);
	EXPECT_EQ(writes[1], "{\"event\":\"error\",\"id\":\"8\",\"message\":\"unsupported key\"}\n");
}
```

- [ ] **Step 2: Verify RED**

Run:

```bash
rtk touch src/debug/debug.cpp
rtk make -j3 -C src/debug
rtk make -j3 -C src dosbox-x
rtk ./src/dosbox-x -tests --gtest_filter='HostControlProtocolTest.SessionRunnerQueuesInputText:HostControlProtocolTest.SessionRunnerRejectsUnsupportedInputKey'
```

Expected: tests fail because `run_control_session()` treats input requests like exec requests and emits result events.

- [ ] **Step 3: Handle input requests in `run_control_session()`**

In `src/misc/host_control.cpp`, inside `run_control_session()` after the `status` block and before `CommandResult command_result = {};`, add:

```cpp
		if (request.op == "input_text" || request.op == "key") {
			std::vector<uint16_t> codes = {};
			std::string error = {};
			const bool built = request.op == "input_text"
			                         ? build_input_codes_for_text(request.text, codes, error)
			                         : build_input_codes_for_key(request.key, codes, error);
			if (!built) {
				if (!emit_session_line(make_error_json_line(request.id, error))) {
					result.had_io_error = true;
					break;
				}
				continue;
			}

			const auto queued = queue_input_codes(codes);
			if (!queued.ok) {
				if (!emit_session_line(make_error_json_line(request.id, queued.error))) {
					result.had_io_error = true;
					break;
				}
				continue;
			}

			if (!emit_session_line(make_input_result_json_line(request.id, true, queued.queued))) {
				result.had_io_error = true;
				break;
			}
			continue;
		}
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
rtk touch src/debug/debug.cpp
rtk make -j3 -C src/debug
rtk make -j3 -C src dosbox-x
rtk ./src/dosbox-x -tests --gtest_filter='HostControlProtocolTest.SessionRunnerQueuesInputText:HostControlProtocolTest.SessionRunnerRejectsUnsupportedInputKey'
rtk ./src/dosbox-x -tests --gtest_filter='*HostControl*'
```

Expected: tests pass.

- [ ] **Step 5: Commit**

```bash
rtk git add src/misc/host_control.cpp tests/host_control_protocol_tests.cpp
rtk git commit -m "feat: handle host control input requests"
```

---

### Task 6: Async Socket Reader For Input During Exec

**Files:**
- Modify: `src/misc/host_control.cpp`
- Test: `tests/host_control_protocol_tests.cpp`

- [ ] **Step 1: Add a focused async-session seam**

Add a testable helper declaration to `include/host_control.h`:

```cpp
SessionResult run_control_socket_session(const Options &options,
                                         int client_fd,
                                         const ExecRequestFn &exec_request);
```

Implementation rules:
- `run_socket_shell()` should call this helper after `accept()`.
- The helper owns the accepted client fd for the session but does not close the listening socket.
- The helper uses a reader thread for request parsing and a main-thread loop for `exec`.
- Input requests may be answered from the reader thread because they only call `build_input_codes_*()` and `queue_input_codes()`.
- All writes use one mutex-protected `write_fd_line()`.

- [ ] **Step 2: Write an async socket regression test**

Add this Unix-only test to `tests/host_control_protocol_tests.cpp`:

```cpp
#if defined(__unix__) || defined(__APPLE__)
TEST(HostControlProtocolTest, SocketSessionAcceptsInputWhileExecIsRunning)
{
	host_control::clear_queued_input();
	int fds[2] = {-1, -1};
	ASSERT_EQ(socketpair(AF_UNIX, SOCK_STREAM, 0, fds), 0);

	std::promise<void> exec_started;
	std::promise<void> allow_exec_finish;
	auto exec_started_future = exec_started.get_future();
	auto allow_exec_finish_future = allow_exec_finish.get_future();

	std::thread server([&]() {
		(void)host_control::run_control_socket_session(
		        {host_control::Transport::Socket, "/tmp/test.sock"},
		        fds[0],
		        [&](const host_control::Request &, host_control::CommandResult &result) {
			        exec_started.set_value();
			        allow_exec_finish_future.wait();
			        result.shell_exit = false;
			        result.errorlevel = 0;
			        result.drive = "Z";
			        result.cwd = "Z:\\";
			        return true;
		        });
	});

	auto read_line = [&](std::string &line) {
		line.clear();
		for (;;) {
			char byte = 0;
			const auto received = read(fds[1], &byte, 1);
			if (received <= 0) {
				return false;
			}
			if (byte == '\n') {
				return true;
			}
			line += byte;
		}
	};

	std::string line = {};
	ASSERT_TRUE(read_line(line));
	EXPECT_NE(line.find(R"("event":"ready")"), std::string::npos);

	const std::string exec_request = R"({"id":"1","op":"exec","command":"hang"})" "\n";
	ASSERT_EQ(write(fds[1], exec_request.data(), exec_request.size()),
	          static_cast<ssize_t>(exec_request.size()));
	ASSERT_EQ(exec_started_future.wait_for(std::chrono::seconds(2)), std::future_status::ready);

	const std::string input_request = R"({"id":"2","op":"input_text","text":"dir\n"})" "\n";
	ASSERT_EQ(write(fds[1], input_request.data(), input_request.size()),
	          static_cast<ssize_t>(input_request.size()));

	ASSERT_TRUE(read_line(line));
	EXPECT_NE(line.find(R"("event":"input_result")"), std::string::npos);
	EXPECT_NE(line.find(R"("id":"2")"), std::string::npos);

	allow_exec_finish.set_value();
	ASSERT_TRUE(read_line(line));
	EXPECT_NE(line.find(R"("event":"result")"), std::string::npos);
	EXPECT_NE(line.find(R"("id":"1")"), std::string::npos);

	close(fds[1]);
	server.join();
	host_control::clear_queued_input();
}
#endif
```

Add required includes near the top of the test file:

```cpp
#include <future>
#include <sys/socket.h>
```

- [ ] **Step 3: Verify RED**

Run:

```bash
rtk touch src/debug/debug.cpp
rtk make -j3 -C src/debug
```

Expected: compile fails because `run_control_socket_session()` does not exist.

- [ ] **Step 4: Implement `run_control_socket_session()`**

In `src/misc/host_control.cpp`, add includes:

```cpp
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>
```

Add a local state type inside the anonymous namespace:

```cpp
struct SocketSessionState {
	Options options = {};
	int client_fd = -1;
	std::mutex mutex = {};
	std::condition_variable cv = {};
	std::deque<Request> exec_requests = {};
	bool disconnected = false;
	bool had_io_error = false;
};
```

Add a helper:

```cpp
bool write_socket_session_line(SocketSessionState &state, const std::string &line)
{
	std::lock_guard<std::mutex> lock(state.mutex);
	if (!write_fd_line(state.client_fd, line)) {
		state.had_io_error = true;
		state.disconnected = true;
		state.cv.notify_all();
		return false;
	}
	return true;
}
```

Add input handling:

```cpp
void handle_socket_input_request(SocketSessionState &state, const Request &request)
{
	std::vector<uint16_t> codes = {};
	std::string error = {};
	const bool built = request.op == "input_text"
	                         ? build_input_codes_for_text(request.text, codes, error)
	                         : build_input_codes_for_key(request.key, codes, error);
	if (!built) {
		(void)write_socket_session_line(state, make_error_json_line(request.id, error));
		return;
	}

	const auto queued = queue_input_codes(codes);
	if (!queued.ok) {
		(void)write_socket_session_line(state, make_error_json_line(request.id, queued.error));
		return;
	}

	(void)write_socket_session_line(
	        state, make_input_result_json_line(request.id, true, queued.queued));
}
```

Add the reader-thread function:

```cpp
void socket_session_reader(SocketSessionState &state)
{
	for (;;) {
		std::string line = {};
		if (!read_fd_line(state.client_fd, line)) {
			std::lock_guard<std::mutex> lock(state.mutex);
			state.disconnected = true;
			state.cv.notify_all();
			return;
		}
		if (line.empty()) {
			continue;
		}

		const auto request = parse_request_line(line);
		if (!request.ok) {
			(void)write_socket_session_line(state, make_error_json_line(request.id, request.error));
			continue;
		}

		if (request.op == "input_text" || request.op == "key") {
			handle_socket_input_request(state, request);
			continue;
		}

		std::lock_guard<std::mutex> lock(state.mutex);
		state.exec_requests.push_back(request);
		state.cv.notify_all();
	}
}
```

Implement the session helper:

```cpp
SessionResult run_control_socket_session(const Options &options,
                                         const int client_fd,
                                         const ExecRequestFn &exec_request)
{
	SocketSessionState state = {};
	state.options = options;
	state.client_fd = client_fd;

	SessionResult session_result = {};
	if (!write_socket_session_line(state, make_ready_json_line(options))) {
		session_result.had_io_error = true;
		return session_result;
	}
	session_result.started = true;
	session_active = true;
	session_write_failed = false;

	std::thread reader(socket_session_reader, std::ref(state));

	for (;;) {
		Request request = {};
		{
			std::unique_lock<std::mutex> lock(state.mutex);
			state.cv.wait(lock, [&]() {
				return state.disconnected || !state.exec_requests.empty();
			});
			if (state.exec_requests.empty()) {
				break;
			}
			request = state.exec_requests.front();
			state.exec_requests.pop_front();
		}

		if (request.op == "status") {
			(void)write_socket_session_line(state, make_status_json_line(request.id, snapshot_status(options)));
			continue;
		}

		CommandResult command_result = {};
		active_write_line = [&](const std::string &line) {
			return write_socket_session_line(state, line);
		};
		active_request_id = request.id;
		reset_buffered_output(buffered_output, request.id);
		session_active = true;
		session_write_failed = false;

		const auto start_ms = get_monotonic_ms();
		const bool ok = exec_request(request, command_result);
		const auto end_ms = get_monotonic_ms();
		command_result.duration_ms = end_ms >= start_ms ? (end_ms - start_ms) : 0;
		(void)emit_session_line(flush_buffered_output_json_line(buffered_output));
		active_request_id.clear();
		(void)emit_session_line(make_exec_result_json_line(request.id, ok, command_result));
		active_write_line = {};
		reset_buffered_output(buffered_output, {});
		session_write_failed = false;

		if (command_result.shell_exit) {
			break;
		}
	}

	if (reader.joinable()) {
		shutdown(client_fd, SHUT_RDWR);
		reader.join();
	}
	reset_session_state();
	session_result.had_io_error = state.had_io_error;
	return session_result;
}
```

This code intentionally handles `input_text`/`key` immediately on the reader thread but routes `exec`/`status` through the main thread.

- [ ] **Step 5: Wire `run_socket_shell()` to the new helper**

Replace the existing `run_control_session()` call in `run_socket_shell()` with:

```cpp
	const auto result = run_control_socket_session(
	        control->opt_host_control,
	        client_fd,
	        [](const Request &request, CommandResult &result) {
		        const bool ok = SHELL_ExecuteHostCommand(request.command, result.shell_exit);
		        if (ok) {
			        populate_command_result(result);
		        }
		        return ok;
	        });
```

Keep the existing `close_fd(client_fd); close_socket_server(server); return result.started;`.

- [ ] **Step 6: Verify GREEN**

Run:

```bash
rtk touch src/debug/debug.cpp
rtk make -j3 -C src/debug
rtk make -j3 -C src dosbox-x
rtk ./src/dosbox-x -tests --gtest_filter='HostControlProtocolTest.SocketSessionAcceptsInputWhileExecIsRunning'
rtk ./src/dosbox-x -tests --gtest_filter='*HostControl*'
```

Expected: async socket regression and focused HostControl tests pass.

- [ ] **Step 7: Commit**

```bash
rtk git add include/host_control.h src/misc/host_control.cpp tests/host_control_protocol_tests.cpp
rtk git commit -m "feat: keep socket input responsive during exec"
```

---

### Task 7: Live Socket Input Smoke

**Files:**
- Modify: `tests/host_control_live_tests.py`

- [ ] **Step 1: Add live socket input test**

Add a socket helper and test to `tests/host_control_live_tests.py`:

```python
    def run_socket_repl(self, commands, timeout_seconds=10):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "control.sock"
            server = subprocess.Popen(
                [
                    str(self.dosbox_x),
                    "-control-socket",
                    str(sock_path),
                    "-headless",
                    "-noconfig",
                    "-noautoexec",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline and not sock_path.exists():
                    time.sleep(0.05)
                self.assertTrue(sock_path.exists(), "socket was not created")

                proc = subprocess.run(
                    [
                        sys.executable,
                        str(CLIENT),
                        "--timeout",
                        str(timeout_seconds),
                        "socket",
                        str(sock_path),
                        "repl",
                    ],
                    input=commands,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout_seconds + 5,
                    check=False,
                )
                server.wait(timeout=timeout_seconds)
            finally:
                if server.poll() is None:
                    server.terminate()
                    server.wait(timeout=2)

        events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        return proc, events

    def test_socket_input_text_runs_dir_after_prompt(self):
        proc, events = self.run_socket_repl(
            "input dir\nkey enter\nquit\n",
            timeout_seconds=10,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(any(event.get("event") == "input_result" for event in events), proc.stdout)
        self.assertTrue(any(event.get("event") == "output" for event in events), proc.stdout)
```

Add `import time` at the top.

- [ ] **Step 2: Verify live test**

Run:

```bash
rtk env DOSBOX_X_LIVE_TESTS=1 python3 -m unittest tests.host_control_live_tests
```

Expected: live tests pass.

- [ ] **Step 3: Commit**

```bash
rtk git add tests/host_control_live_tests.py
rtk git commit -m "test: add live host control socket input smoke"
```

---

### Task 8: Documentation And Final Verification

**Files:**
- Modify: `docs/host-control.md`

- [ ] **Step 1: Document socket input requests**

Add a section to `docs/host-control.md` after the existing request documentation:

````markdown
### `input_text`

Socket-only in Milestone 4.

```json
{"id":"7","op":"input_text","text":"dir\r"}
```

Queues printable ASCII text for DOS keyboard input. `\r` and `\n` are normalized to Enter. Non-ASCII text is rejected.

Completion:

```json
{"event":"input_result","id":"7","ok":true,"queued":4}
```

`queued` is the number of ASCII input characters accepted after newline normalization. Completion means the input was accepted into DOSBox-X's host-control input queue, not that the guest has processed it.

### `key`

Socket-only in Milestone 4.

```json
{"id":"8","op":"key","key":"enter"}
```

Supported keys: `enter`, `escape`, `tab`, `backspace`, `up`, `down`, `left`, `right`.
````

- [ ] **Step 2: Run full verification**

Run:

```bash
rtk python3 -m unittest tests.host_control_client_tests
rtk env DOSBOX_X_LIVE_TESTS=1 python3 -m unittest tests.host_control_live_tests
rtk ./src/dosbox-x -tests --gtest_filter='*HostControl*'
rtk ./src/dosbox-x -tests
```

Expected:
- client tests pass
- live tests pass
- focused HostControl tests pass
- full C++ tests pass

- [ ] **Step 3: Real project smoke**

Run a socket REPL against the supplied NBA Hangtime project:

```bash
rtk bash -lc '
set -euo pipefail
project=/home/fld/Projects/hangtime/Asure/nba-hangtime-rebuild-main/BACKUP/XCODE101L
sock=$(mktemp -u /tmp/dosboxx-control-XXXXXX.sock)
out=$(mktemp)
err=$(mktemp)
server_err=$(mktemp)
./src/dosbox-x -control-socket "$sock" -headless -noconfig -noautoexec >/dev/null 2>"$server_err" &
pid=$!
for _ in $(seq 1 100); do [ -S "$sock" ] && break; sleep 0.1; done
printf "exec mount c $project\nexec c:\ninput dir\nkey enter\nstatus\nquit\n" |
  scripts/host_control_client.py --timeout 30 socket "$sock" repl >"$out" 2>"$err"
wait "$pid"
python3 - "$out" <<PY
import json, sys
events = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
assert any(event.get("event") == "input_result" for event in events)
assert any(event.get("event") == "output" for event in events)
assert any(event.get("event") == "status" and event.get("drive") == "C" for event in events)
print("real project socket input smoke passed")
PY
rm -f "$sock"
'
```

Expected: prints `real project socket input smoke passed`.

- [ ] **Step 4: Commit docs**

```bash
rtk git add docs/host-control.md
rtk git commit -m "docs: document host control input requests"
```

- [ ] **Step 5: Final status check**

Run:

```bash
rtk git status --short --branch
rtk git log --oneline -8
```

Expected: clean worktree on `feat/host-control-status-op`, with the Milestone 4 commits at the top.

---

## Self-Review Checklist

Spec coverage:
- Protocol additions are covered by Task 1.
- Input translation and queueing are covered by Task 2.
- Main-thread input draining is covered by Task 3.
- Client socket commands are covered by Task 4.
- Socket responsiveness while exec is running is covered by Task 6.
- Live smoke and real project verification are covered by Tasks 7 and 8.
- Documentation is covered by Task 8.

Scope boundaries:
- Stdio input is intentionally not implemented.
- Full terminal emulation, screenshots, mouse, held-key timing, cancellation, and multi-client control are intentionally not implemented.

Risk notes:
- Direct BIOS buffer insertion is intentionally chosen for the first milestone because it is narrow and testable for command prompt/setup automation.
- If live tests show BIOS buffer insertion is insufficient for target installers, switch the Task 3 drain implementation to `KEYBOARD_AddKey()` press/release pairs before expanding protocol scope.
