"""Read-mostly helpers behind the dashboard's git, file and CLI-job routes.

Everything here is additive: ``server.py`` registers the routes, this module
holds the logic so ``RunState`` and the existing routes keep their shape.

* **git** -- status / diff / log for a run's workspace root (``workspace_root``
  in ``run.spec.json``). Read-only: only ``git status|diff|log|show|grep`` are
  ever executed, always with ``--no-pager`` and a timeout.
* **files** -- directory listing, bounded text reads and a bounded text search
  under one of two roots: the run directory or the run's workspace. Every path
  is resolved and must stay inside its root (symlink escapes included).
* **cli jobs** -- the CLI verbs with no dedicated dashboard route (``status``,
  ``resync``, ``cache clear``, ``eval``, ``bench``) run as allow-listed
  subprocesses whose output is tailed to the browser. Arguments are built from
  structured fields, never from a shell string.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

GIT_TIMEOUT_S = 15
MAX_DIFF_BYTES = 600_000
MAX_FILE_BYTES = 400_000
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_SEARCH_HITS = 200
SEARCH_BUDGET_S = 4.0
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".pytest_cache"}


# ---------------------------------------------------------------------------
# roots and path safety
# ---------------------------------------------------------------------------
def workspace_root(run_dir: Path) -> Path | None:
    """The run's workspace directory, or None for corpus/greenfield runs."""
    try:
        spec = json.loads((run_dir / "run.spec.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = spec.get("workspace_root") if isinstance(spec, dict) else None
    if not raw:
        return None
    root = Path(str(raw)).expanduser()
    return root.resolve() if root.is_dir() else None


def resolve_root(run_dir: Path, which: str) -> Path | None:
    if which == "workspace":
        return workspace_root(run_dir)
    return run_dir.resolve() if run_dir.is_dir() else None


def safe_join(root: Path, rel: str) -> Path | None:
    """``root / rel`` resolved, or None when it escapes ``root``."""
    rel = (rel or "").lstrip("/")
    if "\x00" in rel:
        return None
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str]:
    cmd = ["git", "--no-pager", "-c", "core.quotepath=false", "-C", str(root), *args]
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace")


def is_git_repo(root: Path) -> bool:
    code, out = _git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


def git_status(root: Path) -> dict[str, Any]:
    if not is_git_repo(root):
        return {"repo": False, "root": str(root)}
    _, head = _git(root, "rev-parse", "--short", "HEAD")
    code, branch_out = _git(root, "status", "--porcelain=v1", "-b", "-z", "--untracked-files=all")
    entries = [e for e in branch_out.split("\x00") if e]
    branch, upstream, ahead, behind = "", "", 0, 0
    files: dict[str, dict[str, Any]] = {}
    skip_next = False
    for i, entry in enumerate(entries):
        if skip_next:
            skip_next = False
            continue
        if entry.startswith("## "):
            head_line = entry[3:]
            m = re.match(r"^(?P<b>[^. ]+)(?:\.\.\.(?P<u>\S+))?(?: \[(?P<t>.*)\])?$", head_line)
            if m:
                branch = m.group("b")
                upstream = m.group("u") or ""
                track = m.group("t") or ""
                am = re.search(r"ahead (\d+)", track)
                bm = re.search(r"behind (\d+)", track)
                ahead = int(am.group(1)) if am else 0
                behind = int(bm.group(1)) if bm else 0
            else:
                branch = head_line
            continue
        xy, path = entry[:2], entry[3:]
        if xy[0] in "RC":
            skip_next = True  # the next NUL field is the original path
        files[path] = {
            "path": path,
            "index": xy[0].strip(),
            "worktree": xy[1].strip(),
            "untracked": xy == "??",
            "added": 0,
            "deleted": 0,
        }
    for flag in ("--cached", None):
        args = ["diff", "--numstat", "-z"] + ([flag] if flag else [])
        _, numstat = _git(root, *args)
        for rec in numstat.split("\x00"):
            m = re.match(r"^(\d+|-)\t(\d+|-)\t(.*)$", rec)
            if not m or m.group(3) not in files:
                continue
            f = files[m.group(3)]
            f["added"] += int(m.group(1)) if m.group(1) != "-" else 0
            f["deleted"] += int(m.group(2)) if m.group(2) != "-" else 0
    return {
        "repo": True,
        "root": str(root),
        "branch": branch,
        "upstream": upstream,
        "head": head.strip(),
        "ahead": ahead,
        "behind": behind,
        "files": sorted(files.values(), key=lambda f: f["path"]),
    }


def git_log(root: Path, limit: int = 40) -> list[dict[str, Any]]:
    if not is_git_repo(root):
        return []
    limit = max(1, min(int(limit), 200))
    sep = "\x1f"
    code, out = _git(root, "log", f"-n{limit}", f"--pretty=format:%H{sep}%h{sep}%an{sep}%at{sep}%s")
    if code != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(sep)
        if len(parts) == 5:
            rows.append(
                {"sha": parts[0], "short": parts[1], "author": parts[2], "ts": int(parts[3] or 0), "subject": parts[4]}
            )
    return rows


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


def parse_unified_diff(text: str) -> list[dict[str, Any]]:
    """Unified diff -> ``[{path, old_path, status, added, deleted, binary, hunks}]``;
    each hunk holds ``lines: [{kind: add|remove|context|meta, old, new, text}]``."""
    files: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    hunk: dict[str, Any] | None = None
    old_n = new_n = 0
    for raw in text.split("\n"):
        if raw.startswith("diff --git "):
            m = re.match(r"^diff --git a/(.*) b/(.*)$", raw)
            cur = {
                "path": m.group(2) if m else raw[11:],
                "old_path": m.group(1) if m else "",
                "status": "modified",
                "added": 0,
                "deleted": 0,
                "binary": False,
                "hunks": [],
            }
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if hunk is None:
            if raw.startswith("new file mode"):
                cur["status"] = "added"
            elif raw.startswith("deleted file mode"):
                cur["status"] = "deleted"
            elif raw.startswith("rename from "):
                cur["status"] = "renamed"
                cur["old_path"] = raw[12:]
            elif raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
                cur["binary"] = True
            elif raw.startswith("+++ ") and raw != "+++ /dev/null":
                cur["path"] = raw[6:] if raw.startswith("+++ b/") else raw[4:]
        m = _HUNK_RE.match(raw)
        if m:
            old_n, new_n = int(m.group(1)), int(m.group(3))
            hunk = {"header": raw, "context": m.group(5), "lines": []}
            cur["hunks"].append(hunk)
            continue
        if hunk is None:
            continue
        if raw.startswith("+"):
            hunk["lines"].append({"kind": "add", "old": None, "new": new_n, "text": raw[1:]})
            new_n += 1
            cur["added"] += 1
        elif raw.startswith("-"):
            hunk["lines"].append({"kind": "remove", "old": old_n, "new": None, "text": raw[1:]})
            old_n += 1
            cur["deleted"] += 1
        elif raw.startswith("\\"):
            hunk["lines"].append({"kind": "meta", "old": None, "new": None, "text": raw})
        elif raw.startswith(" "):
            hunk["lines"].append({"kind": "context", "old": old_n, "new": new_n, "text": raw[1:]})
            old_n += 1
            new_n += 1
    return files


def git_diff(root: Path, *, path: str = "", staged: bool = False, ref: str = "") -> dict[str, Any]:
    if not is_git_repo(root):
        return {"repo": False, "files": []}
    if ref and not re.fullmatch(r"[0-9A-Za-z._/~^-]{1,100}", ref):
        return {"repo": True, "files": [], "error": "invalid ref"}
    if path and safe_join(root, path) is None:
        return {"repo": True, "files": [], "error": "invalid path"}
    if ref:
        args = ["show", "--format=", "-U3", ref]
    else:
        args = ["diff", "-U3"] + (["--cached"] if staged else [])
    args += ["--", path] if path else []
    code, out = _git(root, *args)
    untracked_text = ""
    if not ref and not staged and path:
        full = safe_join(root, path)
        if full is not None and full.is_file() and not out.strip():
            code2, tracked = _git(root, "ls-files", "--error-unmatch", "--", path)
            if code2 != 0:  # untracked: render as an all-added file
                _, untracked_text = _git(root, "diff", "--no-index", "--", os.devnull, str(full))
                shown = str(full).lstrip("/")  # git prints the absolute path without its leading slash
                untracked_text = untracked_text.replace(f"a/{shown}", f"a/{path}").replace(f"b/{shown}", f"b/{path}")
    text = out or untracked_text
    truncated = len(text.encode("utf-8", errors="replace")) > MAX_DIFF_BYTES
    if truncated:
        text = text[:MAX_DIFF_BYTES]
    return {"repo": True, "files": parse_unified_diff(text), "truncated": truncated}


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------
def list_dir(root: Path, rel: str = "") -> dict[str, Any] | None:
    target = safe_join(root, rel)
    if target is None or not target.is_dir():
        return None
    entries = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return None
    for child in children:
        if child.name in _SKIP_DIRS:
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size": 0 if child.is_dir() else st.st_size,
                "mtime": st.st_mtime,
            }
        )
    base = root.resolve()
    return {"path": "" if target == base else str(target.relative_to(base)), "entries": entries}


