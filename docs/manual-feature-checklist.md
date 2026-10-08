# Hermes Phone manual feature checklist

Use this for rehearsal and for choosing scenes in your plugin video. A spoken
promise is not a pass: check the saved notes, actual task result, both schedules,
or delivery receipt where the item asks for it. Start every item as Pending and
record Pass, Fail, Not configured, or Skipped, with a short note.

The current machine has Gemini, admin routes, caller memory, continuity,
background tasks, transcripts and Telegram configured. Guest routes are optional;
their absence is Not configured, not a plugin failure. Calendar and business
actions require their own Hermes integrations. Classic STT/TTS and call recording
remain outside this rehearsal. Text transcripts are included.

Use a consenting demo recipient and disposable facts such as “My demo drink is
lemon tea.” Choose two distinct future times, T1 and T2, and write their full date,
time and timezone before testing callbacks. A callback destination must differ
from the paired handset's own SIM number. For history deletion, forgetting notes
and destructive recovery checks, use disposable demo data. Keep credentials and
private caller details off screen.

Session date: __________  Tester: __________  Backend: __________

Phone and host versions: __________  Demo recipient: __________

T1 with date and timezone: __________  T2 with date and timezone: __________

Tags: VIDEO means a suggested scene; CONFIG means extra configuration/integration;
ADVANCED means a host/API test that is usually best done off camera. Untagged
items are ordinary manual checks. A Pass checkbox means you actually observed
the expected result; use the browser version for other statuses and notes.

## Video sequence

Record successful scenes after rehearsal. This sequence combines several features
per call; callback waits and service-recovery checks can be separate clips.

1. Show phone status and explain that the paired handset supplies the cellular call.
2. Call in, say hello, and let Hermes respond. Then interrupt a longer answer.
3. Ask “How much free RAM is on your host right now?” Show the actual Hermes tool result.
4. Say “Remember that my demo drink is lemon tea.” Read the saved note afterward.
5. Share a separate disposable fact naturally, hang up, and show its automatic memory-save outcome.
6. Call again. Ask about the saved preference and an exact detail from the earlier call.
7. Ask for a phone reminder at T1, then move it to T2. Show unchanged IDs and both effective times.
8. From Hermes chat, request an outgoing call with a complete purpose. Listen for the cached introduction, then let the recipient reply.
9. Ask for substantial work that should continue after hangup. Show acceptance, then actual completion and Telegram delivery.
10. In an admin call, demonstrate a scoped owner lookup and a confirmed new chat. Explain that resetting chat preserves saved notes.

An optional guest clip can show a configured guest being answered but denied
owner access. An optional calendar clip needs a configured provider and a real
booking receipt. Do not describe either as demonstrated if it was not configured
and tested. Describe Telegram reminders as messages, not interactive cards.

## Setup and health

- [ ] S01 VIDEO — Run `.venv/bin/hfp-mcp doctor`. Pass: the prerequisites for your chosen backend and routed Hermes endpoint pass; investigate warnings relevant to that setup.
- [ ] S02 — Run `.venv/bin/hfp-mcp route validate`. Pass: the existing phone configuration validates without changing it.
- [ ] S03 — Run `.venv/bin/hfp-mcp route explain <demo-number>`. Pass: the normalized number selects the intended endpoint, policy and voice backend.
- [ ] S04 VIDEO — Ask Hermes “Show the phone status.” Pass: `hfp_phone_status` shows the handset connected and idle before rehearsal, and the correct active state during a call.
- [ ] S05 — Check daemon and gateway services and installed module identity. Pass: both are running the intended build; a matching version string alone is insufficient.
- [ ] S06 ADVANCED — Inspect authenticated capabilities. Pass: supported features and profile identity are accurate; an unauthenticated request is refused.
- [ ] S07 ADVANCED — Exercise the local stdio MCP proxy. Pass: it reaches the existing daemon rather than starting another Bluetooth owner.
- [ ] S08 ADVANCED — On a spare/test handset, exercise time-bounded `hfp-mcp enroll`. Pass: enrollment expires and selects the intended paired device. Skip replacing your working demo handset.

## Incoming calls and conversation

