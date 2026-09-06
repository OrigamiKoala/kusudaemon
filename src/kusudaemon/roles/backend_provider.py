"""Agent backend-driven role provider (ROLE-CALLS-VIA-BACKENDS-PLAN.md §1.2).

Executes reasoning and structured-output role calls as one-shot, tool-less CLI
episodes on the configured agent backend (opencode, claude, or codex) instead of
raw HTTP calls.
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any, Callable

from ..adapters.base import EpisodeBudget
from ..environment.base import Environment
from ..environment.local import LocalEnvironment
from ..v0.events import EventLog
from ..v0.run_dir import events_path
from ..v1.json_schema import validate
from ..v1.provider import ProviderError
from .episode_loop import get_episode_loop
from .json_io import (
    _parse_json_object,
    _repair_common_schema_omissions,
    _unwrap_schema_echo,
    build_json_instruction,
    extract_last_json_object,
)
from .protocol import RoleProviderBase


def _flatten_messages(
    messages: list[dict[str, str]],
    schema: dict[str, Any],
) -> str:
    """Flatten structured chat messages and JSON schema into a single prompt string."""
    system_parts: list[str] = [build_json_instruction(schema)]
    conversation_parts: list[dict[str, str]] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            system_parts.append(content)
        else:
            conversation_parts.append({"role": role, "content": content})

    system_text = "\n\n".join(part.strip() for part in system_parts if part.strip())

    if not conversation_parts:
        return system_text

    if len(conversation_parts) == 1 and conversation_parts[0]["role"] == "user":
        user_content = conversation_parts[0]["content"].strip()
        return f"{system_text}\n\n{user_content}"

    formatted_convo: list[str] = []
    for msg in conversation_parts:
        role_label = msg["role"].capitalize()
        formatted_convo.append(f"[{role_label}]:\n{msg['content'].strip()}")

    convo_text = "\n\n".join(formatted_convo)
    return f"{system_text}\n\n---\n\n{convo_text}"


class BackendRoleProvider(RoleProviderBase):
    """Executes role calls as one-shot CLI episodes via the backend adapter."""

    def __init__(
        self,
        *,
        backend: str,
        run_dir: Path | str,
        env: Environment | None = None,
        model: str | None = None,
        budget: EpisodeBudget | None = None,
        timeout: float | None = None,
        max_episode_retries: int = 2,
        log: EventLog | None = None,
        concurrency: int = 4,
        adapter_factory: Callable[[str], AgentAdapter] | None = None,
        role: str = "role",
    ) -> None:
        self.role = role or "role"
        self.backend = str(backend).strip().lower()
        self.run_dir = Path(run_dir)
        self.env = env
        self.model = model or ""
        if budget is not None:
            self.budget = budget
        elif timeout is not None:
            # Explicit caller timeout (PLAN-REVIEW-LATENCY.md T0-6:
            # roles/factory.make_role_provider threads driver._role_provider's
            # 45 s reviewer/triage budget through here). Wins over env.
            self.budget = EpisodeBudget(
                max_duration_seconds=int(timeout), max_output_tokens=2048
            )
        elif self.role in ("reviewer", "triage"):
            timeout_env = os.getenv("KUSUDAEMON_REVIEWER_TIMEOUT") or os.getenv("KUSUDAEMON_ROLE_TIMEOUT")
            default_timeout = int(timeout_env) if timeout_env and timeout_env.isdigit() else 120
            self.budget = EpisodeBudget(max_duration_seconds=default_timeout, max_output_tokens=2048)
        else:
            timeout_env = os.getenv("KUSUDAEMON_ROLE_TIMEOUT")
            default_timeout = int(timeout_env) if timeout_env and timeout_env.isdigit() else 300
            self.budget = EpisodeBudget(max_duration_seconds=default_timeout, max_output_tokens=2048)
        self.max_episode_retries = max_episode_retries
        self._concurrency = concurrency
        self._adapter_factory = adapter_factory
        if log is not None:
            self.log: EventLog | None = log
        else:
            p = events_path(self.run_dir)
            self.log = EventLog(p) if p.parent.exists() else None

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
        """Execute a schema-constrained call via one-shot backend episode(s)."""
        from ..pipeline.backends import build_role_adapter

        base_messages = list(messages)
        last_error = "empty response"
        episode_loop = get_episode_loop(concurrency=self._concurrency)

        # Hoist adapter and env construction out of attempt retry loop (T0-3)
        if self._adapter_factory is not None:
            adapter = self._adapter_factory(self.role)
        else:
            adapter = build_role_adapter(
                backend=self.backend,
                run_dir=self.run_dir,
                phase=self.role,
                model=self.model or None,
                env=self.env,
            )
        env = self.env or LocalEnvironment(tmp_dir=str(self.run_dir / "tmp"))

        for attempt in range(retries + 1):
            if self._should_abort is not None and self._should_abort():
                raise ProviderError("Execution aborted by driver")

            prompt = _flatten_messages(base_messages, schema)
            call_id = f"call_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
            roles_dir = self.run_dir / "roles" / self.role
            roles_dir.mkdir(parents=True, exist_ok=True)
            live_trajectory = roles_dir / f"{call_id}_raw_trajectory.jsonl"

            # Run episode with retries for episode-level failures (timeout/error)
            episode_result = None
            for ep_try in range(self.max_episode_retries + 1):
                if self._should_abort is not None and self._should_abort():
                    raise ProviderError("Execution aborted by driver")

                res = episode_loop.run_coroutine(
                    adapter.run_episode(
                        prompt,
                        env,
                        self.budget,
                        live_trajectory_path=str(live_trajectory),
                    )
                )

                # Observability: forward thinking/reasoning if recorded
                if on_reasoning is not None and live_trajectory.is_file():
                    try:
                        for line in live_trajectory.read_text(encoding="utf-8", errors="replace").splitlines():
                            line = line.strip()
                            if line:
                                rec = json.loads(line)
                                if rec.get("type") in ("thinking", "reasoning") and rec.get("content"):
                                    on_reasoning(str(rec["content"]))
                    except Exception:
                        pass

                if res.status in ("error", "timeout"):
                    if self.log is not None:
                        try:
                            self.log.append(
                                {
                                    "node_id": "-",
                                    "role": "role",
                                    "type": "role_episode_failed",
                                    "backend": self.backend,
                                    "status": res.status,
                                    "error": res.error or "",
                                    "attempt": ep_try + 1,
                                }
                            )
                        except Exception:
                            pass
                    if ep_try < self.max_episode_retries:
                        if "rate limit" in (res.error or "").lower() or (res.actions_log and "rate limit" in res.actions_log.lower()):
                            time.sleep(5 * (ep_try + 1))
                        continue
                    raise ProviderError(
                        f"role episode failed ({res.status}) after {self.max_episode_retries + 1} attempts: {res.error or 'unknown error'}"
                    )
                episode_result = res
                break

            if episode_result is None:
                raise ProviderError(f"role episode failed on backend {self.backend}")

            # Extract output
            content = (episode_result.metadata or {}).get("assistant_visible_output") or ""
            parsed = None
            parse_err = ""
            if content.strip():
                parsed, parse_err = extract_last_json_object(content, schema=schema)
                if parsed is None:
                    parsed, parse_err = _parse_json_object(content)
                    if parsed is not None and parsed.get("type") in {"usage", "logdir", "heartbeat", "thinking", "system", "session_captured"}:
                        parsed = None
            if parsed is None and episode_result.actions_log:
                # Fallback to scanning actions_log for valid JSON matching schema
                parsed, parse_err = extract_last_json_object(episode_result.actions_log, schema=schema)

            try:
                from ..v0.cost import CostLedger
                from ..v0.run_dir import cost_path

                cp = cost_path(self.run_dir)
                if cp.parent.exists():
                    meta = episode_result.metadata or {}
                    pt = int(meta.get("prompt_tokens", 0) or 0)
                    ct = int(meta.get("completion_tokens", 0) or 0)
                    rt = int(meta.get("reasoning_tokens", 0) or 0)
                    CostLedger(cp).record(
                        role="role",
                        phase="role",
                        node="-",
                        model=self.model or self.backend,
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        reasoning_tokens=rt,
                        cost_usd=float(meta["cost_usd"]) if meta.get("cost_usd") is not None else None,
                        prompt_text=prompt if pt == 0 else "",
                        completion_text=(content or episode_result.actions_log) if ct == 0 else "",
                    )
            except Exception:
                pass

            if parsed is not None:
                parsed = _repair_common_schema_omissions(_unwrap_schema_echo(parsed, schema), schema)
                schema_errors = validate(parsed, schema)
                if not schema_errors:
                    return parsed
                last_error = "; ".join(schema_errors)
            else:
                last_error = parse_err or "could not extract JSON object from output"

            # Check for output-cap termination: do not retry repeatedly if capped
            comp_tokens = int((episode_result.metadata or {}).get("completion_tokens", 0) or 0)
            if comp_tokens >= 32000 or (self.budget.max_output_tokens and comp_tokens >= self.budget.max_output_tokens):
                if attempt >= 1:
                    raise ProviderError(
                        f"structured output terminated at output cap ({comp_tokens} tokens): {last_error}"
                    )

            # Reprompt on failure (clean schema restatement, no unbounded message history growth)
            capped_preview = ""
            raw_preview = content or episode_result.actions_log
            if raw_preview:
                words = raw_preview.split()
                if len(words) > 150:
                    capped_preview = f"\n[Output preview: {' '.join(words[:150])}...]"
                else:
                    capped_preview = f"\n[Output preview: {raw_preview}]"

            base_messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        f"That did not validate: {last_error}.{capped_preview}\n\n"
                        "Return only a single valid JSON object matching the schema. No prose, no reasoning, no code fences. "
                        "Do not wrap inside top-level 'properties' or 'type'."
                    ),
                },
            ]

        raise ProviderError(
            f"structured output failed after {retries + 1} attempts: {last_error}"
        )
