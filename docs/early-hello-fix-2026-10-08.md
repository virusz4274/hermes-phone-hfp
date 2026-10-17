# Early hello during async startup — 8 October 2026

Historical build/rehearsal evidence; retain its original observations and limits.
For current scheduling and deployment evidence, see
[native phone workflows](hermes-call-workflows.md) and
[the subsequent workflow verification](hermes-owner-flow-verification-2026-10-08.md).
Older scheduler/reminder statements below do not describe the current lifecycle.

Follow-up: the user confirmed quick answering and a response to the first hello
after deployment. The two latest incoming traces corroborate preserved input
and no provider reconnects; see the successful rehearsal below.

The user reported two incoming calls: the first rang for a while and responded
slowly; the second answered quickly but did not respond to the first hello. A
second hello about 15 seconds later received a response.

Both calls report `outgoing_opening.state=disabled`. The cached outgoing greeting
was not played. Async capture, playback and Gemini connection remain separate
concurrent tasks. Both the earlier installed build backed up on 7 October and
tracked HEAD already have the same 200 ms live input queue and 100 ms setup trim.
Recent greeting changes did not replace this with synchronous startup or change
those queue limits.

## Observed incoming traces

Private caller numbers and dialogue are omitted. Times are IST; media elapsed
times start at audio acquisition, while controller timings start at call setup.

| Audit reference / start | Evidence |
| --- | --- |
| `715191e83acf4ed387a3b20a64e7d014` / 13:14:28.471 | 10.39-second call; already active at controller admission. Gemini ready at 1,366 ms; microphone activity captured at 2,821 ms and submitted at 2,904 ms; first HFP reply sent at 5,093 ms. |
| `6723f6d2a6e948b8b071625296d3663b` / 13:14:43.115 | 25.02-second call. Answer requested at 95 ms, accepted at 158 ms, active at 367 ms. Early microphone activity captured at 1,165 ms, before Gemini ready at 1,908 ms, but not observed in submitted audio. Later activity captured at 16,005 ms and submitted at 16,104 ms; HFP reply sent at 17,649 ms. |

The second trace supports loss of the early hello during provider setup, rather
than a 15-second response to a hello Gemini received. Its live queue overflowed
91 frames and setup trimmed another five. These timings are signal-activity
observations, not a word-level analysis or a measurement of handset audibility.
The user confirms hearing a response after the second hello.

The longer ringing episode separately had real RFCOMM `ETIMEDOUT` errors and
socket closure/reconnection around 13:14:06, 13:14:17 and 13:14:25. BlueZ also
logged disconnected profile transports. Intermediate incoming call segments
ended before voice startup. The cause of those Bluetooth transport losses is
not established; relevant kernel logs showed no matching event. Existing real
transport-failure handling and Bluetooth configuration are retained. A healthy
control link accepted the second call's answer in 158 ms.

## Change

HFP capture continues while Gemini setup runs. A separate bounded RAM buffer
holds up to five seconds of audio before the first successful Live connection.
The sender drains it once in existing small batches, then consumes the normal
200 ms freshness queue. It yields during catch-up so capture, interruption and
cancellation remain responsive. No synthetic greeting or provider text turn is
added. Incoming callers still speak first; cached outgoing playback remains
independent and interruptible.

Startup overflow discards the oldest frames and is separately visible. Remaining
startup audio is discarded on provider disconnection, context reset or call end;
uncertain sends are never replayed. A new physical call clears prior audio and
resets diagnostics. Raw startup audio is not written to disk or retained as a
recording. This preserves early speech within the bounded window; it does not
guarantee retention during arbitrarily long provider outages.

`startup_input_buffer` status/final metrics expose capacity, capture state,
queued/captured frames, frames released to the sender, overflow and discarded
frames. Release is not proof of provider receipt; existing successful submission
counters and activity observations remain the evidence for submission.

## Verification and acceptance

Regression cases reproduce early speech followed by silence during a gated
provider connection on both incoming and outgoing paths. They verify exact
submission once, small batches, later live speech, bounded overflow, the unchanged
short live queue, uncertain-send reconnect, reset with unsent audio and cleanup
across physical calls. Existing outgoing greeting, interruption and hangup tests
are also run.

