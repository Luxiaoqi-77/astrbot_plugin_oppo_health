"use strict";

// This page uses only AstrBot's same-origin Plugin Page bridge. It does not
// keep credentials or configuration in browser storage.
const ruleDefinitions = [
  { id: "predicted_period_lead", name: "预测经期提前提醒", description: "只按来源明确给出的预测日期提前提醒，不代表经期已开始。", leadDays: 3 },
  { id: "confirmed_period_start", name: "已确认经期开始", description: "只有收到明确的开始标记才可能安排；不会按日历推断。" },
  { id: "confirmed_period_end", name: "已确认经期结束", description: "只有收到明确的结束标记才可能安排；缺少标记时不推断结束。" },
  { id: "weight_date_linked", name: "体重按记录日期联动", description: "跟随体重记录本身的日期；语气温和，不评价数值。" },
];

let pluginPage = null;
let careConfig = null;
let revision = 0;
let isDirty = false;
let pageState = null;

const byId = (id) => document.getElementById(id);
const clone = (value) => JSON.parse(JSON.stringify(value));
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));

function prettyTime(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium", timeStyle: "short",
  }).format(parsed);
}

function setError(message) {
  const error = byId("page-error");
  error.textContent = message || "";
  error.hidden = !message;
}

function setDirty(message = "设置有未保存的更改") {
  isDirty = true;
  byId("save-message").textContent = message;
  byId("preview-message").hidden = true;
  renderSummary();
}

function renderData() {
  const sources = pageState?.data_sources || [];
  if (!sources.length) {
    byId("data-grid").innerHTML = '<article class="data-card"><div class="data-top"><span class="data-name">来源状态</span><span class="state-badge missing">暂不可用</span></div><p class="data-value">尚未连接健康数据来源</p><p class="data-meta">没有可显示的日期或数值</p></article>';
    return;
  }
  const statusLabels = {
    ok: "可用",
    unavailable: "暂不可用",
    stale: "已过期",
    unsupported: "来源尚不支持",
    not_configured: "未开启",
    error: "读取异常",
  };
  byId("data-grid").innerHTML = sources.map((item) => {
    const status = item.is_stale === true ? "已过期" : (statusLabels[item.status] || "暂不可用");
    const badgeClass = item.is_stale === true || item.status === "stale" ? "stale" : (item.status === "ok" ? "fresh" : "missing");
    const date = item.data_date ? `<p class="data-meta">数据日期：<strong>${esc(item.data_date)}</strong></p>` : '<p class="data-meta">数据日期：暂无</p>';
    const observed = item.observed_at ? `<p class="data-meta">读取时间：${esc(prettyTime(item.observed_at))}</p>` : "";
    return `<article class="data-card">
      <div class="data-top"><span class="data-name">${esc(item.name || item.source)}</span><span class="state-badge ${badgeClass}">${esc(status)}</span></div>
      <p class="data-value">${esc(item.reason || (item.status === "ok" ? "来源记录可用" : "没有可用于规则的记录"))}</p>
      ${date}${observed}
    </article>`;
  }).join("");
}

function renderExistingCare() {
  const items = pageState?.existing_care || [];
  byId("existing-care-grid").innerHTML = items.map((item) => `<article class="data-card">
    <div class="data-top"><span class="data-name">${esc(item.name)}</span><span class="state-badge ${item.enabled ? "fresh" : "missing"}">${item.enabled ? "已开启" : "已关闭"}</span></div>
    <p class="data-value">${esc(item.reason || "沿用插件现有设置")}</p>
  </article>`).join("") || '<article class="data-card"><p class="data-value">未读取到现有设置</p></article>';
}

function field(id, label, control, help = "") {
  return `<div class="field ${id.endsWith("timezone") ? "span-2" : ""}">
    <label for="${esc(id)}">${esc(label)}</label>${control}${help ? `<small>${esc(help)}</small>` : ""}
  </div>`;
}

