from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.roles.protocol import RoleProvider, RoleProviderBase
from kusudaemon.roles.json_io import _parse_json_object, build_json_instruction, extract_last_json_object
from kusudaemon.roles.factory import LazyRoleProvider
from kusudaemon.roles.backend_provider import BackendRoleProvider
from kusudaemon.v1.provider import OpenAICompatibleProvider


class _DummyScriptedProvider:
    model: str = "dummy-model"

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
        retries: int = 2,
        on_reasoning: Any = None,
        streaming: bool = False,
    ) -> dict[str, Any]:
        return {"action": "dispatch", "reason": "ok"}


class RoleProviderProtocolTest(unittest.TestCase):
    def test_role_provider_protocol_runtime_check(self) -> None:
        scripted = _DummyScriptedProvider()
        self.assertIsInstance(scripted, RoleProvider)

        openai_p = OpenAICompatibleProvider(model="test-model")
        self.assertIsInstance(openai_p, RoleProvider)
        self.assertIsInstance(openai_p, RoleProviderBase)

        lazy_p = LazyRoleProvider(lambda: openai_p)
        self.assertIsInstance(lazy_p, RoleProvider)
        self.assertIsInstance(lazy_p, RoleProviderBase)

    def test_role_provider_base_hooks(self) -> None:
        base = RoleProviderBase()
        halted = False
        base.set_abort_hook(lambda: halted)
        self.assertIsNotNone(base._should_abort)
        self.assertFalse(base._should_abort())

        events: list[dict[str, Any]] = []
        base.set_event_hook(lambda from_m, to_m, r: events.append({"from": from_m, "to": to_m, "reason": r}))
        self.assertIsNotNone(base._on_model_fallback)
        base._on_model_fallback("a", "b", "rate limit")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], {"from": "a", "to": "b", "reason": "rate limit"})

    def test_json_io_helpers(self) -> None:
        schema = {
            "type": "object",
            "required": ["status", "count"],
            "properties": {
                "status": {"type": "string"},
                "count": {"type": "integer"},
            },
        }
        instruction = build_json_instruction(schema)
        self.assertIn("JSON object", instruction)
        self.assertIn('"status"', instruction)

        raw = 'Some preface text\n```json\n{"status": "ok", "count": 42}\n```\nTrailing text'
        parsed, err = extract_last_json_object(raw)
        self.assertEqual(parsed, {"status": "ok", "count": 42})
        self.assertEqual(err, "")

        obj, err = _parse_json_object('{"status": "ok", "count": 42}')
        self.assertEqual(obj, {"status": "ok", "count": 42})
        self.assertEqual(err, "")

        bad_obj, bad_err = _parse_json_object("[1, 2, 3]")
        self.assertIsNone(bad_obj)
        self.assertIn("not a JSON object", bad_err)

        # Test trailing usage records are skipped
        log_with_usage = (
            '{"items": ["a", "b"], "verdict": "pass"}\n'
            '{"type": "usage", "prompt_tokens": 100, "completion_tokens": 20, "reasoning_tokens": 0, "total_tokens": 120}'
        )
        test_schema = {"type": "object", "required": ["items", "verdict"], "properties": {"items": {"type": "array"}, "verdict": {"type": "string"}}}
        parsed_usage, err_usage = extract_last_json_object(log_with_usage, schema=test_schema)
        self.assertEqual(parsed_usage, {"items": ["a", "b"], "verdict": "pass"})
        self.assertEqual(err_usage, "")

        # Extra data on line 2 (e.g. char 110 error case)
        extra_data = '{"status": "ok", "count": 42}\nExtra data: line 2 column 1'
        obj_extra, err_extra = _parse_json_object(extra_data)
        self.assertEqual(obj_extra, {"status": "ok", "count": 42})
        self.assertEqual(err_extra, "")

        # Fenced JSON with trailing prose
        fenced_trailing = '```json\n{"status": "ok", "count": 42}\n```\nHere is some explanation.'
        obj_fenced, err_fenced = _parse_json_object(fenced_trailing)
        self.assertEqual(obj_fenced, {"status": "ok", "count": 42})
        self.assertEqual(err_fenced, "")

        # Braces inside string values
        braces_str = '{"defect": "formula {x} is invalid", "verdict": "fail"}'
        obj_braces, err_braces = extract_last_json_object(f"Preamble\n{braces_str}\nPostamble")
        self.assertEqual(obj_braces, {"defect": "formula {x} is invalid", "verdict": "fail"})
        self.assertEqual(err_braces, "")

    def test_openai_provider_complete_json_with_extra_data(self) -> None:
        def mock_transport(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"status": "ok", "count": 42}\nExtra data: line 2 column 1 (char 110)',
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

        p = OpenAICompatibleProvider(model="test-model", transport=mock_transport)
        schema = {
            "type": "object",
            "required": ["status", "count"],
            "properties": {
                "status": {"type": "string"},
                "count": {"type": "integer"},
            },
        }
        result = p.complete_json([{"role": "user", "content": "hello"}], schema=schema)
        self.assertEqual(result, {"status": "ok", "count": 42})


if __name__ == "__main__":
    unittest.main()
