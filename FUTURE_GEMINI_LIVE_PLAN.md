# Future Plan: Gemini Flash Live + Hermes Phone Calls

This document captures the future architecture for using Gemini Flash Live as
the real-time spoken call agent while Hermes remains the task, memory, and tool
orchestration layer.

## Goal

Use the paired Android phone as the cellular endpoint, `hfp-mcp` as the
Bluetooth HFP audio/control gateway, Gemini Flash Live as the low-latency spoken
conversation engine, and Hermes as the assistant brain for tasks, permissions,
memory, schedules, reminders, and follow-up actions.

The intended runtime shape is:

```text
Cellular caller
  <-> Android phone
  <-> Bluetooth HFP
  <-> hfp-mcp SCO/WebSocket audio stream
  <-> Gemini Live bridge
  <-> Gemini Flash Live
          |
          | tool/events/context
          v
        Hermes
```

## Design Principles

- Keep `hfp-mcp` generic. It should own phone control and 8 kHz HFP PCM audio,
  not Gemini or Hermes-specific behavior.
- Keep Hermes authoritative for tasks, permissions, memory, schedules, and
  integrations.
- Let Gemini Live own real-time speech, interruptions, and conversational flow
  during active calls.
- Connect Gemini and Hermes through explicit tools/events instead of making one
  pretend to be the other.
- Treat the phone audio WebSocket as a single-owner media stream. Only one live
  conversation engine should write audio to a call at a time.

## Gemini Live Bridge Responsibilities

Add a separate service/process later, tentatively named `gemini_live_bridge`.
It should:

- Call `ensure_audio_stream("active-call")` through MCP and connect to the
  returned `client_stream_url`.
- Read HFP caller audio from the WebSocket as 8 kHz `pcm_s16le` mono.
- Send caller audio to Gemini Live with the correct PCM MIME type.
- Receive Gemini audio output and send it back to the HFP WebSocket.
- Resample when useful:
  - HFP input: 8 kHz PCM from Bluetooth HFP.
  - Gemini Live native input: 16 kHz PCM, though the API can resample other
    rates when the MIME type includes the sample rate.
  - Gemini Live output: 24 kHz PCM.
  - HFP playback: 8 kHz PCM.
- Listen for Gemini tool calls and forward approved requests to Hermes.
- Send Hermes task results, context updates, and urgent instructions back into
  the active Gemini Live session.
- Stop HFP playback immediately when Gemini is interrupted or when Hermes sends
  a high-priority correction.

## Gemini <-> Hermes Contract

Expose a small set of bridge tools to Gemini Live:

```text
ask_hermes(task, context, urgency?)
notify_hermes(event, transcript, caller_id?, metadata?)
get_hermes_context(topic, caller_id?)
handoff_to_hermes(reason, transcript?)
```

Suggested behavior:

- `ask_hermes` is for caller requests such as "Hermes, remind me at 7" or
  "do xyz and let me know." The bridge sends the request to Hermes and returns
  either a final result or a pending/accepted status to Gemini.
- `notify_hermes` records call events, transcripts, caller intent, and outcomes.
- `get_hermes_context` fetches relevant memory or task state for the caller.
- `handoff_to_hermes` is for cases Gemini should stop handling directly, such
  as privileged actions, unclear permissions, or long-running workflows.

Hermes should be able to send messages back to the bridge:

```text
context_update(text, priority=normal)
task_result(task_id, result, speak_to_caller=true)
urgent_instruction(text)
end_call(reason?)
```

## Live Session Information Flow

Gemini Live can receive information during an active session, but different
paths have different tradeoffs:

- Realtime text/audio input can be sent continuously while the session is
  active. This is good for lightweight or non-urgent context, but ordering
  across realtime streams is not guaranteed.
- Ordered client-content updates are better for exact context changes, but they
  interrupt current model generation.
- Tool results should be returned as tool responses, not as unrelated text
  context.

Bridge policy:

- Queue non-urgent Hermes context until Gemini finishes the current spoken turn.
- Interrupt Gemini for urgent Hermes updates, safety corrections, task
  completion that must be spoken immediately, or user barge-in.
- Clear queued HFP playback whenever Gemini generation is interrupted, otherwise
  old audio may continue playing into the call after the model has stopped.