function renderRules() {
  if (!careConfig) return;
  const stateById = Object.fromEntries((pageState?.rules || []).map((row) => [row.rule_id, row]));
  byId("rule-list").innerHTML = ruleDefinitions.map((definition) => {
    const rule = careConfig.rules[definition.id];
    const serverStatus = stateById[definition.id];
    const ruleState = !rule.enabled ? "已关闭" : statusText(serverStatus?.state || "no_source");
    const leadControl = definition.leadDays
      ? field(`${definition.id}-lead_days`, "提前天数", `<input id="${definition.id}-lead_days" data-rule="${definition.id}" data-path="lead_days" type="number" min="1" max="14" step="1" value="${rule.lead_days}" />`, "默认 3 天；只用于有预测日期的提醒。")
      : "";
    return `<article class="rule-card" data-rule-card="${definition.id}">
      <div class="rule-top">
        <div>
          <div class="rule-title-line"><h3>${esc(definition.name)}</h3><span class="rule-state ${rule.enabled ? "on" : ""}">${esc(ruleState)}</span></div>
          <p class="rule-description">${esc(definition.description)}</p>
          ${serverStatus?.expected_at ? `<p class="data-meta">预计：${esc(prettyTime(serverStatus.expected_at))}</p>` : (serverStatus?.reason ? `<p class="data-meta">${esc(serverStatus.reason)}</p>` : "")}
        </div>
        <label class="toggle-wrap"><span>开启</span><input class="toggle" type="checkbox" data-rule="${definition.id}" data-path="enabled" ${rule.enabled ? "checked" : ""} aria-label="开启${esc(definition.name)}" /></label>
      </div>
      <details class="details">
        <summary>时间与提醒细节</summary>
        <div class="details-grid">
          ${leadControl}
          ${field(`${definition.id}-timezone`, "时区", `<input id="${definition.id}-timezone" data-rule="${definition.id}" data-path="timezone" type="text" value="${esc(rule.timezone)}" autocomplete="off" />`, "使用 IANA 时区，例如 Asia/Shanghai。")}
          ${field(`${definition.id}-window-start`, "可发送时段", `<div class="quiet-row"><input id="${definition.id}-window-start" aria-label="可发送时段开始" data-rule="${definition.id}" data-path="send_window.start" type="time" value="${esc(rule.send_window.start)}" /><input id="${definition.id}-window-end" aria-label="可发送时段结束" data-rule="${definition.id}" data-path="send_window.end" type="time" value="${esc(rule.send_window.end)}" /></div>`, "开始至结束；此处只是规则设置。")}
          ${field(`${definition.id}-max`, "每日频率上限", `<input id="${definition.id}-max" data-rule="${definition.id}" data-path="max_per_day" type="number" min="1" max="5" step="1" value="${rule.max_per_day}" />`, "失败尝试也会计入上限。")}
          ${field(`${definition.id}-duplicate`, "重复保护时长（分钟）", `<input id="${definition.id}-duplicate" data-rule="${definition.id}" data-path="duplicate_window_minutes" type="number" min="1" max="1440" step="1" value="${rule.duplicate_window_minutes}" />`, "避免短时间重复安排。")}
          ${field(`${definition.id}-quiet-start`, "免打扰时段", `<div class="quiet-row"><input id="${definition.id}-quiet-start" aria-label="免打扰开始" data-rule="${definition.id}" data-path="quiet_hours.start" type="time" value="${esc(rule.quiet_hours.start)}" /><input id="${definition.id}-quiet-end" aria-label="免打扰结束" data-rule="${definition.id}" data-path="quiet_hours.end" type="time" value="${esc(rule.quiet_hours.end)}" /></div>`, "跨午夜时也可设置，例如 22:00 至 08:00。")}
          ${field(`${definition.id}-tone`, "语气", `<select id="${definition.id}-tone" data-rule="${definition.id}" data-path="tone"><option value="gentle" ${rule.tone === "gentle" ? "selected" : ""}>温和克制</option><option value="brief" ${rule.tone === "brief" ? "selected" : ""}>简短自然</option></select>`)}
          ${field(`${definition.id}-stale`, "数据过期阈值（小时）", `<input id="${definition.id}-stale" data-rule="${definition.id}" data-path="stale_after_hours" type="number" min="1" max="336" step="1" value="${rule.stale_after_hours}" />`, "超过后暂停使用来源记录。")}
          ${field(`${definition.id}-dedup`, "去重方式", `<select id="${definition.id}-dedup" data-rule="${definition.id}" data-path="deduplication"><option value="per_event" ${rule.deduplication === "per_event" ? "selected" : ""}>每条来源事件一次</option><option value="per_local_date" ${rule.deduplication === "per_local_date" ? "selected" : ""}>每个本地日期一次</option></select>`)}
          ${field(`${definition.id}-reschedule`, "错过时段后", `<select id="${definition.id}-reschedule" data-rule="${definition.id}" data-path="reschedule"><option value="next_allowed_window" ${rule.reschedule === "next_allowed_window" ? "selected" : ""}>顺延到下一可用时段</option><option value="skip_if_late" ${rule.reschedule === "skip_if_late" ? "selected" : ""}>跳过，不补发</option></select>`)}
        </div>
      <div class="rule-footer"><span>来源明确且未过期时才可能进入计划</span><span>${pageState?.privacy_status?.health_context_to_model_enabled ? "当前安全预检通过" : "当前安全预检未通过，已暂停"}</span></div>
      </details>
    </article>`;
  }).join("");
  attachInputHandlers();
  renderSummary();
}

