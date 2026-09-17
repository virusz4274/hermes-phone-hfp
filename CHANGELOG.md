# Changelog

## 0.1.0rc1 — public preview

- Added MIT license, contributor/security guidance, issue templates, and release metadata.
- Added read-only host preflight and private configuration backups before installation.
- Added endpoint/profile/credential and local voice prerequisite checks to `doctor`,
  including offline mode and custom Hermes homes.
- Added native Hermes compatibility checks and optional compaction readiness reporting.
- Separated the daemon's MCP dependency from the portable Hermes plugin; constrained
  the daemon to the supported MCP major version.
- Extracted HTTP control routes and Gemini audio playback/resampling components.
- Added CI, source/wheel completeness checks, and isolated Hermes integration validation.
- Clarified country selection, continuity opt-in, data retention, and hardware limitations.

This preview includes conversation continuity and native task coordination from the
development checkout. Provider access, call quality, and new hardware combinations
still require the documented live acceptance checks. No live hardware certification
is implied by the automated checks.
