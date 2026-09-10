"""Role provider factory and lazy proxy (ROLE-CALLS-VIA-BACKENDS-PLAN.md §1.1 & §1.7).

Constructs the appropriate RoleProvider (OpenAICompatibleProvider or BackendRoleProvider)
based on backend selection, provider.json configuration, and environment overrides.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from ..environment.base import Environment
from ..provider_config import config_file_path, read_config_file
from ..v0.events import EventLog
from .backend_provider import BackendRoleProvider
from .protocol import RoleProvider, RoleProviderBase


class LazyRoleProvider(RoleProviderBase):
    """Lazy proxy that defers provider instantiation until first call or property access."""

    def __init__(self, factory: Callable[[], RoleProvider]) -> None:
        self._factory = factory
        self._instance: RoleProvider | None = None
        self._pending_abort: Callable[[], bool] | None = None
        self._pending_event: Callable[[str, str, str], None] | None = None
        self._pending_backoff: Callable[[int, float], None] | None = None

    def _get_instance(self) -> RoleProvider:
        if self._instance is None:
            self._instance = self._factory()
            if self._pending_abort is not None and hasattr(self._instance, "set_abort_hook"):
                self._instance.set_abort_hook(self._pending_abort)
            if self._pending_event is not None and hasattr(self._instance, "set_event_hook"):
                self._instance.set_event_hook(self._pending_event)
            if self._pending_backoff is not None and hasattr(self._instance, "set_backoff_hook"):
                self._instance.set_backoff_hook(self._pending_backoff)
        return self._instance

    def _resolve(self) -> RoleProvider:
        return self._get_instance()

    @property
    def model(self) -> str:
        return self._get_instance().model

    @model.setter
    def model(self, value: str) -> None:
        self._get_instance().model = value

    def set_abort_hook(self, should_abort: Callable[[], bool] | None) -> None:
        self._pending_abort = should_abort
        if self._instance is not None and hasattr(self._instance, "set_abort_hook"):
            self._instance.set_abort_hook(should_abort)

    def set_event_hook(
        self, on_model_fallback: Callable[[str, str, str], None] | None
    ) -> None:
        self._pending_event = on_model_fallback
        if self._instance is not None and hasattr(self._instance, "set_event_hook"):
            self._instance.set_event_hook(on_model_fallback)

    def set_backoff_hook(
        self, on_backoff: Callable[[int, float], None] | None
    ) -> None:
        self._pending_backoff = on_backoff
        if self._instance is not None and hasattr(self._instance, "set_backoff_hook"):
            self._instance.set_backoff_hook(on_backoff)

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
        return self._get_instance().complete_json(
            messages,
            schema,
            temperature=temperature,
            retries=retries,
            on_reasoning=on_reasoning,
            streaming=streaming,
        )


def _resolve_role_transport(
    run_backend: str,
    config_path: Path | None = None,
    log: EventLog | None = None,
    run_dir: Path | str | None = None,
) -> tuple[str, str]:
    """Resolve (effective_backend, transport) for role calls.

    Returns e.g. ("opencode", "backend") or ("gptme", "http").
    """
    env_transport = os.getenv("KUSUDAEMON_ROLE_TRANSPORT")
    env_backend = os.getenv("KUSUDAEMON_ROLE_BACKEND")

    file_data = read_config_file(config_path or config_file_path())
    roles_cfg = file_data.get("roles") if isinstance(file_data.get("roles"), dict) else {}

    cfg_backend = str(roles_cfg.get("backend") or "").strip() if roles_cfg.get("backend") else None
    cfg_transport = str(roles_cfg.get("transport") or "").strip() if roles_cfg.get("transport") else None

    effective_backend = env_backend or cfg_backend or run_backend or "gptme"
    effective_backend = str(effective_backend).strip().lower()

    degradation_reason: str | None = None

    if env_transport:
        transport = env_transport.strip().lower()
    else:
        desired_transport = cfg_transport.lower() if cfg_transport else "http"
        if effective_backend == "gptme":
            transport = "http"
        elif desired_transport == "backend":
            transport = "backend"
        else:
            try:
                from ..provider_config import resolve
                res = resolve()
                if res.api_key and res.base_url:
                    transport = "http"
                else:
                    transport = "backend"
                    if not res.api_key:
                        degradation_reason = "no_api_key"
                    elif not res.base_url:
                        degradation_reason = "no_base_url"
                    else:
                        degradation_reason = "resolve_failed"
            except Exception:
                transport = "backend"
                degradation_reason = "resolve_failed"

    if degradation_reason:
        target_log = log
        if target_log is None and run_dir is not None:
            try:
                from ..v0.run_dir import events_path
                p = events_path(Path(run_dir))
                if p.parent.exists():
                    target_log = EventLog(p)
            except Exception:
                pass
        if target_log is not None:
            try:
                target_log.append(
                    {
                        "node_id": "-",
                        "role": "role",
                        "type": "role_transport_degraded",
                        "reason": degradation_reason,
                        "backend": effective_backend,
                        "transport": transport,
                    }
                )
            except Exception:
                pass

    return effective_backend, transport


def make_role_provider(
    options: Any = None,
    *,
    run_dir: Path | str | None = None,
    env: Environment | None = None,
    log: EventLog | None = None,
    model: str | None = None,
    provider: str | None = None,
    backend: str | None = None,
    on_backoff: Callable[[int, float], None] | None = None,
    timeout: float | None = None,
    http_timeout: float | None = None,
    lazy: bool = False,
    provider_cls: Any = None,
    role: str | None = None,
    phase: str | None = None,
) -> RoleProvider:
    """Build a RoleProvider instance for reasoning/role calls.

    ``timeout`` is honored on both transports: the HTTP provider takes it
    directly, and the backend path threads it into BackendRoleProvider's
    episode budget (PLAN-REVIEW-LATENCY.md T0-6 — driver._role_provider's
    45 s reviewer/triage budget previously died here). None preserves each
    path's own default (300 s HTTP; env/defaults backend-side).

    PLAN-SWEEP-REPAIR.md §F: ``timeout`` and ``http_timeout`` are *different
    quantities wearing the same name*, which is how §F's halt happened.
    ``timeout`` is an episode budget — "a reviewer subprocess still running
    at 45 s has already failed at something" (docs/PLAN-REVIEW-LATENCY.md
    T0-6). On the HTTP branch the same number lands on ``urllib`` as the
    socket read timeout for one entire non-streaming completion, where 45 s
    is not a fail-fast threshold but a guillotine: reviewer prompts carry the
    full artifact, so their latency grows with the document and every
    100-floor arm-C seed crossed it mid-run. ``http_timeout`` lets a caller
    say what *that* transport should use while leaving the episode budget
    alone; it is ignored on the backend path, and ``timeout`` still applies
    to both when it is not given.

    ``role`` and ``phase`` are forwarded to the HTTP provider as well as the
    backend one (§F2's ``[phase role node ...]`` context was reaching the
    raise site as ``phase=unknown role=unknown`` because this branch dropped
    them; the same fields drive the ``calls_by_role``/``tokens_by_role``
    split in benchmark records, so both were blind together)."""
    run_backend = backend or (options.backend if options is not None and hasattr(options, "backend") else None) or "gptme"
    resolved_model = model or (options.model if options is not None and hasattr(options, "model") else None)
    resolved_provider = provider or (options.provider if options is not None and hasattr(options, "provider") else None)

    def _builder() -> RoleProvider:
        from ..v1.provider import OpenAICompatibleProvider

        effective_backend, transport = _resolve_role_transport(run_backend, log=log, run_dir=run_dir)

        if transport == "http" or effective_backend == "gptme":
            cls = provider_cls or OpenAICompatibleProvider
            effective_timeout = http_timeout if http_timeout is not None else timeout
            return cls(
                model=resolved_model,
                provider=resolved_provider,
                on_backoff=on_backoff,
                timeout=300.0 if effective_timeout is None else effective_timeout,
                role=role or "unknown",
                phase=phase or "unknown",
            )

        target_dir = Path(run_dir) if run_dir is not None else Path.cwd()
        return BackendRoleProvider(
            backend=effective_backend,
            run_dir=target_dir,
            env=env,
            model=resolved_model,
            log=log,
            role=role or "role",
            timeout=timeout,
        )

    if not lazy:
        return _builder()

    return LazyRoleProvider(_builder)
