import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT = REPO_ROOT / "scripts" / "host_control_client.py"
DEFAULT_DOSBOX_X = REPO_ROOT / "src" / "dosbox-x"
EXAMPLE_RECIPES = REPO_ROOT / "examples" / "host-control"


@dataclass
class ReplResult:
    proc: subprocess.CompletedProcess
    events: list
    server_stdout: str = ""
    server_stderr: str = ""
    recipe_path: Optional[Path] = None
    transcript_path: Optional[Path] = None
    recent_events: Optional[List[str]] = None

    def diagnostics(self):
        details = []
        if self.recipe_path is not None:
            details.append(f"recipe path: {self.recipe_path}")
        if self.transcript_path is not None:
            details.append(f"transcript path: {self.transcript_path}")
        details.extend(
            [
                f"client return code: {self.proc.returncode}",
                f"client stdout:\n{self.proc.stdout}",
                f"client stderr:\n{self.proc.stderr}",
            ]
        )
        if self.recent_events:
            details.append("recent events:")
            details.extend(self.recent_events)
        if self.server_stdout or self.server_stderr:
            details.extend(
                [
                    f"server stdout:\n{self.server_stdout}",
                    f"server stderr:\n{self.server_stderr}",
                ]
            )
        return "\n".join(details)


class HostControlLiveDiagnosticsTest(unittest.TestCase):
    def test_recipe_diagnostics_include_paths_recent_events_and_logs(self):
        proc = subprocess.CompletedProcess(
            ["host_control_client.py"],
            1,
            stdout='{"event":"ready","transport":"socket"}\n',
            stderr="client failed\n",
        )
        result = ReplResult(
            proc,
            [{"event": "ready", "transport": "socket"}],
            server_stdout="server out\n",
            server_stderr="server err\n",
            recipe_path=Path("examples/host-control/status.json"),
            transcript_path=Path("/tmp/host-control/run.jsonl"),
            recent_events=['{"event":"ready","transport":"socket"}'],
        )

        diagnostics = result.diagnostics()

        self.assertIn(f"recipe path: {Path('examples/host-control/status.json')}", diagnostics)
        self.assertIn(f"transcript path: {Path('/tmp/host-control/run.jsonl')}", diagnostics)
        self.assertIn("recent events:", diagnostics)
        self.assertIn('{"event":"ready","transport":"socket"}', diagnostics)
        self.assertIn("client stderr:\nclient failed\n", diagnostics)
        self.assertIn("server stdout:\nserver out\n", diagnostics)
        self.assertIn("server stderr:\nserver err\n", diagnostics)


class HostControlLiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("DOSBOX_X_LIVE_TESTS") != "1":
            raise unittest.SkipTest("set DOSBOX_X_LIVE_TESTS=1 to run live DOSBox-X tests")

        cls.dosbox_x = Path(os.environ.get("DOSBOX_X_BINARY", DEFAULT_DOSBOX_X))
        if not cls.dosbox_x.exists():
            raise unittest.SkipTest(f"DOSBox-X binary not found: {cls.dosbox_x}")

    def parse_events(
        self,
        proc,
        server_stdout="",
        server_stderr="",
        recipe_path=None,
        transcript_path=None,
        recent_events=None,
    ):
        events = []
        for line_number, line in enumerate(proc.stdout.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                result = ReplResult(
                    proc,
                    events,
                    server_stdout,
                    server_stderr,
                    recipe_path=recipe_path,
                    transcript_path=transcript_path,
                    recent_events=recent_events,
                )
                self.fail(
                    "failed to parse JSON event line "
                    f"{line_number}: {line!r}\n"
                    f"parse error: {exc}\n"
                    f"{result.diagnostics()}"
                )
        return events

    def read_server_logs(self, stdout_path, stderr_path):
        server_stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        server_stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        return server_stdout, server_stderr

    def read_recent_transcript_events(self, transcript_path, limit=10):
        if not transcript_path.exists():
            return []
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
        recent = []
        for line in lines[-limit:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                recent.append(line)
                continue
            recent.append(entry.get("raw", line).rstrip("\r\n"))
        return recent

    def server_diagnostics(self, stdout_path, stderr_path, server):
        server_stdout, server_stderr = self.read_server_logs(stdout_path, stderr_path)
        return (
            f"server return code: {server.returncode}\n"
            f"server stdout:\n{server_stdout}\n"
            f"server stderr:\n{server_stderr}"
        )

    def recipe_diagnostics(
        self,
        recipe_path,
        transcript_path,
        proc=None,
        server_stdout="",
        server_stderr="",
        recent_events=None,
    ):
        if proc is None:
            proc = subprocess.CompletedProcess([], -1, stdout="", stderr="")
        return ReplResult(
            proc,
            [],
            server_stdout,
            server_stderr,
            recipe_path=recipe_path,
            transcript_path=transcript_path,
            recent_events=recent_events,
        ).diagnostics()

    def run_stdio_repl(self, commands, timeout_seconds=10):
        proc = subprocess.run(
            [
                sys.executable,
                str(CLIENT),
                "--timeout",
                str(timeout_seconds),
                "stdio",
                "repl",
                "--",
                str(self.dosbox_x),
                "-control-stdio",
                "-headless",
                "-noconfig",
                "-noautoexec",
            ],
            input=commands,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds + 5,
            check=False,
        )
        events = self.parse_events(proc)
        return ReplResult(proc, events)

    def run_socket_repl(self, commands, timeout_seconds=10):
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "control.sock"
            stdout_path = Path(tmpdir) / "server.stdout"
            stderr_path = Path(tmpdir) / "server.stderr"
            with stdout_path.open("w", encoding="utf-8") as server_stdout, stderr_path.open(
                "w", encoding="utf-8"
            ) as server_stderr:
                server = subprocess.Popen(
                    [
                        str(self.dosbox_x),
                        "-control-socket",
                        str(sock_path),
                        "-headless",
                        "-noconfig",
                        "-noautoexec",
                    ],
                    stdout=server_stdout,
                    stderr=server_stderr,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + timeout_seconds
                    while time.monotonic() < deadline and not sock_path.exists():
                        if server.poll() is not None:
                            server_stdout.flush()
                            server_stderr.flush()
                            self.fail(
                                "server exited before creating socket\n"
                                + self.server_diagnostics(stdout_path, stderr_path, server)
                            )
                        time.sleep(0.05)
                    if not sock_path.exists():
                        server_stdout.flush()
                        server_stderr.flush()
                        self.fail(
                            "socket was not created\n"
                            + self.server_diagnostics(stdout_path, stderr_path, server)
                        )

                    try:
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
                    except subprocess.TimeoutExpired as exc:
                        server_stdout.flush()
                        server_stderr.flush()
                        self.fail(
                            f"client timed out after {exc.timeout} seconds\n"
                            f"client stdout:\n{exc.stdout or ''}\n"
                            f"client stderr:\n{exc.stderr or ''}\n"
                            + self.server_diagnostics(stdout_path, stderr_path, server)
                        )
                finally:
                    if server.poll() is None:
                        server.terminate()
                        try:
                            server.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            server.kill()
                            server.wait()
                    server_stdout.flush()
                    server_stderr.flush()
                server_stdout_text, server_stderr_text = self.read_server_logs(
                    stdout_path, stderr_path
                )

        events = self.parse_events(proc, server_stdout_text, server_stderr_text)
        return ReplResult(proc, events, server_stdout_text, server_stderr_text)

    def run_socket_recipe(self, recipe_path, timeout_seconds=10, server_cwd=None):
        artifact_dir = Path(tempfile.mkdtemp(prefix="dosbox-x-host-control-recipe-"))
        sock_path = artifact_dir / "control.sock"
        transcript_path = artifact_dir / "transcript.jsonl"
        stdout_path = artifact_dir / "server.stdout"
        stderr_path = artifact_dir / "server.stderr"
        server_cwd = None if server_cwd is None else Path(server_cwd)
        with stdout_path.open("w", encoding="utf-8") as server_stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as server_stderr:
            server = subprocess.Popen(
                [
                    str(self.dosbox_x),
                    "-control-socket",
                    str(sock_path),
                    "-headless",
                    "-noconfig",
                    "-noautoexec",
                ],
                cwd=server_cwd,
                stdout=server_stdout,
                stderr=server_stderr,
                text=True,
            )
            try:
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline and not sock_path.exists():
                    if server.poll() is not None:
                        server_stdout.flush()
                        server_stderr.flush()
                        server_stdout_text, server_stderr_text = self.read_server_logs(
                            stdout_path, stderr_path
                        )
                        self.fail(
                            "server exited before creating socket\n"
                            + self.recipe_diagnostics(
                                recipe_path,
                                transcript_path,
                                server_stdout=server_stdout_text,
                                server_stderr=server_stderr_text,
                            )
                        )
                    time.sleep(0.05)
                if not sock_path.exists():
                    server_stdout.flush()
                    server_stderr.flush()
                    server_stdout_text, server_stderr_text = self.read_server_logs(
                        stdout_path, stderr_path
                    )
                    self.fail(
                        "socket was not created\n"
                        + self.recipe_diagnostics(
                            recipe_path,
                            transcript_path,
                            server_stdout=server_stdout_text,
                            server_stderr=server_stderr_text,
                        )
                    )

                try:
                    proc = subprocess.run(
                        [
                            sys.executable,
                            str(CLIENT),
                            "--timeout",
                            str(timeout_seconds),
                            "--transcript",
                            str(transcript_path),
                            "socket",
                            str(sock_path),
                            "workflow",
                            str(recipe_path),
                        ],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=timeout_seconds + 5,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    server_stdout.flush()
                    server_stderr.flush()
                    server_stdout_text, server_stderr_text = self.read_server_logs(
                        stdout_path, stderr_path
                    )
                    timeout_proc = subprocess.CompletedProcess(
                        exc.cmd,
                        -1,
                        stdout=exc.stdout or "",
                        stderr=exc.stderr or "",
                    )
                    self.fail(
                        f"client timed out after {exc.timeout} seconds\n"
                        + self.recipe_diagnostics(
                            recipe_path,
                            transcript_path,
                            proc=timeout_proc,
                            server_stdout=server_stdout_text,
                            server_stderr=server_stderr_text,
                            recent_events=self.read_recent_transcript_events(
                                transcript_path
                            ),
                        )
                    )
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
                server_stdout.flush()
                server_stderr.flush()
            server_stdout_text, server_stderr_text = self.read_server_logs(
                stdout_path, stderr_path
            )

        recent_events = self.read_recent_transcript_events(transcript_path)
        events = self.parse_events(
            proc,
            server_stdout_text,
            server_stderr_text,
            recipe_path=recipe_path,
            transcript_path=transcript_path,
            recent_events=recent_events,
        )
        return ReplResult(
            proc,
            events,
            server_stdout_text,
            server_stderr_text,
            recipe_path=recipe_path,
            transcript_path=transcript_path,
            recent_events=recent_events,
        )

    def run_pipe_recipe(self, recipe_path, timeout_seconds=10, server_cwd=None):
        artifact_dir = Path(tempfile.mkdtemp(prefix="dosbox-x-host-control-pipe-"))
        if os.name == "nt":
            pipe_base = f"dosbox-x-{uuid.uuid4().hex[:12]}"
        else:
            pipe_base = artifact_dir / "control"
            input_path = Path(f"{pipe_base}.in")
            output_path = Path(f"{pipe_base}.out")
        transcript_path = artifact_dir / "transcript.jsonl"
        stdout_path = artifact_dir / "server.stdout"
        stderr_path = artifact_dir / "server.stderr"
        server_cwd = None if server_cwd is None else Path(server_cwd)
        with stdout_path.open("w", encoding="utf-8") as server_stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as server_stderr:
            server = subprocess.Popen(
                [
                    str(self.dosbox_x),
                    "-control-pipe",
                    str(pipe_base),
                    "-headless",
                    "-noconfig",
                    "-noautoexec",
                ],
                cwd=server_cwd,
                stdout=server_stdout,
                stderr=server_stderr,
                text=True,
            )
            try:
                if os.name != "nt":
                    deadline = time.monotonic() + timeout_seconds
                    while time.monotonic() < deadline and not (
                        input_path.exists() and output_path.exists()
                    ):
                        if server.poll() is not None:
                            server_stdout.flush()
                            server_stderr.flush()
                            server_stdout_text, server_stderr_text = self.read_server_logs(
                                stdout_path, stderr_path
                            )
                            self.fail(
                                "server exited before creating pipe FIFOs\n"
                                + self.recipe_diagnostics(
                                    recipe_path,
                                    transcript_path,
                                    server_stdout=server_stdout_text,
                                    server_stderr=server_stderr_text,
                                )
                            )
                        time.sleep(0.05)
                    if not (input_path.exists() and output_path.exists()):
                        server_stdout.flush()
                        server_stderr.flush()
                        server_stdout_text, server_stderr_text = self.read_server_logs(
                            stdout_path, stderr_path
                        )
                        self.fail(
                            "pipe FIFOs were not created\n"
                            + self.recipe_diagnostics(
                                recipe_path,
                                transcript_path,
                                server_stdout=server_stdout_text,
                                server_stderr=server_stderr_text,
                            )
                        )
                try:
                    client_command = [
                        sys.executable,
                        str(CLIENT),
                        "--timeout",
                        str(timeout_seconds),
                        "--transcript",
                        str(transcript_path),
                        "pipe",
                        str(pipe_base),
                        "workflow",
                        str(recipe_path),
                    ]
                    if os.name != "nt":
                        proc = subprocess.run(
                            client_command,
                            text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=timeout_seconds + 5,
                            check=False,
                        )
                    else:
                        deadline = time.monotonic() + timeout_seconds
                        while True:
                            remaining = max(0.1, deadline - time.monotonic())
                            client_command[3] = str(remaining)
                            proc = subprocess.run(
                                client_command,
                                text=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                timeout=remaining + 5,
                                check=False,
                            )
                            if (
                                proc.returncode == 0
                                or "failed to open pipe transport" not in proc.stderr
                                or time.monotonic() >= deadline
                            ):
                                break
                            time.sleep(0.05)
                except subprocess.TimeoutExpired as exc:
                    server_stdout.flush()
                    server_stderr.flush()
                    server_stdout_text, server_stderr_text = self.read_server_logs(
                        stdout_path, stderr_path
                    )
                    timeout_proc = subprocess.CompletedProcess(
                        exc.cmd,
                        -1,
                        stdout=exc.stdout or "",
                        stderr=exc.stderr or "",
                    )
                    self.fail(
                        f"client timed out after {exc.timeout} seconds\n"
                        + self.recipe_diagnostics(
                            recipe_path,
                            transcript_path,
                            proc=timeout_proc,
                            server_stdout=server_stdout_text,
                            server_stderr=server_stderr_text,
                            recent_events=self.read_recent_transcript_events(
                                transcript_path
                            ),
                        )
                    )
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
                server_stdout.flush()
                server_stderr.flush()
            server_stdout_text, server_stderr_text = self.read_server_logs(
                stdout_path, stderr_path
            )

        recent_events = self.read_recent_transcript_events(transcript_path)
        events = self.parse_events(
            proc,
            server_stdout_text,
            server_stderr_text,
            recipe_path=recipe_path,
            transcript_path=transcript_path,
            recent_events=recent_events,
        )
        return ReplResult(
            proc,
            events,
            server_stdout_text,
            server_stderr_text,
            recipe_path=recipe_path,
            transcript_path=transcript_path,
            recent_events=recent_events,
        )

    def run_socket_workflow(self, recipe, timeout_seconds=10):
        artifact_dir = Path(tempfile.mkdtemp(prefix="dosbox-x-host-control-workflow-"))
        recipe_path = artifact_dir / "recipe.json"
        recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
        return self.run_socket_recipe(recipe_path, timeout_seconds=timeout_seconds)

    def run_pipe_workflow(self, recipe, timeout_seconds=10, server_cwd=None):
        artifact_dir = Path(tempfile.mkdtemp(prefix="dosbox-x-host-control-pipe-workflow-"))
        recipe_path = artifact_dir / "recipe.json"
        recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
        return self.run_pipe_recipe(
            recipe_path, timeout_seconds=timeout_seconds, server_cwd=server_cwd
        )

    def test_exec_mount_returns_result_event(self):
        with tempfile.TemporaryDirectory() as mount_dir:
            result = self.run_stdio_repl(
                f"exec mount c {mount_dir}\nquit\n",
                timeout_seconds=10,
            )

        proc = result.proc
        events = result.events
        self.assertEqual(proc.returncode, 0, proc.stderr)
        results = [event for event in events if event.get("event") == "result"]
        self.assertEqual(len(results), 1, proc.stdout)
        self.assertEqual(results[0].get("id"), "1")
        self.assertTrue(results[0].get("ok"), proc.stdout)

    def test_socket_input_text_runs_dir_after_prompt(self):
        result = self.run_socket_repl(
            "input dir\nkey enter\nquit\n",
            timeout_seconds=10,
        )

        self.assertEqual(result.proc.returncode, 0, result.diagnostics())
        events = result.events
        input_results = [event for event in events if event.get("event") == "input_result"]
        self.assertEqual(len(input_results), 2, result.diagnostics())
        self.assertEqual(
            [event.get("id") for event in input_results], ["1", "2"], result.diagnostics()
        )

    def test_socket_exec_dir_returns_output_event(self):
        result = self.run_socket_repl(
            "exec dir\nquit\n",
            timeout_seconds=10,
        )

        self.assertEqual(result.proc.returncode, 0, result.diagnostics())
        self.assertTrue(
            any(event.get("event") == "output" for event in result.events), result.diagnostics()
        )

    def test_socket_workflow_interactive_dir_streams_output(self):
        result = self.run_socket_workflow(
            {
                "steps": [
                    {
                        "exec_interactive": {
                            "command": "dir",
                            "steps": [
                                {"wait_for": "output"},
                                {"wait_for": {"event": "result", "ok": True}},
                            ],
                        }
                    }
                ]
            },
            timeout_seconds=10,
        )

        self.assertEqual(result.proc.returncode, 0, result.diagnostics())
        self.assertTrue(
            any(event.get("event") == "output" for event in result.events), result.diagnostics()
        )
        self.assertTrue(
            any(event.get("event") == "result" and event.get("ok") for event in result.events),
            result.diagnostics(),
        )

    def test_socket_status_recipe_runs(self):
        result = self.run_socket_recipe(EXAMPLE_RECIPES / "status.json", timeout_seconds=10)

        self.assertEqual(result.proc.returncode, 0, result.diagnostics())
        self.assertTrue(result.transcript_path.exists(), result.diagnostics())
        self.assertTrue(
            any(event.get("event") == "status" for event in result.events),
            result.diagnostics(),
        )

    def test_pipe_status_recipe_runs(self):
        result = self.run_pipe_recipe(EXAMPLE_RECIPES / "status.json", timeout_seconds=10)

        self.assertEqual(result.proc.returncode, 0, result.diagnostics())
        self.assertTrue(result.transcript_path.exists(), result.diagnostics())
        if os.name == "nt":
            self.assertEqual(
                [event.get("event") for event in result.events[:2]],
                ["ready", "status"],
                result.diagnostics(),
            )
        else:
            self.assertTrue(
                any(event.get("event") == "status" for event in result.events),
                result.diagnostics(),
            )

    def test_socket_interactive_dir_recipe_streams_output(self):
        result = self.run_socket_recipe(
            EXAMPLE_RECIPES / "interactive-dir.json",
            timeout_seconds=10,
        )

        self.assertEqual(result.proc.returncode, 0, result.diagnostics())
        self.assertTrue(result.transcript_path.exists(), result.diagnostics())
        self.assertTrue(
            any(event.get("event") == "output" for event in result.events),
            result.diagnostics(),
        )
        self.assertTrue(
            any(event.get("event") == "result" and event.get("ok") for event in result.events),
            result.diagnostics(),
        )

    def test_socket_mount_current_dir_recipe_lists_c_drive(self):
        result = self.run_socket_recipe(
            EXAMPLE_RECIPES / "mount-current-dir-and-list.json",
            timeout_seconds=10,
            server_cwd=REPO_ROOT,
        )

        self.assertEqual(result.proc.returncode, 0, result.diagnostics())
        self.assertTrue(result.transcript_path.exists(), result.diagnostics())
        self.assertTrue(
            any(event.get("event") == "output" for event in result.events),
            result.diagnostics(),
        )
        self.assertTrue(
            any(event.get("event") == "result" and event.get("ok") for event in result.events),
            result.diagnostics(),
        )

    def test_socket_hangtime_project_recipe_lists_project_root(self):
        project_root = os.environ.get("HANGTIME_PROJECT_ROOT")
        if not project_root:
            raise unittest.SkipTest(
                "set HANGTIME_PROJECT_ROOT to run NBA Hangtime recipe smoke"
            )
        project_root = Path(project_root)
        if not project_root.exists():
            raise unittest.SkipTest(f"HANGTIME_PROJECT_ROOT not found: {project_root}")

        result = self.run_socket_recipe(
            EXAMPLE_RECIPES / "hangtime" / "list-project-root.json",
            timeout_seconds=10,
            server_cwd=project_root,
        )

        self.assertEqual(result.proc.returncode, 0, result.diagnostics())
        self.assertTrue(result.transcript_path.exists(), result.diagnostics())
        self.assertTrue(
            any(event.get("event") == "output" for event in result.events),
            result.diagnostics(),
        )
        self.assertTrue(
            any(event.get("event") == "result" and event.get("ok") for event in result.events),
            result.diagnostics(),
        )

    def test_pipe_batch_exec_completes_synchronously(self):
        with tempfile.TemporaryDirectory(prefix="dosbox-x-batch-") as temp:
            root = Path(temp)
            (root / "EXIT7.COM").write_bytes(bytes.fromhex("B44CB007CD21"))
            (root / "BUILD.BAT").write_text(
                "@echo BATCH-BEFORE\r\n@echo done>DONE.TXT\r\n@EXIT7.COM\r\n",
                encoding="ascii",
            )
            result = self.run_pipe_workflow(
                {
                    "steps": [
                        {"exec": f'mount c "{root}"'},
                        {"exec": "c:"},
                        {"exec": {"command": "BUILD.BAT", "expect_errorlevel": 7}},
                    ]
                },
                timeout_seconds=20,
            )
            self.assertTrue((root / "DONE.TXT").exists(), result.diagnostics())

        batch_result_index = next(
            i for i, event in enumerate(result.events)
            if event.get("event") == "result" and event.get("errorlevel") == 7
        )
        output_indexes = [
            i for i, event in enumerate(result.events)
            if event.get("event") == "output" and event.get("id") == "3"
        ]
        self.assertTrue(output_indexes, result.diagnostics())
        self.assertLess(max(output_indexes), batch_result_index, result.diagnostics())

    def test_pipe_nested_batch_exec_completes_synchronously(self):
        with tempfile.TemporaryDirectory(prefix="dosbox-x-nested-batch-") as temp:
            root = Path(temp)
            (root / "INNER.BAT").write_text("@echo inner>INNER.TXT\r\n", encoding="ascii")
            (root / "OUTER.BAT").write_text(
                "@call INNER.BAT\r\n@echo outer>OUTER.TXT\r\n", encoding="ascii"
            )
            result = self.run_pipe_workflow(
                {
                    "steps": [
                        {"exec": f'mount c "{root}"'},
                        {"exec": "c:"},
                        {"exec": "OUTER.BAT"},
                    ]
                },
                timeout_seconds=20,
            )
            self.assertTrue((root / "INNER.TXT").exists(), result.diagnostics())
            self.assertTrue((root / "OUTER.TXT").exists(), result.diagnostics())

    def test_pipe_batch_exit_terminates_shell(self):
        # The batch is named QUIT.BAT because DOSBox-X treats EXIT.BAT as the
        # internal EXIT command, which skips the batch body entirely.
        with tempfile.TemporaryDirectory(prefix="dosbox-x-exit-batch-") as temp:
            root = Path(temp)
            (root / "QUIT.BAT").write_text("@echo bye>BYE.TXT\r\n@exit\r\n", encoding="ascii")
            result = self.run_pipe_workflow(
                {
                    "steps": [
                        {"exec": f'mount c "{root}"'},
                        {"exec": "c:"},
                        {"exec": "QUIT.BAT"},
                    ]
                },
                timeout_seconds=20,
            )
            # The batch body must drain before the shell exits, and the workflow
            # must return promptly instead of hanging on the closing pipe. The
            # shell_exit result is asserted once the client tolerates the server
            # closing the pipe after the final result (Task 5).
            self.assertTrue((root / "BYE.TXT").exists(), result.diagnostics())

    def test_pipe_high_volume_output_is_byte_exact(self):
        payload = b"0123456789ABCDEF\r\n" * 700_000
        with tempfile.TemporaryDirectory(prefix="dosbox-x-large-") as temp:
            root = Path(temp)
            (root / "LARGE.TXT").write_bytes(payload)
            result = self.run_pipe_workflow(
                {
                    "steps": [
                        {"exec": f'mount c "{root}"'},
                        {"exec": "c:"},
                        {"exec": "type LARGE.TXT"},
                    ]
                },
                timeout_seconds=300,
            )

        self.assertEqual(result.proc.returncode, 0, result.diagnostics())
        large_request_id = None
        for event in result.events:
            if event.get("event") == "result":
                large_request_id = event.get("id")
        self.assertIsNotNone(large_request_id, result.diagnostics())

        decoded = b"".join(
            base64.b64decode(event["data"])
            for event in result.events
            if event.get("event") == "output"
            and event.get("id") == large_request_id
            and event.get("data")
        )
        self.assertEqual(decoded, payload, result.diagnostics())

    def test_pipe_disconnect_during_exec_terminates_cleanly(self):
        if os.name != "nt":
            raise unittest.SkipTest("Windows named-pipe disconnect test")

        spec = importlib.util.spec_from_file_location("host_control_client", CLIENT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        api = module.WindowsPipeApi()
        pipe_base = f"dosbox-x-{uuid.uuid4().hex[:12]}"
        endpoint = module.normalize_windows_pipe_endpoint(pipe_base)

        server = subprocess.Popen(
            [
                str(self.dosbox_x),
                "-control-pipe",
                pipe_base,
                "-headless",
                "-noconfig",
                "-noautoexec",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        handle = module.INVALID_HANDLE_VALUE
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and handle == module.INVALID_HANDLE_VALUE:
                handle = api.create_file(endpoint)
                if handle != module.INVALID_HANDLE_VALUE:
                    break
                error = api.get_last_error()
                if error not in (module.ERROR_PIPE_BUSY, module.ERROR_FILE_NOT_FOUND):
                    break
                time.sleep(0.05)

            self.assertNotEqual(
                handle, module.INVALID_HANDLE_VALUE, "could not open named pipe"
            )

            api.write_file(handle, b'{"id":"1","op":"exec","command":"dir"}\n')

            saw_output = False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not saw_output:
                chunk = api.read_file(handle, 4096)
                if not chunk:
                    break
                for line in chunk.splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "output":
                        saw_output = True
                        break
            self.assertTrue(saw_output, "server produced no output before disconnect")

            api.close_handle(handle)
            handle = module.INVALID_HANDLE_VALUE

            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                    self.fail("DOSBox-X hung after client disconnect")
        finally:
            if handle != module.INVALID_HANDLE_VALUE:
                api.close_handle(handle)
            if server.poll() is None:
                server.kill()
                server.wait()
            if server.stdout is not None:
                server.stdout.close()
            if server.stderr is not None:
                server.stderr.close()


if __name__ == "__main__":
    unittest.main()
