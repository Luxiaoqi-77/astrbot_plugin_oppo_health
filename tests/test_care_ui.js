"use strict";

const assert = require("node:assert/strict");
const { validateConfigClient } = require("../pages/care/care.js");

function rule(lead = false) {
  return {
    enabled: false,
    timezone: "Asia/Shanghai",
    send_window: { start: "09:00", end: "21:00" },
    max_per_day: 1,
    duplicate_window_minutes: 30,
    quiet_hours: { start: "22:00", end: "08:00" },
    tone: "gentle",
    stale_after_hours: 48,
    deduplication: "per_event",
    reschedule: "skip_if_late",
    ...(lead ? { lead_days: 3 } : {}),
  };
}

function wellnessRule() {
  return {
    ...rule(),
    max_per_day: 7,
    mode: "unconfirmed",
    timezone_confirmed: false,
    low_score_threshold: null,
    confirmation_minutes: null,
    minimum_independent_samples: null,
    recovery_score_threshold: null,
    recovery_debounce_minutes: null,
    maximum_sample_age_minutes: null,
    repeat_cooldown_minutes: null,
    weekly_target: 5,
  };
}

function latePeriodRule() {
  return {
    ...rule(),
    start_day: 4,
    probability: 0.5,
    cooldown_days: 1,
    observation_window_days: 14,
  };
}

const config = {
  schema_version: 1,
  rules: {
    predicted_period_lead: rule(true),
    confirmed_period_start: rule(),
    confirmed_period_end: rule(),
    period_late_inquiry: latePeriodRule(),
    weight_date_linked: rule(),
    wellness_low_state: wellnessRule(),
    sunlight_evening: { ...rule(), send_window: { start: "20:00", end: "21:00" }, quiet_hours: { start: "21:00", end: "08:00" } },
  },
};

assert.equal(validateConfigClient(config), true);

const badTimezone = structuredClone(config);
badTimezone.rules.weight_date_linked.timezone = "Mars/Olympus";
assert.throws(() => validateConfigClient(badTimezone), /时区/);

const overlap = structuredClone(config);
overlap.rules.confirmed_period_start.send_window = { start: "07:00", end: "09:00" };
assert.throws(() => validateConfigClient(overlap), /重叠/);

const tooMany = structuredClone(config);
tooMany.rules.confirmed_period_end.max_per_day = 2;
assert.throws(() => validateConfigClient(tooMany), /固定规则/);

const missingDuplicateWindow = structuredClone(config);
delete missingDuplicateWindow.rules.weight_date_linked.duplicate_window_minutes;
assert.throws(() => validateConfigClient(missingDuplicateWindow), /重复保护/);

const obsoleteBudget = structuredClone(config);
obsoleteBudget.health_budget = { enabled: true };
assert.throws(() => validateConfigClient(obsoleteBudget), /格式不完整/);

const invalidSunlightWindow = structuredClone(config);
invalidSunlightWindow.rules.sunlight_evening.send_window = { start: "19:00", end: "20:00" };
assert.throws(() => validateConfigClient(invalidSunlightWindow), /随机检查时段固定/);

const invalidLateStart = structuredClone(config);
invalidLateStart.rules.period_late_inquiry.start_day = 3;
assert.throws(() => validateConfigClient(invalidLateStart), /起始日/);

const invalidLateProbability = structuredClone(config);
invalidLateProbability.rules.period_late_inquiry.probability = 1.1;
assert.throws(() => validateConfigClient(invalidLateProbability), /随机概率/);

const invalidLateWindow = structuredClone(config);
invalidLateWindow.rules.period_late_inquiry.observation_window_days = 3;
assert.throws(() => validateConfigClient(invalidLateWindow), /观察窗/);

console.log("care UI validation: 10 checks passed");
