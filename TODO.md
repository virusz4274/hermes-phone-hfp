# TODO

Reviewed 2026-09-29 against `development` at `cce260a` and the local
late-correction patch. Checked items below describe implemented mechanisms with
automated coverage; they do not certify every live voice workflow. Unchecked
items distinguish remaining implementation, deployment, and live acceptance.
That audit used 538 passing tests plus the isolated native Hermes compatibility
check. A subsequent Gemini configuration change passed 70 targeted tests. The
owner-call requirements below were added after reviewing a later live call;
they remain planned work, not implemented features.

## Next improvements

Prioritize direct rescheduling, verified confirmations, and unresolved-correction
reporting. Reuse the existing scheduler, task registry, notes, and delivery tools.

- [ ] **Direct reminder rescheduling.** Provide one operation to change an
  existing callback and its linked Telegram reminder together. Preserve their
  association, prevent duplicates, and handle partial failures and restarts.
  Scheduling and linked cancellation already exist; the current rescheduling
  workflow is cancel, confirm cancellation, then create a replacement.
- [ ] **Verify saved details before confirming.** Read back the authoritative
  callback and reminder state after creation or rescheduling. Confirm the saved
  date, time, and timezone, and expose any mismatch between the schedules.
  Creation receipts already distinguish full and partial success; the remaining
  work is verification of the effective state and reliable spoken confirmation.
- [ ] **Surface unresolved corrections.** Show both the effective result and
  requested change, for example: “6 PM is scheduled; the change to 4:30 PM
  failed.” Keep pending, failed, and uncertain corrections visible in owner
  status and completion notices. The local recovery patch below handles the
  completion race, but does not provide this full comparison with scheduler state.
- [ ] **Clear hangup handling for unfinished work.** Validate the spoken
  experience: explain that authorized work is still pending and where its result
  will arrive; otherwise explain that hanging up stops the task. Honor a caller's
  condition to wait for confirmation before ending the call. Continuation,
  cancellation, and expiry are already implemented; reliable model selection and
  truthful announcements remain live acceptance work.
- [ ] **Per-call audio diagnostic report.** Combine existing captured/submitted
  input activity, transcription/audio timing, playback counters, and disconnect
  timestamps into a readable report. Identify missing evidence and which metrics
  survive restart. Application queue drops are not Bluetooth packet-loss
  measurements, and a disconnect at hangup does not establish the cause of
  earlier static. Do not add raw audio retention by default.
- [ ] **Running-code identification.** Expose the revision loaded at startup,
  package version, service startup time, and whether the checkout has changed
  since launch. Cover the daemon and installed Hermes package separately,
  including editable installs, and indicate when a restart is required.

## Owner access and voice behavior from live-call review

The owner should be able to ask by phone or Hermes chat what other callers said,
what messages they left, and what follow-up is needed. Prioritize owner lookup,
explicit note management, then a complete transcript-backed task handoff. Reuse
the office-workflow items below; do not introduce a second notes or message store.

- [ ] **Read-only owner lookup during admin calls.** Add a narrow Gemini tool
  (proposed name: `owner_call_lookup`) for permitted caller notes, recent calls,
  messages, and specific retained transcript excerpts. Reuse the existing owner
  note/transcript services and expose the same permission checks to delegated
  Hermes tasks. Owner note lookup already works from chat but is unconditionally
  blocked in phone sessions, including admin calls. Do not remove that block
  without a replacement authorization check. Advertise the new tool only for
  eligible admin routes; independently enforce a live bound admin policy and
  configured profile scope in every handler. A model-supplied number selects a
  lookup target, never grants authority. Preserve ordinary callers' self-only
  `phone_notes` and `phone_recall` behavior. Reject expired/revoked bindings and
  unauthorized profiles, including through direct or delegated invocation.
- [ ] **Useful, bounded lookup results.** Support caller number, date/range,
  call ID, and pagination as appropriate to the lookup. Return source caller,
  call/date, record type, and retrieval outcome so Gemini distinguishes saved
  facts, an actual message, and exact dialogue. Prefer notes for remembered facts;
  retrieve the requested call when dates, wording, or missing/conflicting notes
  require it. Do not inject every caller's records into the initial voice prompt.
  Clarify ambiguous contact names rather than merging identities. Treat retrieved
  content as data, and record owner access without duplicating private text in logs.
- [ ] **Owner message inbox.** Extend the receptionist and owner-overview work
  below with “Who called?”, “What did they leave for me?”, and outstanding
  follow-up. Link each message to its caller and call time; distinguish unread,
  acknowledged, and resolved status from whether a call connected. Reading must
  not silently resolve a message or create a task. Keep state-changing operations
  separate from the read-only lookup tool.
