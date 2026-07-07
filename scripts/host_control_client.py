#!/usr/bin/env python3

import argparse
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


WORKFLOW_ACTIONS = {"comment", "exec", "status", "input_text", "key", "wait_for"}
WAIT_EVENT_ALIASES = {"ready", "output", "result", "status", "error", "input_result"}


def parse_workflow_recipe(recipe):
    if not isinstance(recipe, dict):
        raise WorkflowError("recipe: expected object")
    steps = recipe.get("steps")
    if not isinstance(steps, list):
        raise WorkflowError("recipe.steps: expected array")

    parsed = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise WorkflowError(f"step {index}: expected object")
        if not step:
            parsed.append(WorkflowStep("noop"))
            continue

        actions = [key for key in step if key in WORKFLOW_ACTIONS]
        unknown = [key for key in step if key not in WORKFLOW_ACTIONS]
        if unknown:
            raise WorkflowError(f"step {index}: unknown action {unknown[0]}")
        if len(actions) != 1:
            raise WorkflowError(f"step {index}: multiple actions")

        action = actions[0]
        value = step[action]
        if action == "comment":
            if not isinstance(value, str):
                raise WorkflowError(f"step {index}: comment must be a string")
        elif action == "exec":
            if not isinstance(value, str) or not value:
                raise WorkflowError(f"step {index}: exec must be a non-empty string")
        elif action == "status":
            if value not in (True, None) and value != {}:
                raise WorkflowError(f"step {index}: status must be true, null, or object")
        elif action == "input_text":
            if not isinstance(value, str):
                raise WorkflowError(f"step {index}: input_text must be a string")
        elif action == "key":
            if not isinstance(value, str) or not value:
                raise WorkflowError(f"step {index}: key must be a non-empty string")
        elif action == "wait_for":
            if not isinstance(value, (str, dict)):
                raise WorkflowError(f"step {index}: wait_for must be a string or object")
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
    if allow_input:
        return
    for index, step in enumerate(steps):
        if step.action in ("input_text", "key"):
            raise WorkflowError(f"step {index}: {step.action} actions are socket-only")


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
    fail_on_error=False,
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
            if fail_on_error and event.get("event") == "error":
                raise WorkflowError(
                    f"server error for request {request_id}: {event.get('message', '')}"
                )
            return 0


def run_one_shot(transport, op, command=None, text=None, key=None, timeout=None):
    read_event_line(transport, make_deadline(timeout), "ready event")
    return run_request(
        transport,
        1,
        op,
        command=command,
        text=text,
        key=key,
        timeout=timeout,
    )


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
            sys.stderr.write("input actions are socket-only\n")
            sys.stderr.flush()
            continue

        if op == "input_text":
            run_request(transport, next_request_id, op, text=command, timeout=timeout)
        elif op == "key":
            run_request(transport, next_request_id, op, key=command, timeout=timeout)
        else:
            run_request(transport, next_request_id, op, command=command, timeout=timeout)
        next_request_id += 1


def run_workflow(transport, steps, timeout=None, allow_input=True, transcript=None):
    recorder = EventRecorder(transcript=transcript)
    read_event_line(transport, make_deadline(timeout), "ready event", recorder=recorder)
    next_request_id = 1

    for index, step in enumerate(steps):
        if step.action in ("noop", "comment"):
            continue
        try:
            if step.action == "exec":
                run_request(
                    transport,
                    next_request_id,
                    "exec",
                    command=step.value,
                    timeout=timeout,
                    recorder=recorder,
                    fail_on_error=True,
                )
                next_request_id += 1
            elif step.action == "status":
                run_request(
                    transport,
                    next_request_id,
                    "status",
                    timeout=timeout,
                    recorder=recorder,
                    fail_on_error=True,
                )
                next_request_id += 1
            elif step.action == "input_text":
                if not allow_input:
                    raise WorkflowError("input_text actions are socket-only")
                run_request(
                    transport,
                    next_request_id,
                    "input_text",
                    text=step.value,
                    timeout=timeout,
                    recorder=recorder,
                    fail_on_error=True,
                )
                next_request_id += 1
            elif step.action == "key":
                if not allow_input:
                    raise WorkflowError("key actions are socket-only")
                run_request(
                    transport,
                    next_request_id,
                    "key",
                    key=step.value,
                    timeout=timeout,
                    recorder=recorder,
                    fail_on_error=True,
                )
                next_request_id += 1
            elif step.action == "wait_for":
                wait_for_workflow_event(
                    transport,
                    step.value,
                    timeout=timeout,
                    recorder=recorder,
                )
        except (RequestTimeout, RuntimeError, WorkflowError) as exc:
            raise WorkflowError(format_workflow_failure(index, step, exc, recorder)) from exc
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
    subparsers = parser.add_subparsers(dest="transport", required=True)

    socket_parser = subparsers.add_parser("socket")
    socket_parser.add_argument("path")
    socket_parser.add_argument(
        "action",
        choices=("status", "exec", "input-text", "key", "repl", "workflow"),
    )
    socket_parser.add_argument("command", nargs="?")

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

    if args.transport == "stdio" and args.action in ("input-text", "key"):
        parser.error("input actions are socket-only")
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
    return StdioTransport(args.spawn_command)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    workflow_steps = None
    try:
        if args.action == "workflow":
            workflow_steps = load_workflow_recipe(args.command)
            validate_workflow_for_transport(
                workflow_steps,
                allow_input=args.transport == "socket",
            )
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        transport = make_transport(args)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    aborted = False
    try:
        if args.action == "repl":
            return run_repl(transport, args.timeout, allow_input=args.transport == "socket")
        if args.action == "workflow":
            return run_workflow(
                transport,
                workflow_steps,
                args.timeout,
                allow_input=args.transport == "socket",
            )
        if args.action == "input-text":
            return run_one_shot(transport, "input_text", text=args.command, timeout=args.timeout)
        if args.action == "key":
            return run_one_shot(transport, "key", key=args.command, timeout=args.timeout)
        return run_one_shot(transport, args.action, command=args.command, timeout=args.timeout)
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


if __name__ == "__main__":
    raise SystemExit(main())
