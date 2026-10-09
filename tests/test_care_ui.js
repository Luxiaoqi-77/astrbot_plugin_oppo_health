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
    reschedule: "next_allowed_window",
    ...(lead ? { lead_days: 3 } : {}),
  };
}

const config = {
  schema_version: 1,
  rules: {
    predicted_period_lead: rule(true),
    confirmed_period_start: rule(),
    confirmed_period_end: rule(),
    weight_date_linked: rule(),
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
tooMany.rules.confirmed_period_end.max_per_day = 6;
assert.throws(() => validateConfigClient(tooMany), /频率上限/);

const missingDuplicateWindow = structuredClone(config);
delete missingDuplicateWindow.rules.weight_date_linked.duplicate_window_minutes;
assert.throws(() => validateConfigClient(missingDuplicateWindow), /重复保护/);

console.log("care UI validation: 5 checks passed");
