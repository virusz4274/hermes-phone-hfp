# TODO

The [implementation specification](docs/implementation-spec.md) defines the remaining
requirements. The [structured backlog](docs/implementation-backlog.yaml) tracks
dependencies, acceptance checks, and evidence for future implementation sessions.
Use the [goal prompt](docs/implementation-spec.md#goal-prompt-for-another-session)
to hand off the work. Requirement links below map unfinished items; historical
evidence is retained and superseded behavior is labeled. Code verification,
deployment, live acceptance, and audio investigations have separate completion states.

Reconciled 2026-10-25 against the current `development` checkout. Since the
2026-10-09 checklist, configurable reminder delivery and callback retries,
revision-safe caller-note reconciliation, and explicit wait-for-confirmation
handling have been implemented. This file now lists remaining implementation,
deployment, live-acceptance, and investigation work; completed implementation
details remain in the specification, tests, and dated evidence. The 2026-09-29
audit (538 tests and isolated Hermes checks) and subsequent 70-test configuration
check are historical.
The [8 October report](docs/hermes-owner-flow-verification-2026-10-08.md) records
658 source/installed tests and coordinated deployment for the new workflow.
Owner lookup and direct rescheduling are implemented. Basic owner note read and
whole-note replacement/forget controls exist, and automatic after-call note
reconciliation now supports targeted revisions. A dedicated owner voice workflow
for selectively removing/consolidating facts, an inbox, and complete transcript
handoff remain unfinished.

## Next improvements

Prioritize configuration/CLI reliability and missing readiness/identity/reporting
interfaces (QOL-01/02, OPS-01/02/03), then the remaining owner/office workflows.
Phone scheduling, reminders, and callback retries use native Hermes jobs; these
items do not call for a second scheduler.

- [ ] [QOL-01](docs/implementation-spec.md#qol-01) / [QOL-02](docs/implementation-spec.md#qol-02) — **Configuration and CLI reliability.**
  Finish unknown-key and missing-file validation, concise operational errors,
  stable exit codes, and bounded status request timeouts.
- [ ] [OPS-01](docs/implementation-spec.md#ops-01) — **Operational readiness.**
  Add structured authenticated readiness details for the daemon, HFP connection,
  routing, gateways, and voice prerequisites, with safe guest-facing status.

- [ ] [TASK-03](docs/implementation-spec.md#task-03) — **Clear hangup handling for unfinished work.** A caller can explicitly
  ask the call to stay open until task confirmation; an assistant hangup is
  blocked while the task is pending, and the caller can retract that condition.
  Continuation, cancellation, and expiry are implemented. Validate the spoken
  experience: explain that authorized work is pending and where its result will
  arrive, or explain that hanging up stops it. Reliable model behavior and
  truthful announcements remain live acceptance work.
- [ ] [OPS-03](docs/implementation-spec.md#ops-03) — **Per-call audio diagnostic report.** Combine existing captured/submitted
  input activity, transcription/audio timing, playback counters, and disconnect
  timestamps into a readable report. Identify missing evidence and which metrics
  survive restart. Application queue drops are not Bluetooth packet-loss
  measurements, and a disconnect at hangup does not establish the cause of
  earlier static. Do not add raw audio retention by default.
- [ ] [OPS-02](docs/implementation-spec.md#ops-02) — **Running-code identification.** Expose the revision loaded at startup,
  package version, service startup time, and whether the checkout has changed
  since launch. Cover the daemon and installed Hermes package separately,
  including editable installs, and indicate when a restart is required.

## Owner access and voice behavior from live-call review

The owner should be able to ask by phone or Hermes chat what other callers said,
what messages they left, and what follow-up is needed. Build on implemented owner
lookup with explicit note management and complete transcript-backed task handoff. Reuse
the office-workflow items below; do not introduce a second notes or message store.

- [ ] [MSG-01](docs/implementation-spec.md#msg-01) — **Owner message inbox.** Extend the receptionist and owner-overview work
  below with “Who called?”, “What did they leave for me?”, and outstanding
  follow-up. Link each message to its caller and call time; distinguish unread,
  acknowledged, and resolved status from whether a call connected. Reading must
  not silently resolve a message or create a task. Keep state-changing operations
  separate from the read-only lookup tool.
- [ ] [OWNER-02](docs/implementation-spec.md#owner-02) — **Explicit caller-note cleanup.** Provide clear operations to correct,
  remove selected facts, clear, or consolidate a caller's saved notes. Existing
  replacement/forget primitives are groundwork, not a finished voice workflow.
  Distinguish note consolidation from Hermes session compaction and transcript
  deletion. Preserve relevant facts and provenance when consolidating; use current
  revisions and handle conflicts. Confirm the target and scope before destructive
  clearing, refresh the active voice context, and prevent stale closeout work from
  restoring forgotten information. A question about whether cleanup is possible
  must not itself trigger cleanup or session compaction.
- [ ] [TASK-03](docs/implementation-spec.md#task-03) — **Preserve the current request over unrelated memory.** Keep task labels,
  delegated instructions, and spoken acknowledgements aligned with the caller's
  current request. Old project notes must not replace the topic or invent a task.
  Ground acknowledgements in submitted arguments and actual task receipts; carry
  corrections into the same logical task where appropriate.
- [ ] [REPORT-01](docs/implementation-spec.md#report-01) — **Detailed follow-up and final transcript handoff.** Extend the owner call
  report with the requested features, examples, constraints, unresolved questions,
  and exact source-call reference. For an explicit request to review the whole
  discussion after hangup, let Hermes retrieve all permitted finalized transcript
  pages, not just the bounded recent dialogue attached to task submission.
  Distinguish partial live context from the finalized transcript and report
  unavailable retention honestly. Reuse native Telegram delivery for an immediate
  message; do not ask for a future reminder time when the owner says “send now”.
  Claim handoff/delivery only after the corresponding confirmation. Access to a
  transcript does not authorize implementing every action mentioned inside it.
- [ ] [VOICE-01](docs/implementation-spec.md#voice-01) — **Accurate answers about this assistant.** Supply trusted runtime facts
  for model/backend identity, transcription versus local retention, and actual
  caller-routing permissions. Explain Gemini's voice role and Hermes delegation
  when asked, without denying the configured model or overstating capabilities.
  Describe number-based routing accurately; do not claim it proves the human
  caller's identity or guarantees that only the owner can use that number. Follow
  the existing identity decision below; this adds no OTP/PIN requirement.
- [ ] [LIVE-01](docs/implementation-spec.md#live-01) — **Live acceptance for owner workflows.** Exercise owner lookup by phone
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

Implemented and planned surfaces (status is explicit; planned names remain provisional):

| Tool or extension | Owner/admin | Guest or restricted caller |
| --- | --- | --- |
| `owner_call_lookup` (implemented) | Read permitted caller notes, calls, and retained dialogue; messages await MSG-01 | Denied, including through Hermes delegation |
| `owner_notes_manage` (planned) | Separate authorized operations for another caller's note correction, consolidation, and confirmed clearing | Denied; retain only separately permitted self-note operations |
| `leave_message` (planned) | May leave a message when explicitly requested | Only with an explicit message-taking capability; submit to the configured owner destination without inbox access |
| Extend `phone_status` (partial) | Accurate runtime model, voice, retention, and effective caller capabilities | Only safe facts about this call; no other caller records, credentials, or private profile internals |
| Extend `ask_hermes` handoff (planned) | Detailed authorized work with finalized source-call retrieval when requested | Existing profile/tool limits and permitted own-call context only |

- [ ] [MSG-01](docs/implementation-spec.md#msg-01) — **Add a narrowly scoped message-taking capability.** Persist the message
  with the bound caller and source-call reference; let configured delivery use
  existing infrastructure. The caller cannot choose arbitrary recipients, browse
  the inbox, or impersonate another sender. Return a stored/delivery receipt and
  enforce duplicate prevention and bounded input. Do not automatically grant this
  capability to all guests or notes-only outgoing calls.
## Remaining live acceptance and audio investigation

- [ ] [LIVE-02](docs/implementation-spec.md#live-02) — Verify saved-fact -> later-call -> recall on incoming known/guest routes and
  outgoing explicit/fallback routes. Include delivery/RAM detail, missing notes,
  stale notes updated by the owner mid-call, and questions requiring retained
  dialogue. Check natural greetings and accurate claims about what was retrieved
  or saved; the existing tools and prompts do not prove consistent model use.
- [ ] [LIVE-02](docs/implementation-spec.md#live-02) — Exercise caller/profile isolation end to end, including initial model
  context, not only retrieval handlers. Cover equivalent number formats, different
  callers/profiles, withheld/invalid numbers, `remember: false`, expired/revoked
  bindings, identity changes, and misleading instructions in notes. Implementation
  and regression coverage exist; the full physical/model acceptance matrix remains
  open, including honest limitation messages and resistance to note instructions.
- [ ] [LIVE-02](docs/implementation-spec.md#live-02) — Validate live memory quality after abrupt hangup: useful dated facts,
  uncertain recognition fragments, concurrent edits, duplicate prevention, and
  save-failure reporting. Earlier successful note saves do not complete the
  delivery/RAM recall scenario.
- [ ] [INVEST-01](docs/implementation-spec.md#invest-01) — Investigate immediate speech during connection and remaining turn-detection
  or provider response delay. The cold SDK fix is complete. Earlier calls still
  took about 10–11 seconds to first playback despite fast dependency checks;
  instrument and compare speech/response stages before choosing further tuning.
- [ ] [INVEST-02](docs/implementation-spec.md#invest-02) — Diagnose intermittent static if reproducible with call-specific evidence.
  The two 2026-09-29 morning failures had no recorded assistant audio and zero
  Gemini playback drops; historical overflow counters do not explain them. A
  later call worked according to the user. Low battery and the intervening
  Bluetooth/service restarts remain possible explanations, not established causes.
- [ ] [LIVE-03](docs/implementation-spec.md#live-03) — Verify native scheduled-call voice delivery and the configured gateway reporting policy after
  hangup and gateway restart, including a schedule time correction against the
  same native job (the old occurrence must not fire), busy, unanswered,
  disconnected, missed, and partial-failure cases. Check exhaustion/expiry/guidance
  notices, no routine retry/completion/refusal/cancellation reports, and no gateway
  notices for call-only reminders. Successful scheduling and a
  successful manually tested call do not establish this whole workflow. Include
  the caller's condition to wait for confirmation before hanging up. Historical
  stranded corrections are not automatically repaired.

## Further office workflows

These build on existing notes, recall, tasks, and scheduling rather than replacing
those mechanisms. No calendar backend or complete business workflow is bundled.

- [ ] [REPORT-01](docs/implementation-spec.md#report-01) — **Owner call report.** Extend the implemented attempt/decision report to a complete report covering the call's objective,
  outcome, decisions, open questions, next actions, and memory-save result. Link
  outgoing reports to the owner request and exact call. Reuse existing summaries
  and native delivery, distinguish voice connection from completed objectives,
  and handle failed report delivery. Memory closeout itself is already implemented.
- [ ] [OFFICE-01](docs/implementation-spec.md#office-01) — **Current status and dated history per person.** Combine existing notes and
  scoped retained dialogue to answer “What's he up to?” and “What did he report
  last week?” Preserve reporting dates, caller attribution, uncertainty, retention,
  and profile boundaries; improve historical/date selection where needed.
- [ ] [OFFICE-02](docs/implementation-spec.md#office-02) — **Commitments and outstanding work.** Track who agreed to do what, for whom,
  and by when through existing task integrations. Distinguish intentions, agreed
  commitments, reported completion, and verified results. Saving a fact must not
  silently create an external action.
- [ ] [OFFICE-02](docs/implementation-spec.md#office-02) — **Contact names and calling preferences.** Default workflow retry hours exist; recipient-specific preferences remain. Resolve owner-maintained names,
  nicknames, company/project, language, and calling hours through an existing
  contact integration where available. Clarify ambiguous destinations. Enforce
  calling hours for scheduled calls; labels must not merge identities or grant
  permissions. Any retry policy must be explicit and bounded.
- [ ] [MSG-01](docs/implementation-spec.md#msg-01), [OFFICE-03](docs/implementation-spec.md#office-03) — **Receptionist messages and meetings.** Deliver admitted callers' messages
  and claimed urgency to the configured owner destination. Follow owner escalation
  preferences. Use authorized calendar integrations to check availability and
  book/change meetings; restricted callers may propose meetings without gaining
  private calendar access. Confirm bookings only from actual tool results.
- [ ] [OFFICE-01](docs/implementation-spec.md#office-01) — **Owner overview and corrections.** Summarize today's calls, pending work,
  people waiting, and failed/uncertain operations using the records above. Build
  on existing read/update/forget tools for corrections. Recurring briefings require
  an owner request; no unsolicited periodic reports.
- [ ] [VERIFY-01](docs/implementation-spec.md#verify-01), [LIVE-04](docs/implementation-spec.md#live-04) — **End-to-end office scenario.** Test successive calls about a delivery,
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
Use native Hermes execution/approvals, cron scheduling, follow-up decisions, and
delivery. The plugin executes admitted calls and returns evidence; no daemon timer. Do not revive the obsolete
memory-branch plan or add another memory store, sender plugin, or task scheduler.

For operational behavior see [phone routing](docs/phone-routing.md),
[maintenance](docs/maintenance.md), and [security](SECURITY.md).