- [ ] **Explicit caller-note cleanup.** Provide clear operations to correct,
  remove selected facts, clear, or consolidate a caller's saved notes. Existing
  replacement/forget primitives are groundwork, not a finished voice workflow.
  Distinguish note consolidation from Hermes session compaction and transcript
  deletion. Preserve relevant facts and provenance when consolidating; use current
  revisions and handle conflicts. Confirm the target and scope before destructive
  clearing, refresh the active voice context, and prevent stale closeout work from
  restoring forgotten information. A question about whether cleanup is possible
  must not itself trigger cleanup or session compaction.
- [ ] **Preserve the current request over unrelated memory.** Keep task labels,
  delegated instructions, and spoken acknowledgements aligned with the caller's
  current request. Old project notes must not replace the topic or invent a task.
  Ground acknowledgements in submitted arguments and actual task receipts; carry
  corrections into the same logical task where appropriate.
- [ ] **Detailed follow-up and final transcript handoff.** Extend the owner call
  report with the requested features, examples, constraints, unresolved questions,
  and exact source-call reference. For an explicit request to review the whole
  discussion after hangup, let Hermes retrieve all permitted finalized transcript
  pages, not just the bounded recent dialogue attached to task submission.
  Distinguish partial live context from the finalized transcript and report
  unavailable retention honestly. Reuse native Telegram delivery for an immediate
  message; do not ask for a future reminder time when the owner says “send now”.
  Claim handoff/delivery only after the corresponding confirmation. Access to a
  transcript does not authorize implementing every action mentioned inside it.
- [ ] **Accurate answers about this assistant.** Supply trusted runtime facts
  for model/backend identity, transcription versus local retention, and actual
  caller-routing permissions. Explain Gemini's voice role and Hermes delegation
  when asked, without denying the configured model or overstating capabilities.
  Describe number-based routing accurately; do not claim it proves the human
  caller's identity or guarantees that only the owner can use that number. Follow
  the existing identity decision below; this adds no OTP/PIN requirement.
- [ ] **Live acceptance for owner workflows.** Exercise owner lookup by phone
  and chat, guest denial, profile boundaries, note cleanup versus session
  compaction, unrelated-memory distractions, and a detailed follow-up after
  hangup. Verify saved records and actual tool outcomes against the spoken claims.

### Gemini tool inventory and planned access rules

Current routed calls expose `ask_hermes`, `end_call`, and `phone_status` by
default. `phone_notes` is added when caller-note reads are permitted; writes are
checked separately. Continuity adds `phone_recall`, `phone_session`, and
`hermes_task`. Notes-only outgoing routes replace `ask_hermes` with `phone_notes`.
The generic adapter also defines `notify_hermes`, `get_hermes_context`, and
`handoff_to_hermes`, but normal routed calls do not currently advertise them.
See [route tool selection](src/hfp_mcp/server.py) and
[tool definitions](src/hfp_mcp/gemini_live.py).

Guest profiles are supported by routing; their actual tools depend on the
configured route, profile, capabilities, memory policy, and continuity setting.
Do not infer an active guest profile from the presence of guest examples or the
restricted outgoing fallback. Review deployed routing before live acceptance.

Proposed additions/extensions (names are provisional; all remain unimplemented):

| Tool or extension | Owner/admin | Guest or restricted caller |
| --- | --- | --- |
| `owner_call_lookup` | Read permitted caller notes, calls, messages, and retained dialogue | Denied, including through Hermes delegation |
| `owner_notes_manage` | Separate authorized operations for another caller's note correction, consolidation, and confirmed clearing | Denied; retain only separately permitted self-note operations |
| `leave_message` | May leave a message when explicitly requested | Only with an explicit message-taking capability; submit to the configured owner destination without inbox access |
| Extend `phone_status` | Accurate runtime model, voice, retention, and effective caller capabilities | Only safe facts about this call; no other caller records, credentials, or private profile internals |
| Extend `ask_hermes` handoff | Detailed authorized work with finalized source-call retrieval when requested | Existing profile/tool limits and permitted own-call context only |

- [ ] **Implement owner lookup first.** Reuse existing lookup services rather
  than adding a general database, filesystem, or arbitrary-query tool to Gemini.
  Keep substantial reasoning, external actions, scheduling, and Telegram delivery
  in Hermes; short deterministic phone-data operations can use direct tools.
