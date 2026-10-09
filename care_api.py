"""Authenticated AstrBot Plugin Page API for local care configuration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import inspect
from typing import Any

from astrbot.api.web import error_response, json_response, request

from .care_rules import RULE_IDS, build_preview_plan
from .care_store import CareRepository, CareRevisionConflict, CareStorageError
from .care_web_security import dashboard_admin_matches, same_origin_write


_RULE_NAMES = {
    "predicted_period_lead": "预测经期提前提醒",
    "confirmed_period_start": "已确认经期开始",
    "confirmed_period_end": "已确认经期结束",
    "weight_date_linked": "体重按记录日期联动",
}
_SOURCE_NAMES = {
    "predicted_period": "经期预测",
    "confirmed_period_start": "经期开始确认",
    "confirmed_period_end": "经期结束确认",
    "weight_entry": "体重记录",
}


class CarePageAPI:
    """Routes are protected by AstrBot plugin scope plus exact admin identity."""

    def __init__(
        self,
        context: Any,
        plugin_config: Mapping[str, Any],
        repository: CareRepository,
        status_provider: Any = None,
    ):
        self.context = context
        self.plugin_config = plugin_config
        self.repository = repository
        self.status_provider = status_provider
        self.prefix = "/oppo_health/care"

    def register(self) -> None:
        self.context.register_web_api(
            f"{self.prefix}/state", self.state, ["GET"], "Read local care configuration and status"
        )
        self.context.register_web_api(
            f"{self.prefix}/preview", self.preview, ["GET"], "Preview local care rules without dispatch"
        )
        self.context.register_web_api(
            f"{self.prefix}/config", self.save_config, ["POST"], "Save local care configuration"
        )
        self.context.register_web_api(
            f"{self.prefix}/privacy-status", self.privacy_status, ["GET"],
            "Read local care readiness and perform a read-only route preflight",
        )

    def unregister(self) -> None:
        routes = getattr(self.context, "registered_web_apis", None)
        if not isinstance(routes, list):
            return
        routes[:] = [
            route for route in routes
            if not (
                isinstance(route, tuple) and len(route) > 1 and isinstance(route[0], str)
                and route[0].startswith(self.prefix)
                and getattr(route[1], "__self__", None) is self
            )
        ]

    def _is_admin(self) -> bool:
        try:
            settings = self.context.get_config()
        except Exception:
            return False
        return dashboard_admin_matches(settings, request.username)

    def _write_is_same_origin(self) -> bool:
        headers = request.headers
        return same_origin_write(
            headers.get("origin"),
            headers.get("host"),
            headers.get("sec-fetch-site"),
        )

    async def _status_snapshot(self) -> dict[str, Any]:
        """Return sanitized cached readiness; a provider may run a read-only preflight."""
        try:
            value = self.status_provider() if callable(self.status_provider) else {}
            if inspect.isawaitable(value):
                value = await value
        except Exception:
            value = {}
        value = value if isinstance(value, dict) else {}
        fields = {
            "source_state": value.get("source_state"),
            "source_observed_at": value.get("source_observed_at"),
            "route_observed_at": value.get("route_observed_at"),
            "core_privacy_api_available": value.get("core_privacy_api_available") is True,
            "supported_runner": value.get("supported_runner") is True,
            "resolved_provider_host": value.get("resolved_provider_host"),
            "host_approved": value.get("host_approved") is True,
            "health_context_to_model_enabled": value.get("health_context_to_model_enabled") is True,
            "last_route_error_code": value.get("last_route_error_code"),
            "data_sources": value.get("data_sources", []),
        }
        if fields["source_state"] not in {"not_configured", "unavailable", "ready", "stale", "error"}:
            fields["source_state"] = "unavailable"
        for key in ("source_observed_at", "route_observed_at", "resolved_provider_host"):
            item = fields[key]
            fields[key] = item if isinstance(item, str) and len(item) <= 253 else None
        if fields["last_route_error_code"] not in {
            None, "unapproved_host", "runner_unsupported", "preflight_unavailable",
            "private_session_unavailable", "source_unavailable",
        }:
            fields["last_route_error_code"] = "preflight_unavailable"
        sources = []
        if isinstance(fields["data_sources"], list):
            for item in fields["data_sources"][:4]:
                if not isinstance(item, dict):
                    continue
                source = item.get("source")
                if source not in _SOURCE_NAMES:
                    continue
                status = item.get("status")
                if status not in {"ok", "unavailable", "stale", "unsupported", "not_configured", "error"}:
                    status = "unavailable"
                data_date = item.get("data_date")
                observed_at = item.get("observed_at")
                reason = item.get("reason")
                sources.append({
                    "source": source,
                    "name": _SOURCE_NAMES[source],
                    "status": status,
                    "data_date": data_date if isinstance(data_date, str) and len(data_date) <= 32 else None,
                    "observed_at": observed_at if isinstance(observed_at, str) and len(observed_at) <= 64 else None,
                    "is_stale": item.get("is_stale") if isinstance(item.get("is_stale"), bool) else None,
                    "reason": reason if isinstance(reason, str) and len(reason) <= 160 else None,
                })
        fields["data_sources"] = sources
        if fields["source_observed_at"] is None:
            observed = [item["observed_at"] for item in sources if item.get("observed_at")]
            fields["source_observed_at"] = max(observed) if observed else None
        return fields

    async def privacy_status(self):
        if not self._is_admin():
            return error_response("仅 Dashboard 管理员可查看关怀状态", status_code=403)
        status = await self._status_snapshot()
        return json_response(status)

    async def state(self):
        if not self._is_admin():
            return error_response("仅 Dashboard 管理员可查看关怀设置", status_code=403)
        try:
            config, revision = self.repository.load_config()
            persisted = self.repository.load_scheduler_state()
        except CareStorageError:
            return error_response("本机关怀配置无法安全读取", status_code=503)

        now = datetime.now(timezone.utc)
        plan = build_preview_plan(config, {}, now)
        jobs_by_rule: dict[str, list[dict[str, Any]]] = {rule_id: [] for rule_id in RULE_IDS}
        for job in persisted["jobs"].values():
            if job.get("rule_id") in jobs_by_rule:
                jobs_by_rule[job["rule_id"]].append(job)
        rules = []
        for item in plan["plans"]:
            jobs = jobs_by_rule[item["rule_id"]]
            pending = sorted(
                (job for job in jobs if job.get("state") == "pending"),
                key=lambda job: str(job.get("expected_at", "")),
            )
            current_job = pending[0] if pending else (jobs[-1] if jobs else None)
            rules.append({
                "rule_id": item["rule_id"],
                "name": _RULE_NAMES[item["rule_id"]],
                "enabled": item["enabled"],
                "state": current_job.get("state") if current_job else item["state"],
                "expected_at": current_job.get("expected_at") if current_job and current_job.get("state") == "pending" else None,
                "reason": current_job.get("reason") if current_job else item["reason"],
            })

        daily_care = bool(self.plugin_config.get("daily_care", False))
        attempts = [
            {
                "at": item["at"],
                "rule_id": item["rule_id"],
                "outcome": item["outcome"],
                "reason": item.get("reason", ""),
                "platform_receipt": None,
                "demo": False,
            }
            for item in persisted["attempts"][-10:]
        ]
        runtime_status = await self._status_snapshot()
        sources_by_id = {item["source"]: item for item in runtime_status["data_sources"]}
        data_sources = [
            sources_by_id.get(source, {
                "source": source,
                "name": name,
                "status": "not_configured",
                "data_date": None,
                "observed_at": None,
                "is_stale": None,
                "reason": None,
            })
            for source, name in _SOURCE_NAMES.items()
        ]
        return json_response({
            "schema_version": 1,
            "demo": False,
            "revision": revision,
            "evaluated_at": now.isoformat(),
            "config": config,
            "rules": rules,
            "existing_care": [
                {
                    "rule_id": "sleep_wake",
                    "name": "睡眠关怀",
                    "enabled": daily_care,
                    "state": "enabled" if daily_care else "disabled",
                    "reason": "沿用现有插件设置",
                },
                {
                    "rule_id": "random_activity",
                    "name": "随机活动关怀",
                    "enabled": daily_care and bool(self.plugin_config.get("random_activity_care", True)),
                    "state": "enabled" if daily_care and bool(self.plugin_config.get("random_activity_care", True)) else "disabled",
                    "reason": "受现有睡眠关怀总开关控制",
                },
            ],
            "data_sources": data_sources,
            "recent_attempts": attempts,
            "model_access": {
                "policy": "follow_chat_model",
                "current_host": runtime_status["resolved_provider_host"],
                "approval_state": "approved" if runtime_status["host_approved"] else "unavailable",
                "read_only": True,
            },
            "privacy_status": runtime_status,
            "notice": (
                "新规则仅在本机来源、runner 和当前模型授权预检均通过后运行。"
                if runtime_status["health_context_to_model_enabled"]
                else "新规则当前处于暂停状态；本机安全预检未全部通过，不会读取新来源或提交关怀。"
            ),
        })

    async def preview(self):
        if not self._is_admin():
            return error_response("仅 Dashboard 管理员可查看关怀设置", status_code=403)
        try:
            config, revision = self.repository.load_config()
        except CareStorageError:
            return error_response("本机关怀配置无法安全读取", status_code=503)
        plan = build_preview_plan(config, {}, datetime.now(timezone.utc))
        plan["revision"] = revision
        plan["notice"] = "当前没有接入来源记录；此预览不读取健康数据，也不会发送消息。"
        return json_response(plan)

    async def save_config(self):
        if not self._is_admin():
            return error_response("仅 Dashboard 管理员可保存关怀设置", status_code=403)
        if not self._write_is_same_origin():
            return error_response("请求来源校验失败，请从 AstrBot 管理面板保存", status_code=403)
        payload = await request.json(default=None)
        if (not isinstance(payload, dict) or set(payload) != {"expected_revision", "config"}):
            return error_response("保存内容格式不正确", status_code=400)
        try:
            config, revision = self.repository.save_config(
                payload.get("config"), payload.get("expected_revision")
            )
        except CareRevisionConflict as exc:
            return error_response(str(exc), status_code=409)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except CareStorageError:
            return error_response("本机关怀配置无法安全保存", status_code=503)
        return json_response({"config": config, "revision": revision})