## Required hfp-mcp Improvements Before Gemini

These are useful now and make the future bridge safer:

- Add richer `ensure_audio_stream` diagnostics:
  - `client_stream_url`
  - `health.listening`
  - `health.bind_host`
  - `health.public_host`
  - `health.port`
  - `session_reused`
  - `client_attached`
  - `token_reissued`
  - `sco_mtu_bytes`
- Keep backwards-compatible `stream_url` and `mtu` during migration.
- Add actionable errors:
  - `call_not_active`
  - `server_not_configured`
  - `sco_connect_failed`
  - `session_already_attached`
  - `session_not_found`
  - `buffer_full`
  - `unsupported_audio_format`
- Add diagnostics:
  - `play_test_tone(session_id="active-call", duration_ms=1000, hz=440)`
  - `play_audio_file(path, session_id="active-call", format=None)`
  - richer `play_audio` return metadata
- Add playback interruption support:
  - `clear_audio_playback(session_id="active-call")`
  - or an equivalent WebSocket control message for clearing queued output.

## Implementation Phases

### Phase 1: Harden hfp-mcp audio

- Implement the required MCP diagnostics and playback-clear tool.
- Improve stream metadata and lifecycle reporting.
- Document the difference between Bluetooth SCO MTU and WebSocket PCM frame
  size.
- Keep Hermes unchanged except for using `client_stream_url` when present.

### Phase 2: Build a local Gemini Live bridge prototype

- Add a standalone bridge service that connects to MCP and Gemini Live.
- Use a clear credential fallback order for Gemini API credentials and model
  choice.
- Start with one active call/session only.
- Resample audio with `ffmpeg`, `soxr`, or a small Python audio dependency.
- Declare the Hermes bridge tools in the Gemini Live session setup.
- Log transcripts, tool calls, interruptions, latency, and audio format
  conversions.

Credential fallback order:

1. If the bridge is launched by Hermes, read the Gemini API key/model from the
   Hermes runtime configuration or environment that Hermes already uses.
2. If Hermes does not expose a runtime config API, inherit standard Gemini env
   vars from the Hermes process environment: `GEMINI_API_KEY` first, then
   `GOOGLE_API_KEY`.
3. If the bridge is used without Hermes, require bridge-specific env vars:
   `HFP_GEMINI_API_KEY` and optional `HFP_GEMINI_LIVE_MODEL`.
4. Never write or copy the Hermes key into repo files, logs, MCP responses, or
   the HFP status endpoint.

This lets a Hermes deployment reuse its existing Gemini credentials, while a
standalone Gemini Live bridge can still run with its own key.

### Phase 3: Hermes coordination

- Add a Hermes-facing bridge API or plugin endpoint for:
  - task requests from Gemini
  - context requests from Gemini
  - task result callbacks to Gemini
  - urgent instruction/correction messages
- Add caller permissions before allowing privileged tasks from phone calls.
- Add pending approval flows for trusted/unknown callers.

### Phase 4: Production behavior

- Add session resumption or clean session restart handling for Live API session
  limits.
- Add context summarization so long calls do not overload the Live context
  window.
- Add metrics for latency, interruptions, audio queue depth, failed tool calls,
  and disconnect reasons.
- Add a mode switch so a call is handled by either Hermes classic STT/TTS or
  Gemini Live, not both at once.

## Open Decisions

- Exact Gemini Flash Live model ID and API surface to target.
- Whether to use Google AI Studio API or Vertex AI for production.
- Exact Hermes API/config hook for safely reading provider credentials when the
  bridge runs inside Hermes.
- Whether Gemini should produce audio directly or produce text that Hermes TTS
  speaks. Direct Gemini audio is lower latency; Hermes TTS keeps voice output
  consistent with existing Hermes behavior.
- Bridge transport between Gemini bridge and Hermes: MCP tools, Hermes plugin
  endpoint, local HTTP, or message bus.
- Permission model for callers and whether caller ID support must land first.

## References

- Gemini Live API reference: https://ai.google.dev/api/live
- Gemini Live capabilities and audio formats:
  https://ai.google.dev/gemini-api/docs/live-api/capabilities
- Vertex/Gemini Live session management:
  https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/start-manage-session