function setPath(target, path, value) {
  const parts = path.split(".");
  let cursor = target;
  for (const part of parts.slice(0, -1)) cursor = cursor[part];
  cursor[parts.at(-1)] = value;
}

function attachInputHandlers() {
  document.querySelectorAll("[data-rule][data-path]").forEach((control) => {
    control.addEventListener("change", () => {
      const value = control.type === "checkbox" ? control.checked
        : (control.type === "number" ? Number(control.value) : control.value);
      setPath(careConfig.rules[control.dataset.rule], control.dataset.path, value);
      setDirty();
      const card = control.closest(".rule-card");
      const badge = card?.querySelector(".rule-state");
      const enabled = careConfig.rules[control.dataset.rule].enabled;
      if (badge) {
        badge.textContent = !enabled ? "已关闭" : "等待来源接入";
        badge.classList.toggle("on", enabled);
      }
    });
  });
}

function renderSummary(nextTime = null) {
  if (!careConfig) return;
  const enabled = ruleDefinitions.filter(({ id }) => careConfig.rules[id].enabled).length;
  const scheduled = pageState?.rules?.map((row) => row.expected_at).filter(Boolean).sort()[0];
  byId("enabled-count").textContent = `${enabled} / 4`;
  byId("next-preview").textContent = nextTime || (scheduled ? prettyTime(scheduled) : (enabled ? "等待来源或安全预检" : "还没有安排"));
}

function outcomeLabel(outcome) {
  return ({
    handed_off: "已交给流程（未确认送达）",
    failed: "失败",
    failed_uncertain: "结果不确定，已暂停重试",
    paused_privacy: "安全预检未通过，已暂停",
    skipped: "已跳过",
    dispatching: "处理中",
  })[outcome] || outcome || "状态未知";
}

function renderHistory() {
  const entries = pageState?.recent_attempts || [];
  if (!entries.length) {
    byId("history-list").innerHTML = '<article class="history-row"><div class="history-main"><p class="history-title">暂时没有关怀发送记录</p><p class="history-reason">新规则没有连接发送流程。</p></div></article>';
    return;
  }
  byId("history-list").innerHTML = entries.slice().reverse().map((item) => {
    const definition = ruleDefinitions.find((row) => row.id === item.rule_id);
    const name = definition?.name || item.rule_id || "关怀记录";
    return `<article class="history-row">
      <div class="history-main"><p class="history-title">${esc(name)}${item.demo ? " · 演示" : ""}</p><p class="history-reason">${esc(item.reason || "没有更多说明")}</p></div>
      <div class="history-meta"><span class="history-outcome">${esc(outcomeLabel(item.outcome))}</span><time>${esc(prettyTime(item.at))}</time></div>
    </article>`;
  }).join("");
}

