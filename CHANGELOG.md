# Changelog

## 0.3.0 — Release candidate

- Integrate the locally deployed privacy-gated health request flow, care settings page, and four configurable care rules.
- Keep `approved_provider_hosts` empty by default; keep the local health page and all new care rules disabled until the user enables them locally.
- Require the matching AstrBot v4.28.2 core privacy patch for model-bound health context and page preflight. The core patch is distributed separately under AGPL-3.0-or-later; this plugin remains MIT.
- Preserve v0.2.0 Windows DPAPI storage and immediate `/oppohealth` queries; add only the sleep record metadata required by freshness-checked wake care.
- Keep the `oppo-health-cloud-mcp` source attribution and MIT notice.

### Migration

1. Back up the plugin code and private local configuration/state outside this repository.
2. Apply and validate the separate v4.28.2 core patch before enabling model-bound health features.
3. Update the plugin, keep pages and new rules off initially, then configure only the provider hosts the user explicitly approves in local settings.
4. Enable only the desired page and care rules after local validation. Never commit credentials, private host values, account identifiers, or health records.

For recovery, disable the plugin or the affected page/rules, restore the prior plugin code and private local backup, and reverse the core patch only when its target files have not changed since application. See the plugin README for the full install and recovery steps.
