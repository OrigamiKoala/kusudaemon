"""Worktree manager for optimistic concurrent leaf execution (PLAN-CONCURRENCY-AND-SHARED-STATE.md §B4).

Features:
- Runs wave members in isolated worktrees (git worktree) or hardlink/copy directories.
- Computes observed change sets post-episode without predicting writes.
- Applies patches sequentially onto trunk.
- Detects conflicts: losers are marked with conflicting hunks to be re-run rebased.
- Caps conflict retries (loser twice runs alone).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class WorktreeManager:
    """Manages isolated workspaces for parallel wave members."""

    def __init__(self, workspace_path: str | Path, runs_dir: str | Path | None = None) -> None:
        self.workspace_path = Path(workspace_path).expanduser().resolve()
        self.runs_dir = Path(runs_dir).expanduser().resolve() if runs_dir else None
        self._is_git = (self.workspace_path / ".git").exists()
        self._worktrees: dict[str, Path] = {}
        self._conflict_counts: dict[str, int] = {}

    @property
    def is_git(self) -> bool:
        return self._is_git

    def conflict_count(self, node_id: str) -> int:
        return self._conflict_counts.get(node_id, 0)

    def record_conflict(self, node_id: str) -> int:
        self._conflict_counts[node_id] = self._conflict_counts.get(node_id, 0) + 1
        return self._conflict_counts[node_id]

    def create_worktree(self, node_id: str) -> Path:
        """Create an isolated worktree or copy for a node."""
        base_tmp = self.runs_dir / "worktrees" if self.runs_dir else self.workspace_path.parent / ".kusudaemon_worktrees"
        base_tmp.mkdir(parents=True, exist_ok=True)
        wt_path = base_tmp / f"wt_{node_id}"

        # Clean up stale worktree if present
        if wt_path.exists():
            self.remove_worktree(node_id)

        if self._is_git:
            try:
                subprocess.run(
                    ["git", "worktree", "add", "--detach", str(wt_path), "HEAD"],
                    cwd=self.workspace_path,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self._worktrees[node_id] = wt_path
                return wt_path
            except subprocess.CalledProcessError:
                pass

        # Fallback to copy / hardlinks for non-git workspaces
        try:
            shutil.copytree(self.workspace_path, wt_path, symlinks=True, ignore=shutil.ignore_patterns(".git", ".kusudaemon*"))
        except Exception:
            wt_path.mkdir(parents=True, exist_ok=True)

        self._worktrees[node_id] = wt_path
        return wt_path

    def compute_patch(self, node_id: str) -> tuple[str, list[str]]:
        """Compute the diff/patch of modified files in node's worktree."""
        wt_path = self._worktrees.get(node_id)
        if not wt_path or not wt_path.exists():
            return "", []

        if self._is_git:
            try:
                # Stage all untracked files to capture new files in diff
                subprocess.run(["git", "add", "-A"], cwd=wt_path, capture_output=True, text=True)
                diff = subprocess.run(
                    ["git", "diff", "--cached", "HEAD"],
                    cwd=wt_path,
                    capture_output=True,
                    text=True,
                ).stdout
                status_out = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=wt_path,
                    capture_output=True,
                    text=True,
                ).stdout
                modified_files = []
                for line in status_out.splitlines():
                    line = line.strip()
                    if line:
                        parts = line.split(maxsplit=1)
                        if len(parts) == 2:
                            modified_files.append(parts[1])
                return diff, modified_files
            except Exception:
                pass

        # Non-git diff computation
        modified_files = []
        for root, _, files in os.walk(wt_path):
            for f in files:
                wt_file = Path(root) / f
                rel = wt_file.relative_to(wt_path)
                trunk_file = self.workspace_path / rel
                if not trunk_file.exists() or trunk_file.read_bytes() != wt_file.read_bytes():
                    modified_files.append(str(rel))
        return "", modified_files

    def apply_patch_to_trunk(self, node_id: str, patch_text: str, modified_files: list[str]) -> tuple[bool, str]:
        """Apply the patch sequentially to trunk.

        Returns (success, conflict_detail).
        """
        if not modified_files and not patch_text:
            return True, ""

        wt_path = self._worktrees.get(node_id)

        if self._is_git and patch_text:
            proc = subprocess.run(
                ["git", "apply", "--3way", "-"],
                cwd=self.workspace_path,
                input=patch_text,
                text=True,
                capture_output=True,
            )
            if proc.returncode == 0:
                # Commit or stage applied node changes for bisectability
                try:
                    subprocess.run(["git", "add", "-A"], cwd=self.workspace_path, capture_output=True)
                    subprocess.run(
                        ["git", "commit", "-m", f"node {node_id} passed"],
                        cwd=self.workspace_path,
                        capture_output=True,
                    )
                except Exception:
                    pass
                return True, ""
            conflict_msg = proc.stderr.strip() or "git apply conflict"
            return False, conflict_msg

        # Non-git or file-copy fallback
        if wt_path and wt_path.exists():
            for rel in modified_files:
                src = wt_path / rel
                dest = self.workspace_path / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.is_file():
                    shutil.copy2(src, dest)
            return True, ""

        return False, "worktree not found"

    def remove_worktree(self, node_id: str) -> None:
        """Clean up worktree for a node."""
        wt_path = self._worktrees.pop(node_id, None)
        if not wt_path:
            base_tmp = self.runs_dir / "worktrees" if self.runs_dir else self.workspace_path.parent / ".kusudaemon_worktrees"
            wt_path = base_tmp / f"wt_{node_id}"

        if not wt_path.exists():
            return

        if self._is_git:
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=self.workspace_path,
                    capture_output=True,
                    text=True,
                )
            except Exception:
                pass

        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)

    def cleanup_all(self) -> None:
        """Remove all active worktrees."""
        for nid in list(self._worktrees.keys()):
            self.remove_worktree(nid)
