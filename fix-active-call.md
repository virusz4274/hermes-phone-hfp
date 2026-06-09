# HFP Per-Call Hermes Sessions And Playback Fix

## Summary

Separate each cellular call into its own Hermes session, while still allowing all turns within that one call to share context. Also fix one-way playback/session-token ownership, active-call reuse, arbitrary number targets, and cleanup after plugin-initiated playback failures.

## Key Changes

- Add per-call Hermes session identity:
  - create a call instance id when HFP enters `incoming`, `dialing`, `ringing`, or `active` from an idle/no-call state
  - reset it when status returns to `idle` or `disconnected`
  - use chat/session ids like `hfp-phone:<caller-or-device>:<call-id>` for `SessionSource.chat_id` and `user_id`
  - keep all messages inside one phone call in the same Hermes session, but make the next call a fresh Hermes session

- Keep stable caller metadata separately from session identity:
  - `raw_message.hfp_caller_id` remains the Bluetooth address or caller number when available
  - `raw_message.hfp_call_id` stores the generated call id
  - `chat_name` can remain “Bluetooth phone call” or include a short call id for debugging
  - caller role classification still uses caller/device identity, not the per-call session id

- Fix outbound target parsing:
  - accept send targets like `hfp_phone:+917...`, `hfp-phone:+917...`, `tel:+917...`, and direct numeric chat IDs
  - keep plain `hfp_phone`, `hfp-phone`, `home`, and `owner` mapped to `HFP_PHONE_HOME_CHANNEL` / `HFP_PHONE_OWNER_NUMBER`
  - do not treat per-call session ids as outbound phone numbers after the call has ended; stale replies should be dropped, not redialed

- Use distinct audio session ownership:
  - live duplex call handling may keep `active-call`
  - standalone one-way calls/reminders use unique ids like `hfp-call-<uuid>`
  - this avoids WebSocket token collisions between gateway live audio and one-shot playback

- Prefer MCP-owned one-shot playback:
  - add MCP `play_audio_file(audio_file, session_id, tail_ms=1000)`
  - add MCP `dial_and_play_audio_file(number, audio_file, session_id=None, timeout_seconds=30, hangup_after=false, tail_ms=1000)`
  - for Hermes `message`, generate TTS first, then call the MCP file playback tool when the file is visible to MCP
  - for `audio_file`, skip TTS and play/convert directly
  - keep WebSocket streaming for live duplex calls and as fallback when Hermes has a local file MCP cannot access

- Fix cleanup after playback failure:
  - track whether `_standalone_send` initiated the dial
  - if it did and playback fails, call `hangup()` and `cleanup_audio_sessions()`
  - always call `stop_audio_capture(session_id)` for the session the plugin opened
  - if attached to a pre-existing active call, do not hang up that call on failure

- Harden WebSocket sidecar token handling:
  - keep recent tokens valid briefly per session instead of replacing the only valid token immediately
  - log invalid-token vs missing-session rejection reasons
  - keep repeated `ensure_audio_stream` calls safe for the plugin and direct MCP users

## Test Plan

- Hermes session tests:
  - same call emits multiple events with the same per-call `chat_id`
  - a second call from the same Bluetooth device gets a different `chat_id`
  - `hfp_caller_id`, `hfp_role`, and `hfp_call_id` metadata are present
  - stale per-call session replies after hangup do not place a new call

- Plugin playback tests:
  - standalone calls use unique `hfp-call-<uuid>` audio session ids
  - playback failure after plugin-initiated dial calls `hangup`, `cleanup_audio_sessions`, and `stop_audio_capture`
  - playback failure on an already active call does not hang up
  - `audio_file` skips TTS
  - arbitrary number send targets normalize correctly

- MCP/sidecar tests:
  - file playback converts to `pcm_s16le`, mono, 8000 Hz
  - active-call playback reuses existing audio without redialing
  - repeated token issuance does not immediately invalidate recent URLs

## Assumptions

- Desired Hermes behavior is: one session per phone call, not one session forever per HFP device.
- The generated call id does not need to be globally meaningful; it only needs to be stable for the duration of a call and unique enough across calls.
- True cellular caller ID can be added later through `+CLIP`/`+CLCC`; this plan uses the current Bluetooth/device identifier until then.
