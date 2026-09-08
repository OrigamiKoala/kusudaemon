from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import time
from pathlib import Path

import subprocess
from ..types import DEFAULT_TMP_DIR, ExecResult
from ..utils.process_group import (
    kill_process_group,
    signal_process_group,
    track_process_group,
    untrack_process_group,
)


def _get_pgid_cputime(pid: int) -> float:
    """Total user + sys CPU seconds across all processes in proc's process group."""
    try:
        pgid = os.getpgid(pid)
        out = subprocess.run(
            ["ps", "-o", "time=", "-g", str(pgid)],
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout
        total = 0.0
        for line in out.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) == 3:
                total += int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                total += int(parts[0]) * 60 + float(parts[1])
        return total
    except Exception:
        return 0.0


def _has_established_socket(pid: int) -> bool:
    """Check if any process in the process group has an established TCP socket connection."""
    try:
        pgid = os.getpgid(pid)
        pids_out = subprocess.run(
            ["pgrep", "-g", str(pgid)],
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout
        pids = [p.strip() for p in pids_out.split() if p.strip()]
        if not pids:
            pids = [str(pid)]
        pids_arg = ",".join(pids)
        res = subprocess.run(
            ["lsof", "-a", "-iTCP", "-sTCP:ESTABLISHED", "-p", pids_arg],
            capture_output=True,
            timeout=2.0,
        )
        return b"ESTABLISHED" in res.stdout
    except Exception:
        return False


_ENV_MUTATING_PATTERNS = (
    "pip install", "pip3 install", "pip uninstall", "pip3 uninstall",
    "npm install", "npm i ", "npm uninstall", "npm ci",
    "yarn add", "yarn remove", "yarn install",
    "pnpm add", "pnpm install",
    "apt-get", "apt ", "dpkg",
    "brew install", "brew uninstall",
    "docker ", "podman ",
    "cargo install", "cargo add",
    "go install", "go get",
    "http.server",
)


def is_env_mutating_command(cmd: str) -> bool:
    """PLAN-CONCURRENCY-AND-SHARED-STATE.md §B4: dynamic command inspection."""
    low = cmd.lower()
    return any(pat in low for pat in _ENV_MUTATING_PATTERNS)


class LocalEnvironment:
    def __init__(self, tmp_dir: str | None = None, *, env_lock: asyncio.Lock | None = None) -> None:
        # Library usage falls back to user-scoped scratch storage.
        self._tmp_dir = Path(tmp_dir).expanduser() if tmp_dir else Path(DEFAULT_TMP_DIR)
        self._env_lock = env_lock or asyncio.Lock()

    @property
    def staging_dir(self) -> Path:
        """Where callers may stage files before uploading them into this env."""
        return self._tmp_dir

    async def exec(
        self,
        command: str,
        timeout: int = 300,
        tee_path: str | None = None,
        grace_period: int | None = None,
        activity_window: float | None = None,
    ) -> ExecResult:
        if is_env_mutating_command(command):
            async with self._env_lock:
                return await self._exec_impl(
                    command,
                    timeout=timeout,
                    tee_path=tee_path,
                    grace_period=grace_period,
                    activity_window=activity_window,
                )
        return await self._exec_impl(
            command,
            timeout=timeout,
            tee_path=tee_path,
            grace_period=grace_period,
            activity_window=activity_window,
        )

    async def _exec_impl(
        self,
        command: str,
        timeout: int = 300,
        tee_path: str | None = None,
        grace_period: int | None = None,
        activity_window: float | None = None,
    ) -> ExecResult:
        raw_env_timeout = os.getenv("KUSUDAEMON_EXEC_TIMEOUT")
        if raw_env_timeout:
            try:
                timeout = int(raw_env_timeout)
            except ValueError:
                pass
        start = time.monotonic()
        proc = None
        io_task: asyncio.Task[None] | None = None
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        activity_ref: list[float] = [start]
        soft_ext = (
            int(os.getenv("KUSUDAEMON_SOFT_TIMEOUT_EXTENSION", "1800"))
            if grace_period is None
            else grace_period
        )
        act_win = (
            float(os.getenv("KUSUDAEMON_ACTIVITY_WINDOW", "60.0"))
            if activity_window is None
            else activity_window
        )
        max_deadline = start + timeout + max(0, soft_ext)
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Own session, so one killpg reaps the agent CLI and everything
                # it spawned. It also detaches the child from our terminal, so
                # Ctrl+C never reaches it, so every exit path below must kill it.
                start_new_session=True,
                # Claude Code emits one JSON object per line in stream-json mode.
                # A single line (e.g. a tool_result carrying a base64 screenshot)
                # can far exceed asyncio's default 64KB StreamReader limit and
                # truncate the trajectory. Raise the limit so full lines survive.
                limit=64 * 1024 * 1024,
            )
            track_process_group(proc.pid)
            # Always drain incrementally. Besides powering the live TUI trace
            # view, this leaves the bytes already received available if a
            # timeout or cancellation happens before the child exits normally.
            io_task = asyncio.create_task(
                self._communicate_streaming(
                    proc,
                    tee_path,
                    stdout_chunks,
                    stderr_chunks,
                    activity_ref,
                )
            )
            last_cpu: list[float] = [_get_pgid_cputime(proc.pid)]
            last_cpu_time: list[float] = [start]

            while True:
                now = time.monotonic()
                time_left = (start + timeout) - now
                if time_left > 0:
                    wait_slice = min(time_left, 5.0)
                else:
                    # Past base timeout: evaluate true process & socket activity
                    stream_active = (now - activity_ref[0]) <= act_win
                    current_cpu = _get_pgid_cputime(proc.pid)
                    if current_cpu > last_cpu[0] + 0.05:
                        last_cpu[0] = current_cpu
                        last_cpu_time[0] = now
                        cpu_active = True
                    else:
                        cpu_active = (now - last_cpu_time[0]) <= act_win
                    socket_active = _has_established_socket(proc.pid)

                    is_active = stream_active or cpu_active or socket_active
                    if not is_active or now >= max_deadline:
                        raise asyncio.TimeoutError()
                    wait_slice = min(max(0.1, act_win - (now - max(activity_ref[0], last_cpu_time[0]))), max(0.1, max_deadline - now), 5.0)

                try:
                    await asyncio.wait_for(asyncio.shield(io_task), timeout=wait_slice)
                    break
                except asyncio.TimeoutError:
                    if io_task.done():
                        break
                    now = time.monotonic()
                    if now >= start + timeout:
                        stream_active = (now - activity_ref[0]) <= act_win
                        current_cpu = _get_pgid_cputime(proc.pid)
                        if current_cpu > last_cpu[0] + 0.05:
                            last_cpu[0] = current_cpu
                            last_cpu_time[0] = now
                            cpu_active = True
                        else:
                            cpu_active = (now - last_cpu_time[0]) <= act_win
                        socket_active = _has_established_socket(proc.pid)
                        if not (stream_active or cpu_active or socket_active) or now >= max_deadline:
                            raise
            return ExecResult(
                stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
                stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
                exit_code=proc.returncode if proc.returncode is not None else -1,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except asyncio.TimeoutError:
            await self._terminate(proc)
            await self._finish_io(io_task)
            captured_stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            timeout_message = f"Command timed out after {timeout}s"
            return ExecResult(
                stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
                stderr=(captured_stderr + "\n" + timeout_message).lstrip("\n"),
                exit_code=-1,
                duration_ms=int((time.monotonic() - start) * 1000),
                termination_reason="timeout",
            )
        except BaseException:
            # Covers Ctrl+C and task cancellation: kill the agent before the
            # exception unwinds, or it keeps running with no parent.
            await self._terminate(proc)
            await self._finish_io(io_task)
            raise
        finally:
            if proc is not None:
                untrack_process_group(proc.pid)

    @staticmethod
    async def _terminate(proc) -> None:
        """SIGTERM the agent's whole group, escalating to SIGKILL if it lingers."""
        if proc is None or proc.returncode is not None:
            return
        signal_process_group(proc.pid, signal.SIGTERM)
        try:
            # Shielded because this often runs while a CancelledError propagates,
            # and an unshielded await would be cancelled before the child exits.
            await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=5))
        except asyncio.TimeoutError:
            signal_process_group(proc.pid, signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=5))
        except asyncio.CancelledError:
            # Cancelled again mid-wait: fall back to the blocking sweep so the
            # agent cannot outlive us.
            kill_process_group(proc.pid)

    @staticmethod
    async def _finish_io(io_task: asyncio.Task[None] | None) -> None:
        if io_task is None or io_task.done():
            return
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(io_task), timeout=5)

    @staticmethod
    async def _communicate_streaming(
        proc,
        tee_path: str | None,
        stdout_chunks: list[bytes],
        stderr_chunks: list[bytes],
        activity_ref: list[float] | None = None,
    ) -> None:
        """Drain both streams while optionally teeing stdout to a live file.

        stdout is written incrementally (one line at a time) so an external
        reader (the TUI's Thinking tab) sees the agent's stream-json
        trajectory grow live. The caller owns the chunk lists, so partial
        output survives even when the wait is interrupted by timeout or
        cancellation.
        """
        path = Path(tee_path) if tee_path else None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

        async def _read_stdout() -> None:
            # Open in binary APPEND mode: the trace is append-only across
            # attempts (v0/runner.py writes a boundary marker between them),
            # so truncating here would wipe the prior attempt's history that
            # the dashboard's chat view is rebuilt from.
            fh = open(path, "ab", buffering=0) if path is not None else None
            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    # Heartbeats from worker threads do not indicate model progress
                    is_heartbeat = b'"type": "heartbeat"' in line or b'"type":"heartbeat"' in line
                    if not is_heartbeat and activity_ref is not None:
                        activity_ref[0] = time.monotonic()
                    stdout_chunks.append(line)
                    if fh is not None:
                        try:
                            fh.write(line)
                        except OSError:
                            pass
            finally:
                if fh is not None:
                    fh.close()

        async def _read_stderr() -> None:
            while True:
                chunk = await proc.stderr.read(64 * 1024)
                if not chunk:
                    return
                if activity_ref is not None:
                    activity_ref[0] = time.monotonic()
                stderr_chunks.append(chunk)

        await asyncio.gather(_read_stdout(), _read_stderr(), proc.wait())

    async def screenshot(self) -> bytes:
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        path = self._tmp_dir / "_kusudaemon_screenshot.png"
        await self.exec(f"gnome-screenshot -f {path} 2>/dev/null || import -window root {path} 2>/dev/null", timeout=10)
        return path.read_bytes() if path.exists() else b""

    async def upload(self, local_path: str, remote_path: str) -> None:
        Path(remote_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, remote_path)

    async def download(self, remote_path: str, local_path: str) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(remote_path, local_path)
