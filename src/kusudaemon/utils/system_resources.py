"""System hardware resources & memory admission helpers (PLAN-CONCURRENCY-AND-SHARED-STATE.md §B8).

Provides:
- stdlib-only available memory detection for macOS (vm_stat) and Linux (SC_AVPHYS_PAGES).
- memory-derived concurrency clamp (§B8.2).
- wave-fill admission gate (§B8.3).
"""

from __future__ import annotations

import os
import subprocess
import sys


def get_available_memory_bytes() -> int | None:
    """Return available memory in bytes without third-party dependencies.

    Uses sysconf on Linux, vm_stat on Darwin. Returns None if undetectable.
    """
    # Linux via sysconf
    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_AVPHYS_PAGES")
            page_size = os.sysconf("SC_PAGESIZE")
            if pages > 0 and page_size > 0:
                return int(pages * page_size)
        except (ValueError, OSError):
            pass

    # macOS vm_stat fallback
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(["vm_stat"], text=True, timeout=2.0)
            page_size = 4096
            counts: dict[str, int] = {}
            for line in out.splitlines():
                if "page size of" in line:
                    parts = line.split("page size of")
                    if len(parts) > 1:
                        try:
                            page_size = int(parts[1].split("bytes")[0].strip())
                        except ValueError:
                            pass
                elif ":" in line:
                    k, v = line.split(":", 1)
                    try:
                        counts[k.strip()] = int(v.strip().rstrip("."))
                    except ValueError:
                        pass
            free_pages = (
                counts.get("Pages free", 0)
                + counts.get("Pages inactive", 0)
                + counts.get("Pages speculative", 0)
            )
            if free_pages > 0:
                return free_pages * page_size
        except Exception:
            pass

    return None


def derive_memory_concurrency(
    *,
    safety: float = 0.75,
    default_episode_rss_mb: int = 600,
    hard_cap: int = 16,
) -> int:
    """PLAN-CONCURRENCY-AND-SHARED-STATE.md §B8.2:

    concurrency = clamp(1, available_bytes * safety / episode_rss, hard_cap)
    Defaults conservatively (~600 MB/episode) unless KUSUDAEMON_EPISODE_RSS_MB is set.
    """
    rss_mb_str = os.getenv("KUSUDAEMON_EPISODE_RSS_MB")
    rss_mb = default_episode_rss_mb
    if rss_mb_str:
        try:
            rss_mb = int(rss_mb_str)
        except ValueError:
            pass
    rss_bytes = max(1, rss_mb * 1024 * 1024)

    avail_bytes = get_available_memory_bytes()
    if avail_bytes is None or avail_bytes <= 0:
        # Fall back to CPU-based estimate if memory unavailable
        cpu = os.cpu_count() or 1
        return min(hard_cap, max(1, cpu))

    concurrency = int((avail_bytes * safety) / rss_bytes)
    return max(1, min(hard_cap, concurrency))


def can_admit_episode(*, floor_bytes: int | None = None) -> bool:
    """PLAN-CONCURRENCY-AND-SHARED-STATE.md §B8.3:

    Admit at wave-fill time: return False if available memory is below floor.
    Default floor is 600MB.
    """
    if floor_bytes is None:
        rss_mb_str = os.getenv("KUSUDAEMON_EPISODE_RSS_MB")
        rss_mb = int(rss_mb_str) if rss_mb_str and rss_mb_str.isdigit() else 600
        floor_bytes = rss_mb * 1024 * 1024

    avail = get_available_memory_bytes()
    if avail is None:
        return True
    return avail >= floor_bytes