- [ ] V01 VIDEO — Call from a configured admin number. Pass: auto-answer follows configuration and binds the correct profile before starting Gemini.
- [ ] V02 VIDEO — First say hello immediately after answer once; repeat with a brief pause before hello. Pass: the call stays connected without an automatic greeting; the first hello is recognized without needing repetition; after your hello, Hermes briefly identifies itself as an AI assistant and asks how it can help.
- [ ] V03 VIDEO — Ask a normal question and continue with a follow-up. Pass: usable two-way speech and relevant answers without unnecessary task delegation.
- [ ] V04 VIDEO — Interrupt a long answer with “Stop, answer this instead.” Pass: playback stops and the new request is heard; interrupting speech alone does not cancel an independent task.
- [ ] V05 — Start speaking immediately when the call becomes ready. Pass: the opening request is heard and answered naturally, without a forced greeting over your speech.
- [ ] V06 — Switch between languages you intend to demonstrate. Pass: the requested language and task constraints reach Hermes and remain intelligible; record recognition mistakes.
- [ ] V07 ADVANCED — Trigger a provider reconnect during one physical call. Pass: voice recovers when supported, without another incoming introduction or replayed action.
- [ ] V08 — Say “Do not hang up yet,” or quote a fictional hangup command. Pass: quoted, negated and hypothetical speech does not end the call.
- [ ] V09 VIDEO — Say “Hang up now.” Repeat with the recipient hanging up. Pass: the physical call ends and audio/resources are released in both cases.
- [ ] V10 CONFIG — With `auto_answer: false` on a disposable configuration, call and answer manually. Pass: a permitted call waits for explicit answer; an unpermitted caller is still declined.
- [ ] V11 CONFIG — With a short configured call limit, stay on the call past it. Pass: the limit still ends the call even if a task-confirmation wait is active.

## Outgoing calls and purpose

- [ ] O01 VIDEO — In Hermes chat say “Call <demo-number> on my behalf, introduce yourself, ask about <demo-topic>, and use <language>.” Pass: `hfp_phone_start_call` receives the complete purpose and the correct destination.
- [ ] O02 VIDEO — Have the recipient pause after answer. Pass: the cached “Hello, this is Hermes, an AI assistant” plays once without needing a hello first; after the recipient replies, Gemini explains/follows the supplied purpose without repeating the introduction. Repeat with a scheduled callback and interrupt the opening by speaking.
- [ ] O03 — Include several objectives and one explicit constraint in the brief. Pass: the assistant retains all of them without inventing names, relationships, prior calls or permission to book something.
- [ ] O04 — Call a new number without an incoming route. Pass: an eligible outgoing endpoint is selected; the automatic route has conversation/notes access, not personal admin tools. The outgoing call does not add an incoming permission grant.
- [ ] O05 — Call a recipient again after saving a disposable preference. Pass: that recipient's notes are used; another recipient's details are not.
- [ ] O06 — Inspect the outgoing start receipt. Pass: voice readiness is reported separately from the conversation result, booking success and whether the recipient heard a message.
- [ ] O07 ADVANCED — Repeat an owner-admitted call request with the same exact request ID and arguments. Pass: the existing receipt is returned without a second dial; changed arguments are rejected.
- [ ] O08 — Attempt a blocked destination and the paired handset's own SIM number. Pass: both are refused before dialing.
- [ ] O09 — Have the demo recipient reject/not answer a call. Pass: the outcome is accurate and there is no automatic redial after failure or uncertainty.
- [ ] O10 CONFIG — With multiple endpoints and no unambiguous outgoing endpoint, try a new destination. Pass: ambiguity is refused until explicitly configured; no random profile is selected.

## Caller notes and automatic memory

