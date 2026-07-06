#!/usr/bin/env python3

import argparse
import json
import socket
import subprocess
import sys


def encode_request(request_id, op, command=None):
    payload = {"id": str(request_id), "op": op}
    if command is not None:
        payload["command"] = command
    return json.dumps(payload, separators=(",", ":"))


def event_completes_request(event, request_id, op):
    if str(event.get("id", "")) != str(request_id):
        return False
    if event.get("event") == "error":
        return True
    if op == "status":
        return event.get("event") == "status"
    return event.get("event") == "result"


def parse_repl_command(text):
    text = text.strip()
    if not text:
        return None
    if text == "status":
        return ("status", None)
    if text == "quit":
        return ("quit", None)
    if text == "help":
        return ("help", None)
    if text.startswith("exec "):
        return ("exec", text[5:])
    raise ValueError("unknown command")


class SocketTransport:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(path)
        self.reader = self.socket.makefile("r", encoding="utf-8", newline="\n")
        self.writer = self.socket.makefile("w", encoding="utf-8", newline="\n")

    def readline(self):
        return self.reader.readline()

    def writeline(self, line):
        self.writer.write(line)
        self.writer.write("\n")
        self.writer.flush()

    def close(self):
        try:
            self.writer.close()
        finally:
            try:
                self.reader.close()
            finally:
                self.socket.close()


class StdioTransport:
    def __init__(self, command):
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
        )

    def readline(self):
        assert self.process.stdout is not None
        return self.process.stdout.readline()

    def writeline(self, line):
        assert self.process.stdin is not None
        self.process.stdin.write(line)
        self.process.stdin.write("\n")
        self.process.stdin.flush()

    def close(self):
        try:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
        finally:
            if self.process.stdout is not None and not self.process.stdout.closed:
                self.process.stdout.close()
            self.process.wait()


def read_event_line(transport):
    line = transport.readline()
    if not line:
        raise RuntimeError("unexpected EOF from host control transport")
    sys.stdout.write(line)
    sys.stdout.flush()
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError("received invalid JSON event") from exc


def run_request(transport, request_id, op, command=None):
    transport.writeline(encode_request(request_id, op, command))
    while True:
        event = read_event_line(transport)
        if event_completes_request(event, request_id, op):
            return 0


def run_one_shot(transport, op, command=None):
    read_event_line(transport)
    return run_request(transport, 1, op, command)


def run_repl(transport):
    read_event_line(transport)
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
            sys.stderr.write("commands: status | exec <command> | help | quit\n")
            sys.stderr.flush()
            continue

        if parsed is None:
            continue

        op, command = parsed
        if op == "quit":
            return 0
        if op == "help":
            sys.stderr.write("commands: status | exec <command> | help | quit\n")
            sys.stderr.flush()
            continue

        run_request(transport, next_request_id, op, command)
        next_request_id += 1


def parse_args(argv):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="transport", required=True)

    socket_parser = subparsers.add_parser("socket")
    socket_parser.add_argument("path")
    socket_parser.add_argument("action", choices=("status", "exec", "repl"))
    socket_parser.add_argument("command", nargs="?")

    stdio_parser = subparsers.add_parser("stdio")
    stdio_parser.add_argument("action", choices=("status", "exec", "repl"))
    stdio_parser.add_argument("command", nargs="?")
    stdio_parser.add_argument("spawn_command", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)

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

    try:
        if args.action == "repl":
            return run_repl(transport)
        return run_one_shot(transport, args.action, args.command)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
