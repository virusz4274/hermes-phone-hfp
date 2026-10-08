# Hermes Phone implementation requirements

This specification defines the remaining work for everyday operation, owner access,
receptionist and office workflows, and verification. It is a handoff for an engineer
or autonomous coding session. Read it together with the
[structured backlog](implementation-backlog.yaml). The backlog records dependencies,
acceptance checks, and progress; this document defines the intended behavior.

Status: approved implementation scope; implementation and live acceptance remain
pending until individually verified. Writing these documents does not implement
the requirements or authorize live operations.

## Baseline and completion boundary

Prepared on 2026-10-04 from `development` at
`0c0680d5ea7e1b953bcd33b9a8c3f4e4bc6e4b6b`. The preceding review ran
`.venv/bin/python -m pytest -q -o faulthandler_timeout=60`: 554 tests passed,
with one Starlette test-client deprecation warning. This is historical evidence,
not a substitute for checks against the eventual implementation.

Scope includes every unfinished item in [TODO](../TODO.md) at that revision and
the review's configuration, CLI, and readiness findings. Completed TODO entries
remain implemented groundwork and regression obligations. Inspect current code
before changing it; do not rebuild features merely because an old checklist is
stale. In particular, continuity, closeout, scheduling, cancellation, and native
task steering already exist.

The autonomous implementation goal is complete when all `implementation` and
`automated_verification` items pass their acceptance checks, required documentation
and migrations are present, and the backlog contains evidence. Deployment,
physical calls, provider behavior, and audio investigations have separate items.
They can remain pending without implying that the whole deployed system has been
validated. A blocked implementation item still prevents code-scope completion.

This scope does not add a web dashboard, new calendar/contact service, alternate
scheduler, general database tool for Gemini, new identity challenge, or automatic
calling/delivery retries. Actual integrations remain operator-configured.

## How to execute and record progress

1. Inspect the current checkout, repository instructions, this spec, and the YAML.
   Reconcile already implemented behavior against acceptance checks before edits.
2. Choose pending work whose dependencies are done; use ascending priority, then
   YAML order. An item may be partially developed early, but cannot be marked done
   until its dependencies and acceptance checks pass.
3. Make focused changes using existing services and stores. Add regression tests
   for behavioral changes and update the affected public documentation.
4. Record test commands, results, source/test references, and the tested revision
   or working-tree description. Do not store private configurations or call data.
5. Mark an item done only when every acceptance check has passing evidence. If a
   prerequisite is unavailable, record the specific blocker and continue with
   independent items. Never lower a requirement merely to clear a blocker.
6. Keep TODO requirement links and completion claims consistent with the YAML.
   Historical completed-work notes remain intact. Report outstanding external
   configuration and physical verification separately at handoff.

The YAML uses stable requirement and acceptance IDs. Item status is `pending`,
`in_progress`, `blocked`, or `done`; acceptance status is `pending`, `passed`,
`failed`, or `blocked`. Evidence entries contain `command_or_procedure`, `result`,
`tested_revision`, and `references`. `blocker` is null or an object with `reason`
and `needed_to_resume`. Do not renumber completed IDs. Add acceptance checks when
needed without deleting failed evidence. Source references identify TODO entries
by their original opening text, independent of shifting line numbers.

## Shared architecture and behavior

- The daemon remains the sole Bluetooth/audio owner. Hermes remains responsible
  for substantial reasoning, external tools, approvals, native task sessions,
  cron reminders, and Telegram delivery. Short deterministic phone-data operations
  may be direct voice tools.
- Reuse daemon call/audit storage and the routed profile's caller store. New
  structured records, operation receipts, or inbox tables belong in those stores;
  do not introduce a second notes database or delivery service. Use additive,
  repeatable migrations and test upgrade from existing data.
- The handset-reported normalized number selects caller identity and routing; it
  does not authenticate a human. Spoken claims, contact labels, retrieved text,
  and model arguments never grant authority. Keep existing normal approvals.
- Tool declarations reflect route permissions, but every handler independently
  verifies live authority, scope, expiry/revocation, and target ownership. Check
  authority again before returning asynchronous data to a voice session. Reject
  inaccessible resources without revealing their existence.
- Preserve current transcript/continuity opt-ins and retention, note revisions,
  forget generations, single-call ownership, task limits, cancellation, and
  no-replay behavior. Withheld callers never acquire another caller's persistent
  identity. Retrieved content is data, not execution permission.
