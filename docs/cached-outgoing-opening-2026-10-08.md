# Cached outgoing opening — 8 October 2026

The user received a callback but needed to say hello to start Gemini. They
requested an audible outgoing opening and selected the cached spoken phrase:
“Hello, this is Hermes, an AI assistant.” This supersedes the outgoing
recipient-first opening in the previous rehearsal; incoming calls still wait
for the caller's hello or request.

The 3.36-second WAV was generated once with Gemini `gemini-3.8-flash-tts`, prebuilt
voice Achernar, and converted to mono 8 kHz 16-bit PCM. Only the generic phrase was
sent to the TTS provider; no caller data or private call purpose was included.
The packaged WAV is reusable and contains no credentials. Runtime calls do not
invoke TTS or run FFmpeg. This adds no classic STT/TTS conversation backend.
Asset SHA-256: `9d238e50d8b8fe47311cb3f4f1dc40b20c59ed1de53feb9c8a0d9324528ce50b`.

Once an outgoing call is active and its existing HFP stream is acquired, the
manager queues this opening into the same bounded, paced playback pipeline used
by Gemini. It does not acquire a second audio owner, send a synthetic model-turn
trigger, or end the call when the clip finishes. It can begin before Gemini
setup is ready. Incoming calls do not get the clip. Scheduled callbacks use the
same outgoing route and behavior.

The opening is claimed once per physical call before queueing. An interrupted,
cancelled or uncertain send is not retried on that call; provider reconnects and
context resets do not replay it. Consecutive microphone activity cuts the local
opening even while Gemini is still connecting. Input transcription or a model
reply also clears remaining local playback, avoiding overlap. After recipient
speech, Gemini follows the supplied purpose and avoids repeating the cached
introduction unless clarification is needed. Existing hangup guards and caller
permissions remain in force.

Status and final call metrics report `outgoing_opening` state, mode, duration and
delivery uncertainty. A fully sent spoken clip has a transcript event with source
`cached_outgoing_opening`; it is not a provider-generated utterance or proof it
was heard. Speech recognition and local activity detection must not treat the
opening itself as caller authorization to hang up.

`HFP_GEMINI_OUTGOING_GREETING=spoken` is the default. `chime` selects a soft cue
followed by the model introduction after recipient speech; `off` retains the
previous silent recipient-first behavior. The user's selected setup uses spoken.

## Verification

Integration/regression cases cover direction filtering, packaged cached
formats, playback before provider readiness, no synthetic greeting turn, reset
without replay, local interruption, incoming silence and uncertain-send handling.
Release-content checks now require the WAV and opening module in wheel and sdist.
Portable smoke checks load the packaged audio independently of this checkout.
The initial synthetic provider check repeated Hermes's name after the cached
receipt. A specific cached-opening setup mode removes the conflicting generic
introduction instruction. The final configured-model check produced no startup
audio/tools and, after a synthetic hello, went directly to “I am calling to ask
if you are available to discuss a demo tomorrow,” without repeating the cached
identity phrase. It placed no phone call and played no audio to a recipient.

Live outgoing/callback audibility, recognition, interruption and purpose delivery
still require user-initiated rehearsal after deployment. The earlier callback
proved the existing recipient-first behavior, not delivery of this new clip.

The user's pre-change callback report is corroborated by an outgoing call at
12:41:36 IST lasting 18.26 seconds (audit `f02d63c8da9142e887308cac7fdedb9f`).
This records receipt of the prior call, not a pass for the new cached opening,
schedule correction or Telegram delivery.

The subsequent [early-hello follow-up](early-hello-fix-2026-10-08.md) preserves
early microphone input during async startup and supersedes this build after
deployment. Neither of the two incoming calls used the outgoing opening.

## Tested build and deployment

- Full final suite: 614 passed, one existing Starlette warning, 48.85 seconds.
- Final opening/greeting regressions: 17 passed, 8.82 seconds.
- Isolated native Hermes compatibility check passed in disposable state with
  network actions blocked, including reminder revision/rescheduling read-back.
- Final wheel and sdist content/privacy inspection and `twine check` passed.
  Both fresh portable installations loaded the packaged greeting and passed
  dependency/import and CLI smoke checks.
- Wheel SHA-256: `946f1fee1be8d5ca0cde55814e8935a41f1636cb26f041750abb9d640bfa51a8`.
- Build: `/tmp/hfp-cached-opening-final-build-appvld_u/`.
- Idle calls and settled native tasks were verified before private backup at
  `.local-backups/gemini-demo-20261008-130243/`.
- The same wheel was installed without dependency changes into daemon and Hermes.
  All 52 Python modules and the WAV (53 package files) match source and wheel in
  both environments. No permissions, routing or Bluetooth configuration changed.
- Both services restarted at 13:03:00 IST on 8 October.
- Installed-code opening, Gemini, greeting, controller, continuity and task
  regressions: 165 passed, 37.09 seconds.
- Actual daemon process environment resolves opening mode to `spoken`; installed
  cache loads the selected phrase at 3.36 seconds. Both services are active with
  no automatic restarts. The handset is connected/idle, controller has no error,
  and authenticated Hermes owner-lookup/rescheduling capabilities pass.

The user was asked to request another outgoing call/callback through Hermes,
remain quiet after answer to hear the cached opening, then reply. New real-call
audibility, handoff and interruption are pending; no rehearsal call was placed
automatically by this implementation run.
