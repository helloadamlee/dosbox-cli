#!/usr/bin/env python3
"""Drive the NBA Hangtime build through the DOSBox-X Windows pipe.

Usage:
  python hangtime-pipe-test/drive_build.py <pipe-name> <command>

Connects to the named pipe, mounts the Hangtime build dir as C:, sets PATH,
then runs <command> via a host-control exec request while streaming decoded
DOS console output to stdout. Answers "Are you sure [Y/N]?" prompts (and the
marker that precedes them) by injecting "y\\r" through an input_text request.
Exits 0 on result, nonzero otherwise.
"""
import base64
import json
import sys
import time

sys.path.insert(0, "scripts")
import host_control_client as hc  # noqa: E402

MOUNT = r"C:\Projects\hangtime\Asure\nba-hangtime-rebuild-main\BACKUP\XCODE101L"


def log(*args):
    print(*args, file=sys.stderr)
    sys.stderr.flush()


def send_and_wait_result(transport, request_id, op, command=None, text=None):
    transport.writeline(hc.encode_request(request_id, op, command=command, text=text))
    while True:
        event = hc.read_event_line(transport, hc.make_deadline(120), f"{op} result")
        if event.get("event") in ("result", "error"):
            return event


def main():
    if len(sys.argv) != 3:
        log("usage: drive_build.py <pipe-name> <command>")
        return 2
    pipe_name = sys.argv[1]
    command = sys.argv[2]

    transport = hc.PipeTransport(pipe_name, timeout=1800)
    try:
        transport.connect()
    except OSError as exc:
        log(f"connect failed: {exc}")
        return 1

    try:
        ready = hc.read_event_line(transport, hc.make_deadline(60), "ready event")
        log(f"ready: {json.dumps(ready)}")

        for request_id, cmd in (
            (1, f'mount c "{MOUNT}"'),
            (2, "c:"),
            (3, r"path Z:\;C:\;C:\tools"),
        ):
            event = send_and_wait_result(transport, request_id, "exec", command=cmd)
            log(
                f"[exec {request_id} -> ok={event.get('ok')} "
                f"errorlevel={event.get('errorlevel')}] {cmd!r}"
            )

        transport.writeline(hc.encode_request(4, "exec", command=command))
        log(f"sent exec: {command!r}")

        next_id = 5
        injected = 0
        accumulated = ""
        while True:
            event = hc.read_event_line(transport, hc.make_deadline(1800), "event")
            kind = event.get("event")

            if kind == "output":
                raw = event.get("data") or ""
                try:
                    decoded = base64.b64decode(raw).decode("cp437", errors="replace")
                except Exception:
                    decoded = ""
                sys.stdout.write(decoded)
                sys.stdout.flush()
                accumulated += decoded
                # The flush fix means the full "Are you sure [Y/N]?" prompt
                # now reaches us reliably, so we answer exactly that prompt.
                if injected < 5 and "Are you sure" in accumulated:
                    log("\n[injecting y<Enter> to answer del confirmation]")
                    transport.writeline(
                        hc.encode_request(next_id, "input_text", text="y\r")
                    )
                    next_id += 1
                    injected += 1
                    accumulated = ""
                    time.sleep(0.2)
            elif kind == "input_result":
                log(
                    f"[input_result id={event.get('id')} ok={event.get('ok')} "
                    f"queued={event.get('queued')}]"
                )
            elif kind == "result":
                log(
                    f"[result ok={event.get('ok')} shell_exit={event.get('shell_exit')} "
                    f"errorlevel={event.get('errorlevel')} duration_ms={event.get('duration_ms')}]"
                )
                return 0 if event.get("ok") else 1
            elif kind == "error":
                log(f"[server error: {json.dumps(event)}]")
                return 1
            else:
                log(f"[other event: {json.dumps(event)}]")
    finally:
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
