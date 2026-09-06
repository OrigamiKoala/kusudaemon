from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.adapters.antigravity import AntigravityAdapter
from kusudaemon.adapters.claude_code import ClaudeCodeAdapter
from kusudaemon.adapters.codex import CodexAdapter
from kusudaemon.adapters.opencode import OpenCodeAdapter
from kusudaemon.pipeline.backends import build_role_adapter
from kusudaemon.pipeline.driver import RunOptions
from kusudaemon.roles.backend_provider import BackendRoleProvider
from kusudaemon.roles.factory import make_role_provider, _resolve_role_transport
from kusudaemon.v1.provider import OpenAICompatibleProvider


class RoleAdapterMatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp_path = Path(td.name)

    def test_build_role_adapter_matrix(self) -> None:
        run_dir = self.tmp_path / "run1"

        # opencode
        opencode_adapter = build_role_adapter("opencode", run_dir=run_dir, phase="classify")
        self.assertIsInstance(opencode_adapter, OpenCodeAdapter)
        self.assertEqual(opencode_adapter.workspace_path, str(run_dir / "tmp" / "roles" / "classify"))
        self.assertIn("OPENCODE_PERMISSION", opencode_adapter._env_prefix)
        self.assertIn("deny", opencode_adapter._env_prefix)

        # antigravity
        antigravity_adapter = build_role_adapter("antigravity", run_dir=run_dir, phase="classify")
        self.assertIsInstance(antigravity_adapter, AntigravityAdapter)
        self.assertEqual(antigravity_adapter.workspace_path, str(run_dir / "tmp" / "roles" / "classify"))
        self.assertIn("--sandbox", antigravity_adapter.command_template)

        # claude
        claude_adapter = build_role_adapter("claude", run_dir=run_dir, phase="plan")
        self.assertIsInstance(claude_adapter, ClaudeCodeAdapter)
        self.assertEqual(claude_adapter.workspace_path, str(run_dir / "tmp" / "roles" / "plan"))
        self.assertIn("--disallowedTools", claude_adapter._claude_parts)

        # codex
        codex_adapter = build_role_adapter("codex", run_dir=run_dir, phase="survey")
        self.assertIsInstance(codex_adapter, CodexAdapter)
        self.assertEqual(codex_adapter.workspace_path, str(run_dir / "tmp" / "roles" / "survey"))
        self.assertIn("--sandbox", codex_adapter.command_template)
        self.assertIn("read-only", codex_adapter.command_template)

    def test_resolve_role_transport(self) -> None:
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in (
                "OPENAI_API_KEY",
                "NVIDIA_API_KEY",
                "OPENCODE_API_KEY",
                "ANTHROPIC_API_KEY",
                "KUSUDAEMON_PROVIDER_API_KEY",
                "KUSUDAEMON_ROLE_TRANSPORT",
                "KUSUDAEMON_ROLE_BACKEND",
            )
        }
        patcher = mock.patch.dict(os.environ, clean_env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        empty_cfg = self.tmp_path / "empty_provider.json"
        empty_cfg.write_text("{}", encoding="utf-8")

        # Default auto with gptme backend -> http
        self.assertEqual(_resolve_role_transport("gptme", config_path=empty_cfg), ("gptme", "http"))

        # Default auto with opencode backend -> backend
        self.assertEqual(_resolve_role_transport("opencode", config_path=empty_cfg), ("opencode", "backend"))

        # Env override
        with mock.patch.dict(os.environ, {"KUSUDAEMON_ROLE_TRANSPORT": "backend"}):
            self.assertEqual(_resolve_role_transport("gptme", config_path=empty_cfg), ("gptme", "backend"))

        with mock.patch.dict(os.environ, {"KUSUDAEMON_ROLE_TRANSPORT": "http"}):
            self.assertEqual(_resolve_role_transport("opencode", config_path=empty_cfg), ("opencode", "http"))

    def test_make_role_provider(self) -> None:
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in (
                "OPENAI_API_KEY",
                "NVIDIA_API_KEY",
                "OPENCODE_API_KEY",
                "ANTHROPIC_API_KEY",
                "KUSUDAEMON_PROVIDER_API_KEY",
                "KUSUDAEMON_ROLE_TRANSPORT",
                "KUSUDAEMON_ROLE_BACKEND",
            )
        }
        patcher = mock.patch.dict(os.environ, clean_env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        run_dir = self.tmp_path / "run_make"

        # Direct: with backend='opencode', make_role_provider produces BackendRoleProvider
        options = RunOptions(backend="opencode")
        provider = make_role_provider(options=options, run_dir=run_dir)
        self.assertIsInstance(provider, BackendRoleProvider)
        self.assertEqual(provider.backend, "opencode")

        # Direct: with backend='gptme', make_role_provider produces OpenAICompatibleProvider
        options_gptme = RunOptions(backend="gptme")
        provider_gptme = make_role_provider(options=options_gptme, run_dir=run_dir)
        self.assertIsInstance(provider_gptme, OpenAICompatibleProvider)

        # Lazy: with lazy=True, make_role_provider produces LazyRoleProvider
        lazy_provider = make_role_provider(options=options, run_dir=run_dir, lazy=True)
        underlying = lazy_provider._resolve()
        self.assertIsInstance(underlying, BackendRoleProvider)
        self.assertEqual(underlying.backend, "opencode")


if __name__ == "__main__":
    unittest.main()
