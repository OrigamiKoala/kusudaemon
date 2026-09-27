"""Guarantee that agent CLIs die with the harness.

Every agent runs in its own session (``start_new_session=True``) so a single
``killpg`` reaps the CLI plus whatever it spawned. The trade-off is that the
child no longer shares our terminal's process group, so Ctrl+C reaches only the
harness. Without the bookkeeping here, killing the harness would leave a
``claude``/``codex`` process running against the same workspace.

Two layers cover the realistic exit paths:

* ``LocalEnvironment.exec`` kills its own child on timeout and on cancellation.
* The handlers installed here catch what ``exec`` cannot see (SIGTERM, SIGHUP,
  and interpreter shutdown) and sweep any still-tracked group.
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time

_lock = threading.Lock()
_tracked: set[int] = set()
_installed = False


def track_process_group(pid: int) -> None:
    _install_handlers()
    with _lock:
        _tracked.add(pid)


def untrack_process_group(pid: int) -> None:
    with _lock:
        _tracked.discard(pid)


def signal_process_group(pid: int, sig: int) -> bool:
    """Best-effort ``killpg``; False means the group is already gone."""
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def descendant_process_groups(pid: int) -> set[int]:
    """Process groups of everything descended from group ``pid``, except ``pid`` itself.

    A child that starts its own session (``_agent_worker.py`` runs ``opencode``
    that way so it can kill the CLI without killing itself) leaves our group,
    so ``killpg(pid)`` never reaches it. 2026-09-27: an opencode episode
    "timed out" this way kept writing for 50 minutes after its run ended.
    Collect these *before* signalling: once the parent dies, its children are
    reparented to init and the ancestry is gone.
    """
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,pgid="],
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    children: dict[int, list[int]] = {}
    pgid_of: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            p, pp, g = (int(x) for x in parts)
        except ValueError:
            continue
        children.setdefault(pp, []).append(p)
        pgid_of[p] = g
    frontier = [p for p, g in pgid_of.items() if g == pid] or [pid]
    seen = set(frontier)
    groups: set[int] = set()
    while frontier:
        p = frontier.pop()
        for c in children.get(p, ()):
            if c in seen:
                continue
            seen.add(c)
            frontier.append(c)
            g = pgid_of.get(c)
            if g is not None and g != pid and g > 1:
                groups.add(g)
    return groups


def kill_process_group(pid: int, *, grace_seconds: float = 1.0) -> None:
    """SIGTERM the group and its detached descendants, then SIGKILL stragglers.

    Blocking, so it suits signal and atexit handlers. Async callers should
    escalate around ``await proc.wait()`` rather than block the event loop.
    """
    groups = [pid, *sorted(descendant_process_groups(pid))]
    alive = [g for g in groups if signal_process_group(g, signal.SIGTERM)]
    deadline = time.monotonic() + grace_seconds
    while alive and time.monotonic() < deadline:
        # Signal 0 only probes for existence; once the group is gone the CLI has
        # flushed its trajectory and there is nothing left to escalate against.
        alive = [g for g in alive if signal_process_group(g, 0)]
        if alive:
            time.sleep(0.05)
    for g in alive:
        signal_process_group(g, signal.SIGKILL)


def kill_all_tracked() -> None:
    with _lock:
        pids = list(_tracked)
        _tracked.clear()
    for pid in pids:
        kill_process_group(pid)


def _install_handlers() -> None:
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True

    atexit.register(kill_all_tracked)

    # Signal handlers must be installed from the main thread; a harness embedded
    # in someone else's worker thread still gets the atexit sweep.
    if threading.current_thread() is not threading.main_thread():
        return

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous = signal.getsignal(sig)
        except (ValueError, OSError):
            continue
        # Leave a caller-installed handler alone; overriding it would break the
        # embedding application's own shutdown. SIGINT is deliberately absent:
        # Python already raises KeyboardInterrupt for it, which unwinds through
        # `exec` and triggers the per-child kill there.
        if previous is not signal.SIG_DFL:
            continue
        try:
            signal.signal(sig, _terminating_handler)
        except (ValueError, OSError):
            continue


def _terminating_handler(signum, frame):
    kill_all_tracked()
    # Restore the default action so our own exit code stays 128+signum.
    try:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    except (OSError, ValueError):
        sys.exit(128 + signum)