- [ ] **Define owner scope across guest profiles explicitly.** An owner's
  configured access may include messages/records from a guest profile serving
  that owner, without granting the guest access in the reverse direction. Resolve
  target profiles through trusted routing and explicit owner scope, not a model
  argument or an unrestricted scan of all Hermes profiles. Profile separation
  alone is not sufficient authorization for this new cross-caller read path.
- [ ] **Add a narrowly scoped message-taking capability.** Persist the message
  with the bound caller and source-call reference; let configured delivery use
  existing infrastructure. The caller cannot choose arbitrary recipients, browse
  the inbox, or impersonate another sender. Return a stored/delivery receipt and
  enforce duplicate prevention and bounded input. Do not automatically grant this
  capability to all guests or notes-only outgoing calls.
- [ ] **Enforce authorization beyond tool visibility.** Filter Gemini's
  declarations per route, then recheck live binding, expiry/revocation, policy,
  target ownership, and retention at execution. Apply equivalent checks in the
  daemon and Hermes paths. Guessed call IDs, forged profile/number arguments,
  spoken admin claims, and instructions embedded in notes must not expand access.
  A rejected lookup must not disclose another caller's existence or contents.
- [ ] **Test route changes and asynchronous result isolation.** Cover admin ->
  guest calls, reconnects, hangup during lookup, delayed task completion, and
  stale cached results. Never reuse an owner's retrieved context or pending
  response in another caller's Gemini session. Test direct-handler calls and
  delegated Hermes attempts, not only hidden tool declarations.

## Implemented groundwork — reuse rather than rebuild

### Caller identity, notes, and recall

- [x] Normalize numbers and scope notes to the routed profile and caller identity.
  Owner note tools and phone bindings share the same store. Different numbers and
  profiles remain separate; equivalent local/international forms normalize before
  owner lookup. Evidence: [caller-context tests](tests/test_caller_context.py),
  [owner-note tests](tests/test_owner_notes.py).
- [x] Load permitted notes during binding and before native Hermes model calls.
  Expose direct `phone_notes` access on permitted normal and restricted outgoing
  routes; refresh the voice context after a successful lookup. Read-only,
  disabled-memory, denied, conflicting, and failed lookups have distinct results.
  Evidence: [voice-note tests](tests/test_voice_notes.py),
  [phone bridge](src/hfp_mcp/hermes_bridge.py).
- [x] Enforce caller/profile authority in note handlers, reject forged
  number/profile arguments, keep withheld callers nonpersistent, and revoke call
  authority on hangup/expiry. A changed bound number ends the call instead of
  switching profiles. Restricted profiles cannot load shared private memory.
  Evidence: [caller-context tests](tests/test_caller_context.py),
  [controller tests](tests/test_phone_controller.py),
  [outgoing-route tests](tests/test_outgoing_routes.py).
- [x] Provide caller-scoped retained-dialogue recall, paging, archive/reset/delete,
  and native session continuity. Respect opt-ins and retention rather than
  importing unrelated historical transcripts. Evidence:
  [conversation tests](tests/test_phone_conversations.py),
  [transcript tests](tests/test_transcripts.py).
- [x] Supply notes-first recall guidance, natural incoming greeting guidance, and
  instructions to distinguish missing notes from unavailable history or lookup
  failures. Guidance is implemented; model behavior still needs the live checks
  below. Evidence: [voice configuration](src/hfp_mcp/gemini_live.py),
  [phone bridge](src/hfp_mcp/hermes_bridge.py).

### Memory closeout and quality

- [x] Checkpoint and finalize permitted caller facts after hangup, including final
  available transcription. Use bounded recovery, duplicate receipts, revision
  conflicts, and merge retries without reviving caller authority or losing
  concurrent owner edits. Expose save outcomes and preserve enabled detailed
  transcripts. Evidence: [memory tests](tests/test_call_memory.py).
- [x] Ground extracted facts in caller quotes, confidence, and relevance. Stamp
  explicit and automatic updates with trusted call dates/timezones/provenance;
  preserve older facts' provenance and avoid repeated labels or duplicate saves.
  Model interpretation still requires live validation. Evidence:
  [memory tests](tests/test_call_memory.py), [note-text tests](tests/test_note_text.py).
- [x] Complete the previously recorded review of three affected caller-note
  records: correct verifiable provenance/formatting issues and flag uncertain
  fragments while preserving transcripts. This is historical completed work
  recorded in the earlier TODO, not a new data-repair action from this review.

### Tasks, callbacks, and reminders