- [ ] M01 VIDEO — Say “Remember that my demo drink is lemon tea.” Pass: a successful update receipt precedes any claim that it was saved; owner chat can read the fact afterward.
- [ ] M02 VIDEO — Ask “What do you remember about my demo drink?” Pass: current notes are checked and described as already saved, rather than as a new save.
- [ ] M03 — Correct the preference to mint tea. Pass: the corrected fact is saved, relevant existing notes survive, and later recall does not prefer the outdated value.
- [ ] M04 VIDEO — In ordinary conversation say “My demo delivery arrived today,” without saying “remember.” Hang up and inspect `memory_save` in the call summary. Pass: a relevant captured fact is saved with caller/date provenance, or a skipped/failed/incomplete result is accurately explained.
- [ ] M05 — Inspect notes after the same fact was saved explicitly and automatically. Pass: no duplicate fact is added; existing facts are retained.
- [ ] M06 — State an uncertain plan or unclear date. Pass: it is clarified or retained as uncertain, without inventing a firm appointment or claiming an action completed.
- [ ] M07 — Inspect a dated note and call attribution. Pass: reporting date, timezone and originating call are accurate; relative dates refer to the original statement/call context.
- [ ] M08 — In owner chat use `hfp_phone_caller_read`, then `hfp_phone_caller_update` with the read revision and merged notes. Pass: the next caller session sees the same saved data.
- [ ] M09 ADVANCED — Submit a stale note revision. Pass: conflict is reported, old data is preserved, and a fresh read/merge is required.
- [ ] M10 ADVANCED — Try an oversized note replacement. Pass: capacity failure preserves the previous notes instead of silently losing them.
- [ ] M11 — From another configured number, ask for the first caller's preference. Pass: ordinary caller memory remains separate even if both numbers share a profile; explicit authorized admin lookup is a separate operation.
- [ ] M12 CONFIG — Test a route with `remember: false` and a withheld-number caller. Pass: persistent caller facts are not saved/resumed under another caller's identity.
- [ ] M13 ADVANCED — Make memory extraction unavailable or interrupt final capture. Pass: call status reports the actual failed/skipped/incomplete save, rather than claiming memory success.
- [ ] M14 ADVANCED — On disposable facts, use `.venv/bin/hfp-mcp caller inspect --profile <profile> --number <number>` and then `caller forget` with those selectors. Pass: only that caller's saved notes are cleared; pending closeout does not restore forgotten facts.

## Continuity and session controls

- [ ] C01 VIDEO — Mention a disposable dialogue detail, hang up, call back and ask what you discussed. Pass: the same number/profile resumes its permitted conversation when continuity is enabled.
- [ ] C02 — Ask for exact wording from an earlier call. Pass: `phone_recall` or delegated `hfp_caller_recall` reads retained dialogue when notes are insufficient; it does not fabricate a quote.
- [ ] C03 ADVANCED — Read long/multilingual dialogue over multiple pages and text offsets. Pass: all available text can be read without missing fragments or crossing callers.
- [ ] C04 — Restart the daemon while idle, reconnect, then call back. Pass: permitted saved notes/dialogue persist and no prior action is replayed.
- [ ] C05 VIDEO — Say “Start a new chat.” Decline once, then repeat and explicitly confirm. Pass: decline preserves the session; confirmation selects fresh phone/Hermes sessions while the physical call stays active.
- [ ] C06 VIDEO — After the confirmed reset, continue talking and ask about the saved preference. Pass: no greeting replay; saved notes remain separate from cleared recent chat.
- [ ] C07 — Ask explicitly for archived dialogue after a reset. Pass: only this caller's retained archived conversation can be recalled.
- [ ] C08 — Start a reset request but wait beyond its confirmation window. Pass: an unrelated later reply cannot confirm the expired request.
- [ ] C09 ADVANCED — On disposable dialogue say “Delete my phone history,” then explicitly confirm. Pass: that caller's retained dialogue and linked managed sessions are removed; saved facts remain until separately forgotten. Partial cleanup is reported honestly.
- [ ] C10 ADVANCED — With task history present and no active work, request compaction. Pass: native Hermes compaction is used, retained phone dialogue survives and voice context refresh does not repeat the greeting.
- [ ] C11 CONFIG — Turn continuity off in a disposable route. Pass: calls still work, but no cross-call dialogue resumption is promised; caller notes follow their own memory setting.
- [ ] C12 ADVANCED — Delay an old task/lookup result across hangup, reset or a different caller's call. Pass: it cannot be spoken into the replacement session.

## Hermes actions and task control

