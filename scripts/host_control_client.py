#!/usr/bin/env python3

import argparse
import json
import os
import select
import socket
import subprocess
import sys
import time


class RequestTimeout(RuntimeError):
    pass


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


def read_event_line(transport, deadline=None, description="event"):
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
        return json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("received invalid JSON event") from exc


def run_request(transport, request_id, op, command=None, text=None, key=None, timeout=None):
    deadline = make_deadline(timeout)
    transport.writeline(encode_request(request_id, op, command, text, key))
    while True:
        event = read_event_line(transport, deadline, f"{op} request {request_id}")
        if event_completes_request(event, request_id, op):
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


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="seconds to wait for each host-control response",
    )
    subparsers = parser.add_subparsers(dest="transport", required=True)

    socket_parser = subparsers.add_parser("socket")
    socket_parser.add_argument("path")
    socket_parser.add_argument("action", choices=("status", "exec", "input-text", "key", "repl"))
    socket_parser.add_argument("command", nargs="?")

    stdio_parser = subparsers.add_parser("stdio")
    stdio_parser.add_argument("action", choices=("status", "exec", "input-text", "key", "repl"))
    stdio_parser.add_argument("command", nargs="?")
    stdio_parser.add_argument("spawn_command", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)

    if args.timeout is not None and args.timeout <= 0:
        parser.error("timeout must be greater than zero")

    if args.transport == "stdio" and args.action in ("input-text", "key"):
        parser.error("input actions are socket-only")
    if args.action in ("input-text", "key") and not args.command:
        parser.error(f"{args.action} requires a value")

    if args.transport == "stdio":
        if args.action != "exec" and args.command is not None:
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

    try:
        transport = make_transport(args)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    aborted = False
    try:
        if args.action == "repl":
            return run_repl(transport, args.timeout, allow_input=args.transport == "socket")
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