function statusText(state) {
  return ({
    disabled: "已关闭",
    no_source: "等待来源接入",
    stale_source: "来源已过期",
    paused_stale: "来源已过期，已暂停",
    paused_source_missing: "来源暂不可用",
    paused_invalid: "来源信息无效，已暂停",
    paused_privacy: "安全预检未通过，已暂停",
    pending: "等待发送时段",
    handed_off: "已交给流程（未确认送达）",
    failed: "上次尝试失败",
    failed_uncertain: "结果不确定，已暂停重试",
    skipped: "上次计划已跳过",
    cancelled: "计划已取消",
  })[state] || "暂无安排";
}

async function loadState() {
  pluginPage = window.AstrBotPluginPage;
  if (!pluginPage || typeof pluginPage.ready !== "function") {
    throw new Error("请从 AstrBot 管理面板打开本页面。");
  }
  await pluginPage.ready();
  pageState = await pluginPage.apiGet("care/state");
  careConfig = clone(pageState.config);
  revision = pageState.revision;
  byId("connection-status").innerHTML = '<span class="demo-dot" aria-hidden="true"></span>已连接本机 AstrBot';
  byId("evaluated-at").textContent = `状态读取：${prettyTime(pageState.evaluated_at)}`;
  const approval = pageState.model_access?.approval_state;
  const privacy = pageState.privacy_status || {};
  const routeHost = pageState.model_access?.current_host;
  const routeCheckedAt = privacy.route_observed_at
    ? ` · 核验于 ${prettyTime(privacy.route_observed_at)}`
    : "";
  byId("host-status").textContent = routeHost
    ? (approval === "approved"
      ? `当前 host ${routeHost} 已获本机批准${routeCheckedAt}`
      : `当前 host ${routeHost} 未获本机批准${routeCheckedAt}`)
    : (privacy.last_route_error_code === "preflight_unavailable"
      ? "核心路由预检尚未接通"
      : "当前模型路由或授权状态不可用");
  byId("runtime-state-title").textContent = privacy.health_context_to_model_enabled
    ? "本机安全预检已通过"
    : "新增关怀已暂停";
  byId("runtime-state-description").textContent = privacy.health_context_to_model_enabled
    ? "启用的规则会按本机来源安排；实际交给 AstrBot 后，平台送达状态仍需单独确认。"
    : (privacy.last_route_error_code === "preflight_unavailable"
      ? "核心只读隐私预检尚未接通或未返回有效状态；系统不会采集来源数据或提交关怀。"
      : (privacy.last_route_error_code === "runner_unsupported"
        ? "当前内部回复路径未通过安全检查；系统不会采集来源数据或提交关怀。"
        : "来源、内部回复路径或当前模型授权预检未全部通过；系统不会采集来源数据或提交关怀。"));
  renderData();
  renderExistingCare();
  renderRules();
  renderHistory();
  byId("preview-button").disabled = false;
  byId("save-button").disabled = false;
  byId("save-message").textContent = "设置已从本机读取";
}