New live acceptance remains pending. The user should initiate an incoming call,
say hello immediately after answer once, and report reply delay/audibility. Repeat
with a pause before hello, then check outgoing cached introduction separately.
Record both capture/submission and transport health rather than treating a
healthy provider session as proof of Bluetooth or handset delivery.

## Tested build and deployment

- Targeted startup, opening, greeting and Gemini checks: 92 passed, 17.34 seconds.
- Final source suite: 620 passed, one existing Starlette warning, 57.59 seconds.
- Full installed-code suite: 620 passed, the same existing warning, 52.82 seconds.
  This includes late task correction and scheduling/authorization regressions.
- Isolated native Hermes compatibility check passed using disposable state and
  blocked network actions. Wheel/sdist content/privacy and `twine check` passed;
  both fresh portable installs passed dependency/import/CLI checks.
- Wheel SHA-256: `a63e74e7dcfa205185926ead6cd300e0bffdb16550e101da23449e96b6e796df`.
- Build: `/tmp/hfp-startup-buffer-build-fgif5aj6/`.
- Calls were idle and continued native tasks settled before private backup at
  `.local-backups/gemini-demo-20261008-133245/`.
- The same tested wheel was installed with no dependency changes into daemon
  and Hermes. All 52 Python modules and the WAV (53 package files) match the
  wheel/source in both environments. The Hermes interpreter imports the new
  buffer and reports its 5,000 ms capacity.
- Services restarted at 13:32:59 IST on 8 October, both active with no automatic
  restarts. Configuration/credential files match the backup. HFP session,
  BlueZ profile and manager code are unchanged from the preceding deployment.
- Authenticated MCP initialization and tool listing pass (43 tools). Authenticated
  daemon HTTP and native Hermes capability checks pass; owner lookup and callback
  rescheduling remain available. Native configuration with its normal dotenv
  loading confirms Telegram enabled and its home destination configured. No
  reminder or Telegram message was sent by these checks.

The handset reaches connected/idle and the controller is idle, but RFCOMM
`ETIMEDOUT` disconnects continue during idle operation after restart, including
13:33:38, 13:33:55, 13:34:13 and 13:34:30. A connection-and-wait readiness client
stalled and was stopped without stopping either service; a separate bounded
read-only MCP check passed. A momentary connected state is not a stable-link pass.
The user was asked to physically reconnect the handset's Bluetooth and repeat
an immediate-hello incoming call. That result and outgoing greeting audibility
remain pending. No claim is made that this software buffer repair resolves the
Bluetooth transport losses or establishes a new measured response latency.

## Incoming first-hello rehearsal confirmed

The user subsequently reported: “So it was quick. Answering and response to my
first hello.” Two incoming calls on the deployed build provide the following
corroboration, without publishing private caller details or dialogue:

| Call / start (IST) | Answer accepted | Provider ready (audio-relative) | Startup frames released | First microphone activity captured / submitted | First reply sent to HFP |
| --- | --- | --- | --- | --- | --- |
| `fcdb9ded0448449d90a7de7b4fde1a1a` / 13:39:53.768 | 398 ms | 1,437 ms | 77 | 1,409 / 1,529 ms | 4,219 ms |
| `b60347f4401143a9ad1407f33c60e578` / 13:40:26.770 | 317 ms | 1,280 ms | 69 | 1,408 / 1,430 ms | 3,359 ms |

Both have zero input-queue overflow, zero startup-buffer overflow/discard,
zero dropped playback, no provider reconnects and no transcript storage error.
The first call includes microphone activity before provider readiness that was
subsequently observed in submitted audio. It later completed an `end_call` tool;
the latest call has no tool invocation. Both ended normally. The latest retained
transcript contains one input event and one output event. Outgoing opening was
disabled on both, as intended for incoming calls.

For the latest call, activity ended at 1,744 ms and reply was sent to HFP at
3,359 ms: approximately 1.62 seconds between those observations. RMS activity
is not exact word timing and HFP submission is not handset playback timing.
Audibility and response to the first hello are confirmed by the user's report.

This passes the basic incoming first-hello rehearsal on the tested deployed
build while keeping asynchronous startup. The current snapshot is connected,
call idle and controller idle. These successful calls do not establish that
intermittent Bluetooth transport losses are permanently resolved. Outgoing
cached opening, interruption and a controlled latency comparison remain separate
live checks.
