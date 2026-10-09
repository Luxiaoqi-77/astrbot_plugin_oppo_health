"""Fail-closed policy helpers for optional local OPPO Health pages."""

from __future__ import annotations

import importlib
import inspect
import json
import re
from typing import Any
from urllib.parse import urlsplit


CORE_PRIVACY_MODULE = "astrbot.core.provider.local_health_privacy"
CORE_PROVIDER_ENTITIES_MODULE = "astrbot.core.provider.entities"
CORE_PRIVACY_REQUIRED = (
    "stage_local_health_context",
    "clear_local_health_context",
    "local_health_request_context",
    "is_local_health_request",
)
CORE_PROVIDER_REQUIRED = ("provider_allowed_for_request", "sanitize_provider_api_host")
CLOUD_HEALTH_TOOL_NAME = "get_oppo_health"


class CorePrivacyAPI:
    """Adapter for the privacy helpers that the supported AstrBot core actually exposes."""

    def __init__(self, privacy_module: Any, provider_module: Any):
        self._privacy_module = privacy_module
        self._provider_module = provider_module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._privacy_module, name)

    def provider_allowed_for_request(self, api_base: str, required_host: str) -> bool:
        return self._provider_module.provider_allowed_for_request(api_base, required_host)


def load_core_privacy_api(importer=importlib.import_module) -> tuple[Any | None, str | None]:
    """Load existing request-privacy and provider-host guards from AstrBot core."""
    try:
        privacy_module = importer(CORE_PRIVACY_MODULE)
    except Exception:
        return None, "core_privacy_module_unavailable"
    if any(not callable(getattr(privacy_module, name, None)) for name in CORE_PRIVACY_REQUIRED):
        return None, "core_privacy_api_incomplete"
    try:
        inspect.signature(privacy_module.stage_local_health_context).bind(
            object(),
            "",
            required_provider_host=None,
            allowed_provider_hosts=(),
        )
    except (TypeError, ValueError):
        return None, "core_privacy_api_incomplete"
    try:
        provider_module = importer(CORE_PROVIDER_ENTITIES_MODULE)
    except Exception:
        return None, "core_provider_host_policy_unavailable"
    if any(not callable(getattr(provider_module, name, None)) for name in CORE_PROVIDER_REQUIRED):
        return None, "core_provider_host_policy_unavailable"
    return CorePrivacyAPI(privacy_module, provider_module), None


def normalize_provider_host(value: Any) -> str | None:
    """Normalize a bare host or origin and reject paths, credentials, and fragments."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    except ValueError:
        return None
    if (
        parsed.scheme not in ("", "https")
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname.casefold().rstrip(".")
    if not hostname:
        return None
    if port not in (None, 443):
        return None
    return hostname


def normalize_approved_provider_hosts(value: Any) -> tuple[str, ...]:
    """Validate an explicitly configured host list; empty disables model use."""
    if not isinstance(value, (list, tuple, set, frozenset)) or not value:
        return ()
    normalized = []
    for item in value:
        host = normalize_provider_host(item)
        if host is None:
            return ()
        normalized.append(host)
    return tuple(sorted(set(normalized)))


def health_query_mentions_other_person(text: str) -> bool:
    """Return whether a health question explicitly targets someone else."""
    query = " ".join(text.casefold().split())
    return bool(re.search(
        r"我(?:的)?(?:妈妈|妈|母亲|爸爸|爸|父亲|家人|朋友|同事|伴侣|老公|老婆|妻子|丈夫|孩子|儿子|女儿|哥哥|姐姐|弟弟|妹妹|爷爷|奶奶|姥爷|姥姥)"
        r"|(?:他|她|他们|她们)(?:的)?"
        r"|\b(?:his|her|their)\b"
        r"|\bmy\s+(?:mom|mother|dad|father|parent|parents|family|friend|partner|husband|wife|spouse|child|children|kid|kids|son|daughter|brother|sister|grandmother|grandfather|grandparent|aunt|uncle|cousin)\b",
        query,
        re.IGNORECASE,
    ))


def health_query_mentions_known_metric(text: str) -> bool:
    """Return whether text mentions one of the OPPO health page metrics."""
    query = text.casefold()
    return any(
        re.search(pattern, query, re.IGNORECASE)
        for pattern in (
            r"体重|\bweight\b",
            r"日照|日晒|sun exposure|sunlight duration",
            r"状态值|状态评分|身心状态|mind and body|wellness",
            r"活动卡路里|活动热量|active calories",
            r"步数|\bsteps\b",
            r"步行距离|走路距离|步行路程|walking distance|walk distance",
            r"经期|月经|例假|menstrual|predicted period|cycle tracker|\bperiod\b",
            r"健康|心率|血氧|睡眠|手表|运动|\bheart rate\b|\boxygen\b|\bsleep\b",
        )
    )


def provider_host_is_approved(core_api: Any, configured_host: str) -> bool:
    """Require a configured HTTPS host to pass AstrBot's request host guard."""
    normalized = normalize_provider_host(configured_host)
    validator = getattr(core_api, "provider_allowed_for_request", None)
    if normalized is None or not callable(validator):
        return False
    authority = f"[{normalized}]" if ":" in normalized else normalized
    try:
        return validator(f"https://{authority}/", normalized) is True
    except Exception:
        return False