- Separate requested, accepted, persisted, executed, and delivered outcomes.
  Voice connection alone does not prove the recipient heard a message or that
  the call's objective was achieved. Unknown external outcomes remain unknown.
- Preserve existing APIs and default CLI JSON output where feasible; add fields
  and explicit modes. Document intentional validation changes and additive
  capability negotiation. A missing capability in an older plugin produces a
  clear unavailable result, not an unhandled error or relaxed authorization.
- Public diagnostics contain no secrets. Detailed owner records stay behind
  existing authentication. No raw audio retention or live provider probe is
  enabled merely to implement diagnostics.

## Implementation requirements

### QOL-01

**Configuration validation.** Reject unknown keys within the owned `phone`
configuration and its nested structures, while preserving unrelated root YAML
sections. Validate country codes even with no number routes. Name the invalid
field and offer a likely spelling where unambiguous, without echoing secrets.
An explicitly supplied `route --config` path that does not exist is an error.
An absent optional default configuration remains supported and clearly reports
routing disabled. Preserve documented defaults and already valid configurations.
Regression examples: `auto_answr: false`, `default_region: NOT_A_REGION`, duplicate
keys, malformed types, and an explicitly missing file.

### QOL-02

**CLI usability.** Convert expected configuration, token-file, HTTP authorization,
connection, malformed-response, and timeout errors to concise messages and
nonzero exit status without tracebacks or secret-bearing response bodies. Keep
default successful JSON output; add `--human` to phone status and `--json` to new
diagnostic commands. Ordinary status HTTP reads use a five-second timeout per
request. Existing potentially long mutations retain their operation-specific
limits, and unknown mutation outcomes are never automatically retried. Usage and
validation errors exit 2; operational failures exit 1; success exits 0.

### OPS-01

**Operational readiness.** Add an authenticated, structured `readiness` section
to owner phone status and its CLI/Hermes presentation. Include daemon liveness,
HFP connection, routing/admission state, per-endpoint gateway checks, voice
prerequisites, observation time, blocking reasons, and suggested next actions.
Distinguish `ready`, `not_ready`, and `unknown` per component. Provider credentials
being present is not proof of provider access or working audio. Bounded checks
must not prevent reporting local status when a gateway is unreachable.
Keep `/healthz` and `/readyz` compatible as existing liveness/profile checks;
document their narrower meaning rather than silently redefining monitoring.
Guest in-call status exposes only facts safe for that call.

### OPS-02

**Running implementation identity.** Expose separate daemon/plugin package
versions, startup timestamps, source revisions where available, editable-install
status, and detected source changes since startup. Capture identity at startup;
do not substitute the checkout's current HEAD for the revision originally loaded.
Use a source fingerprint to detect uncommitted/editable changes, distinguish code
changes from documentation-only changes, and never include environment secrets.
Report restart-needed or identity-unknown explicitly. Installed wheels without
Git must work; the optional revision may be unknown. Extend authenticated
capabilities/status without requiring Git or native Bluetooth in the base plugin.

### OPS-03

**Call diagnostics and support export.** Add
`hfp-mcp call diagnose <call-id> [--json] [--redacted] [--output PATH]` backed by
authenticated lookup of existing metrics. Summarize call setup, captured versus
submitted signal activity, transcription/provider/first-playback timing, queue
loss, underruns, disconnect timing, and persistence limitations. Missing metrics
are unavailable, not zero. Keep multiple audio streams attached to the correct
call and distinguish live from final evidence.
Redacted export uses an allowlist of structured diagnostic fields, replacing
identifiers where correlation is needed and omitting dialogue, numbers, Bluetooth
addresses, private paths, credentials, arbitrary exception text, and provider
bodies. Exports use private permissions and refuse overwrite. Do not equate
application queue drops with Bluetooth packet loss or infer static's cause from
an unrelated call. Retain metrics under existing diagnostic retention.

### TASK-01

