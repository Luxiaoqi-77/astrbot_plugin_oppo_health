# Changelog

## 0.4.0 — Prepared release candidate

- Keep the sustained wellness-state rule disabled by default with blank, user-confirmed thresholds and recovery settings; missing quality labels are displayed as an evidence limit.
- Remove the superseded global daily-2 / 240-minute cap. New category rules use their own limits and can occur without a shared cross-category budget.
- Treat missing wellness quality labels as a visible evidence limitation rather than permanently disabling state care; support `Slow down` classification and repeat reminders for new samples within the same sustained episode after cooldown.
- Add exact sleep 50% per-record choice, random 60–120 minute wake delay, local-day goodnight silence, actual weight measurement-time opt-in filtering, explicit source-date/time/completeness status, and deterministic offline regressions.
- Honor explicit menstrual start/end events only; the current collector does not provide them, so those paths remain paused with a reason.
- Add the late-period inquiry rule: start at configurable D4 by default, draw one stable 0.5 daily opportunity, enforce a configurable cooldown/observation window, cancel pending inquiry on explicit OPPO end, and fail closed when either explicit event channel is absent.
- Page save now performs a state GET and compares the saved revision/config. Real AstrBot iframe and device-page integration remain unverified.
- Keep scheduler event history bounded by removing absent jobs after their windows have expired for 180 days; this exceeds the maximum configurable 60-day cycle observation window and preserves current-source event deduplication.
- Offline validation: 157 Python tests ran, with 156 passing and one Windows-only skip; 10 UI checks passed.

## 0.3.0 — Release candidate

- Integrate the locally deployed privacy-gated health request flow, care settings page, and four configurable care rules.
- Keep `approved_provider_hosts` empty by default; keep the local health page and all new care rules disabled until the user enables them locally.
- Require the matching AstrBot v4.28.2 core privacy patch for model-bound health context and page preflight. The core patch is distributed separately under AGPL-3.0-or-later; this plugin remains MIT.
- Preserve v0.2.0 Windows DPAPI storage and immediate `/oppohealth` queries; add only the sleep record metadata required by freshness-checked wake care.
- Keep the `oppo-health-cloud-mcp` source attribution and MIT notice.

### Migration

For v0.4.0, a schema-v1 v0.3 configuration is read into the canonical rule set: missing categories stay disabled, the old shared `health_budget` is discarded, and fixed period/sunlight settings are normalized. Reading alone does not rewrite the local JSON; the next explicit page save writes the migrated configuration. Weight mode remains off until the person directly opts in again, which establishes a fresh measurement-time boundary.

1. Back up the plugin code and private local configuration/state outside this repository.
2. Apply and validate the separate v4.28.2 core patch before enabling model-bound health features.
3. Update the plugin, keep pages and new rules off initially, then configure only the provider hosts the user explicitly approves in local settings.
4. Enable only the desired page and care rules after local validation. Never commit credentials, private host values, account identifiers, or health records.

For recovery, disable the plugin or the affected page/rules, restore the prior plugin code and private local backup, and reverse the core patch only when its target files have not changed since application. See the plugin README for the full install and recovery steps.