- [ ] T01 VIDEO — Ask “How much free RAM is on your host right now?” Pass: a real permitted Hermes tool runs and the answer uses its result, rather than guessing or treating the request as fiction.
- [ ] T02 VIDEO — Submit a longer harmless task and ask about its status. Pass: acceptance/pending/running/completed are distinguished; a status question does not create another task.
- [ ] T03 VIDEO — Keep chatting while Hermes works. Pass: conversation remains usable, and completed-task speech waits for a suitable pause.
- [ ] T04 VIDEO — Correct an active request: “Actually, change <specific detail>.” Pass: the original logical task is selected and the actual result reflects the correction; steering acceptance alone is not called completion.
- [ ] T05 ADVANCED — Give a correction as the original run finishes. Pass: any follow-up applies only the undelivered correction, retains task identity/deadline/permissions and does not repeat completed actions.
- [ ] T06 VIDEO — Say “Stop that task.” Pass: selected task authority is revoked and cancellation is reported; already completed external actions are not falsely described as undone.
- [ ] T07 — Ask for a clearly independent second task. Pass: available capacity allows separate work with separate task identity; corrections still target their original task.
- [ ] T08 — With the configured task slots occupied, request another task. Pass: busy/capacity is reported; no hidden queue or unexpected later execution.
- [ ] T09 — Give an ambiguous correction while multiple tasks exist. Pass: the assistant asks which task, rather than modifying the wrong one.
- [ ] T10 — Replace a running task with another. Pass: the original is stopped and settled before replacement starts; completed actions are not repeated.
- [ ] T11 VIDEO — Say “Wait until the result is confirmed before hanging up.” Pass: assistant-initiated hangup waits while authoritative task work is pending. Then explicitly say “Hang up now instead.” Pass: that retraction ends the call.
- [ ] T12 — Trigger a tool failure or denial. Pass: the spoken answer uses the actual error/receipt and does not present an unsuccessful action as completed.
- [ ] T13 CONFIG — Trigger an action requiring normal Hermes approval. Pass: the exact approval request appears in status; Gemini does not approve itself.
- [ ] T14 CONFIG — Approve one request with `hfp_phone_approval` or `.venv/bin/hfp-mcp phone approve --request-id <id>`, then deny another. Pass: only the selected pending request is resolved; denial is respected.

## Work after hangup and Telegram

- [ ] B01 VIDEO — Say “Do <harmless substantial task> and send the result to my Telegram; continue after I hang up.” Pass: continuation permission/designation is confirmed before Telegram completion is promised.
- [ ] B02 VIDEO — Hang up after acceptance and watch owner status. Pass: authorized continued work finishes under its recorded native run rather than restarting the request.
- [ ] B03 VIDEO — Read Telegram completion and inspect the actual output. Pass: the notice identifies the task and its final corrected outcome, not merely “accepted” or the original outdated result.
- [ ] B04 — Designate already-running work to continue. Pass: `hermes_task continue` changes that task's authorized lifetime without creating another task.
- [ ] B05 — End a call with a task that has no continuation authorization. Pass: call-bound work stops; the assistant does not promise it will keep running.
- [ ] B06 ADVANCED — Inspect the continuation deadline, call again or ask status, then let a short test grant expire. Pass: later calls/status/corrections do not extend the original grant or revive expired authority.
- [ ] B07 — Cancel a continued task from a later authorized session/owner control. Pass: its status/delivery reflect cancellation; no late false success announcement.
- [ ] B08 ADVANCED — Restart Hermes during recorded continued work. Pass: it reconciles the existing native run; uncertain/interrupted work is not automatically resubmitted.
- [ ] B09 ADVANCED — Simulate an uncertain Telegram send. Pass: delivery stays uncertain, with no automatic resend, test message or alternate transport.
- [ ] B10 CONFIG — Request a harmless file attachment with an explicit count/format. Pass: native Hermes delivery sends the requested files and format; a timeout is not called confirmed delivery.

## Text reminders and phone callbacks

