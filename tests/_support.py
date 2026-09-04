"""Shared test support fixtures and helpers for kusudaemon tests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.adapters.base import AgentAdapter
from kusudaemon.environment.base import Environment
from kusudaemon.types import EpisodeBudget, EpisodeResult
from kusudaemon.v0.run_dir import create_run_dir, node_artifact_path
from kusudaemon.v1.json_schema import validate


class FakeProvider:
    """Fake OpenAICompatibleProvider/RoleProvider for tests.

    Returns pre-scripted structured-output responses in order, and validates
    each one against the schema it was asked for before handing it back.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []

    def complete_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
        retries: int = 2,
        on_reasoning: Callable[[str], None] | None = None,
        streaming: bool = False,
    ) -> dict[str, Any]:
        self.calls.append((messages, schema))
        if not self._responses:
            raise AssertionError(
                f"FakeProvider ran out of canned responses (call #{len(self.calls)})"
            )
        response = self._responses.pop(0)
        errors = validate(response, schema)
        assert not errors, f"canned response {response!r} does not match schema: {errors}"
        return response


class _InMemoryWriterAdapter(AgentAdapter):
    """Writes fixed content directly to the node's artifact path without
    shelling out to anything."""

    has_file_tools = True
    supports_session_resume = False

    def __init__(self, artifact_path: Path, content: str) -> None:
        self._artifact_path = artifact_path
        self._content = content

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        live_trajectory_path: Path | None = None,
        **kwargs: Any,
    ) -> EpisodeResult:
        self._artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self._artifact_path.write_text(self._content, encoding="utf-8")
        return EpisodeResult(status="done", actions_log="", duration_ms=1, metadata={})


def in_memory_writer_factory(
    run_dir: Path,
    content: str = "a small, real artifact body.\n",
) -> Callable[[Any], _InMemoryWriterAdapter]:
    def factory(node: Any) -> _InMemoryWriterAdapter:
        return _InMemoryWriterAdapter(node_artifact_path(run_dir, node.id), content)

    return factory


def make_temp_run_dir(parent: Path | None = None, name: str = "run") -> Path:
    """Create a temporary run directory populated with basic run structure."""
    if parent is None:
        parent = Path(tempfile.mkdtemp())
    run_dir = parent / name
    create_run_dir(parent, name)
    return run_dir