- [x] Keep phone tasks in native Hermes sessions with status, steering, approvals,
  cancellation, and bounded continuation after hangup. Enforce shared task capacity
  and separate run authority; uncertain admission/restart does not replay actions.
  Evidence: [task tests](tests/test_phone_tasks.py),
  [native-authority tests](tests/test_native_phone_authority.py).
- [x] Persist one-shot callbacks in the HFP daemon with number, purpose, and exact
  due time. Support cancellation, pending-job restart recovery, stale-job rejection,
  and visible failed/uncertain outcomes without automatic redial. Evidence:
  [callback scheduler](src/hfp_mcp/callbacks.py), [callback tests](tests/test_callbacks.py).
- [x] Use native Hermes cron for Telegram reminders with an explicit configured
  destination. Link the reminder to its callback, share the exact due time, pause
  the linked reminder on cancellation, and report partial scheduling failures.
  This supersedes the old plan to use an API-origin cron job to deliver or dial.
  Evidence: [reminder implementation](src/hfp_mcp/reminders.py),
  [reminder tests](tests/test_reminders.py).
- [x] Allow authorized scheduling tasks to finish after hangup and deliver their
  completion through native Telegram. Record uncertain notification delivery
  without automatically sending twice. Evidence:
  [reminder tests](tests/test_reminders.py), [task tests](tests/test_phone_tasks.py).
- [x] Carry the owner's self-contained purpose into the exact outgoing call, load
  permitted recipient notes, and preserve destination routing. Unconfigured
  destinations can use a restricted notes-only fallback without granting incoming
  access or owner tools. Evidence: [outbound-brief tests](tests/test_outbound_brief.py),
  [outgoing-route tests](tests/test_outgoing_routes.py).

A callback's successful start means voice setup succeeded; it does not establish
that the recipient heard the reminder or that the call's objectives were completed.

### Audio startup and diagnostics

- [x] Preload Gemini dependencies before Bluetooth/call admission and move
  call-time availability checks off the event loop. The previously recorded
  2026-09-22 test verified the cold-start improvement; further response latency
  remains open. Evidence: [startup tests](tests/test_startup_diagnostics.py).
- [x] Record call setup stages, bounded captured/submitted signal activity,
  startup input loss, and first transcription/provider/audio timings. Retain final
  voice metrics in the call audit and expose bounded retired-stream transport
  metrics in status/summaries. RMS activity does not identify speech; not every
  transport metric is persisted across restart. Evidence:
  [startup tests](tests/test_startup_diagnostics.py),
  [media-metric tests](tests/test_server_media_metrics.py),
  [call-end auditing](src/hfp_mcp/server.py).

## Local late-correction fix — deployment and live acceptance pending

- [x] Handle a completed native run that returns an unprocessed `pending_steer`:
  submit only the correction in a fresh native run under the same logical task
  and session. Preserve the original continuation deadline, retire the previous
  grant, and keep the task pending until the follow-up finishes. Cover completion
  races, hangup, cancellation, expiry, uncertain admission, restart, and completion
  notification behavior. Evidence: [task implementation](src/hfp_mcp/phone_tasks.py),
  [regression tests](tests/test_phone_tasks.py).
- [x] Clarify voice guidance that accepting a correction does not confirm the
  revised outcome. This is guidance plus code-level recovery, not a guarantee of
  correct spoken confirmation in every call.
- [ ] Deploy the tested local patch to both runtimes and restart after active
  calls/tasks settle. The earlier 0.1.1rc1 installation predates this patch.
- [ ] Run a live schedule -> time correction -> hangup test. Confirm the saved
  callback, linked Telegram reminder, spoken result, and completion notice all
  agree, and that the original schedule does not also fire. The patch does not
  automatically reschedule previously completed tasks with stranded corrections.

## Remaining live acceptance and audio investigation

- [ ] Verify saved-fact -> later-call -> recall on incoming known/guest routes and
  outgoing explicit/fallback routes. Include delivery/RAM detail, missing notes,
  stale notes updated by the owner mid-call, and questions requiring retained
  dialogue. Check natural greetings and accurate claims about what was retrieved
  or saved; the existing tools and prompts do not prove consistent model use.
- [ ] Exercise caller/profile isolation end to end, including initial model
  context, not only retrieval handlers. Cover equivalent number formats, different
  callers/profiles, withheld/invalid numbers, `remember: false`, expired/revoked
  bindings, identity changes, and misleading instructions in notes. Implementation
  and regression coverage exist; the full physical/model acceptance matrix remains
  open, including honest limitation messages and resistance to note instructions.