**Direct callback and reminder rescheduling.** Add an owner operation
`hfp_phone_reschedule_callback` taking the exact callback ID, an idempotency
request ID, the previously read callback revision, and a timezone-aware target
time. Extend existing callback control and native Hermes reminder management;
preserve callback/reminder identity and their link. Reject a firing or terminal
callback and stale revisions without applying an update.
Coordinate changes with durable operation receipts and read-back reconciliation;
do not claim a transaction across two independent stores. Serialize reschedule
against firing/cancellation, and prevent the callback from firing while its
schedule change is unresolved. If only one side changes, persist both observed
times and the requested time, expose partial/uncertain state, and require
reconciliation before firing resumes. Recovery may inspect state and retry a
provably idempotent schedule update; it must not replay a dial or an uncertain
notification. An unlinked callback can move, but its missing reminder remains
visible. Preserve the existing stale-callback firing window.

### TASK-02

**Verified results and corrections.** After schedule creation or change, read
the authoritative callback/reminder state and confirm actual date, time, and
timezone. Expose requested versus effective values, pending/failed/uncertain
corrections, and separate verification failures from failed writes. For example,
say that 6 PM remains scheduled when a requested move to 4:30 failed. Status,
task receipts, spoken guidance, and completion messages use the same outcome
record. Do not repair historical stranded tasks silently or interpret an accepted
steer as evidence that the underlying schedule changed.

### TASK-03

**Current intent and task lifetime.** Preserve the caller's current request in
labels, delegated instructions, and acknowledgements despite unrelated stored
notes. Apply corrections to the same logical task where appropriate, reusing the
implemented late-steer recovery. Retain original deadlines, cancellation,
capacity, and approval boundaries across follow-up runs. Explain authorized
continuation and its delivery destination before ending a call when possible;
otherwise explain that hangup cancels work. An explicit request to wait for
confirmation prevents an assistant-initiated hangup while confirmation is pending,
subject to existing call limits. A remote hangup still ends the physical call.
Tests establish deterministic state behavior; spoken consistency has live gates.

### OWNER-01

**Bounded owner lookup with explicit scope.** Add `owner_call_lookup` for eligible
admin voice sessions and an equivalent owner-chat/delegated path using the same
authorization service. Read permitted notes, call metadata, retained dialogue,
and messages once MSG-01 exists. Support normalized number, call ID, date/range,
record kind, and bounded cursor pagination; default 20 records, maximum 100,
with a 12,000-character response budget and continuation rather than silent loss.
Return source caller/profile, call/date, record type, and retrieval outcome.
Prefer notes for remembered facts and retrieve dialogue for exact wording or
insufficient/conflicting notes. Never preload all callers into the prompt.

Introduce `phone.owner_access`, keyed by owner endpoint name, with lists of
additional permitted target endpoint names. Default to the bound admin endpoint
only. Resolve endpoints to authenticated profiles through trusted routing;
validate references and enforce the originating owner's scope in both daemon
and gateway paths. Cross-profile access is one-way and does not make the target
profile an owner. Guessed IDs, model-supplied profiles, forged numbers, revoked
bindings, and guest delegation never expand scope. Log access metadata without
private record text. Old owner-tool restrictions remain until replaced with
equivalent or stronger checks. Message queries may report unavailable until
MSG-01 is implemented; they must use its shared records afterward.

### OWNER-02

**Explicit note management.** Add `owner_notes_manage` with separate correction,
selected removal, consolidation, and clear operations. Resolve the target through
OWNER-01 scope, read the current revision, and use existing revision-checked
replacement/forget primitives. Consolidation preserves relevant facts, dates,
sources, and uncertainty. Confirm target and scope before destructive clearing;
confirmation must correspond to the actual operation, not merely asking whether
cleanup is possible. Refresh active permitted voice context and prevent pending
closeout work from restoring cleared or removed information. Keep notes, session
compaction, and transcript deletion distinct. Conflicts require reread rather
than overwriting concurrent edits.

### MSG-01

**Message taking and owner inbox.** Add an explicitly allowed `leave_message`
capability and separate authorized inbox state operations. Store bounded message
text (maximum 8,000 characters), trusted caller/call/profile attribution, time,
an idempotency receipt, and independent inbox/delivery states in the existing
profile store. A withheld caller may leave a call-scoped message only when its
route explicitly permits it; do not invent a persistent phone identity.
Use `unread`, `acknowledged`, and `resolved` inbox states. Reads change none of
them and create no task. The caller cannot browse the inbox, change another
message, choose a delivery recipient, or impersonate a sender.

