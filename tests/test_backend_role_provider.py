from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.adapters.base import AgentAdapter
from kusudaemon.environment.base import Environment
from kusudaemon.roles.backend_provider import BackendRoleProvider, _flatten_messages
from kusudaemon.roles.protocol import RoleProvider
from kusudaemon.types import EpisodeBudget, EpisodeResult
from kusudaemon.v1.provider import ProviderError


class _FakeRoleAdapter(AgentAdapter):
    def __init__(self, responses: list[str | Exception]):
        self.responses = list(responses)
        self.prompts_received: list[str] = []

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        **kwargs: Any,
    ) -> EpisodeResult:
        self.prompts_received.append(prompt)
        if not self.responses:
            return EpisodeResult(
                status="error",
                error="no more responses",
            )
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return EpisodeResult(
            status="done",
            actions_log=resp,
        )


class _DummyEnv(Environment):
    def read_file(self, path: str) -> str:
        return ""

    def write_file(self, path: str, content: str) -> None:
        pass

    def run_command(self, cmd: str, timeout: int = 30) -> tuple[int, str, str]:
        return 0, "", ""


class BackendRoleProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp_path = Path(td.name)

    def test_flatten_messages(self) -> None:
        messages = [
            {"role": "system", "content": "You are a classifier."},
            {"role": "user", "content": "Hello, classify this."},
            {"role": "assistant", "content": "Sure, what is it?"},
            {"role": "user", "content": "Task A."},
        ]
        prompt = _flatten_messages(messages, {"type": "object"})
        self.assertIn("JSON object", prompt)
        self.assertIn("You are a classifier.", prompt)
        self.assertIn("Hello, classify this.", prompt)
        self.assertIn("[Assistant]", prompt)
        self.assertIn("Sure, what is it?", prompt)
        self.assertIn("Task A.", prompt)

    def test_backend_role_provider_success(self) -> None:
        valid_json = json.dumps({"action": "dispatch", "reason": "single ready node"})
        adapter = _FakeRoleAdapter([valid_json])
        provider = BackendRoleProvider(
            backend="opencode",
            run_dir=self.tmp_path,
            env=_DummyEnv(),
            adapter_factory=lambda phase: adapter,
            model="test-model",
        )
        self.assertIsInstance(provider, RoleProvider)
        self.assertEqual(provider.model, "test-model")

        schema = {
            "type": "object",
            "required": ["action", "reason"],
            "properties": {
                "action": {"type": "string"},
                "reason": {"type": "string"},
            },
        }
        result = provider.complete_json(
            [{"role": "user", "content": "What next?"}],
            schema=schema,
        )
        self.assertEqual(result, {"action": "dispatch", "reason": "single ready node"})
        self.assertEqual(len(adapter.prompts_received), 1)

    def test_backend_role_provider_retry_on_invalid_json(self) -> None:
        invalid = "I cannot output JSON directly, but here is my thought..."
        valid_json = json.dumps({"action": "dispatch", "reason": "corrected"})
        adapter = _FakeRoleAdapter([invalid, valid_json])
        provider = BackendRoleProvider(
            backend="opencode",
            run_dir=self.tmp_path,
            env=_DummyEnv(),
            adapter_factory=lambda phase: adapter,
            model="test-model",
        )

        schema = {
            "type": "object",
            "required": ["action", "reason"],
        }
        result = provider.complete_json(
            [{"role": "user", "content": "What next?"}],
            schema=schema,
            retries=2,
        )
        self.assertEqual(result, {"action": "dispatch", "reason": "corrected"})
        self.assertEqual(len(adapter.prompts_received), 2)
        self.assertIn("did not validate", adapter.prompts_received[1])

    def test_backend_role_provider_exhausted_retries(self) -> None:
        invalid1 = "Not json 1"
        invalid2 = "Not json 2"
        adapter = _FakeRoleAdapter([invalid1, invalid2])
        provider = BackendRoleProvider(
            backend="opencode",
            run_dir=self.tmp_path,
            env=_DummyEnv(),
            adapter_factory=lambda phase: adapter,
            model="test-model",
        )

        schema = {"type": "object", "required": ["action"]}
        with self.assertRaises(ProviderError) as exc_info:
            provider.complete_json(
                [{"role": "user", "content": "What next?"}],
                schema=schema,
                retries=1,
            )
        self.assertIn("structured output failed after", str(exc_info.exception))

    def test_backend_role_provider_abort(self) -> None:
        adapter = _FakeRoleAdapter([json.dumps({"action": "dispatch"})])
        provider = BackendRoleProvider(
            backend="opencode",
            run_dir=self.tmp_path,
            env=_DummyEnv(),
            adapter_factory=lambda phase: adapter,
        )
        provider.set_abort_hook(lambda: True)

        with self.assertRaisesRegex(ProviderError, "aborted"):
            provider.complete_json(
                [{"role": "user", "content": "What next?"}],
                schema={"type": "object"},
            )


if __name__ == "__main__":
    unittest.main()
