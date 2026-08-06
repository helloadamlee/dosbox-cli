# Host-Control Windows Named Pipe Design

## Goal

Add Windows named-pipe support to the existing host-control NDJSON protocol.
The implementation must build with both MinGW and MSVC, preserve the existing
Unix FIFO-pair behavior, and keep the public client command shape unchanged.

## Endpoint Contract

On Windows, `-control-pipe <endpoint>` accepts either a short pipe name or a
full local named-pipe path. A short name is normalized to
`\\.\pipe\<name>`; a value already starting with `\\.\pipe\` is used as
provided. Empty endpoints fail with a clear diagnostic. Unix-like systems keep
using `<base>.in` and `<base>.out` FIFOs unchanged.

## Server Architecture

The host-control pipe transport is split into a transport-neutral duplex
session runner plus platform I/O adapters.

The shared runner owns the request queue, serialized output, input request
handling, request/result ordering, buffered stdout events, and disconnect
state. It accepts operations to read a newline-delimited request, write a
complete JSON line, determine peer disconnection, and close or unblock the
reader during teardown.

The POSIX adapter continues to use the existing FIFO descriptors and cleanup.
The Windows adapter creates one `PIPE_ACCESS_DUPLEX` byte-mode named pipe with
`CreateNamedPipeW`, waits for one client using `ConnectNamedPipe`, and provides
the shared runner with `ReadFile` and `WriteFile` operations over the connected
handle. It treats broken-pipe and no-data failures as peer disconnects, then
calls `DisconnectNamedPipe` and `CloseHandle` on teardown. Only one client is
served per DOSBox-X process, matching current socket and FIFO semantics.

The protocol remains NDJSON on both platforms: ready, output, result, status,
and error events retain their existing shape and ordering.

## Client Architecture

`scripts/host_control_client.py pipe <endpoint> ...` retains its current CLI.
On Windows, its pipe transport normalizes the endpoint and opens the duplex
pipe with `CreateFileW` through `ctypes`, without requiring pywin32. If the
server reports `ERROR_PIPE_BUSY`, the client retries until the configured
timeout. Read and write helpers use `ReadFile` and `WriteFile` while preserving
the current line buffering, request sequencing, transcript JSONL, REPL, and
diagnostics. A missing or unavailable pipe includes the normalized endpoint in
the error message.

Unix client behavior continues to open the `.in` and `.out` FIFOs.

## Error Handling

- Invalid or empty Windows endpoints fail before opening a handle.
- Pipe-creation, connection, read, and write failures include the Win32 error
  message and the normalized endpoint.
- Client connection retry is bounded by the existing timeout and reports
  timeout versus immediate invalid-endpoint errors distinctly.
- A client disconnect during a request ends the session cleanly without
  emitting partial JSON events or blocking the host-control reader thread.

## Tests

Add platform-independent tests for endpoint normalization, including short,
full, and empty values. Extend Python client tests with a fake Windows I/O
adapter to cover timeout, busy-pipe retry, request writes, response reads, and
workflow transcript preservation without requiring Windows.

On Windows, add native lifecycle tests for creation, connection, duplex NDJSON
exchange, disconnect cleanup, and missing-endpoint diagnostics. Add an opt-in
live smoke test that starts DOSBox-X with `-control-pipe`, runs the existing
status recipe through the Python client, and verifies transcript event shape.
The existing Unix host-control test suites remain regression coverage for FIFO
behavior.

## Non-Goals

- Reconnection or multiple simultaneous clients.
- Changes to the host-control JSON schema or command syntax.
- Supporting arbitrary remote or network named-pipe paths.