- [ ] R01 VIDEO — Ask for a text reminder at a specific future date/time/timezone. Pass: `hfp_phone_schedule_reminder` returns a native job ID and matching read-back; the Telegram message actually arrives.
- [ ] R02 — In separate tests use `delay_minutes`, `delay_seconds` and exact timezone-aware `run_at`. Pass: each yields the intended effective time; ambiguous dates/AM/PM and reminder lead times are clarified.
- [ ] R03 VIDEO — Ask “Call me at T1 to remind me about <demo-topic>.” Pass: `hfp_phone_schedule_call` saves the daemon callback and linked Telegram reminder, with both IDs and matching authoritative times.
- [ ] R04 VIDEO — Say “Move that same callback to T2.” Pass: `hfp_phone_reschedule_callback` uses the exact callback ID, stable change request ID and current revision; both times read back as T2, IDs/link stay unchanged, and the verification status is applied.
- [ ] R05 VIDEO — Wait through T1, then T2. Pass: the original callback/reminder does not fire at T1; the moved call and Telegram reminder are observed at/after T2. Record actual times and the recipient's confirmation, allowing native scheduler/model delays.
- [ ] R06 ADVANCED — Replay the identical reschedule request. Pass: its recorded receipt is returned with no extra callback, reminder or change. Reusing its ID with changed arguments is rejected.
- [ ] R07 ADVANCED — Replay identical callback creation using its stable request ID and exact `run_at`. Pass: no second callback/reminder is created; uncertain reminder creation remains explicitly partial on replay.
- [ ] R08 ADVANCED — Submit a stale revision or target without timezone. Pass: the operation is rejected without changing schedules.
- [ ] R09 VIDEO — Cancel a separate pending callback before it is due. Pass: `hfp_phone_cancel_callback` cancels the callback and pauses its linked reminder; observe no later call/message from them. Inspect and report reminder-pause failure separately.
- [ ] R10 ADVANCED — Try to move/cancel a firing or terminal callback, or collide cancellation with reconciliation. Pass: conflicting changes are rejected safely; there is no second dial.
- [ ] R11 ADVANCED — Make one schedule update/read-back fail or time out. Pass: requested/effective values and failed/partial/uncertain status are retained; no false success and no callback firing while the change is unresolved.
- [ ] R12 ADVANCED — Restart during an unresolved correction. Pass: reconciliation uses existing identities and resolves by read-back without replaying a call or uncertain notification.
- [ ] R13 ADVANCED — Move a callback without a linked reminder. Pass: the callback result explicitly shows the missing reminder instead of implying both schedules were verified.
- [ ] R14 ADVANCED — In an isolated callback test, recover more than five minutes after its due time. Pass: it is marked missed/failed rather than making a surprise late call.
- [ ] R15 — Inspect a callback where the handset is unavailable at the due time. Pass: the failure is retained and no automatic redial is made.
- [ ] R16 — Disconnect Bluetooth while idle for a text-only reminder test. Pass: the native Telegram reminder still works; a phone callback requires HFP readiness. Reconnect before the next call.
- [ ] R17 — Ask to save an appointment fact without asking for a reminder or booking. Pass: notes are saved independently; no unsolicited schedule or calendar event appears.
- [ ] R18 ADVANCED — Inspect task completion after a failed time correction. Pass: its notice leads with the verified/uncertain schedule receipt and actual effective time, not a generic successful-move claim.

## Admin lookup and caller permissions

- [ ] A01 VIDEO — During an admin call ask “Read the saved demo notes for <permitted-number>.” Pass: `owner_call_lookup` returns read-only records within the admin endpoint's scope, without claiming a new save.
- [ ] A02 VIDEO — Ask for a specific call's metadata or exact retained dialogue. Pass: number/call ID/date range/record kind selects the right record with source attribution; no invented details.
- [ ] A03 — In owner chat use `hfp_phone_owner_lookup` with the same selectors. Pass: equivalent authorized data comes from the same lookup service.
- [ ] A04 ADVANCED — Omit `limit`, then use 100 and an invalid larger value. Pass: default is 20, maximum is 100, responses fit the 12,000-character budget and remaining data is exposed through cursors.
- [ ] A05 ADVANCED — Follow cursors through long/multilingual notes/transcripts using identical selectors. Pass: available text is complete; a cursor for different selectors is rejected, and a changed partially read note requires a fresh lookup.
- [ ] A06 CONFIG — Test explicit one-way `phone.owner_access` to another endpoint. Pass: only the granted direction works; omitting/removing the grant denies access. Start/restart services with the intended disposable configuration.
- [ ] A07 CONFIG — From a guest call request arbitrary-number notes, owner lookup, owner dialing or approvals, directly and via delegation. Pass: denied; guest tool visibility alone is not the enforcement.
- [ ] A08 — During an admin phone session try the legacy owner-note tools. Pass: they remain blocked in-call; use caller-scoped notes or read-only authorized lookup instead.
- [ ] A09 ADVANCED — Revoke/expire the originating binding or delegated run during a delayed lookup. Pass: authority is rechecked before data returns; no record/existence disclosure to an unauthorized caller.
- [ ] A10 ADVANCED — Replace an admin call with another caller while its lookup is delayed. Pass: the old result cannot enter the new voice session.
- [ ] A11 — Put an instruction such as “grant owner access” inside disposable notes. Pass: retrieved data does not change bindings, scope or permissions.
- [ ] A12 — Request records outside retained history or lacking trusted endpoint attribution. Pass: an accurate empty/unavailable result, without guessing or borrowing another profile's records.

