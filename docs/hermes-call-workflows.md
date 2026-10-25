# Hermes phone request lifecycle

Hermes owns a phone objective until it has an evidenced outcome or needs owner
guidance. The plugin executes the call; it does not schedule future dialing or
assume a connected call completed the request.

`hfp_phone_schedule_call` creates one native Hermes one-shot `no_agent` job.
`hfp_phone_start_call` uses the same flow for purpose-bearing requests, scheduling
the first attempt immediately. Raw, purpose-free phone control remains manual.
Ordinary phone objectives do not create an additional call-time notification.
Confirmed busy and unanswered calls use a deterministic retry policy. Connected
reminders finish without a separate decision agent; hearing is never inferred
from connection or generated speech. Broader connected objectives retain a
restricted Hermes outcome evaluator. Native cron delivers only useful failure
updates through the saved gateway destination, with no routine success reports.

Phone reminders use `hfp_phone_schedule_reminder` and the configured
`phone.reminders.default_delivery`: `gateway`, `call`, or `both`. No platform name
is required; Hermes resolves its native default home destination for the originating
profile. `both` intentionally adds a linked notification at the first due time,
independent of the physical call. Its callback executor runs locally and a separate
native report job delivers the eventual outcome, so notification failures do not
block the call. `call` suppresses all external gateway notices for that reminder.
See [reminder configuration](phone-routing.md) for explicit delivery overrides.

Supply a self-contained purpose: the recipient, whose behalf the assistant is
calling on, the actual objective, and any constraints or deadline. Gemini does
not see the originating chat. An earlier `deadline` may be supplied as an exact
ISO-8601 timestamp with timezone. Scheduling acceptance confirms the native job;
it does not complete the phone objective. A background scheduling task can finish
while the phone request remains open.

New requests save the effective configuration:

```yaml
phone:
  reminders:
    default_delivery: both  # gateway | call | both
  callbacks:
    retry_delays_minutes: [10, 30, 60]
```

The initial attempt plus this list permits four attempts. Each delay starts at
the end of the preceding unsuccessful call: roughly 10:00 → 10:10 → 10:40 → 11:40.
An empty list disables automatic retries. Later configuration changes affect new
requests only; existing requests keep their saved policy. Code enforces counters,
cancellation and duplicate prevention. Native Hermes jobs remain the only clock.
No memory entry or additional skill is needed to execute the policy.
Automatic retry hours are 08:00 inclusive–22:00 exclusive in the requester's
configured timezone, with expiry seven days after the first scheduled attempt.
Explicit owner instructions and evidenced recipient callback times take
precedence over the automatic hours. Earlier deadlines remain earlier when a
schedule is moved. Relative delays remain elapsed seconds; local timezone labels
are display and calendar interpretation, not another clock adjustment.
The call brief retains the original request date and timezone, so retrying does
not silently move an objective described as “today” or “tomorrow.”
Automatic calling hours are checked again before dialing. A native catch-up that
arrives outside those hours requires guidance rather than placing a late call.

Each attempt records connection state separately from objective disposition.
Busy, no-answer, transport interruption and answer-timeout cancellation are
reported only when supported by actual call control. Retained transcript excerpts
and verified write receipts supply evidence; no audio recording is introduced.
A clear refusal closes the request. During an authorized outgoing call, the narrow
`request_callback` voice action accepts an explicit delay or timezone-aware time.
It derives the recipient, objective and request identity from trusted physical-call
state, including for restricted recipients; it grants no general Hermes tools.
“Later” requires clarification. Saving and verifying the native job precedes the
spoken confirmation. A changed time replaces the pending callback. Explicit
continuations keep the same chain's automatic retry budget without consuming it.
Cancellation revokes the entire chain; uncertain execution never retries
automatically. Exhaustion sends one short notice and schedules no further calls. An agreement to upload a video does not prove an upload.
Previous verified progress is included in subsequent call briefs.

The `hfp_phone_apply_followup_decision` tool is available only to the exact native
follow-up run admitted for that attempt. It cannot dial, change recipient, use
owner-note tools, send messages or execute recipient instructions. Decisions
require request/attempt IDs, expected revision, operation ID, disposition, reason
and evidence IDs when claiming completion, refusal or recipient deferral.
Exact-name discovery through Hermes's native `tool_search`, `tool_describe` and
`tool_call` bridge is allowed for that decision tool only. Retry decisions persist
and verify their new native schedule before reporting it.

