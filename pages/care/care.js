"use strict";

// This page uses only AstrBot's same-origin Plugin Page bridge. It does not
// keep credentials or configuration in browser storage.
const ruleDefinitions = [
  { id: "predicted_period_lead", name: "预测经期提前提醒", description: "仅按 OPPO 预测日期，在预计开始前 D-3、D-2、D-1 每天关怀一次；这是预测，不代表已开始。", leadDays: 3 },
  { id: "confirmed_period_start", name: "已记录经期开始", description: "本人在 OPPO 记录开始后，D1、D2、D3 每天关怀一次；不代替本人记录。" },
  { id: "confirmed_period_end", name: "已记录经期结束", description: "只对 OPPO 明确记录的实际结束事件关怀一次；来源没有结束事件时暂停并显示原因。" },
  { id: "period_late_inquiry", name: "经期后段随机询问", description: "从明确开始记录后的 D4 起，每个合格日按概率随机询问一次是否已结束；仅在 OPPO 明确结束记录前、配置观察窗内运行。不会因本人回答而替她修改 OPPO 记录。" },
  { id: "weight_date_linked", name: "减肥模式下的体重观察", description: "只由本人私聊明确说“我想减肥/我要减肥”开启；“我不减肥”关闭。仅观察开启后实际测量的新记录，不读取体重数值。" },
  { id: "wellness_low_state", name: "持续低状态关怀", description: "分值越高越好。可按 Slow down 分类或待确认的数值阈值；持续低时每周平均约 5 个随机时段，有新样本且间隔满足时同一持续阶段也可多次关怀。" },
  { id: "sunlight_evening", name: "日照记录关怀", description: "每天 20:00–21:00 随机检查；当天真实记录为 0–5 分钟（含边界）才关怀一次，超过 5 跳过，缺失不按 0。" },
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
    const measured = item.measured_at ? `<p class="data-meta">测量时间：${esc(item.measured_at)}</p>` : "";
    const observed = item.observed_at ? `<p class="data-meta">读取时间：${esc(prettyTime(item.observed_at))}</p>` : "";
    const labels = [item.category ? `分类：${item.category}` : "", item.quality ? `质量：${item.quality}` : "", item.completeness ? `完整性：${item.completeness}` : ""].filter(Boolean);
    return `<article class="data-card">
      <div class="data-top"><span class="data-name">${esc(item.name || item.source)}</span><span class="state-badge ${badgeClass}">${esc(status)}</span></div>
      <p class="data-value">${esc(item.reason || (item.status === "ok" ? "来源记录可用" : "没有可用于规则的记录"))}</p>
      ${date}${measured}${observed}${labels.map((label) => `<p class="data-meta">${esc(label)}</p>`).join("")}
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
    const weightModeEnabled = pageState?.existing_care?.find((item) => item.rule_id === "weight_mode")?.enabled === true;
    const effectiveEnabled = definition.id === "weight_date_linked" ? weightModeEnabled : rule.enabled;
    const ruleState = !effectiveEnabled ? "已关闭" : statusText(serverStatus?.state || "no_source");
    const leadControl = definition.leadDays
      ? field(`${definition.id}-lead_days`, "固定提前阶段", `<output id="${definition.id}-lead_days" class="read-only-value">D-3、D-2、D-1</output>`, "每天最多一次；此阶段不可调为别的天数。")
      : "";
    const wellnessControls = definition.id === "wellness_low_state" ? `
      ${field(`${definition.id}-mode`, "低值判定方式", `<select id="${definition.id}-mode" data-rule="${definition.id}" data-path="mode"><option value="unconfirmed" ${rule.mode === "unconfirmed" ? "selected" : ""}>尚未确认</option><option value="numeric" ${rule.mode === "numeric" ? "selected" : ""}>数值低值阈值</option><option value="slow_down_category" ${rule.mode === "slow_down_category" ? "selected" : ""}>只按 Slow down 分类</option></select>`, "分类方案不设分值顺序，只在来源明确显示 Slow down 时判为低；恢复按离开该分类并完成去抖。")}
      ${field(`${definition.id}-tz-confirmed`, "设备时区已核实", `<input id="${definition.id}-tz-confirmed" type="checkbox" data-rule="${definition.id}" data-path="timezone_confirmed" ${rule.timezone_confirmed ? "checked" : ""} />`, "状态来源没有写明设备时区；未勾选时不会解释测量时间。")}
      ${field(`${definition.id}-low`, "低值阈值（数值方案）", `<input id="${definition.id}-low" type="number" min="0" max="999" step="1" data-nullable="true" data-rule="${definition.id}" data-path="low_score_threshold" value="${rule.low_score_threshold ?? ""}" />`, "留空不会触发；当前没有确认官方分值范围。")}
      ${field(`${definition.id}-recovery`, "恢复阈值（数值方案）", `<input id="${definition.id}-recovery" type="number" min="0" max="999" step="1" data-nullable="true" data-rule="${definition.id}" data-path="recovery_score_threshold" value="${rule.recovery_score_threshold ?? ""}" />`, "需高于低值阈值。")}
      ${field(`${definition.id}-confirmation`, "持续低值时长（分钟）", `<input id="${definition.id}-confirmation" type="number" min="1" max="1440" step="1" data-nullable="true" data-rule="${definition.id}" data-path="confirmation_minutes" value="${rule.confirmation_minutes ?? ""}" />`)}
      ${field(`${definition.id}-samples`, "最少独立样本数", `<input id="${definition.id}-samples" type="number" min="2" max="100" step="1" data-nullable="true" data-rule="${definition.id}" data-path="minimum_independent_samples" value="${rule.minimum_independent_samples ?? ""}" />`, "同一测量时间重复轮询不会增加样本数。")}
      ${field(`${definition.id}-recovery-delay`, "恢复去抖（分钟）", `<input id="${definition.id}-recovery-delay" type="number" min="1" max="1440" step="1" data-nullable="true" data-rule="${definition.id}" data-path="recovery_debounce_minutes" value="${rule.recovery_debounce_minutes ?? ""}" />`)}
      ${field(`${definition.id}-sample-age`, "样本最长年龄（分钟）", `<input id="${definition.id}-sample-age" type="number" min="1" max="1440" step="1" data-nullable="true" data-rule="${definition.id}" data-path="maximum_sample_age_minutes" value="${rule.maximum_sample_age_minutes ?? ""}" />`, "同时限制相邻样本间隔。")}
      ${field(`${definition.id}-cooldown`, "同一持续低状态的重复关怀最短间隔（分钟）", `<input id="${definition.id}-cooldown" type="number" min="1" max="10080" step="1" data-nullable="true" data-rule="${definition.id}" data-path="repeat_cooldown_minutes" value="${rule.repeat_cooldown_minutes ?? ""}" />`, "同一段状态可再次关怀，但须有新的有效测量并达到此间隔。")}
      ${field(`${definition.id}-weekly`, "每周随机时段目标", `<input id="${definition.id}-weekly" type="number" min="1" max="7" step="1" data-rule="${definition.id}" data-path="weekly_target" value="${rule.weekly_target}" />`, "默认约 5 个随机候选时段；没有符合条件的新样本时跳过，不补满。")}
    ` : "";
    const latePeriodControls = definition.id === "period_late_inquiry" ? `
      ${field(`${definition.id}-start-day`, "后段询问起始日", `<input id="${definition.id}-start-day" type="number" min="4" max="30" step="1" data-rule="${definition.id}" data-path="start_day" value="${rule.start_day}" />`, "默认 D4；与 D1–D3 每日关怀互斥。")}
      ${field(`${definition.id}-probability`, "每日随机概率（0–1）", `<input id="${definition.id}-probability" type="number" min="0" max="1" step="0.05" data-rule="${definition.id}" data-path="probability" value="${rule.probability}" />`, "默认 0.5（50%）；每天按经期开始事件和日期生成固定随机结果，轮询不会重抽。")}
      ${field(`${definition.id}-cooldown`, "两次询问冷却（天）", `<input id="${definition.id}-cooldown" type="number" min="1" max="30" step="1" data-rule="${definition.id}" data-path="cooldown_days" value="${rule.cooldown_days}" />`, "默认 1 天；冷却内选中的机会会跳过，不顺延。")}
      ${field(`${definition.id}-observation-window`, "最长观察窗（周期日）", `<input id="${definition.id}-observation-window" type="number" min="4" max="60" step="1" data-rule="${definition.id}" data-path="observation_window_days" value="${rule.observation_window_days}" />`, "默认到 D14；超过后暂停询问，等待 OPPO 明确结束记录。")}
    ` : "";
    return `<article class="rule-card" data-rule-card="${definition.id}">
      <div class="rule-top">
        <div>
          <div class="rule-title-line"><h3>${esc(definition.name)}</h3><span class="rule-state ${effectiveEnabled ? "on" : ""}">${esc(ruleState)}</span></div>
          <p class="rule-description">${esc(definition.description)}</p>
          ${serverStatus?.expected_at ? `<p class="data-meta">预计：${esc(prettyTime(serverStatus.expected_at))}</p>` : ""}
          ${serverStatus?.reason ? `<p class="data-meta">${esc(serverStatus.reason)}</p>` : ""}
          ${serverStatus?.checked_at ? `<p class="data-meta">来源读取：${esc(prettyTime(serverStatus.checked_at))}</p>` : ""}
          ${serverStatus?.measured_at ? `<p class="data-meta">最新测量：${esc(serverStatus.measured_at)}</p>` : ""}
        </div>
        <label class="toggle-wrap"><span>${definition.id === "weight_date_linked" ? "本人模式" : "开启"}</span><input class="toggle" type="checkbox" data-rule="${definition.id}" data-path="enabled" ${effectiveEnabled ? "checked" : ""} ${definition.id === "weight_date_linked" ? "disabled" : ""} aria-label="开启${esc(definition.name)}" /></label>
      </div>
      <details class="details">
        <summary>时间与提醒细节</summary>
        <div class="details-grid">
          ${leadControl}
          ${wellnessControls}
          ${latePeriodControls}
          ${field(`${definition.id}-timezone`, "时区", `<input id="${definition.id}-timezone" data-rule="${definition.id}" data-path="timezone" type="text" value="${esc(rule.timezone)}" autocomplete="off" />`, "使用 IANA 时区，例如 Asia/Shanghai。")}
          ${definition.id === "sunlight_evening"
            ? field(`${definition.id}-window`, "随机检查时段", '<output class="read-only-value">20:00–21:00</output>', "此规则时段固定为每天 20:00–21:00。")
            : field(`${definition.id}-window-start`, "可发送时段", `<div class="quiet-row"><input id="${definition.id}-window-start" aria-label="可发送时段开始" data-rule="${definition.id}" data-path="send_window.start" type="time" value="${esc(rule.send_window.start)}" /><input id="${definition.id}-window-end" aria-label="可发送时段结束" data-rule="${definition.id}" data-path="send_window.end" type="time" value="${esc(rule.send_window.end)}" /></div>`, "开始至结束；此处只是规则设置。")}
          ${definition.id === "wellness_low_state" ? "" : field(`${definition.id}-max`, "该类事件每日上限", `<output id="${definition.id}-max" class="read-only-value">1 次</output>`, "按用户设定每个固定类别每日最多一次。")}
          ${definition.id === "wellness_low_state" || definition.id === "period_late_inquiry" ? "" : field(`${definition.id}-duplicate`, "重复保护时长（分钟）", `<input id="${definition.id}-duplicate" data-rule="${definition.id}" data-path="duplicate_window_minutes" type="number" min="1" max="1440" step="1" value="${rule.duplicate_window_minutes}" />`, "避免同一来源事件短时间重复安排。")}
          ${field(`${definition.id}-quiet-start`, "免打扰时段", `<div class="quiet-row"><input id="${definition.id}-quiet-start" aria-label="免打扰开始" data-rule="${definition.id}" data-path="quiet_hours.start" type="time" value="${esc(rule.quiet_hours.start)}" /><input id="${definition.id}-quiet-end" aria-label="免打扰结束" data-rule="${definition.id}" data-path="quiet_hours.end" type="time" value="${esc(rule.quiet_hours.end)}" /></div>`, "跨午夜时也可设置，例如 22:00 至 08:00。")}
          ${field(`${definition.id}-tone`, "语气", `<select id="${definition.id}-tone" data-rule="${definition.id}" data-path="tone"><option value="gentle" ${rule.tone === "gentle" ? "selected" : ""}>温和克制</option><option value="brief" ${rule.tone === "brief" ? "selected" : ""}>简短自然</option></select>`)}
          ${field(`${definition.id}-stale`, "数据过期阈值（小时）", `<input id="${definition.id}-stale" data-rule="${definition.id}" data-path="stale_after_hours" type="number" min="1" max="336" step="1" value="${rule.stale_after_hours}" />`, "超过后暂停使用来源记录。")}
          ${field(`${definition.id}-dedup`, "去重方式", `<select id="${definition.id}-dedup" data-rule="${definition.id}" data-path="deduplication"><option value="per_event" ${rule.deduplication === "per_event" ? "selected" : ""}>每条来源事件一次</option><option value="per_local_date" ${rule.deduplication === "per_local_date" ? "selected" : ""}>每个本地日期一次</option></select>`)}
          ${field(`${definition.id}-reschedule`, "错过时段后", `<select id="${definition.id}-reschedule" data-rule="${definition.id}" data-path="reschedule"><option value="skip_if_late" selected>跳过，不补发</option></select>`, "错过本次窗口不会在之后的时段补发。")}
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
        : (control.type === "number" ? (control.dataset.nullable === "true" && control.value === "" ? null : Number(control.value)) : control.value);
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
  byId("enabled-count").textContent = `${enabled} / ${ruleDefinitions.length}`;
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
    ok: "来源可用",
    unavailable: "来源不可用",
    unsupported: "来源尚不支持",
    not_configured: "来源未启用",
    error: "来源读取异常",
    stale: "来源已过期",
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
    parameters_unconfirmed: "参数尚未确认",
    timezone_unconfirmed: "时区尚未确认",
    invalid_settings: "设置无效，已暂停",
    paused_window: "观察窗已过，等待明确结束记录",
    ended: "OPPO 已记录结束，后段询问已停止",
    waiting_for_start: "等待 OPPO 明确记录开始",
    waiting_for_late_phase: "等待后段询问起始日",
    not_selected: "本日未命中随机机会",
    missed_window: "已错过今天，不补发",
    invalid_timezone: "时区无效，已暂停",
    paused_source: "来源数据暂不可解释",
    confirming_low_state: "正在确认持续低值",
    low_confirmed: "持续条件已满足，等待随机时段",
    episode_latched: "持续状态跟踪中，等待新样本与最短间隔",
    recovered: "恢复已确认，等待新的下降",
    duplicate_sample: "同一来源测量，未增加样本",
    waiting_for_low_state: "等待低值条件",
    waiting_for_window: "等待随机时间窗口",
    candidate: "已安排候选关怀",
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
      || JSON.stringify(Object.keys(candidate).sort()) !== JSON.stringify(["rules", "schema_version"])
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
    if (id === "predicted_period_lead" && rule.lead_days !== 3) {
      throw new Error(`${name}：提醒阶段固定为 D-3、D-2、D-1。`);
    }
    if (id === "period_late_inquiry") {
      if (!Number.isInteger(rule.start_day) || rule.start_day < 4 || rule.start_day > 30) {
        throw new Error(`${name}：询问起始日需在 D4 至 D30。`);
      }
      if (typeof rule.probability !== "number" || !Number.isFinite(rule.probability)
          || rule.probability < 0 || rule.probability > 1) {
        throw new Error(`${name}：每日随机概率需在 0 至 1。`);
      }
      if (!Number.isInteger(rule.cooldown_days) || rule.cooldown_days < 1 || rule.cooldown_days > 30) {
        throw new Error(`${name}：冷却天数需在 1 至 30。`);
      }
      if (!Number.isInteger(rule.observation_window_days)
          || rule.observation_window_days < rule.start_day || rule.observation_window_days > 60) {
        throw new Error(`${name}：观察窗需不短于起始日且不超过 60 天。`);
      }
    }
    if (id === "sunlight_evening"
        && (rule.send_window?.start !== "20:00" || rule.send_window?.end !== "21:00")) {
      throw new Error(`${name}：随机检查时段固定为 20:00–21:00。`);
    }
    const expectedDailyCap = id === "wellness_low_state" ? 7 : 1;
    if (!Number.isInteger(rule.max_per_day) || rule.max_per_day !== expectedDailyCap) {
      throw new Error(`${name}：该类每日次数设置不符合固定规则。`);
    }
    if (!Number.isInteger(rule.duplicate_window_minutes) || rule.duplicate_window_minutes < 1 || rule.duplicate_window_minutes > 1440) {
      throw new Error(`${name}：重复保护时长需在 1 至 1440 分钟之间。`);
    }
    if (!Number.isInteger(rule.stale_after_hours) || rule.stale_after_hours < 1 || rule.stale_after_hours > 336) {
      throw new Error(`${name}：过期阈值需在 1 至 336 小时之间。`);
    }
    if (id === "wellness_low_state") {
      if (!["unconfirmed", "numeric", "slow_down_category"].includes(rule.mode)
          || typeof rule.timezone_confirmed !== "boolean") {
        throw new Error(`${name}：判定方式或设备时区确认状态无效。`);
      }
      const nullableRanges = {
        low_score_threshold: [0, 999], recovery_score_threshold: [0, 999],
        confirmation_minutes: [1, 1440], minimum_independent_samples: [2, 100],
        recovery_debounce_minutes: [1, 1440], maximum_sample_age_minutes: [1, 1440],
        repeat_cooldown_minutes: [1, 10080],
      };
      for (const [key, [minimum, maximum]] of Object.entries(nullableRanges)) {
        const value = rule[key];
        if (value !== null && (!Number.isInteger(value) || value < minimum || value > maximum)) {
          throw new Error(`${name}：${key} 需留空或填写有效整数。`);
        }
      }
      if (rule.low_score_threshold !== null && rule.recovery_score_threshold !== null
          && rule.recovery_score_threshold <= rule.low_score_threshold) {
        throw new Error(`${name}：恢复阈值必须高于低值阈值。`);
      }
      if (!Number.isInteger(rule.weekly_target) || rule.weekly_target < 1 || rule.weekly_target > 7) {
        throw new Error(`${name}：每周随机时段目标需在 1 至 7 之间。`);
      }
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
        || rule.reschedule !== "skip_if_late") {
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
    const reread = await pluginPage.apiGet("care/state");
    if (reread.revision !== saved.revision
        || JSON.stringify(reread.config) !== JSON.stringify(saved.config)) {
      throw new Error("保存成功后回读的版本或规则与保存结果不一致；请重新读取设置。");
    }
    pageState = reread;
    careConfig = clone(reread.config);
    revision = reread.revision;
    isDirty = false;
    byId("save-message").textContent = "已保存并回读核对；运行时仍需通过安全预检";
    renderData();
    renderExistingCare();
    renderRules();
    renderHistory();
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