def local_fields_for_query(text: str) -> list[str]:
    """Return selected local pages only for an explicit self-directed query."""
    query = " ".join(text.casefold().split())
    rules = (
        ("cycle_calendar", r"经期|月经|例假|menstrual|predicted period|cycle tracker|\bperiod\b"),
        ("weight_history", r"体重|\bweight\b"),
        ("sun_exposure", r"日照|日晒|sun exposure|sunlight duration"),
        ("wellness_home", r"状态值|状态评分|身心状态|mind and body|wellness"),
        ("active_calories", r"活动卡路里|活动热量|active calories"),
        ("steps_daily_summary", r"步数|\bsteps\b"),
        ("steps_daily_details", r"步行距离|走路距离|步行路程|walking distance|walk distance"),
    )
    fields = [field for field, pattern in rules if re.search(pattern, query, re.IGNORECASE)]
    if not fields:
        return []
    refers_to_self = re.search(r"本人|我(?!们)|\b(?:my|mine)\b", query, re.IGNORECASE)
    if health_query_mentions_other_person(text) or not refers_to_self:
        return []
    if "wellness_home" in fields:
        fields.insert(fields.index("wellness_home") + 1, "wellness_detail")
    return fields


def local_request_allowed(
    configured_session: str,
    event_session: str,
    provider_host: str | None,
    approved_provider_hosts: tuple[str, ...],
    core_api: Any,
) -> bool:
    """Require the private session and a host from the explicit allowlist."""
    parts = configured_session.rsplit(":", 2)
    actual_host = normalize_provider_host(provider_host)
    approved_hosts = normalize_approved_provider_hosts(approved_provider_hosts)
    return (
        configured_session == event_session
        and len(parts) == 3
        and parts[1] == "FriendMessage"
        and parts[2].isdigit()
        and actual_host is not None
        and actual_host in approved_hosts
        and provider_host_is_approved(core_api, actual_host)
    )


def remove_cloud_health_tool(tool_set: Any) -> bool:
    """Remove the cloud health tool, or fail if the tool set cannot be inspected."""
    if tool_set is None:
        return True
    remove_tool = getattr(tool_set, "remove_tool", None)
    if not callable(remove_tool):
        return False
    try:
        remove_tool(CLOUD_HEALTH_TOOL_NAME)
    except Exception:
        return False
    return True


def local_result_has_health_values(result: Any, requested_fields: list[str]) -> bool:
    """Return whether selected local pages contain actual health values."""
    pages = result.get("pages", {}) if isinstance(result, dict) else {}
    value_metrics = {
        "cycle_calendar": {"period_dates", "predicted_period_dates"},
        "weight_history": {"weight_history_records"},
        "sun_exposure": {"duration_min", "goal_min"},
        "wellness_home": {"score", "category", "point_time_local"},
        "wellness_detail": {"score", "category"},
        "active_calories": {"active_kcal", "goal_kcal"},
        "steps_daily_summary": {"steps", "goal_steps"},
        "steps_daily_details": {"distance_km"},
    }
    if not isinstance(pages, dict):
        return False
    for field in requested_fields:
        page = pages.get(field)
        data = page.get("data") if isinstance(page, dict) else None
        metrics = data.get("metrics") if isinstance(data, dict) else None
        if not isinstance(metrics, dict):
            continue
        for key in value_metrics.get(field, set()):
            value = metrics.get(key)
            if value is not None and value != "" and value != [] and value != {}:
                return True
    return False


def format_local_health_context(result: Any, requested_fields: list[str]) -> str:
    """Format only requested page data and fail closed on malformed results."""
    lines = ["[本人 OPPO 健康本机页面读取，仅用于本次请求]"]
    pages = result.get("pages", {}) if isinstance(result, dict) else {}
    allowed_metrics = {
        "cycle_calendar": {"period_dates", "predicted_period_dates", "calendar_coverage", "legend_verified"},
        "weight_history": {"weight_history_records"},
        "sun_exposure": {"label", "unit", "duration_min", "goal_min"},
        "wellness_home": {"label", "kind", "score", "category", "point_time_local"},
        "wellness_detail": {"label", "kind", "category_label", "score", "category"},
        "active_calories": {"label", "unit", "active_kcal", "goal_kcal"},
        "steps_daily_summary": {"label", "unit", "steps", "goal_steps"},
        "steps_daily_details": {"label", "unit", "distance_km"},
    }
    for field in requested_fields:
        page = pages.get(field) if isinstance(pages, dict) else None
        if not isinstance(page, dict) or not isinstance(page.get("data"), dict):
            lines.append(f"{field}: 本机页面暂不可用；不得推测数值。")
            continue
        data = page["data"]
        metrics = data.get("metrics")
        if not isinstance(metrics, dict):
            lines.append(f"{field}: 本机页面暂不可用；不得推测数值。")
            continue
        if not local_result_has_health_values({"pages": {field: page}}, [field]):
            lines.append(f"{field}: 本机页面暂不可用；不得推测数值。")
            continue
        visible = {
            key: data[key]
            for key in ("source", "page", "observed_at", "date", "period", "metrics")
            if key in data
        }
        visible["metrics"] = {
            key: value
            for key, value in metrics.items()
            if key in allowed_metrics.get(field, set())
        }
        lines.append(json.dumps(visible, ensure_ascii=False, separators=(",", ":")))
        if page.get("status") == "no_visible_value":
            lines.append(f"{field}: 页面读取成功，但未识别到该项数值；不得将缺失当作 0。")
    lines.append(
        "只回答用户所问且可见的项目。缺失字段按不可用处理，不推测。"
        "区分 Period 与 Predicted period；Mind and Body 使用页面原标签，不称为压力；"
        "Active calories 不等同总卡路里。读取时间不是测量时间，不作诊断。"
    )
    return "\n".join(lines)
