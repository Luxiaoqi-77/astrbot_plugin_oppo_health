"""Bounded in-memory contexts for asynchronous care requests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class PendingCareContexts:
    """Hold sensitive prompt context only until send completion or expiry.

    Args:
        ttl_seconds: Maximum residence time for one pending context.
        max_entries: Maximum number of pending contexts retained at once.
    """

    def __init__(self, ttl_seconds: int = 900, max_entries: int = 32) -> None:
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        if type(max_entries) is not int or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[str, dict[str, Any]] = {}

    def put(
        self,
        token: str,
        recipe: dict[str, Any],
        attempt_id: str | None,
        now: float,
        period_inquiry_event_id: str | None = None,
        low_steps_marker: dict[str, str] | None = None,
        random_activity_attempt_id: str | None = None,
        random_activity_target_at: str | None = None,
    ) -> bool:
        """Add one bounded context entry.

        Args:
            token: Opaque correlation token for the synthetic request.
            recipe: Source and intent metadata only; prompt text is not retained.
            attempt_id: Persisted sleep attempt ID, if this is sleep care.
            now: Monotonic clock value from the caller.

        Returns:
            Whether the context was added.
        """
        if (
            not isinstance(token, str) or not token
            or not isinstance(recipe, dict)
            or recipe.get("kind") not in {"sleep_chat", "scheduled_care"}
            or len(recipe) > 5
            or (recipe.get("kind") == "sleep_chat" and set(recipe) != {
                "kind", "event_hash", "record_id"
            })
            or (recipe.get("kind") == "sleep_chat" and (
                not isinstance(recipe.get("event_hash"), str)
                or len(recipe["event_hash"]) != 64
                or any(c not in "0123456789abcdef" for c in recipe["event_hash"])
                or not isinstance(recipe.get("record_id"), str)
                or not recipe["record_id"] or len(recipe["record_id"]) > 128
            ))
            or (recipe.get("kind") == "scheduled_care" and set(recipe) != {
                "kind", "rules", "legacy"
            })
            or (recipe.get("kind") == "scheduled_care" and (
                not isinstance(recipe.get("rules"), list)
                or len(recipe["rules"]) > 12
                or any(not isinstance(item, dict)
                       or set(item) != {"rule_id", "event_id"}
                       or not isinstance(item.get("rule_id"), str)
                       or not isinstance(item.get("event_id"), str)
                       or len(item["event_id"]) != 64
                       or any(c not in "0123456789abcdef" for c in item["event_id"])
                       for item in recipe["rules"])
                or (recipe.get("legacy") is not None and (
                    not isinstance(recipe["legacy"], dict)
                    or set(recipe["legacy"]) != {
                        "kind", "target_at", "low_steps_marker", "random_activity_attempt_id"
                    }
                    or recipe["legacy"].get("kind") not in {"low_steps", "random_activity"}
                ))
            ))
            or not isinstance(now, (int, float))
            or (attempt_id is not None and (not isinstance(attempt_id, str) or not attempt_id))
            or (period_inquiry_event_id is not None and (
                not isinstance(period_inquiry_event_id, str)
                or len(period_inquiry_event_id) != 64
                or any(char not in "0123456789abcdef" for char in period_inquiry_event_id)
            ))
            or (low_steps_marker is not None and (
                not isinstance(low_steps_marker, dict)
                or set(low_steps_marker) != {"date", "check_at", "window_end", "reserved_at"}
                or any(not isinstance(value, str) or len(value) > 64
                       for value in low_steps_marker.values())
            ))
            or (random_activity_attempt_id is not None and (
                not isinstance(random_activity_attempt_id, str)
                or len(random_activity_attempt_id) != 32
                or any(char not in "0123456789abcdef" for char in random_activity_attempt_id)
            ))
            or (random_activity_target_at is not None and (
                not isinstance(random_activity_target_at, str) or len(random_activity_target_at) > 64
            ))
            or token in self._entries
            or len(self._entries) >= self.max_entries
        ):
            return False
        self._entries[token] = {
            "recipe": deepcopy(recipe),
            "attempt_id": attempt_id,
            "period_inquiry_event_id": period_inquiry_event_id,
            "low_steps_marker": deepcopy(low_steps_marker),
            "random_activity_attempt_id": random_activity_attempt_id,
            "random_activity_target_at": random_activity_target_at,
            "expires_at": float(now) + self.ttl_seconds,
        }
        return True

    def get(self, token: str, now: float) -> dict[str, Any] | None:
        """Return a live context entry without extending its expiry.

        Args:
            token: Opaque request token.
            now: Current monotonic clock value.

        Returns:
            The pending entry, or ``None`` if it is absent or expired.
        """
        entry = self._entries.get(token)
        if not isinstance(entry, dict) or float(now) >= entry["expires_at"]:
            return None
        return entry

    def pop(self, token: str) -> dict[str, Any] | None:
        """Remove one entry after its after-send handler settles it."""
        return self._entries.pop(token, None)

    def set_attempt_id(self, token: str, attempt_id: str) -> bool:
        """Bind a pre-reserved context to its already-persisted attempt.

        Args:
            token: Existing context token.
            attempt_id: Durable attempt identifier.

        Returns:
            Whether the entry exists and was updated.
        """
        entry = self._entries.get(token)
        if not isinstance(entry, dict) or not isinstance(attempt_id, str) or not attempt_id:
            return False
        entry["attempt_id"] = attempt_id
        return True

    def expire(self, now: float) -> list[tuple[str, dict[str, Any]]]:
        """Remove expired entries and return them for durable reconciliation.

        Args:
            now: Current monotonic clock value.

        Returns:
            Expired token and metadata pairs. Callers must lock any referenced
            durable attempt as unknown before permitting a retry.
        """
        expired = [
            (token, entry)
            for token, entry in self._entries.items()
            if float(now) >= entry["expires_at"]
        ]
        for token, _entry in expired:
            self._entries.pop(token, None)
        return expired

    def __len__(self) -> int:
        return len(self._entries)
