# Gemini phone startup fix

Historical build/rehearsal evidence; retain its original observations and limits.
For current scheduling and deployment evidence, see
[native phone workflows](hermes-call-workflows.md) and
[the subsequent workflow verification](hermes-owner-flow-verification-2026-10-08.md).
Older scheduler/reminder statements below do not describe the current lifecycle.

Update: the [caller-first greeting follow-up](caller-first-greeting-2026-10-08.md)
is now deployed. It removes the automatic incoming greeting prompt while keeping
the startup guard. The evidence below records the preceding guarded build.

Preceding guarded wheel SHA-256:
`6118d5f9df6c37598d2d2ca3291fb90506ccc92733930ac677a1c0f735722f70`.
That deployment includes both the API-schema correction and the startup
hangup fence described below. Earlier sections record the first replacement build.

Two incoming rehearsals on 8 October 2026 at approximately 00:33 IST ended after
3.54 and 2.98 seconds. The controller reported `session_failed`; Gemini reported
`1007 Invalid JSON payload` because the owner-lookup declaration serialized
`additional_properties` inside its parameters. The handset remained connected.

The new admin tool was the source of the failure. The previous SDK configuration
test used the broker tool set and only checked local model validation. It did not
exercise the full admin setup serialized onto the Live WebSocket.

The fix removes `additionalProperties` from Gemini's API Schema declaration.
Daemon/gateway argument validation and owner authorization remain unchanged;
unknown lookup arguments still receive HTTP 400. A new regression captures the
real installed SDK's setup payload for notes-only and full admin phone roles on
both supported Live configurations. No actual network/audio is used by the tests.

## Verification

- Targeted Gemini, greeting and owner-lookup regressions: 91 passed.
- Full source suite: 600 passed, one existing Starlette warning, 46.01 seconds.
- Actual Gemini service setup with the configured `gemini-3.8-live` and all eight
  admin phone tools: `setup_complete` received. No audio, tool execution or phone
  call was sent by this provider check; audible call behavior still needs rehearsal.
- Wheel and source distribution built and passed content/privacy and `twine check`.
- Wheel SHA-256: `047a90124bb1c8f278505fa913ab4a46aab7569a663c6806d03eb8790339cca1`.
- Build directory: `/tmp/hfp-schema-fix-build-cyqm4gfc/`.

This build supersedes the wheel in the 7 October deployment report. It retains
package version `0.1.1rc1`; compare module hashes/build digest rather than version
alone. The earlier automated results did not establish successful cloud startup.

## Deployment

- Both fresh portable artifact installations passed dependency/import/CLI smoke checks.
- Linked stores, configuration, credentials and installed packages were backed up
  privately at `.local-backups/gemini-demo-20261008-003928/` after calls/tasks settled.
- The same corrected wheel was installed without changing dependency versions in
  daemon and Hermes environments; all 51 installed Python files match the source
  and wheel in both environments.
- Both services restarted at 00:40:27 IST on 8 October. The handset was reconnected
  using the existing authenticated configuration. No pairing or route change was made.
- Installed-code Gemini/greeting/owner-lookup tests: 91 passed, 25.25 seconds.
- The installed provider also received actual Gemini `setup_complete` with all
  eight admin tools, without sending audio or placing a call.
- Authenticated Hermes capabilities, handset connection and idle controller passed;
  the previous startup error is absent after restart.

## Second rehearsal: model-initiated hangup

The user retried at 00:42:18 IST and reported a two-second termination. The audit
records a 2.69-second call: Gemini setup succeeded, but the model requested
`end_call` before any current caller transcription or generated audio. Captured
startup audio had no above-threshold activity. An earlier disconnect request was
present in this caller's retained history; replay of that request is a plausible
cause, not a proven account of the model's internal reasoning.

The startup correction now fences model-driven `end_call` in both the Gemini
dispatcher and phone controller until nonempty input transcription arrives in
the current physical call. Empty input, model output, retained history and
`cancel_wait` cannot bypass the fence. The evidence survives reconnect/context
reset and clears for the next physical call. Remote hangup and call limits still
apply. With input transcription explicitly disabled, only a bounded startup
fence applies: submitted audio activity or 30 seconds releases it; signal activity
alone is not proof of speech or consent.

Context now identifies the current physical call and explicitly treats old-call
goodbyes, disconnect requests and action requests as history. Retained dialogue
is preserved. Regression coverage reproduces the premature request and verifies
normal current-call hangup, duplicate delivery, reset/reconnect and a new call.

A synthetic cloud check with the configured model and an old disconnect request
received generated audio and no tool calls. No actual phone call, remote tool
execution or audible playback occurred. This does not establish real-call audio
delivery; another user-initiated rehearsal is required after deployment.

## Latest verification and deployment

- Full final source suite: 604 passed, one existing Starlette warning, 47.10 seconds.
- Installed-code regressions for Gemini, greeting, controller, continuity and task
  corrections: 155 passed, 36.92 seconds.
- Isolated native Hermes compatibility check passed, including native reminder
  rescheduling/read-back; it used disposable state and blocked network actions.
- Wheel/sdist content and privacy inspection, `twine check`, and both fresh
  portable package installation smoke checks passed.
- Build directory: `/tmp/hfp-hangup-guard-build-4dy5m35c/`.
- Idle call and settled native tasks were checked before backup/restart. Private
  backup: `.local-backups/gemini-demo-20261008-005906/`.
- The same tested wheel was installed into daemon and Hermes. All 51 Python module
  hashes in both environments match the source and wheel. Dependency versions
  were retained. Both services restarted at 00:59:22 IST on 8 October.
- Existing authenticated Bluetooth configuration reconnected the handset. Runtime
  is connected/idle; authenticated owner lookup and callback rescheduling capabilities
  are enabled in Hermes. No caller permissions or guest configuration were changed.
- The installed provider produced “Hello, I am Hermes, your AI assistant. How can
  I help you today?” in a synthetic old-hangup-history probe, with 203,522 generated
  audio bytes and no tool calls. This audio was neither played nor sent to a handset.

The user has been asked to retry an incoming call with the guarded deployment.
Real-call greeting delivery and continued connection remain pending until that
rehearsal is observed/reported. The earlier 00:42 retry failed and is not a pass.
