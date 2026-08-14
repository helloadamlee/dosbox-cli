#!/usr/bin/env python3

import argparse
import ctypes
import json
import os
import select
import socket
import subprocess
import sys
import time
from dataclasses import dataclass


class RequestTimeout(RuntimeError):
    pass


class WorkflowError(RuntimeError):
    pass


@dataclass
class WorkflowStep:
    action: str
    value: object = None


@dataclass(frozen=True)
class ExecSpec:
    command: str
    expected_errorlevels: tuple = (0,)


WORKFLOW_ACTIONS = {
    "comment",
    "exec",
    "exec_interactive",
    "status",
    "input_text",
    "key",
    "wait_for",
}
WAIT_EVENT_ALIASES = {"ready", "output", "result", "status", "error", "input_result"}


def parse_workflow_recipe(recipe):
    if not isinstance(recipe, dict):
        raise WorkflowError("recipe: expected object")
    steps = recipe.get("steps")
    if not isinstance(steps, list):
        raise WorkflowError("recipe.steps: expected array")
    return parse_workflow_steps(steps)


def parse_expected_errorlevels(value, step_name):
    values = value if isinstance(value, list) else [value]
    if not values or any(isinstance(code, bool) or not isinstance(code, int) for code in values):
        raise WorkflowError(
            f"{step_name}.expect_errorlevel must be an integer or non-empty integer array"
        )
    return tuple(values)


def parse_exec_spec(value, step_name, interactive=False):
    if isinstance(value, str) and not interactive:
        if not value:
            raise WorkflowError(f"{step_name}: exec must be a non-empty string")
        return ExecSpec(value)
    if not isinstance(value, dict):
        raise WorkflowError(f"{step_name}: exec must be a string or object")
    command = value.get("command")
    if not isinstance(command, str) or not command:
        raise WorkflowError(f"{step_name}.command must be a non-empty string")
    expected = parse_expected_errorlevels(value.get("expect_errorlevel", 0), step_name)
    return ExecSpec(command, expected)


def parse_workflow_steps(steps, prefix="step"):
    parsed = []
    for index, step in enumerate(steps):
        step_name = f"{prefix} {index}" if prefix == "step" else f"{prefix}.{index}"
        if not isinstance(step, dict):
            raise WorkflowError(f"{step_name}: expected object")
        if not step:
            parsed.append(WorkflowStep("noop"))
            continue

        actions = [key for key in step if key in WORKFLOW_ACTIONS]
        unknown = [key for key in step if key not in WORKFLOW_ACTIONS]
        if unknown:
            raise WorkflowError(f"{step_name}: unknown action {unknown[0]}")
        if len(actions) != 1:
            raise WorkflowError(f"{step_name}: multiple actions")

        action = actions[0]
        value = step[action]
        if action == "comment":
            if not isinstance(value, str):
                raise WorkflowError(f"{step_name}: comment must be a string")
        elif action == "exec":
            value = parse_exec_spec(value, step_name)
        elif action == "exec_interactive":
            spec = parse_exec_spec(value, step_name, interactive=True)
            nested_steps = value.get("steps")
            if not isinstance(nested_steps, list):
                raise WorkflowError(f"{step_name}: exec_interactive.steps must be an array")
            value = {
                "spec": spec,
                "steps": parse_workflow_steps(nested_steps, prefix=f"{step_name}"),
            }
        elif action == "status":
            if value not in (True, None) and value != {}:
                raise WorkflowError(f"{step_name}: status must be true, null, or object")
        elif action == "input_text":
            if not isinstance(value, str):
                raise WorkflowError(f"{step_name}: input_text must be a string")
        elif action == "key":
            if not isinstance(value, str) or not value:
                raise WorkflowError(f"{step_name}: key must be a non-empty string")
        elif action == "wait_for":
            if not isinstance(value, (str, dict)):
                raise WorkflowError(f"{step_name}: wait_for must be a string or object")
        parsed.append(WorkflowStep(action, value))
    return parsed


