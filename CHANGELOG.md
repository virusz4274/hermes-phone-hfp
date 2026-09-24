# Changelog

## Unreleased

- Ground automatic memory extraction in caller quotes and filter uncertain or
  irrelevant fragments. Stamp explicit caller saves with trusted call metadata,
  preserve unchanged provenance, and avoid duplicate labels and hangup saves.
- Consolidate live voice instructions around natural greetings, relevant memory
  use, and confirmed tool outcomes; remove repeated rules from caller context.
- Preload Gemini dependencies before admitting calls and move call-time SDK
  checks off the event loop. Record bounded startup activity and stage timings
  to distinguish input loss from response delay, without retaining raw audio.
- Automatically checkpoint and save dated caller facts after hangup, with bounded
  recovery, duplicate prevention, and save outcomes in status and call summaries.
- Preserve enabled detailed transcripts alongside notes and retain existing
  transcript settings. Respect caller-note permissions and forgetting.
- Give Gemini direct caller-note access on permitted normal routes and require
  memory lookups before reporting missing notes or earlier conversations.
- Require `expected_revision` on public notes replacement writes to prevent stale
  owner/call updates from overwriting concurrent changes. Upgrade daemon and plugin
  together; existing stored notes are preserved.

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
