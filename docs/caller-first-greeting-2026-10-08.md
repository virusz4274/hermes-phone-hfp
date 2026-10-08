# Caller-first phone openings — 8 October 2026

Follow-up: the user subsequently requested a cached spoken opening for outgoing
calls/callbacks. See [cached outgoing opening](cached-outgoing-opening-2026-10-08.md).
The incoming caller-first behavior and historical rehearsal below remain applicable.
Later incoming calls exposed a missed first hello; see the
[early-hello follow-up](early-hello-fix-2026-10-08.md). The earlier successful
calls do not establish acceptance of that subsequent failure scenario.

The user reported that automatic greetings had caused similar trouble in older
versions and preferred incoming callers to say hello first. Outgoing introduction
and purpose remain important for the demo. This changes the earlier plan's
automatic incoming introduction requirement.

The Gemini session supervisor no longer sends `HFP_CALL_CONNECTED` when a call
becomes ready. Incoming calls wait for the caller's hello or opening request;
Gemini then responds naturally. Outgoing calls wait for recipient speech, then
identify Hermes as an AI assistant and explain the supplied purpose. Reconnects
and resets inject no greeting trigger. Startup hangup guards, historical-request
boundaries, caller permissions and existing Bluetooth configuration are retained.

The earlier call audit proves that Gemini requested `end_call` before current
caller speech; it does not prove why the model chose that action. The greeting
trigger is a plausible contributing factor. Removing it avoids an unsolicited
model response over retained history at startup. This change does not establish
that every historical greeting failure had the same cause.

No MP3, chime or additional playback owner was added. Existing file playback is
available for explicitly requested announcements. Automatic playback within an
interactive Gemini call would need coordination with the existing audio stream
and interruption handling. The outgoing introduction stays dynamic so it can
include the call's purpose and requested language.

## Verification

- Full final source suite: 605 passed, one existing Starlette warning, 46.15 seconds.
- Source Gemini/greeting/controller/continuity regressions: 114 passed, 30.28 seconds.
- Setup/reconnect/reset regression covers both incoming and outgoing calls and
  verifies no provider text greeting is sent at startup.
- Synthetic checks against configured `gemini-3.8-live` used historical disconnect
  text, silent PCM at startup and a synthetic hello. Both sessions returned zero
  startup audio bytes and no startup tool calls.
- After the synthetic hello, incoming output was: “Hello, I'm Hermes, an AI
  assistant. How can I help you today?” Outgoing output identified Hermes and
  stated the supplied demo-discussion purpose. Neither session requested a tool.
- These provider checks placed no actual phone calls and played no audio. They
  validate provider behavior in the synthetic scenario, not handset delivery.
- The manual Markdown/browser checklists retain all 146 check IDs and now instruct
  the incoming demo caller to say hello first.

Basic incoming connection/conversation is confirmed by the user rehearsal below.
Interruption and outgoing greeting delivery still require user-initiated rehearsal.

## Deployment

- Wheel SHA-256: `ae8f3404704829d8e2bb40bad82a15adc090d9cbf4315c04d6437e7e3fb9d9d0`.
- Build: `/tmp/hfp-caller-first-build-hg0xx67m/`. Wheel and sdist passed
  content/privacy inspection and `twine check`; the wheel passed a fresh portable
  installation, dependency/import and CLI smoke check. Packaging/migrations did
  not change in this follow-up; both formats also passed portable smoke checks
  during the preceding deployment.
- Calls were idle and native tasks settled before private backup at
  `.local-backups/gemini-demo-20261008-010855/`.
- This same wheel was installed into daemon and Hermes with `--no-deps`;
  all 51 Python module hashes in both environments match source and wheel.
- Both services restarted at 01:09:15 IST on 8 October, superseding the previous
  guarded build. The installed package version remains `0.1.1rc1`; use the build
  digest/module hashes to distinguish these working-tree builds.
- Installed-code Gemini, greeting, controller, continuity and task regressions:
  156 passed, 37.32 seconds.
- Handset reconnected through authenticated MCP using existing configuration;
  runtime is connected/idle with no controller error. Authenticated Hermes
  capabilities confirm owner lookup and callback rescheduling. The initial
  capability request ran during gateway warm-up; the readiness retry passed.

Incoming rehearsals begin with the caller saying hello after answer. The earlier
request to wait for an automatic introduction is superseded.

## Incoming rehearsal confirmed

On 8 October the user reported: “Yeah i called and its working.” The two latest
incoming audit records started at 08:28:51 and 08:29:09 IST and lasted 13.50 and
18.34 seconds respectively. Both reached `voice_ready`, retained input and output
transcription, and recorded audio sent to HFP. Both ended with `call_ended: ok`.
There were no model tool calls, including `end_call`, no reconnects, no dropped
playback frames and no transcript storage error in either call's final metrics.
Call references: `5aa06642b06144be94981e6d49a5b3f1` and
`d7546526335a43fc85c30f29a55510fc`. Private numbers and dialogue are omitted.

This passes the basic incoming connection/conversation rehearsal with the user's
confirmation. Provider counters alone do not prove audibility; that conclusion
relies on the user's report. Both services are active with no automatic restarts,
the handset is connected/idle, and all 51 installed module hashes in each
environment still match the caller-first wheel.

Outgoing introduction/purpose, interruption, assistant-requested hangup, memory
recall, lookup and scheduling/delivery demonstrations remain separate live checks.