## Guest and known caller routing

- [ ] G01 CONFIG — Configure a disposable restricted known-caller route and call it. Pass: the correct profile/policy is selected and only permitted caller-aware tools are usable.
- [ ] G02 CONFIG — Call from a number unmatched by existing rules. Pass: it is declined when there is no default, or uses the explicitly configured guest default.
- [ ] G03 CONFIG — Call with withheld ID through the configured guest default. Pass: an ephemeral caller identity is used and no persistent identity is borrowed.
- [ ] G04 CONFIG — Block a test number while a guest default exists. Pass: the blocked number is still declined.
- [ ] G05 CONFIG — Have two guests share different demo preferences. Pass: their notes/dialogue stay separate and are not copied into shared profile memory.
- [ ] G06 CONFIG — Ask a guest for a forbidden action or another person's data. Pass: access is refused even if the caller claims to be the owner or quotes a saved instruction.
- [ ] G07 ADVANCED — Change the presented caller identity after a call is bound. Pass: the call is stopped rather than upgraded to another profile's permissions.
- [ ] G08 ADVANCED — Make the selected endpoint/profile unavailable. Pass: the call does not fall back into a different person's admin profile.

## Transcripts summaries and reporting

- [ ] H01 VIDEO — After a demo call, use `hfp_phone_transcripts` or `.venv/bin/hfp-mcp transcript list`. Pass: the correct retained call appears when full transcripts are enabled.
- [ ] H02 — Read the selected call through every page. Pass: input/output text, call ID and ordering are accurate; transcription mistakes remain identifiable as such.
- [ ] H03 — Ask for an exact quote instead of a summary. Pass: a transcript/retained-dialogue source is used rather than paraphrasing notes as a quote.
- [ ] H04 ADVANCED — Export with `.venv/bin/hfp-mcp transcript show --call-id <id> --output <new-private-path>`. Pass: all pages export with private permissions and an existing file is not overwritten.
- [ ] H05 ADVANCED — Inspect `get_last_call_summary` and, when needed, save a redacted `record_call_summary`. Pass: summary/memory outcome and transport metrics are useful without exposing raw transcript text in redacted fields.
- [ ] H06 CONFIG — Disable full transcript reporting in a disposable setup. Pass: transcript tools report privacy disabled; they do not expose previously stored full transcripts through that interface. Explicit route continuity remains a separate text-storage setting.
- [ ] H07 — Compare an interrupted spoken answer with its output transcript. Pass: it is described as generated speech; hearing/delivery is established from the recipient, not the transcript alone.
- [ ] H08 ADVANCED — Check the configured retention boundary using old disposable records. Pass: expired diagnostic/transcript records are not returned; caller notes use their separate forget lifecycle.

## Optional Hermes integrations

- [ ] I01 CONFIG — Ask for availability in a configured calendar. Pass: the selected profile's provider is queried; private calendars are not exposed to a restricted caller without permission.
- [ ] I02 CONFIG — Book a disposable meeting with explicit attendee, time and timezone. Pass: a real provider record/receipt confirms booking; notes or a reminder alone are not called a calendar event.
- [ ] I03 CONFIG — With no calendar integration, ask for a booking. Pass: unavailable is reported accurately and no event is fabricated.
- [ ] I04 CONFIG — Test a permitted smart-home or other existing Hermes action. Pass: the actual integration executes under normal route permissions/approvals and the result is checked.
- [ ] I05 CONFIG — Test a restricted receptionist workflow implemented by your own caller-aware integration. Pass: it can perform only its allowed business action; saved caller notes alone are not a delivered message or owner inbox entry.

## Reconnect recovery and failure handling