def read_file(root: Path, rel: str, *, limit: int = MAX_FILE_BYTES) -> dict[str, Any] | None:
    target = safe_join(root, rel)
    if target is None or not target.is_file():
        return None
    try:
        st = target.stat()
        with target.open("rb") as fh:
            blob = fh.read(limit + 1)
    except OSError:
        return None
    truncated = len(blob) > limit
    blob = blob[:limit]
    binary = b"\x00" in blob[:8192]
    return {
        "path": str(target.relative_to(root.resolve())),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "binary": binary,
        "truncated": truncated,
        "text": "" if binary else blob.decode("utf-8", errors="replace"),
    }


def search_text(root: Path, query: str, *, limit: int = MAX_SEARCH_HITS) -> dict[str, Any]:
    """Case-insensitive literal search over text files under ``root``."""
    needle = query.strip().lower()
    if len(needle) < 2:
        return {"hits": [], "truncated": False}
    deadline = time.monotonic() + SEARCH_BUDGET_S
    hits: list[dict[str, Any]] = []
    truncated = False
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if time.monotonic() > deadline or len(hits) >= limit:
                truncated = True
                break
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                blob = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in blob[:4096]:
                continue
            for n, line in enumerate(blob.decode("utf-8", errors="replace").splitlines(), 1):
                if needle in line.lower():
                    hits.append({"path": str(path.relative_to(root)), "line": n, "text": line.strip()[:240]})
                    if len(hits) >= limit:
                        break
        if truncated:
            break
    return {"hits": hits, "truncated": truncated}


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------
def cost_breakdown(run_dir: Path) -> dict[str, Any]:
    """Ledger rows (``cost.jsonl``) aggregated by role / phase / model / node,
    plus a cumulative series for the cost meter."""
    from ..v0.cost import CostLedger
    from ..v0.run_dir import cost_path

    path = cost_path(run_dir)
    rows = CostLedger(path).read_all() if path.exists() else []
    groups: dict[str, dict[str, dict[str, Any]]] = {"role": {}, "phase": {}, "model": {}, "node": {}}
    totals = {"prompt": 0, "completion": 0, "reasoning": 0, "cost": 0.0, "calls": 0, "cached": 0}
    series: list[dict[str, Any]] = []
    cum_tokens = 0
    cum_cost = 0.0
    for r in sorted(rows, key=lambda x: x.get("ts") or 0):
        p = int(r.get("prompt_tokens") or 0)
        c = int(r.get("completion_tokens") or 0)
        rs = int(r.get("reasoning_tokens") or 0)
        usd = float(r.get("cost_usd") or 0.0)
        totals["prompt"] += p
        totals["completion"] += c
        totals["reasoning"] += rs
        totals["cost"] += usd
        totals["calls"] += 1
        totals["cached"] += 1 if r.get("cached") else 0
        for dim, key in (("role", "role"), ("phase", "phase"), ("model", "model"), ("node", "node")):
            name = str(r.get(key) or "-")
            g = groups[dim].setdefault(name, {"name": name, "prompt": 0, "completion": 0, "reasoning": 0, "cost": 0.0, "calls": 0})
            g["prompt"] += p
            g["completion"] += c
            g["reasoning"] += rs
            g["cost"] += usd
            g["calls"] += 1
        cum_tokens += p + c + rs
        cum_cost += usd
        series.append({"ts": r.get("ts") or 0, "tokens": cum_tokens, "cost": round(cum_cost, 6)})
    if len(series) > 240:
        step = len(series) / 240.0
        series = [series[int(i * step)] for i in range(240)] + [series[-1]]
    out = {dim: sorted(g.values(), key=lambda x: -(x["prompt"] + x["completion"] + x["reasoning"])) for dim, g in groups.items()}
    return {"totals": {**totals, "cost": round(totals["cost"], 6)}, "groups": out, "series": series}