Add trusted per-endpoint `phone.message_delivery` configuration selecting the
owner endpoint; it must name an endpoint whose owner scope includes the source.
The target endpoint uses its existing configured native delivery destination.
Message-taking is disabled unless the capability and destination are configured;
it is never inherited by all guests or notes-only outgoing routes. Explicitly
permitted message storage is separate from transcript/continuity retention.
Provide `phone.message_retention_days` with default 30, positive integer values,
documented expiry, and owner deletion. Repeated submissions return the same
stored receipt. Distinguish stored, delivered, failed, and uncertain delivery;
do not resend an uncertain notification automatically. Expose messages through
OWNER-01 and respect deletions in subsequent lookups.

### REPORT-01

**Call reports and finalized transcript handoff.** Extend existing call summaries
and native delivery with objective, outcome, decisions, requested features,
examples, constraints, open questions, next actions, memory-save result, and exact
source-call reference. Outgoing reports preserve the owner's request and exact
call association. An explicit whole-discussion request retrieves every permitted
finalized transcript page, not just recent task context. Bound individual fetches
and process long transcripts incrementally without silently dropping pages.
Distinguish live partial context, finalized history, retention disabled, expired,
and failed retrieval. After hangup, use existing bounded task authority, scoped
to the authorized source call; do not revive the revoked caller binding.
“Send now” uses immediate native delivery rather than asking for a reminder time.
Report queued, delivered, failed, or uncertain from actual receipts. Transcript
access does not authorize carrying out every action mentioned inside it.

### VOICE-01

**Accurate assistant facts and claims.** Supply trusted safe runtime facts for
the configured voice model/backend, Hermes delegation, effective caller tools,
transcription, local transcript retention, and continuity. Answer only as precisely
as the runtime knows; configured identity is not evidence of a successful provider
session. Explain number-based routing without claiming human authentication.
Distinguish reading existing notes from saving them and task admission from
completion. Keep prompts concise and consolidate conflicting guidance. Never
expose owner profiles, secrets, or other callers' state to a restricted caller.

### OFFICE-01

**Dated person history and owner overview.** Compose notes, scoped retained
dialogue, messages, and native task outcomes into current-person and date-range
answers plus today's calls, people waiting, and pending/failed work. Preserve
source dates, caller attribution, uncertain claims, and distinction between
missing records and unavailable retention. Corrections go through explicit
mutation tools. Reading an overview does not create actions or resolve messages;
recurring briefings require a separate owner request.

### OFFICE-02

**Commitments, contacts, and calling preferences.** Use existing configured Hermes
contact/task integrations; provide a documented narrow adapter contract and fake
adapters for tests, not a bundled provider service. Resolve names, nicknames,
company/project, language, and calling windows into candidate numbers and source
metadata. Clarify ambiguous destinations and normalize before routing. Saving
a caller fact does not create a commitment/task. Distinguish intention, agreed
commitment, reported completion, and verified completion.
Check configured recipient calling windows in their configured timezone when
scheduling and immediately before dialing. If a proposed time is outside the
window, return the next permissible time for explicit selection; do not silently
move or retry the call. A due callback outside the effective window is recorded
as blocked without dialing. Missing integrations report unavailable; they never
invent contacts or commitments. Contact labels never modify permissions.

### OFFICE-03

**Receptionist meetings and escalation.** Use the existing caller-aware
registration contract for availability, meeting proposals, bookings, and changes.
Restricted callers can propose meetings without private-calendar access and can
mutate only resources allowed by their configured capability. Route message
urgency as a caller claim; use configured owner escalation preferences and native
delivery, without caller-selected arbitrary recipients. External writes require
idempotency and authoritative receipts, with partial/uncertain failures exposed.
A note or reminder is not evidence of a calendar booking. Missing calendar or
escalation integration produces a clear unavailable outcome and preserves any
independently stored message.

## Verification and deployment requirements

### VERIFY-01

**Automated system acceptance.** Cover the multi-call delivery/RAM scenario:
distinct callers report details, a later call recalls the correct dated facts,
the owner reviews messages and outstanding work, corrects a fact, and requests
a scheduled follow-up using fresh permitted context. Assert persisted records,
external fake-service receipts, and isolation, not merely prompt text.
Include restart, duplicate requests, concurrent note edits, route changes,
admin-to-guest transitions, delayed results, denied direct/delegated calls,
retention expiry, abrupt hangup, partial failures, and no action replay.

