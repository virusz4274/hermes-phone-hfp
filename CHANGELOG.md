# Changelog

## Unreleased

## 0.1.1rc1 — public preview

- Carry the owner's purpose and relevant context into outgoing calls. Allow calls
  to unconfigured numbers with restricted conversation and per-number memory,
  while preserving configured routes, permissions, and blocked-number rules.
- Add owner controls to read and replace a number's saved notes, with notes-first
  recall and phone-control permission checks inside tool handlers.
- Persist one-shot phone callbacks with restart recovery, cancellation, and visible
  failure outcomes. Reject callbacks more than five minutes overdue and do not
  automatically redial failed or uncertain attempts.
- Use native Hermes cron jobs for Telegram reminders, with an explicit delivery
  destination and linked cancellation for callback reminders. Scheduling can finish
  after hangup when authorized. Callback and reminder creation are separate operations;
  partial failures are reported, and Telegram delivery can lag behind its due time.
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
  together; existing stored notes are preserved. This preview does not promise
  patch-level API compatibility.

Included changes: [#3](https://github.com/virusz4274/hermes-phone-hfp/pull/3),
[#4](https://github.com/virusz4274/hermes-phone-hfp/pull/4), and
[#5](https://github.com/virusz4274/hermes-phone-hfp/pull/5).
Calendar integration and automatic calendar monitoring are not included. End-to-end
callback voice delivery and the full live hardware matrix remain unverified.

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