- [ ] Validate live memory quality after abrupt hangup: useful dated facts,
  uncertain recognition fragments, concurrent edits, duplicate prevention, and
  save-failure reporting. Earlier successful note saves do not complete the
  delivery/RAM recall scenario.
- [ ] Investigate immediate speech during connection and remaining turn-detection
  or provider response delay. The cold SDK fix is complete. Earlier calls still
  took about 10–11 seconds to first playback despite fast dependency checks;
  instrument and compare speech/response stages before choosing further tuning.
- [ ] Diagnose intermittent static if reproducible with call-specific evidence.
  The two 2026-09-29 morning failures had no recorded assistant audio and zero
  Gemini playback drops; historical overflow counters do not explain them. A
  later call worked according to the user. Low battery and the intervening
  Bluetooth/service restarts remain possible explanations, not established causes.
- [ ] Verify scheduled callback voice delivery and linked Telegram delivery after
  hangup and gateway restart, including busy, unanswered, disconnected, missed,
  and partial-failure cases. Successful scheduling and a successful manually
  tested call do not establish this whole workflow. Include the caller's
  condition to wait for confirmation before hanging up.

## Further office workflows

These build on existing notes, recall, tasks, and scheduling rather than replacing
those mechanisms. No calendar backend or complete business workflow is bundled.

- [ ] **Owner call report.** Add a concise report covering the call's objective,
  outcome, decisions, open questions, next actions, and memory-save result. Link
  outgoing reports to the owner request and exact call. Reuse existing summaries
  and native delivery, distinguish voice connection from completed objectives,
  and handle failed report delivery. Memory closeout itself is already implemented.
- [ ] **Current status and dated history per person.** Combine existing notes and
  scoped retained dialogue to answer “What's he up to?” and “What did he report
  last week?” Preserve reporting dates, caller attribution, uncertainty, retention,
  and profile boundaries; improve historical/date selection where needed.
- [ ] **Commitments and outstanding work.** Track who agreed to do what, for whom,
  and by when through existing task integrations. Distinguish intentions, agreed
  commitments, reported completion, and verified results. Saving a fact must not
  silently create an external action.
- [ ] **Contact names and calling preferences.** Resolve owner-maintained names,
  nicknames, company/project, language, and calling hours through an existing
  contact integration where available. Clarify ambiguous destinations. Enforce
  calling hours for scheduled calls; labels must not merge identities or grant
  permissions. Any retry policy must be explicit and bounded.
- [ ] **Receptionist messages and meetings.** Deliver admitted callers' messages
  and claimed urgency to the configured owner destination. Follow owner escalation
  preferences. Use authorized calendar integrations to check availability and
  book/change meetings; restricted callers may propose meetings without gaining
  private calendar access. Confirm bookings only from actual tool results.
- [ ] **Owner overview and corrections.** Summarize today's calls, pending work,
  people waiting, and failed/uncertain operations using the records above. Build
  on existing read/update/forget tools for corrections. Recurring briefings require
  an owner request; no unsolicited periodic reports.
- [ ] **End-to-end office scenario.** Test successive calls about a delivery,
  RAM issue, and agreed follow-up; later recall and owner overview; then an
  authorized scheduled call using fresh permitted context. Check isolation,
  actual objective completion, restart behavior, and duplicate prevention.

## Decisions and implementation constraints

Trust the mobile number reported by the paired phone over Bluetooth HFP as the
caller identity, subject to routing and controller binding. No OTP, PIN, or extra
identity challenge is planned. Spoken claims, notes, and model/tool arguments
cannot replace the bound number or increase permissions. Spoofed, forwarded,
shared, or reassigned numbers are accepted limitations of this identity model;
number hashing isolates records but does not authenticate the person.

An unknown incoming caller needs an explicit guest route. An owner-admitted
outgoing call may use the restricted fallback without enabling incoming access.
Never merge caller records based on a spoken identity claim. Preserve existing
permissions, normal action approvals, opt-ins, retention, and deletion behavior.

Keep prompt changes concise and consolidate conflicting instructions. Enforce
provenance, revisions, isolation, duplicate prevention, and task lifetime in code.
Use native Hermes execution/approvals and cron for text delivery; use the HFP
daemon's callback scheduler for future dialing. Do not revive the obsolete
memory-branch plan or add another memory store, sender plugin, or task scheduler.

For operational behavior see [phone routing](docs/phone-routing.md),
[maintenance](docs/maintenance.md), and [security](SECURITY.md).
