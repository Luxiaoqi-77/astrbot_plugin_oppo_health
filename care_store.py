"""Private, atomic persistence for care-rule configuration and scheduler state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any

from .care_rules import RULE_IDS, default_config, validate_config


_V03_RULE_IDS = {
    "predicted_period_lead", "confirmed_period_start", "confirmed_period_end",
    "weight_date_linked",
}
_PRE_LATE_PERIOD_RULE_IDS = set(RULE_IDS) - {"period_late_inquiry"}


def _upgrade_v03_config(value: object) -> object:
    """Upgrade the original v0.3 shape and discard the superseded global cap."""
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return value
    rules = value.get("rules")
    if not isinstance(rules, dict):
        return value
    if set(rules) == _V03_RULE_IDS:
        upgraded = default_config()
        upgraded["rules"].update(rules)
        predicted = upgraded["rules"].get("predicted_period_lead")
        if isinstance(predicted, dict):
            predicted["lead_days"] = 3
        for rule in upgraded["rules"].values():
            if isinstance(rule, dict):
                rule["reschedule"] = "skip_if_late"
        return upgraded
    if (set(rules) in (
            set(RULE_IDS),
            _PRE_LATE_PERIOD_RULE_IDS,
        )
            and set(value) in ({"schema_version", "rules"}, {"schema_version", "rules", "health_budget"})):
        # A previous local candidate offered a cross-category daily cap. The
        # user's final policy explicitly exempts these category rules, so do
        # not carry that obsolete limiter into the saved configuration.
        upgraded_rules = default_config()["rules"]
        upgraded_rules.update({key: dict(item) if isinstance(item, dict) else item for key, item in rules.items()})
        if isinstance(upgraded_rules.get("predicted_period_lead"), dict):
            upgraded_rules["predicted_period_lead"]["lead_days"] = 3
        for rule_id, rule in upgraded_rules.items():
            if isinstance(rule, dict):
                rule["max_per_day"] = 7 if rule_id == "wellness_low_state" else 1
                rule["reschedule"] = "skip_if_late"
                if rule_id == "sunlight_evening":
                    rule["send_window"] = {"start": "20:00", "end": "21:00"}
        wellness = upgraded_rules.get("wellness_low_state")
        if isinstance(wellness, dict):
            mode = wellness.get("mode")
            common = (
                "confirmation_minutes", "minimum_independent_samples",
                "recovery_debounce_minutes", "maximum_sample_age_minutes",
                "repeat_cooldown_minutes",
            )
            confirmed = (
                mode in {"numeric", "slow_down_category"}
                and wellness.get("timezone_confirmed") is True
                and all(wellness.get(name) is not None for name in common)
                and (mode != "numeric" or (
                    wellness.get("low_score_threshold") is not None
                    and wellness.get("recovery_score_threshold") is not None
                ))
            )
            if not confirmed:
                wellness["enabled"] = False
        return {"schema_version": 1, "rules": upgraded_rules}
    return value


class CareStorageError(RuntimeError):
    """Raised when persisted care data cannot be safely read or written."""


class CareRevisionConflict(CareStorageError):
    """Raised when another page save changed the configuration revision."""


def empty_scheduler_state() -> dict[str, Any]:
    return {"schema_version": 1, "jobs": {}, "attempts": []}


def _validate_scheduler_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "jobs", "attempts"}:
        raise CareStorageError("scheduler state has an unsupported shape")
    if value.get("schema_version") != 1 or isinstance(value.get("schema_version"), bool):
        raise CareStorageError("scheduler state has an unsupported version")
    jobs = value.get("jobs")
    attempts = value.get("attempts")
    if not isinstance(jobs, dict) or not isinstance(attempts, list) or len(attempts) > 5000:
        raise CareStorageError("scheduler state is invalid")
    allowed_states = {
        "pending", "paused_stale", "paused_source_missing", "paused_invalid",
        "paused_privacy",
        "dispatching", "handed_off", "failed", "failed_uncertain", "skipped", "cancelled",
    }
    allowed_job_fields = {
        "event_key", "rule_id", "target_date", "basis", "expected_at", "window_end",
        "timezone", "source_observed_at", "is_actual_event", "state", "reason",
        "updated_at", "created_at",
    }
    allowed_attempt_fields = {"job_id", "rule_id", "at", "outcome", "reason"}
    if len(jobs) > 5000:
        raise CareStorageError("scheduler state contains too many jobs")
    for job_id, job in jobs.items():
        if (not isinstance(job_id, str) or not job_id or not isinstance(job, dict)
                or set(job) - allowed_job_fields
                or job.get("rule_id") not in RULE_IDS
                or job.get("state") not in allowed_states
                or not isinstance(job.get("event_key"), str) or len(job["event_key"]) > 128):
            raise CareStorageError("scheduler job is invalid")
        for field in ("target_date", "basis", "expected_at", "window_end", "timezone",
                      "source_observed_at", "reason", "updated_at", "created_at"):
            if field in job and job[field] is not None and (
                not isinstance(job[field], str) or len(job[field]) > 256
            ):
                raise CareStorageError("scheduler job metadata is invalid")
        if "is_actual_event" in job and not isinstance(job["is_actual_event"], bool):
            raise CareStorageError("scheduler job event marker is invalid")
    for attempt in attempts:
        if (not isinstance(attempt, dict) or set(attempt) - allowed_attempt_fields
                or attempt.get("rule_id") not in RULE_IDS
                or attempt.get("outcome") not in {
                    "dispatching", "handed_off", "failed", "failed_uncertain", "skipped"
                }
                or not isinstance(attempt.get("at"), str)
                or len(attempt["at"]) > 64
                or not isinstance(attempt.get("job_id"), str) or len(attempt["job_id"]) > 128
                or ("reason" in attempt and (
                    not isinstance(attempt["reason"], str) or len(attempt["reason"]) > 256
                ))):
            raise CareStorageError("scheduler attempt is invalid")
    return value


class CareRepository:
    """Store JSON with owner-only permissions and replace-on-write semantics.

    The constructor accepts a directory for tests. Production integration should
    pass ``StarTools.get_data_dir(NAME) / "care"``; no health cache or credentials
    belong in this repository.
    """

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self._lock = threading.RLock()
        self._ensure_directory()
        self.config_path = self.directory / "care-rules.json"
        self.state_path = self.directory / "scheduler-state.json"
        self.wellness_state_path = self.directory / "wellness-runtime.json"

    def _ensure_directory(self) -> None:
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.directory.is_symlink() or not self.directory.is_dir():
                raise CareStorageError("care data path is not a private directory")
            try:
                self.directory.chmod(0o700)
            except OSError:
                # Windows ACLs are inherited from the AstrBot data directory.
                if os.name != "nt":
                    raise
        except OSError as exc:
            raise CareStorageError("cannot create private care data directory") from exc

    def _read_json(self, path: Path) -> object | None:
        if path.is_symlink():
            raise CareStorageError("care data file is not a regular file")
        if not path.exists():
            return None
        if not path.is_file():
            raise CareStorageError("care data file is not a regular file")
        try:
            if os.name != "nt":
                path.chmod(0o600)
            if path.stat().st_size > 1_000_000:
                raise CareStorageError("care data file is too large")
            with path.open("r", encoding="utf-8") as stream:
                return json.load(stream)
        except CareStorageError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CareStorageError("care data file is unreadable") from exc

    def _atomic_write(self, path: Path, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = None
        temp_path = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=self.directory)
            temp_path = Path(temp_name)
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
            if os.name != "nt":
                path.chmod(0o600)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise CareStorageError("cannot save private care data") from exc
        finally:
            if fd is not None:
                os.close(fd)
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def load_config(self) -> tuple[dict[str, Any], int]:
        with self._lock:
            raw = self._read_json(self.config_path)
            if raw is None:
                return default_config(), 0
            if (not isinstance(raw, dict) or set(raw) != {"schema_version", "revision", "config"}
                    or raw.get("schema_version") != 1 or isinstance(raw.get("schema_version"), bool)):
                raise CareStorageError("care configuration has an unsupported shape")
            revision = raw.get("revision")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise CareStorageError("care configuration revision is invalid")
            try:
                config = validate_config(_upgrade_v03_config(raw.get("config")))
            except ValueError as exc:
                raise CareStorageError("care configuration is invalid") from exc
            return config, revision

    def save_config(self, config: object, expected_revision: object) -> tuple[dict[str, Any], int]:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        validated = validate_config(config)
        with self._lock:
            _current, current_revision = self.load_config()
            if expected_revision != current_revision:
                raise CareRevisionConflict("配置已在其他页面更新，请刷新后重试")
            revision = current_revision + 1
            self._atomic_write(self.config_path, {
                "schema_version": 1,
                "revision": revision,
                "config": validated,
            })
            return validated, revision

    def load_scheduler_state(self) -> dict[str, Any]:
        with self._lock:
            raw = self._read_json(self.state_path)
            return empty_scheduler_state() if raw is None else _validate_scheduler_state(raw)

    def save_scheduler_state(self, state: object) -> None:
        normalized = _validate_scheduler_state(state)
        with self._lock:
            self._atomic_write(self.state_path, normalized)

    def load_wellness_state(self) -> dict[str, Any]:
        """Load private score-free state used for source-sample and slot dedupe."""
        from .care_wellness import empty_runtime_state, validate_runtime_state

        with self._lock:
            raw = self._read_json(self.wellness_state_path)
            if raw is None:
                return empty_runtime_state()
            try:
                return validate_runtime_state(raw)
            except ValueError as exc:
                raise CareStorageError("wellness runtime state is invalid") from exc

    def save_wellness_state(self, state: object) -> None:
        """Atomically persist only allowlisted episode identifiers and timestamps."""
        from .care_wellness import validate_runtime_state

        try:
            normalized = validate_runtime_state(state)
        except ValueError as exc:
            raise CareStorageError("wellness runtime state is invalid") from exc
        with self._lock:
            self._atomic_write(self.wellness_state_path, normalized)
