"""Role provider protocol and base class (ROLE-CALLS-VIA-BACKENDS-PLAN.md §1.1).

Defines the duck-typed interface that both direct HTTP model providers
(OpenAICompatibleProvider) and CLI backend-based providers (BackendRoleProvider)
must satisfy for harness reasoning and role model calls (classify, intake, survey,
plan, pilot, review, document review, revalidation, etc.).
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class RoleProvider(Protocol):
    """Protocol for all reasoning/role model calls in the harness."""

    model: str

    def complete_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        temperature: float | None = None,
        retries: int = 2,
        on_reasoning: Callable[[str], None] | None = None,
        streaming: bool = False,
    ) -> dict[str, Any]:
        """Execute a schema-constrained structured output call."""
        ...


class RoleProviderBase:
    """Shared base class providing driver hook management."""

    model: str = ""
    _should_abort: Callable[[], bool] | None = None
    _on_model_fallback: Callable[[str, str, str], None] | None = None
    _on_backoff: Callable[[int, float], None] | None = None
    _on_degenerate: Callable[[dict[str, Any]], None] | None = None

    def set_abort_hook(self, should_abort: Callable[[], bool] | None) -> None:
        self._should_abort = should_abort

    def set_event_hook(
        self, on_model_fallback: Callable[[str, str, str], None] | None
    ) -> None:
        self._on_model_fallback = on_model_fallback

    def set_backoff_hook(
        self, on_backoff: Callable[[int, float], None] | None
    ) -> None:
        self._on_backoff = on_backoff

    def set_degenerate_hook(
        self, on_degenerate: Callable[[dict[str, Any]], None] | None
    ) -> None:
        self._on_degenerate = on_degenerate