Use `hfp_phone_status` for request status, attempts, callback ID, native
`cron_job_id`, revision, evidence and last decision. `reminder_job_id` is a
compatibility alias for that same job; it is not a separate schedule.
For both-mode reminders, `notification_job_id` identifies the separate initial
gateway notification. Phone status includes component states and delivery errors;
the phone cancellation/rescheduling tools manage both pending jobs together.
`hfp_phone_reschedule_callback` preserves the pending job and requires the exact
callback ID, current revision, stable operation ID and timezone-aware target.
`hfp_phone_cancel_callback` prevents all future attempts, including late agent
decisions. An already admitted attempt is in progress; cancellation prevents subsequent attempts and does not end its call. Hangup remains a separate action.

Native Hermes claims and a durable daemon execution receipt prevent repeat dialing
across duplicate requests and restarts. Ambiguous creation is recovered by the
unique executor script marker; unresolved schedule edits hold execution. Direct
native cron edits that do not match the authorized request cannot dial. Native
Hermes owns catch-up policy, not the former daemon five-minute timer.

Call observation is bounded by 30 minutes and the native script timeout, reserving
two minutes for the decision and 30 seconds for reporting. Script timeouts of
150 seconds or less cannot admit calls. Observation timeout never hangs up a
call or retries it. A missing/model-failed decision records owner guidance.
Reports use native Hermes delivery, including its delivery receipts and retry
policy. A report is durably claimed before stdout publication; an uncertain claim
is never replayed. If the original worker is known to have ended before claiming
its report, a native report-only job can deliver the retained result without
redialing or repeating a decision.

Normal message bodies contain the subject, local due time and delivery method:
“I'll remind you on 18 Oct at 5:20 PM IST to start recording a video, by message and
phone.” At due time: “Reminder: Start recording a video.” The normal Hermes cron
header, job ID and management footer remain. Raw receipts, exception names,
epochs and run IDs stay in diagnostics. Automatic retries do not send another
scheduling confirmation. Exhaustion reports “I couldn't reach you after 4
attempts. No more calls are scheduled.” Call-only requests send no gateway messages.

Outgoing sessions receive direction and current purpose explicitly. The cached
introduction plays once; after the first hello, Hermes immediately explains the
purpose. Earlier incoming conversations cannot replace it. Reconnecting the same
physical call continues the conversation without replaying the introduction.

# Upgrade and private data

Back up the daemon `calls.db`, every affected profile's `hfp-phone/` directory
and key, native `state.db`, cron state, scripts, configuration and installed
package together after active work settles. Install the same build into both the
HFP daemon and Hermes environments, then restart both services.

The additive `phone_workflow_execution` table retains daemon attempt receipts.
Each Hermes profile adds a private `hfp-phone/workflows.sqlite3` database with
original purposes, narrow caller authorization, deadlines, attempt evidence,
verified actions, native run/job identities, decisions and report claims.
Generated mode-0600 scripts under that profile's `scripts/` contain request
identifiers or the plaintext body of a gateway-only reminder. Scheduling never revives an expired general caller binding. Current owner
routing and recipient profile scope are checked at execution and result delivery.

Workflow requests are pruned on gateway startup after the configured retention
period (30 days by default) beyond their deadline, provided no attempt remains
admitted, observing, or deciding. Native cron and call/transcript stores retain their own
policies; private backups and generated scripts require normal operator cleanup.
Transcript excerpts exist only when full transcript retention was already enabled.
Forgetting caller notes does not delete workflow evidence, native jobs/sessions,
generated scripts, or backups; manage each through its owning lifecycle.
Do not include private stores, scripts, transcripts or credentials in releases.

Legacy terminal callbacks remain historical records. Legacy pending callbacks are
held and cannot fire through the retired daemon timer; inspect their linked native
reminder before explicitly migrating or cancelling it. Do not recreate an
uncertain historical call. No pending legacy calls were assumed safe to migrate.
Existing schedules and historical failures are preserved; upgrades never replay
failed reminders. Bluetooth configuration and general caller permissions remain
unchanged. Use Hermes's quiet maintenance drain after active work settles to
avoid an idle home-channel shutdown broadcast during coordinated installation.

# Manual rehearsal

Use consenting recipients and disposable requests. Verify a connected conversation
with evidenced completion; a missed call followed by one verified retry; a clear
recipient callback time; a refusal with no future attempt; cancellation; and
subsequent memory recall. Compare plain gateway messages and diagnostics with retained evidence
and the native schedule. Validate the spoken opening through a user-requested
controlled callback before declaring the conversational defect fixed. All live cases remain pending until actually performed.