# ---------------------------------------------------------------------------
# CLI jobs
# ---------------------------------------------------------------------------
class CliJobs:
    """Allow-listed ``kusudaemon <verb>`` subprocesses with tail-able logs."""

    def __init__(self, runs_root: Path | None) -> None:
        self.runs_root = runs_root
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._procs: dict[str, subprocess.Popen] = {}

    # -- argv builders: structured input in, argv out (no shell strings) ----
    def _build(self, command: str, args: dict[str, Any], run_id: str) -> list[str]:
        root = str(self.runs_root) if self.runs_root else None
        rr = ["--runs-root", root] if root else []

        def s(key: str) -> str:
            return str(args.get(key) or "").strip()

        if command == "status":
            if not run_id:
                raise ValueError("run_id is required")
            argv = ["status", run_id, *rr]
            if args.get("brief"):
                argv.append("--brief")
            return argv
        if command == "resync":
            if not run_id or not s("source"):
                raise ValueError("run_id and source are required")
            return ["resync", run_id, "--source", s("source"), *rr]
        if command == "cache-clear":
            return ["cache", "clear", *rr]
        if command == "eval":
            argv = ["eval"]
            if s("task"):
                argv += ["--task", s("task")]
            if s("runs"):
                argv += ["--runs", str(int(s("runs")))]
            return argv
        if command == "bench":
            if not (s("goal") or s("goal_file")):
                raise ValueError("goal or goal_file is required")
            argv = ["bench", *rr]
            for key, flag in (
                ("goal", "--goal"), ("goal_file", "--goal-file"), ("workspace", "--workspace"),
                ("backend", "--backend"), ("model", "--model"), ("provider", "--provider"),
                ("arm", "--arm"), ("task_id", "--task-id"), ("output", "--output"),
                ("max_parallel", "--max-parallel"), ("tier", "--tier"), ("budget_tokens", "--budget-tokens"),
                ("benchmark", "--benchmark"), ("artifact_delimiter", "--artifact-delimiter"),
                ("max_rounds", "--max-rounds"), ("max_attempts", "--max-attempts"),
                ("output_dir", "--output-dir"), ("wall_clock_budget", "--wall-clock-budget"),
            ):
                if s(key):
                    argv += [flag, s(key)]
            if s("seed"):
                argv += ["--seed", str(int(s("seed")))]
            if args.get("json"):
                argv.append("--json")
            return argv
        raise ValueError(f"unsupported command: {command}")

    def start(self, command: str, args: dict[str, Any], run_id: str = "") -> dict[str, Any]:
        if run_id and not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", run_id):
            raise ValueError("invalid run_id")
        argv = self._build(command, args or {}, run_id)
        job_id = uuid.uuid4().hex[:10]
        log_dir = (self.runs_root or Path.home() / ".kusudaemon") / "_cli"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job_id}.log"
        cmd = [sys.executable, "-c", "import sys; from kusudaemon.cli import main; sys.exit(main(sys.argv[1:]))", *argv]
        log_fh = log_path.open("wb")
        # run against this very source tree, never a stale editable install
        env = dict(os.environ)
        src_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = src_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        proc = subprocess.Popen(
            cmd, stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, env=env
        )
        log_fh.close()
        record = {
            "id": job_id,
            "command": command,
            "run_id": run_id,
            "argv": argv,
            "started": time.time(),
            "ended": None,
            "status": "running",
            "exit_code": None,
            "log": str(log_path),
        }
        with self._lock:
            self._jobs[job_id] = record
            self._procs[job_id] = proc
        threading.Thread(target=self._reap, args=(job_id,), daemon=True).start()
        return self._public(record)

    def _reap(self, job_id: str) -> None:
        proc = self._procs.get(job_id)
        if proc is None:
            return
        code = proc.wait()
        with self._lock:
            rec = self._jobs[job_id]
            rec["ended"] = time.time()
            rec["exit_code"] = code
            if rec["status"] == "running":
                rec["status"] = "done" if code == 0 else "failed"
            self._procs.pop(job_id, None)

    @staticmethod
    def _public(rec: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in rec.items() if k != "log"}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public(r) for r in sorted(self._jobs.values(), key=lambda r: -r["started"])]

    def tail(self, job_id: str, since: int = 0) -> dict[str, Any] | None:
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return None
            public = self._public(rec)
            log_path = Path(rec["log"])
        try:
            with log_path.open("rb") as fh:
                fh.seek(max(0, since))
                chunk = fh.read(200_000)
                end = fh.tell()
        except OSError:
            chunk, end = b"", since
        return {**public, "text": chunk.decode("utf-8", errors="replace"), "next": end}

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(job_id)
            rec = self._jobs.get(job_id)
            if proc is None or rec is None or proc.poll() is not None:
                return False
            rec["status"] = "cancelled"
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            proc.terminate()
        return True


def have_git() -> bool:
    return shutil.which("git") is not None
