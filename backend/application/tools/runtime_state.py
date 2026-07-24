"""Process-local observations for the online tool runtime.

The state is intentionally operational rather than authoritative: each service
worker reports what it has observed since startup. Durable, cross-worker
aggregation belongs in the deployment metrics backend.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any


class ToolRuntimeState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._health: dict[str, dict[str, Any]] = {}
        self._stats: dict[str, dict[str, Any]] = {}

    def record_health(
        self,
        tool_name: str,
        *,
        status: str,
        reason: str = "",
        detail: str = "",
    ) -> None:
        name = str(tool_name or "").strip()
        if not name:
            return
        with self._lock:
            self._health[name] = {
                "status": str(status or "unknown"),
                "reason": str(reason or ""),
                "detail": str(detail or "")[:500],
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }

    def record_call(
        self,
        tool_name: str,
        *,
        status: str,
        attempts: int,
        elapsed_ms: float,
        queue_ms: float,
        error_code: str = "",
    ) -> None:
        name = str(tool_name or "tool").strip() or "tool"
        with self._lock:
            stats = self._stats.setdefault(
                name,
                {
                    "calls": 0,
                    "successes": 0,
                    "errors": 0,
                    "timeouts": 0,
                    "retries": 0,
                    "total_elapsed_ms": 0.0,
                    "total_queue_ms": 0.0,
                    "last_error_code": "",
                },
            )
            stats["calls"] += 1
            if status == "success":
                stats["successes"] += 1
            else:
                stats["errors"] += 1
            if error_code == "timeout":
                stats["timeouts"] += 1
            stats["retries"] += max(0, int(attempts) - 1)
            stats["total_elapsed_ms"] += max(0.0, float(elapsed_ms))
            stats["total_queue_ms"] += max(0.0, float(queue_ms))
            stats["last_error_code"] = str(error_code or "")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            names = sorted(set(self._health) | set(self._stats))
            tools = []
            for name in names:
                stats = {
                    "calls": 0,
                    "successes": 0,
                    "errors": 0,
                    "timeouts": 0,
                    "retries": 0,
                    "last_error_code": "",
                    **dict(self._stats.get(name) or {}),
                }
                calls = int(stats.get("calls") or 0)
                total_elapsed = float(stats.pop("total_elapsed_ms", 0.0))
                total_queue = float(stats.pop("total_queue_ms", 0.0))
                stats["average_elapsed_ms"] = round(total_elapsed / calls, 3) if calls else 0.0
                stats["average_queue_ms"] = round(total_queue / calls, 3) if calls else 0.0
                tools.append(
                    {
                        "name": name,
                        "health": dict(
                            self._health.get(name)
                            or {
                                "status": "unknown",
                                "reason": "not_observed",
                                "detail": "",
                                "observed_at": None,
                            }
                        ),
                        "metrics": stats,
                    }
                )
            return {
                "scope": "process",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "tools": tools,
            }

    def reset(self) -> None:
        """Clear observations; intended for isolated tests."""

        with self._lock:
            self._health.clear()
            self._stats.clear()


tool_runtime_state = ToolRuntimeState()


__all__ = ["ToolRuntimeState", "tool_runtime_state"]