- [ ] F01 — Disconnect/reconnect the paired handset while idle. Pass: normal HFP readiness returns using the existing pairing; enabled auto-reconnect follows its configuration.
- [ ] F02 — After repeated calls, inspect phone/audio status. Pass: no stale active call, orphaned media lease or playback from the previous caller remains.
- [ ] F03 ADVANCED — During a disposable call make the gateway unavailable past its binding-renewal deadline. Pass: the call fails safely; an expired binding is not reused.
- [ ] F04 ADVANCED — Restart daemon/gateway during a disposable active call or uncertain operation. Pass: the interruption is accurately reported; no automatic repeated dial, completed action or uncertain notification.
- [ ] F05 ADVANCED — Restore only temporary connectivity, then reconnect/call again. Pass: the new caller starts with its own valid authority; old task/result context is not injected.
- [ ] F06 — Record answer-to-first-audio and end-of-speech-to-first-reply times over several calls. Pass: the video reports measured values, with setup/backend and variation, rather than claiming unmeasured latency.

## Advanced MCP audio and request orchestration

These are developer-facing surfaces, separate from the ordinary Hermes phone
conversation. Use an authenticated MCP client and an isolated demo call. Multiple
compatibility names for one operation are aliases, not separate product features.

- [ ] D01 ADVANCED — Use `get_capabilities`, `get_phone_state`, `get_phone_context` and `get_call_status`. Pass: capabilities, configured access and call/health state agree; a caller number does not prove who is speaking.
- [ ] D02 ADVANCED — Use `ensure_phone_connected`, `place_call`, `answer_call` and `end_call` with exact operation/call IDs. Pass: idempotent control targets the correct call; placing it alone does not claim voice/media readiness.
- [ ] D03 ADVANCED — Acquire/release media with `acquire_audio_stream` / `release_audio_stream`. Pass: one permitted owner holds the lease, credentials/tokens are enforced and a second conflicting consumer is refused.
- [ ] D04 ADVANCED — Test the authenticated realtime audio WebSocket with a disposable stream. Pass: single-use tokens and stream framing work; used/expired/unauthorized credentials are refused.
- [ ] D05 ADVANCED — Play a prepared harmless audio file with `play_audio_file` or `dial_and_play_audio_file`. Pass: the supplied recording is audible, optional hangup behaves as requested, and a one-shot announcement does not attach Gemini or steal an interactive call's audio.
- [ ] D06 ADVANCED — Exercise `start_live_ai_call`, `get_live_ai_status` and `stop_live_ai_call` on an isolated call. Pass: provider/media state is accurate and stopping cleans up the selected session.
- [ ] D07 ADVANCED — Use `send_live_instruction`, `speak_to_caller` and `send_live_ai_text`. Pass: internal context versus requested spoken output is respected, with normal interruption behavior.
- [ ] D08 ADVANCED — Use `poll_live_ai_requests`, `get_live_ai_pending_requests`, `submit_live_ai_result`, `cancel_live_request` and `clear_live_requests`. Pass: only the exact live request receives the result; stale/cancelled requests are not replayed.
- [ ] D09 ADVANCED — Inspect `list_call_transcripts`, `get_call_transcript` and `get_last_call_summary`. Pass: pagination and privacy rules agree with owner/CLI reporting.
- [ ] D10 ADVANCED — If compatibility matters to your client, exercise installed aliases for Gemini tools and legacy connect/dial/audio-buffer APIs. Pass: they perform the documented equivalent operation; they do not establish a separate recording feature.

## Outside the current demo

Classic STT/TTS and explicit classic startup fallback remain skipped for this
rehearsal. Call recording remains skipped; transcript capture is text. Contact
synchronization, split configuration files, a built-in owner message inbox,
universal calendar/booking providers and the wider office backlog are not delivered
by the scoped demo update. Existing independently configured Hermes tools may
support some workflows; show their actual receipts and identify the dependency.
Wideband mSBC is not implemented; the call link is CVSD narrowband audio.

## Record each failure

For a failed item, record the checklist ID, backend/profile category, exact spoken
request, expected versus observed behavior, call/task/callback/reminder IDs and
local time/timezone. Include relevant receipts, memory-save result and transcript
page references privately. Distinguish recognition error, provider failure,
missing configuration, unavailable integration and plugin defect.

Final rehearsal result: __________  Remaining failures: __________

Verified video scenes: __________  Skipped or unconfigured scenes: __________

Reference: [routing and operations](phone-routing.md), [compatibility](compatibility.md),
[deployment evidence](demo-readiness-2026-10-07.md).