def load_workflow_recipe(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return parse_workflow_recipe(json.load(handle))
    except OSError as exc:
        raise WorkflowError(f"failed to read workflow recipe: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"failed to parse workflow recipe: {exc}") from exc


class EventRecorder:
    def __init__(self, transcript=None, recent_limit=10):
        self.recent = []
        self.recent_limit = recent_limit
        self.transcript = transcript

    def record(self, raw_line, event):
        raw_text = raw_line.decode("utf-8", errors="replace")
        self.recent.append(raw_text)
        if len(self.recent) > self.recent_limit:
            self.recent = self.recent[-self.recent_limit:]
        if self.transcript is not None:
            self.transcript.write(
                json.dumps(
                    {"type": "event", "raw": raw_text, "event": event},
                    separators=(",", ":"),
                )
                + "\n"
            )
            self.transcript.flush()


def format_workflow_failure(index, step, exc, recorder):
    lines = [f"workflow step {index} {step.action} failed: {exc}"]
    if recorder.recent:
        lines.append("recent events:")
        lines.extend(line.rstrip("\r\n") for line in recorder.recent)
    return "\n".join(lines)


def event_matches(event, matcher):
    if isinstance(matcher, str):
        if matcher not in WAIT_EVENT_ALIASES:
            raise WorkflowError(f"unsupported wait_for event {matcher}")
        return event.get("event") == matcher
    if not isinstance(matcher, dict) or not matcher:
        raise WorkflowError("wait_for object must not be empty")
    return all(event.get(key) == value for key, value in matcher.items())


def wait_for_workflow_event(transport, matcher, timeout=None, recorder=None):
    deadline = make_deadline(timeout)
    while True:
        event = read_event_line(
            transport,
            deadline,
            "workflow event",
            recorder=recorder,
        )
        if event_matches(event, matcher):
            return event


def validate_workflow_for_transport(steps, allow_input):
    validate_workflow_steps_for_transport(steps, allow_input)


def validate_workflow_steps_for_transport(steps, allow_input, prefix="step"):
    for index, step in enumerate(steps):
        step_name = f"{prefix} {index}" if prefix == "step" else f"{prefix}.{index}"
        if not allow_input and step.action in ("input_text", "key", "exec_interactive"):
            raise WorkflowError(
                f"{step_name}: {step.action} actions require socket or pipe transport"
            )
        if step.action == "exec_interactive":
            validate_workflow_steps_for_transport(
                step.value["steps"],
                allow_input,
                prefix=f"{step_name}",
            )


def encode_request(request_id, op, command=None, text=None, key=None):
    payload = {"id": str(request_id), "op": op}
    if command is not None:
        payload["command"] = command
    if text is not None:
        payload["text"] = text
    if key is not None:
        payload["key"] = key
    return json.dumps(payload, separators=(",", ":"))


def event_completes_request(event, request_id, op):
    if str(event.get("id", "")) != str(request_id):
        return False
    if event.get("event") == "error":
        return True
    if op == "status":
        return event.get("event") == "status"
    if op in ("input_text", "key"):
        return event.get("event") == "input_result"
    return event.get("event") == "result"


def validate_completion(event, request_id, op, expected_errorlevels=None, allow_any_errorlevel=False):
    if event.get("event") == "error":
        raise WorkflowError(
            f"server error for request {request_id}: {event.get('message', '')}"
        )
    if op == "exec" and event.get("event") == "result" and not allow_any_errorlevel:
        expected = (0,) if expected_errorlevels is None else tuple(expected_errorlevels)
        actual = event.get("errorlevel")
        if actual not in expected:
            raise WorkflowError(
                f"DOS command request {request_id} returned errorlevel {actual}; "
                f"expected {list(expected)}"
            )
    return event


def parse_repl_command(text):
    line = text.rstrip("\r\n")
    stripped = line.strip()
    if not stripped:
        return None
    if stripped == "status":
        return ("status", None)
    if stripped == "quit":
        return ("quit", None)
    if stripped == "help":
        return ("help", None)
    if stripped.startswith("exec "):
        return ("exec", stripped[5:])
    if line.startswith("input "):
        return ("input_text", line[6:])
    if stripped.startswith("key "):
        return ("key", stripped[4:])
    raise ValueError("unknown command")


class BufferedLineTransport:
    def __init__(self):
        self._read_buffer = bytearray()

    def read_bytes(self):
        raise NotImplementedError

    def has_buffered_line(self):
        return b"\n" in self._read_buffer

    def read_available(self):
        chunk = self.read_bytes()
        if not chunk:
            return False
        self._read_buffer.extend(chunk)
        return True

    def pop_line(self):
        newline = self._read_buffer.find(b"\n")
        if newline < 0:
            if not self._read_buffer:
                return b""
            line = bytes(self._read_buffer)
            self._read_buffer.clear()
            return line

        end = newline + 1
        line = bytes(self._read_buffer[:end])
        del self._read_buffer[:end]
        return line


class SocketTransport(BufferedLineTransport):
    def __init__(self, path):
        super().__init__()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(path)

    def read_bytes(self):
        return self.socket.recv(4096)

    def fileno(self):
        return self.socket.fileno()

    def writeline(self, line):
        self.socket.sendall(line.encode("utf-8") + b"\n")

    def close(self):
        self.socket.close()

    def abort(self):
        self.close()


GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
ERROR_FILE_NOT_FOUND = 2
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_BUSY = 231
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
WINDOWS_PIPE_PREFIX = "\\\\.\\pipe\\"


def normalize_windows_pipe_endpoint(endpoint):
    if not endpoint:
        raise OSError("Windows pipe endpoint must not be empty")
    if endpoint.startswith(WINDOWS_PIPE_PREFIX):
        return endpoint
    return WINDOWS_PIPE_PREFIX + endpoint


def windows_pipe_error(endpoint, action, error):
    try:
        detail = ctypes.FormatError(error).strip()
    except (AttributeError, OSError):
        detail = f"Win32 error {error}"
    return OSError(f"failed to {action} pipe transport {endpoint} (Windows): {detail}")


def windows_pipe_timeout_error(endpoint, error):
    try:
        detail = ctypes.FormatError(error).strip()
    except (AttributeError, OSError):
        detail = f"Win32 error {error}"
    return OSError(f"timed out opening pipe transport {endpoint} (Windows): {detail}")


def windows_error_code(exc):
    return exc.winerror or exc.errno or 0


class WindowsPipeApi:
    def __init__(self):
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self.kernel32.CreateFileW.restype = ctypes.c_void_p
        self.kernel32.ReadFile.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self.kernel32.ReadFile.restype = ctypes.c_int
        self.kernel32.WriteFile.argtypes = self.kernel32.ReadFile.argtypes
        self.kernel32.WriteFile.restype = ctypes.c_int
        self.kernel32.PeekNamedPipe.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self.kernel32.PeekNamedPipe.restype = ctypes.c_int
        self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel32.CloseHandle.restype = ctypes.c_int

    def create_file(self, endpoint):
        return self.kernel32.CreateFileW(
            endpoint,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            0,
            None,
        )

    def get_last_error(self):
        return ctypes.get_last_error()

    def read_file(self, handle, size):
        buffer = ctypes.create_string_buffer(size)
        bytes_read = ctypes.c_uint32()
        if not self.kernel32.ReadFile(handle, buffer, size, ctypes.byref(bytes_read), None):
            error = self.get_last_error()
            if error == ERROR_BROKEN_PIPE:
                return b""
            raise OSError(error, "ReadFile failed")
        return buffer.raw[: bytes_read.value]

    def peek_file(self, handle):
        available = ctypes.c_uint32()
        if not self.kernel32.PeekNamedPipe(
            handle, None, 0, None, ctypes.byref(available), None
        ):
            error = self.get_last_error()
            if error == ERROR_BROKEN_PIPE:
                return None
            raise OSError(error, "PeekNamedPipe failed")
        return available.value

    def write_file(self, handle, data):
        offset = 0
        buffer = (ctypes.c_char * len(data)).from_buffer_copy(data)
        while offset < len(data):
            bytes_written = ctypes.c_uint32()
            if not self.kernel32.WriteFile(
                handle,
                ctypes.byref(buffer, offset),
                len(data) - offset,
                ctypes.byref(bytes_written),
                None,
            ):
                raise OSError(self.get_last_error(), "WriteFile failed")
            if bytes_written.value == 0:
                raise OSError("WriteFile wrote zero bytes")
            offset += bytes_written.value

    def close_handle(self, handle):
        self.kernel32.CloseHandle(handle)


class PipeTransport(BufferedLineTransport):
    def __init__(self, path, timeout=None):
        super().__init__()
        self.path = path
        self.timeout = timeout
        self.windows_pipe = os.name == "nt"
        if self.windows_pipe:
            self.endpoint = normalize_windows_pipe_endpoint(path)
            self.api = WindowsPipeApi()
            self.handle = INVALID_HANDLE_VALUE
            return

        self.input_path = f"{path}.in"
        self.output_path = f"{path}.out"
        self.read_fd = -1
        self.write_fd = -1
        try:
            self.read_fd = os.open(self.output_path, os.O_RDONLY | os.O_NONBLOCK)
            self.write_fd = os.open(self.input_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            self.close()
            raise OSError(f"failed to open pipe transport {path}: {exc}") from exc

    def connect(self):
        if not self.windows_pipe or self.handle != INVALID_HANDLE_VALUE:
            return

        deadline = make_deadline(self.timeout)
        while True:
            handle = self.api.create_file(self.endpoint)
            if handle != INVALID_HANDLE_VALUE:
                self.handle = handle
                return

            error = self.api.get_last_error()
            if error == ERROR_FILE_NOT_FOUND:
                raise windows_pipe_error(self.endpoint, "open", error)
            if error != ERROR_PIPE_BUSY:
                raise windows_pipe_error(self.endpoint, "open", error)

            remaining = remaining_seconds(deadline)
            if remaining is not None and remaining <= 0:
                raise windows_pipe_timeout_error(self.endpoint, error)
            time.sleep(min(0.01, remaining) if remaining is not None else 0.01)
            remaining = remaining_seconds(deadline)
            if remaining is not None and remaining <= 0:
                raise windows_pipe_timeout_error(self.endpoint, error)

    def wait_for_windows_read(self, timeout):
        deadline = make_deadline(timeout)
        while True:
            try:
                available = self.api.peek_file(self.handle)
            except OSError as exc:
                raise windows_pipe_error(
                    self.endpoint, "check", windows_error_code(exc)
                ) from exc
            if available is None or available > 0:
                return True
            remaining = remaining_seconds(deadline)
            if remaining is not None and remaining <= 0:
                return False
            time.sleep(min(0.01, remaining) if remaining is not None else 0.01)

    def read_bytes(self):
        if self.windows_pipe:
            try:
                return self.api.read_file(self.handle, 4096)
            except OSError as exc:
                raise windows_pipe_error(
                    self.endpoint, "read", windows_error_code(exc)
                ) from exc
        return os.read(self.read_fd, 4096)

    def fileno(self):
        if self.windows_pipe:
            raise OSError("Windows named pipes do not support select")
        return self.read_fd

    def writeline(self, line):
        if self.windows_pipe:
            try:
                self.api.write_file(self.handle, line.encode("utf-8") + b"\n")
            except OSError as exc:
                raise windows_pipe_error(
                    self.endpoint, "write", windows_error_code(exc)
                ) from exc
            return
        os.write(self.write_fd, line.encode("utf-8") + b"\n")

    def close(self):
        if self.windows_pipe:
            if self.handle != INVALID_HANDLE_VALUE:
                self.api.close_handle(self.handle)
                self.handle = INVALID_HANDLE_VALUE
            return
        if self.write_fd >= 0:
            os.close(self.write_fd)
            self.write_fd = -1
        if self.read_fd >= 0:
            os.close(self.read_fd)
            self.read_fd = -1

    def abort(self):
        self.close()


class StdioTransport(BufferedLineTransport):
    def __init__(self, command):
        super().__init__()
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
        )

    def read_bytes(self):
        assert self.process.stdout is not None
        return os.read(self.process.stdout.fileno(), 4096)

    def fileno(self):
        assert self.process.stdout is not None
        return self.process.stdout.fileno()

    def writeline(self, line):
        assert self.process.stdin is not None
        self.process.stdin.write(line.encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def close_stdin(self):
        try:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
        except BrokenPipeError:
            pass

    def close_stdout(self):
        if self.process.stdout is not None and not self.process.stdout.closed:
            self.process.stdout.close()

    def close(self):
        try:
            self.close_stdin()
            if self.process.poll() is None:
                try:
                    self.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.abort()
        finally:
            self.close_stdout()

    def abort(self):
        self.close_stdin()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.close_stdout()


def make_deadline(timeout):
    if timeout is None:
        return None
    return time.monotonic() + timeout


def remaining_seconds(deadline):
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def wait_for_readable(transport, deadline, description):
    remaining = remaining_seconds(deadline)
    if remaining is not None and remaining <= 0:
        raise RequestTimeout(f"timed out waiting for {description}")
    if getattr(transport, "windows_pipe", False):
        if not transport.wait_for_windows_read(remaining):
            raise RequestTimeout(f"timed out waiting for {description}")
        return
    readable, _, _ = select.select([transport.fileno()], [], [], remaining)
    if not readable:
        raise RequestTimeout(f"timed out waiting for {description}")


def read_event_line(transport, deadline=None, description="event", recorder=None):
    while not transport.has_buffered_line():
        wait_for_readable(transport, deadline, description)
        if not transport.read_available():
            break

    raw_line = transport.pop_line()
    if not raw_line:
        raise RuntimeError("unexpected EOF from host control transport")
    sys.stdout.buffer.write(raw_line)
    sys.stdout.flush()
    try:
        line = raw_line.decode("utf-8")
        event = json.loads(line)
        if recorder is not None:
            recorder.record(raw_line, event)
        return event
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("received invalid JSON event") from exc


def run_request(
    transport,
    request_id,
    op,
    command=None,
    text=None,
    key=None,
    timeout=None,
    recorder=None,
    expected_errorlevels=None,
    allow_any_errorlevel=False,
):
    deadline = make_deadline(timeout)
    transport.writeline(encode_request(request_id, op, command, text, key))
    while True:
        event = read_event_line(
            transport,
            deadline,
            f"{op} request {request_id}",
            recorder=recorder,
        )
        if event_completes_request(event, request_id, op):
            return validate_completion(
                event,
                request_id,
                op,
                expected_errorlevels=expected_errorlevels,
                allow_any_errorlevel=allow_any_errorlevel,
            )


def run_one_shot(
    transport,
    op,
    command=None,
    text=None,
    key=None,
    timeout=None,
    allow_nonzero=False,
):
    read_event_line(transport, make_deadline(timeout), "ready event")
    run_request(
        transport,
        1,
        op,
        command=command,
        text=text,
        key=key,
        timeout=timeout,
        expected_errorlevels=None,
        allow_any_errorlevel=allow_nonzero,
    )
    return 0


def run_repl(transport, timeout=None, allow_input=True):
    read_event_line(transport, make_deadline(timeout), "ready event")
    next_request_id = 1

    while True:
        sys.stderr.write("host-control> ")
        sys.stderr.flush()
        line = sys.stdin.readline()
        if not line:
            return 0

        try:
            parsed = parse_repl_command(line)
        except ValueError:
            sys.stderr.write("commands: status | exec <command> | input <text> | key <name> | help | quit\n")
            sys.stderr.flush()
            continue

        if parsed is None:
            continue

        op, command = parsed
        if op == "quit":
            return 0
        if op == "help":
            sys.stderr.write("commands: status | exec <command> | input <text> | key <name> | help | quit\n")
            sys.stderr.flush()
            continue

        if op in ("input_text", "key") and not allow_input:
            sys.stderr.write("input actions require socket or pipe transport\n")
            sys.stderr.flush()
            continue

        if op == "input_text":
            run_request(transport, next_request_id, op, text=command, timeout=timeout)
        elif op == "key":
            run_request(transport, next_request_id, op, key=command, timeout=timeout)
        else:
            try:
                run_request(transport, next_request_id, op, command=command, timeout=timeout)
            except WorkflowError as exc:
                print(f"command failed: {exc}", file=sys.stderr)
        next_request_id += 1


class WorkflowRuntime:
    def __init__(self, transport, timeout=None, allow_input=True, transcript=None):
        self.transport = transport
        self.timeout = timeout
        self.allow_input = allow_input
        self.recorder = EventRecorder(transcript=transcript)
        self.next_request_id = 1
        self.request_ops = {}
        self.completed_requests = {}
        self.pending_events = []

    def read_event(self, description):
        event = read_event_line(
            self.transport,
            make_deadline(self.timeout),
            description,
            recorder=self.recorder,
        )
        self.remember_completion(event)
        return event

    def remember_completion(self, event):
        request_id = str(event.get("id", ""))
        op = self.request_ops.get(request_id)
        if op is not None and event_completes_request(event, request_id, op):
            self.completed_requests[request_id] = event

    def send_request(self, op, command=None, text=None, key=None):
        request_id = str(self.next_request_id)
        self.next_request_id += 1
        self.request_ops[request_id] = op
        self.transport.writeline(encode_request(request_id, op, command, text, key))
        return request_id

    def wait_for_request(self, request_id, expected_errorlevels=None):
        request_id = str(request_id)
        op = self.request_ops[request_id]
        if request_id in self.completed_requests:
            return validate_completion(
                self.completed_requests[request_id],
                request_id,
                op,
                expected_errorlevels=expected_errorlevels,
            )

        remaining_pending = []
        for event in self.pending_events:
            if event_completes_request(event, request_id, op):
                self.completed_requests[request_id] = event
            else:
                remaining_pending.append(event)
        self.pending_events = remaining_pending

        if request_id in self.completed_requests:
            return validate_completion(
                self.completed_requests[request_id],
                request_id,
                op,
                expected_errorlevels=expected_errorlevels,
            )

        while True:
            event = self.read_event(f"{op} request {request_id}")
            if event_completes_request(event, request_id, op):
                return validate_completion(
                    event,
                    request_id,
                    op,
                    expected_errorlevels=expected_errorlevels,
                )
            self.pending_events.append(event)

    def run_request(self, op, command=None, text=None, key=None, expected_errorlevels=None):
        request_id = self.send_request(op, command=command, text=text, key=key)
        self.wait_for_request(request_id, expected_errorlevels=expected_errorlevels)
        return request_id

    def wait_for_event(self, matcher):
        remaining_pending = []
        matched = None
        for event in self.pending_events:
            if matched is None and event_matches(event, matcher):
                matched = event
            else:
                remaining_pending.append(event)
        self.pending_events = remaining_pending
        if matched is not None:
            return matched

        while True:
            event = self.read_event("workflow event")
            if event_matches(event, matcher):
                return event
            self.pending_events.append(event)

    def run_steps(self, steps, context_prefix="workflow step"):
        for index, step in enumerate(steps):
            step_context = f"{context_prefix} {index}"
            self.run_step(step, step_context)

    def run_step(self, step, step_context):
        if step.action in ("noop", "comment"):
            return
        try:
            if step.action == "exec":
                spec = step.value
                self.run_request(
                    "exec",
                    command=spec.command,
                    expected_errorlevels=spec.expected_errorlevels,
                )
            elif step.action == "status":
                self.run_request("status")
            elif step.action == "input_text":
                if not self.allow_input:
                    raise WorkflowError("input_text actions require socket or pipe transport")
                self.run_request("input_text", text=step.value)
            elif step.action == "key":
                if not self.allow_input:
                    raise WorkflowError("key actions require socket or pipe transport")
                self.run_request("key", key=step.value)
            elif step.action == "wait_for":
                self.wait_for_event(step.value)
            elif step.action == "exec_interactive":
                self.run_exec_interactive(step, step_context)
        except RequestTimeout as exc:
            if str(exc).startswith("workflow step "):
                raise
            raise RequestTimeout(
                self.format_failure(step_context, step.action, exc)
            ) from exc
        except WorkflowError as exc:
            if str(exc).startswith("workflow step "):
                raise
            raise WorkflowError(
                self.format_failure(step_context, step.action, exc)
            ) from exc
        except RuntimeError as exc:
            raise WorkflowError(
                self.format_failure(step_context, step.action, exc)
            ) from exc

    def run_exec_interactive(self, step, step_context):
        if not self.allow_input:
            raise WorkflowError("exec_interactive actions require socket or pipe transport")
        spec = step.value["spec"]
        request_id = self.send_request("exec", command=spec.command)
        self.run_steps(
            step.value["steps"],
            context_prefix=f"{step_context} exec_interactive nested step",
        )
        self.wait_for_request(
            request_id,
            expected_errorlevels=spec.expected_errorlevels,
        )

    def format_failure(self, step_context, action, exc):
        lines = [f"{step_context} {action} failed: {exc}"]
        if self.recorder.recent:
            lines.append("recent events:")
            lines.extend(line.rstrip("\r\n") for line in self.recorder.recent)
        return "\n".join(lines)


def run_workflow(transport, steps, timeout=None, allow_input=True, transcript=None):
    runtime = WorkflowRuntime(
        transport,
        timeout=timeout,
        allow_input=allow_input,
        transcript=transcript,
    )
    runtime.read_event("ready event")
    runtime.run_steps(steps)
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="seconds to wait for each host-control response",
    )
    parser.add_argument(
        "--transcript",
        default=None,
        help="write workflow events to a JSONL transcript",
    )
    parser.add_argument(
        "--allow-nonzero",
        action="store_true",
        help="allow any DOS errorlevel for a one-shot exec request",
    )
    subparsers = parser.add_subparsers(dest="transport", required=True)

    socket_parser = subparsers.add_parser("socket")
    socket_parser.add_argument("path")
    socket_parser.add_argument(
        "action",
        choices=("status", "exec", "input-text", "key", "repl", "workflow"),
    )
    socket_parser.add_argument("command", nargs="?")

    pipe_parser = subparsers.add_parser("pipe")
    pipe_parser.add_argument("path")
    pipe_parser.add_argument(
        "action",
        choices=("status", "exec", "input-text", "key", "repl", "workflow"),
    )
    pipe_parser.add_argument("command", nargs="?")

    stdio_parser = subparsers.add_parser("stdio")
    stdio_parser.add_argument(
        "action",
        choices=("status", "exec", "input-text", "key", "repl", "workflow"),
    )
    stdio_parser.add_argument("command", nargs="?")
    stdio_parser.add_argument("spawn_command", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)

    if args.timeout is not None and args.timeout <= 0:
        parser.error("timeout must be greater than zero")

    if args.allow_nonzero and args.action != "exec":
        parser.error("--allow-nonzero can only be used with exec")

    if args.transport == "stdio" and args.action in ("input-text", "key"):
        parser.error("input actions require socket or pipe transport")
    if args.action in ("input-text", "key") and not args.command:
        parser.error(f"{args.action} requires a value")
    if args.action == "workflow" and not args.command:
        parser.error("workflow requires a recipe path")
    if args.transcript is not None and args.action != "workflow":
        parser.error("--transcript can only be used with workflow")

    if args.transport == "stdio":
        if args.action not in ("exec", "workflow") and args.command is not None:
            args.spawn_command = [args.command] + args.spawn_command
            args.command = None
        if args.spawn_command and args.spawn_command[0] == "--":
            args.spawn_command = args.spawn_command[1:]
        if not args.spawn_command:
            parser.error("stdio requires a command after --")
        if "-control-stdio" not in args.spawn_command:
            parser.error("stdio command must include -control-stdio")
    elif args.action == "exec" and not args.command:
        parser.error("exec requires a command")

    if args.action == "exec" and not args.command:
        parser.error("exec requires a command")

    return args


def make_transport(args):
    if args.transport == "socket":
        return SocketTransport(args.path)
    if args.transport == "pipe":
        transport = PipeTransport(args.path, timeout=args.timeout)
        transport.connect()
        return transport
    return StdioTransport(args.spawn_command)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    workflow_steps = None
    transcript = None
    try:
        if args.action == "workflow":
            workflow_steps = load_workflow_recipe(args.command)
            validate_workflow_for_transport(
                workflow_steps,
                allow_input=args.transport in ("socket", "pipe"),
            )
            if args.transcript is not None:
                transcript = open(args.transcript, "w", encoding="utf-8")
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"failed to open transcript: {exc}", file=sys.stderr)
        return 1

    try:
        transport = make_transport(args)
    except OSError as exc:
        if transcript is not None:
            transcript.close()
        print(str(exc), file=sys.stderr)
        return 1

    aborted = False
    try:
        if args.action == "repl":
            return run_repl(
                transport,
                args.timeout,
                allow_input=args.transport in ("socket", "pipe"),
            )
        if args.action == "workflow":
            return run_workflow(
                transport,
                workflow_steps,
                args.timeout,
                allow_input=args.transport in ("socket", "pipe"),
                transcript=transcript,
            )
        if args.action == "input-text":
            return run_one_shot(transport, "input_text", text=args.command, timeout=args.timeout)
        if args.action == "key":
            return run_one_shot(transport, "key", key=args.command, timeout=args.timeout)
        return run_one_shot(
            transport,
            args.action,
            command=args.command,
            timeout=args.timeout,
            allow_nonzero=args.allow_nonzero,
        )
    except RequestTimeout as exc:
        print(str(exc), file=sys.stderr)
        transport.abort()
        aborted = True
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if not aborted:
            transport.close()
        if transcript is not None:
            transcript.close()


if __name__ == "__main__":
    raise SystemExit(main())
