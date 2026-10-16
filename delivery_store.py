"""Serialized private persistence for the sleep-care dispatch ledger."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable

try:
    from .sleep_dispatch import LedgerError, migrate_legacy_state, validate_ledger
except ImportError:  # Allow the offline candidate tests to import this file directly.
    from sleep_dispatch import LedgerError, migrate_legacy_state, validate_ledger


MAX_LEDGER_BYTES = 1_000_000
LEGACY_FIELDS = {
    "date", "sleep_attempted", "sleep_attempted_at",
    "sleep_attempted_record_id", "sleep_plan_record_id",
    "sleep_care_selected", "sleep_care_due_at", "sleep_care_delay_minutes",
}


class SleepDeliveryStore:
    """Load, migrate, validate, and serialize the private delivery ledger.

    Args:
        ledger_path: Dedicated private path for the allowlisted ledger.
        legacy_path: Existing daily-care file used only for one-time migration.
        read_json: The host plugin's private JSON reader.
        write_json: The host plugin's atomic private JSON writer.

    Raises:
        LedgerError: A persisted file is unsafe, oversized, or invalid.
    """

    def __init__(
        self,
        ledger_path: Path | str,
        legacy_path: Path | str,
        read_json: Callable[[Path], object],
        write_json: Callable[[object, Path], None],
    ) -> None:
        self.path = Path(ledger_path)
        self.legacy_path = Path(legacy_path)
        self._read_json = read_json
        self._write_json = write_json
        self.lock = asyncio.Lock()

    def _read_bounded(self, path: Path) -> object:
        if path.is_symlink():
            raise LedgerError("sleep delivery state cannot be a symlink")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise LedgerError("sleep delivery state cannot be inspected") from exc
        if size > MAX_LEDGER_BYTES:
            raise LedgerError("sleep delivery state is oversized")
        return self._read_json(path)

    def load(self, now) -> dict[str, Any]:
        """Read a validated ledger or migrate legacy attempt metadata once.

        Args:
            now: Current timezone-aware time used for the local-day migration.

        Returns:
            A validated ledger.

        Raises:
            LedgerError: Persisted input cannot be safely interpreted.
        """
        if self.path.is_symlink():
            raise LedgerError("sleep delivery state cannot be a symlink")
        if self.path.exists():
            return validate_ledger(self._read_bounded(self.path))

        legacy: dict[str, Any] = {}
        if self.legacy_path.is_symlink():
            raise LedgerError("legacy daily-care state cannot be a symlink")
        if self.legacy_path.exists():
            raw = self._read_bounded(self.legacy_path)
            if not isinstance(raw, dict):
                raise LedgerError("legacy daily-care state is invalid")
            legacy = {key: raw[key] for key in LEGACY_FIELDS if key in raw}
        ledger = migrate_legacy_state(legacy, now)
        return self.save(ledger)

    def save(self, ledger: dict[str, Any]) -> dict[str, Any]:
        """Validate and atomically write the allowlisted ledger.

        Args:
            ledger: Mutable metadata ledger to persist.

        Returns:
            The validated ledger.

        Raises:
            LedgerError: The ledger violates the schema or persistence failed.
        """
        normalized = validate_ledger(ledger)
        try:
            self._write_json(normalized, self.path)
            # The host writer fsyncs the file before atomic replace. On POSIX,
            # fsync the containing directory too so the rename survives a
            # sudden power loss. A failure is fail-closed: callers must not
            # enqueue an attempt whose reservation is not durably recorded.
            if os.name != "nt":
                directory_fd = os.open(
                    self.path.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except Exception as exc:
            raise LedgerError("sleep delivery state could not be saved") from exc
        return normalized