function validateConfigClient(candidate) {
  if (!candidate || candidate.schema_version !== 1 || !candidate.rules
      || Object.keys(candidate.rules).length !== ruleDefinitions.length) {
    throw new Error("设置格式不完整，请重新读取页面。");
  }
  const toMinutes = (value) => {
    if (typeof value !== "string" || !/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(value)) return null;
    return Number(value.slice(0, 2)) * 60 + Number(value.slice(3));
  };
  for (const { id, name } of ruleDefinitions) {
    const rule = candidate.rules[id];
    if (!rule || typeof rule.enabled !== "boolean") throw new Error(`${name}：设置不完整。`);
    if (id === "predicted_period_lead" && (!Number.isInteger(rule.lead_days) || rule.lead_days < 1 || rule.lead_days > 14)) {
      throw new Error(`${name}：提前天数需在 1 至 14 天之间。`);
    }
    if (!Number.isInteger(rule.max_per_day) || rule.max_per_day < 1 || rule.max_per_day > 5) {
      throw new Error(`${name}：频率上限需在每天 1 至 5 次之间。`);
    }
    if (!Number.isInteger(rule.duplicate_window_minutes) || rule.duplicate_window_minutes < 1 || rule.duplicate_window_minutes > 1440) {
      throw new Error(`${name}：重复保护时长需在 1 至 1440 分钟之间。`);
    }
    if (!Number.isInteger(rule.stale_after_hours) || rule.stale_after_hours < 1 || rule.stale_after_hours > 336) {
      throw new Error(`${name}：过期阈值需在 1 至 336 小时之间。`);
    }
    try { new Intl.DateTimeFormat("zh-CN", { timeZone: rule.timezone }); }
    catch { throw new Error(`${name}：请填写有效的 IANA 时区。`); }
    const sendStart = toMinutes(rule.send_window?.start);
    const sendEnd = toMinutes(rule.send_window?.end);
    const quietStart = toMinutes(rule.quiet_hours?.start);
    const quietEnd = toMinutes(rule.quiet_hours?.end);
    if (sendStart === null || sendEnd === null || sendStart >= sendEnd || quietStart === null || quietEnd === null || quietStart === quietEnd) {
      throw new Error(`${name}：请检查发送和免打扰时段。`);
    }
    const inQuiet = (minute) => quietStart < quietEnd
      ? minute >= quietStart && minute < quietEnd
      : minute >= quietStart || minute < quietEnd;
    for (let minute = sendStart; minute < sendEnd; minute += 1) {
      if (inQuiet(minute)) throw new Error(`${name}：发送时段不能与免打扰时段重叠。`);
    }
    if (!["gentle", "brief"].includes(rule.tone)
        || !["per_event", "per_local_date"].includes(rule.deduplication)
        || !["next_allowed_window", "skip_if_late"].includes(rule.reschedule)) {
      throw new Error(`${name}：请检查语气、去重和顺延选项。`);
    }
  }
  return true;
}

async function saveConfig() {
  setError("");
  try {
    validateConfigClient(careConfig);
    byId("save-button").disabled = true;
    byId("save-message").textContent = "正在保存到本机…";
    const saved = await pluginPage.apiPost("care/config", {
      expected_revision: revision,
      config: careConfig,
    });
    careConfig = clone(saved.config);
    revision = saved.revision;
    isDirty = false;
    byId("save-message").textContent = "已保存到本机；运行时仍需通过安全预检";
    renderRules();
  } catch (error) {
    byId("save-message").textContent = error?.message || "保存失败，请重试。";
  } finally {
    byId("save-button").disabled = false;
  }
}

async function previewPlan() {
  setError("");
  try {
    const result = await pluginPage.apiGet("care/preview");
    const lines = (result.plans || []).map((plan) => {
      const definition = ruleDefinitions.find((row) => row.id === plan.rule_id);
      const time = plan.expected_at ? ` · ${prettyTime(plan.expected_at)}` : "";
      return `${definition?.name || plan.rule_id}：${statusText(plan.state)}${time}。${plan.reason || ""}`;
    });
    byId("preview-message").innerHTML = lines.map((line) => `<div class="inline-line">${esc(line)}</div>`).join("")
      + `<div class="inline-line">${esc(result.notice || "不会发送消息。")}</div>`;
    byId("preview-message").hidden = false;
    const next = (result.plans || []).find((row) => row.expected_at)?.expected_at;
    renderSummary(next ? prettyTime(next) : "尚无来源记录");
  } catch (error) {
    setError(error?.message || "无法生成规则预览。");
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { validateConfigClient };
}

if (typeof document !== "undefined") {
  byId("preview-button").addEventListener("click", previewPlan);
  byId("save-button").addEventListener("click", saveConfig);
  loadState().catch((error) => {
    setError(error?.message || "无法读取本机配置。");
    byId("connection-status").innerHTML = '<span class="demo-dot" aria-hidden="true"></span>本机页面暂不可用';
    byId("save-message").textContent = "未连接；页面没有保存更改";
  });
}