Run targeted regression tests and then the full suite. Run shell syntax checks
for installer changes; build, artifact checks, and portable-package smoke tests
for public interface/package changes. Run the existing isolated Hermes
compatibility script with a supported disposable installation. If unavailable,
record the blocker rather than substituting fake tests for that integration gate.
Update usage/configuration, data handling, compatibility, and upgrade docs for
new behavior. Test migrations preserve existing notes, call IDs, and task links.
Public installation docs must not imply the live matrix was completed by CI.

### DEPLOY-01

**Coordinated deployment acceptance.** External step: once explicitly authorized,
settle active calls/tasks, back up linked stores, update daemon and plugin together,
restart affected services, validate routes/readiness, and verify running identities.
Reassess the historical late-correction deployment note against the actual runtime.
Record installed revisions and rollback references. Do not repair old task records
without a specific authorized data-repair request. Use existing maintenance and
release procedures rather than inventing an automatic updater.

### LIVE-01

**Owner workflows.** On explicitly authorized test calls, verify owner lookup by
phone/chat, guest denial, profile scope, note cleanup versus session compaction,
current-request fidelity, accurate runtime answers, and detailed post-call delivery.
Compare spoken claims with stored results and delivery receipts.

### LIVE-02

**Memory and isolation.** Verify saved-fact/later-call recall for known, guest,
outgoing explicit, and restricted fallback routes. Include natural greetings,
delivery/RAM details, missing/stale notes, owner edits during a call, retained
dialogue, remember-disabled/withheld callers, expired bindings, identity changes,
and malicious note instructions. Test abrupt-hangup closeout quality, dates,
uncertain recognition, conflicts, duplicate prevention, and save-failure reporting.

### LIVE-03

**Scheduling and delivery.** Verify schedule, correction, and hangup against the
effective callback and linked reminder. The old schedule must not also fire.
Exercise gateway/daemon restart, busy/unanswered/disconnected phones, missed
windows, partial failures, actual spoken delivery, Telegram receipts, and requests
to wait for confirmation. Scheduling or voice setup alone does not pass delivery.

### LIVE-04

**Office scenario.** Repeat VERIFY-01 with configured contact/calendar/task
integrations and explicitly authorized recipients. Verify actual call objectives,
calendar/task outcomes, owner overview, follow-up, calling windows, and caller
isolation. Publish only sanitized hardware/software compatibility evidence.

### INVEST-01

**Startup speech and latency investigation.** Compare captured speech, provider
submission, transcription, response, and first playback on authorized calls.
Separate cold-start, turn detection, provider, and playback delays. The historical
10–11-second observation is context, not a target or current measurement. Record
repeatable baseline conditions before proposing tuning; validate any change
against interruption, startup speech loss, and audio quality. If no issue is
reproducible, report that result with conditions rather than claiming a fix.

### INVEST-02

**Intermittent static investigation.** Correlate call-specific transport/provider
evidence and observed symptoms. Distinguish a call with no assistant output from
static during output; do not reuse another call's counters. Low battery and service
restarts remain hypotheses until tested. Record hardware/software versions,
reproduction conditions, missing evidence, and mitigation verification. No raw
audio collection is authorized by this spec. A non-reproduced issue remains an
explicit limitation, not proof of a resolved cause.

## Goal prompt for another session

Copy this prompt when the two handoff files are present in the checkout:

> Implement the remaining system described in `docs/implementation-spec.md`,
> using `docs/implementation-backlog.yaml` as the progress record. Inspect the
> current checkout first and preserve existing working behavior. Complete all
> implementation and automated verification items in dependency order, add
> meaningful regression coverage, and update acceptance evidence as work proceeds.
> Continue independent work when an item is blocked. Do not mark items complete
> without their required verification or reduce scope to make checks pass.
> Finish code, documentation, migrations where needed, and automated checks;
> report deployment, external configuration, investigations, and live acceptance
> separately. This goal does not authorize live calls, real messages, restarts of
> deployed services, publishing releases, or repairs to private historical data.

## Handoff document checks

Before using or materially changing this handoff, parse the YAML with a safe
loader; check unique item/acceptance IDs, valid statuses, existing acyclic
dependencies, valid spec/source references, and at least one observable
acceptance check per item. Every unfinished baseline TODO item must map to a
requirement. No implementation or live item is initially complete merely because
this specification was written. Keep the spec authoritative for intended behavior
and the YAML authoritative for progress; reconcile contradictions before execution.
