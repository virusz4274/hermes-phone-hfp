# Configuration and operations

For a first installation, follow [Installation](install.md). This reference covers additional profiles and ongoing operation.

## Services and configuration

Run one `hfp-mcp` Bluetooth owner. Its in-process controller selects the caller's route, establishes an authenticated Hermes session binding, then starts Gemini Live or classic voice. The selected Hermes gateway owns agent execution.

`phone.version: 1` in the daemon YAML enables routing. Without a `phone` section the daemon serves low-level MCP clients without an automatic phone assistant. Routes are loaded at service startup; active calls never change profile or permission policy in place.

The daemon and phone plugin must read equivalent phone routing configuration. The Hermes installer selects the existing routing path or the default `~/.config/hfp-mcp/config.yaml` and writes `HFP_MCP_CONFIG` in both local environment files. Use its `--phone-config` option for a custom path. On separate hosts, keep the routing content equivalent and use the appropriate local path on each host. Keep secrets outside YAML.

Each endpoint names an existing Hermes profile, API base URL, API-token environment variable, and a **different** phone-binding-token environment variable. Use random secrets of at least 32 characters. The bridge token is for daemon-to-plugin authorization and must not be exposed as an agent tool argument.

For a default-profile endpoint:

```dotenv
# Daemon environment (~/.config/hfp-mcp.env)
HFP_HERMES_API_KEY=<at-least-32-random-characters>
HFP_HERMES_BRIDGE_KEY=<different-at-least-32-random-characters>
HFP_GEMINI_API_KEY=<your-gemini-key-if-used>
# HFP_MCP_CONFIG is populated by the installer.
```

```dotenv
# Selected Hermes profile environment
API_SERVER_ENABLED=true
API_SERVER_KEY=<same-value-as-HFP_HERMES_API_KEY>
HFP_HERMES_BRIDGE_KEY=<same-binding-secret-as-the-daemon>
# HFP_MCP_CONFIG is populated by the installer.
```

Protect these files with mode 0600. An explicitly configured Gemini route enables the provider directly. Model/voice provider environment options remain supported.

Install the new plugin into each participating existing Hermes home with `setup/install_hermes.py`. The `--python` argument selects Hermes's interpreter, not the HFP virtualenv. The selected interpreter must have pip available.

Hermes's `api_server` toolset selection governs API sessions. For personal admin parity, configure that platform with the same toolsets you use in owner chat; the installer preserves existing model/tool configuration rather than silently widening it. Normal Hermes approval rules remain in effect.

The client verifies native run submission, events, status, stop and approval support plus phone-plugin version/profile identity before answering. Missing capabilities, credentials or profiles end the call without substituting another profile.

The main `phone` settings are:

| Setting | Default | Meaning |
| --- | --- | --- |
| `version` | Required | Set to `1` to enable the controller |
| `default_region` | `IN` unless set in the service/Bluetooth configuration | Country used to normalize local phone numbers |
| `auto_answer` | `true` | Answer incoming calls that have a valid route |
| `auto_reconnect` | `false` | Retry the configured handset connection while disconnected |
| `blocked` | `[]` | Numbers to decline even when a guest default is configured |
| `default` | None | Optional restricted route for unmatched or withheld numbers |
| Policy `admin` | `false` | Permit the selected profile's tools under its normal approval rules |
| Policy `tools` | `[]` | Exact caller-aware tool names permitted for a restricted caller |
| Policy `remember` | `true` | Allow persistent caller notes for presented numbers |
| Policy `max_minutes` | `10` | Maximum call duration, from 1 to 240 minutes |
| Route `voice` | `gemini_live` | Voice backend: `gemini_live` or `classic` |
| Route `fallback` | None | Optional `classic` fallback if Gemini fails during startup |

To validate a specific YAML file, pass `--config setup/phone.example.yaml` to `route validate` or `route explain`. Otherwise the commands select the YAML path from service configuration.

## Optional guest and known-caller profiles

Create or select a profile only when you need it. A single personal profile plus an explicit owner-number route is supported. Unmatched callers are declined unless a `phone.default` route is configured.

The Hermes profile named `default` and routing's `phone.default` are different:
the first is your personal assistant; the second is the catch-all for unmatched
phone numbers. Keep your owner-number entries and personal endpoint. Point the
catch-all at a separate restricted profile.

Create a fresh guest profile without copying owner memory, skills, channels, or
credentials:

```bash
hermes profile create guest --no-skills
```

Configure its model/provider credentials as needed. Keep SOUL and static context
suitable for every guest. Merge the following into
`~/.hermes/profiles/guest/config.yaml`:

```yaml
memory:
  memory_enabled: false
  user_profile_enabled: false
  provider: none
platform_toolsets:
  api_server: [hfp_caller]
mcp_servers: {}
```

The bridge rejects restricted calls into a profile with shared memory enabled, an unscoped external memory provider, broad API toolsets or raw MCP connections. Profiles isolate Hermes configuration/state; they are not OS sandboxes. Tools that execute arbitrary programs still have their OS account's privileges and must not be registered as restricted caller capabilities.

Install the same plugin into the guest profile, from this repository:

```bash
python3 setup/install_hermes.py \
  --home ~/.hermes/profiles/guest \
  --python ~/.hermes/hermes-agent/venv/bin/python
```

For a separate gateway, add these to `~/.hermes/profiles/guest/.env`. Generate two
different random secrets of at least 32 characters; the bracketed values below
are placeholders, not literal credentials. Do not copy your entire personal `.env`.

```dotenv
API_SERVER_ENABLED=true
API_SERVER_PORT=8643
API_SERVER_KEY=<guest-api-secret>
HFP_GUEST_BRIDGE_KEY=<guest-binding-secret>
HFP_MCP_CONFIG=/absolute/path/to/.config/hfp-mcp/config.yaml
```

Use the same shared routing path as the daemon. In `~/.config/hfp-mcp.env`, add:

```dotenv
HFP_GUEST_API_KEY=<same-guest-api-secret>
HFP_GUEST_BRIDGE_KEY=<same-guest-binding-secret>
```

Keep both environment files at mode 0600. Merge the endpoint, policy and catch-all
into the existing `phone` section of `~/.config/hfp-mcp/config.yaml`:

```yaml
# Inside phone:
endpoints:
  guest:
    profile: guest
    url: http://127.0.0.1:8643
    token_env: HFP_GUEST_API_KEY
    bridge_token_env: HFP_GUEST_BRIDGE_KEY
policies:
  guest:
    admin: false
    tools: [hfp_caller_read, hfp_caller_update]
    remember: true
    max_minutes: 10
# Optional catch-all, only after the endpoint/profile is ready:
default:
  endpoint: guest
  policy: guest
  voice: gemini_live
```

These are merge fragments, not complete configurations; preserve your other policies and endpoints. No automatic contact-book import grants access. Duplicate normalized routes, blocked-and-routed numbers, unknown policy references and admin catch-all routes are errors.

Validate with `hfp-mcp route validate`. Start the separate gateway with
`hermes -p guest gateway` (or install/start its profile-specific gateway service
for persistence). Restart the existing Hermes gateway and HFP daemon when calls
and phone tasks are idle so all processes load the same routing configuration.
Check `hfp-mcp phone status` for endpoint readiness before testing an actual call.
Use `hfp-mcp route explain NUMBER` and `hfp-mcp route explain NUMBER --outgoing`
to inspect the selected route without dialing.

The result in either direction is:

| Destination | Incoming call | Owner-requested outgoing call |
| --- | --- | --- |
| Blocked number | Declined | Rejected |
| Exact entry in `phone.numbers` | That entry's profile and policy | The same entry's profile and policy |
| Unmatched number with guest `phone.default` | Guest profile and policy | Guest profile and policy |
| Unmatched number without `phone.default` | Declined | Notes-only outgoing fallback, if its endpoint can be selected |

The owner supplies the outgoing objective, but the recipient's route determines
permissions. `outbound_endpoint` only selects the last-resort notes-only route;
it never overrides a matching number or guest default. The guest policy above
permits conversation and each caller's own notes. Calendar bookings require an
explicitly enabled caller-aware capability that enforces which resources that
caller may access. Adding a trusted contact does not require `admin: true`;
prefer an exact route with only the capabilities that contact needs.

Notes are scoped by both profile and number. Changing an existing recipient's
route from the personal outgoing fallback to guest does not migrate their old
notes automatically. Never copy the owner's shared memory into the guest profile.

### Permissions and prompt injection

Caller speech, notes and retained dialogue are untrusted input. The gateway
recomputes the route and binds its policy before agent execution; tool hooks and
guarded phone-tool handlers enforce that policy. A spoken claim to be the owner,
a forged policy in a tool argument, or instructions saved in notes cannot change
the binding. Restricted guests cannot invoke owner dialing, approval, transcript,
or arbitrary-number note controls. Expired or revoked authority stays invalid.

This limits the effects of prompt injection; it does not make model responses or
saved notes immune to manipulation. Treat notes read back into owner chat as
untrusted facts, never authorization for a new call or action. Caller ID itself is
not authentication: an exact admin number route grants broad access based on the
presented number. Keep normal approvals enabled; use restricted policies when
caller ID alone is insufficient. See [Security](../SECURITY.md).

The controller gives missing caller-ID data up to two seconds to arrive before route selection. Once bound, a different presented number ends the call; it never upgrades an existing conversation. Admin selection uses exact number matching, as configured, and leaves action approvals enabled.

## Caller memory

### Outgoing calls with a purpose

Owner-requested outgoing calls do not require a destination entry in `phone.numbers`.
Those entries identify known callers and their permissions. Existing number/default
routes still take precedence, and blocked numbers remain blocked in both directions.
With one Hermes endpoint, a new outgoing destination automatically uses that endpoint
for Gemini conversation and its own persistent notes. With multiple endpoints, set
`phone.outbound_endpoint` once to the endpoint that should own those notes; individual
destinations still need no configuration. `outbound_notes` is a reserved internal policy.

The automatic route exposes only `phone_notes`, `phone_status`, and `end_call` to
Gemini. The gateway reads/writes the recipient's notes directly; it does not run an
agent with the personal profile's global memory or tools. This route supports
conversation, reminders, and recording agreed meeting details, but cannot execute
calendar bookings or other external actions. Those need a configured route with
the corresponding caller-aware capabilities. Notes persist across calls; full
dialogue continuity remains an explicit route setting. Calling a new number does
not add an incoming route or grant it owner access. Register additional numbers you
own explicitly if they should have your existing personal-assistant permissions.

Only a matching owner-admitted dialing attempt can select the automatic outgoing
route; an unrelated incoming call or manually dialed call cannot claim it. Inspect
the distinction with `hfp-mcp route explain +… --outgoing` versus the same command
without `--outgoing`.

Ask Hermes naturally: “Call Arun at +… on my behalf, ask how his day was, and
arrange a meeting tomorrow afternoon.” Hermes should call `hfp_phone_start_call`
with the number and a `purpose` containing all requested objectives and relevant
details from your chat. Gemini receives that brief, the recipient's saved notes,
and previous phone dialogue when continuity is enabled. It cannot see your full
owner chat. It waits for the recipient to speak, introduces itself, and follows
the brief. Names, relationships, and previous conversations are used only when
supplied or remembered, never invented.

The optional `purpose` is text of at most 4,000 characters. The HTTP equivalent is
`POST /v1/phone/calls` with `number`, `request_id`, and `purpose`; MCP clients use
`start_phone_call(number, request_id, purpose="")`. Number-only calls remain valid.
The brief belongs to the exact outgoing call and survives voice reconnects and
conversation refreshes. It is not automatically saved as a lasting caller fact.
Reusing a completed request ID returns its original result; changed arguments
are rejected. A successful start means the voice backend is ready, not that the
conversation or a booking has finished. Never automatically redial after an
uncertain or failed attempt.

Gemini delegates real actions such as calendar lookups and meeting bookings to
Hermes's configured tools, retaining the route's permissions and normal approvals.
It reports completion only after the tool confirms it. Useful durable facts and
preferences are saved through Hermes during the conversation when memory is enabled.
Existing facts should be preserved when updating notes; a failed save must not be
reported as remembered.

For “call me to remind me about my meeting,” use the configured owner destination
and include the meeting title, time/timezone, and relevant details in the brief.
For a future reminder, use Hermes's existing scheduler: its job must retain the
destination and a self-contained brief, then invoke the same outgoing tool when
due. Scheduling and calendar integrations remain Hermes capabilities; this plugin
does not add a scheduler or automatically monitor calendars. The destination must
differ from the paired handset's own SIM number, which remains blocked from
self-calling. Cached incoming greetings are separate from this workflow.

### Notes from owner chat

Hermes should read the relevant number's saved notes first when asked about a
person or what was discussed on a call. If those notes answer the question, no
transcript lookup is needed. Read retained dialogue for missing, stale or
conflicting details, a specific call not adequately covered by notes, or a request
for exact wording/full dialogue. Notes can accumulate facts across calls; they
are not necessarily a complete summary of the latest call. This order is model
tool guidance, not a server-enforced restriction on transcript access.

Reading notes does not save anything. Report existing details as already saved;
claim a new save only after an update succeeds.

`hfp_phone_caller_read(number)` reads a recipient's saved facts without a call.
`hfp_phone_caller_update(number, notes)` replaces them; read first and merge useful
existing facts before updating. Both belong to the `hfp_phone` toolset. The daemon
normalizes the number and selects its configured route and Hermes profile. No
profile override is accepted. Results include `number`, `profile`, `persistent`,
and `notes`. These tools are unavailable to phone-bound sessions, including admin
phone sessions; those use the caller-scoped tools below.

Authenticated owner HTTP clients can use `POST /v1/phone/caller-notes/read` with
`number`, or `/v1/phone/caller-notes/update` with `number` and `notes`. Notes remain
bounded to 8,000 characters. New destinations use the outgoing endpoint; blocked
numbers and ambiguous endpoint selection are rejected. With `remember: false`,
reads return empty notes and updates are rejected. These operations use the same
gateway database and identity key as active calls, including remote profiles.
Owner updates appear when the next call binds; in-call updates can be read from
owner chat afterward. Gemini can ask Hermes to retrieve newer notes during a call.

Every call gets an unpredictable `hfp-…` context binding. Each delegated request gets separate execution authority, so cancellation cannot reuse the next request's lease. A stable HMAC-derived caller ID identifies notes independently of the physical call. Notes live in `<Hermes home>/hfp-phone/callers.sqlite3`; its adjacent key file must be backed up with the database to preserve identity mappings. With continuity disabled, requests retain the earlier separate-session behavior and six-exchange in-call history.

### Conversation continuity

Set `continuity: true` on each number route that should keep its phone conversation
(the default for omitted settings is `false`). This explicitly enables text storage
for that route even if general full-transcript reporting is disabled. Gemini input
and output transcription must both remain enabled. No historical test calls are
imported when enabling the feature.

The daemon stores finalized spoken dialogue once in its existing `calls.db`, linked
to a conversation and its physical calls. The same caller and Hermes profile resume
that conversation across reconnects and restarts. Active dialogue is retained until
reset; archived dialogue is retained for 30 days. Withheld callers and routes with
`remember: false` do not resume or recall another call's conversation.

Gemini starts with up to 40 recent messages, bounded to 24,000 text characters.
`phone_recall` retrieves older retained exchanges, with bounded response pages.
Very long messages expose an offset for reading the rest, including multilingual
text. Hermes receives server-selected
phone context with each task and can use `hfp_caller_recall` to retrieve more; add
that exact capability to a restricted policy if it needs it. Retrieved text is
caller-scoped data, never a grant of permissions. Generated audio is not proof that
the caller heard every word. There is no extra summarizer or dialogue mirror in
Hermes: Hermes stores its native task history and performs its own compaction.

Gemini voice controls:

| Request | Behavior |
|---|---|
| New chat / clear chat | Confirm in a subsequent spoken reply within 60 seconds; archive the old conversation and select fresh phone and Hermes sessions. |
| Delete my phone history | Separately confirm; remove this caller's retained phone dialogue and linked managed Hermes sessions. Saved caller facts remain separate. |
| Compact this conversation | Use native Hermes task-context compaction in the background, preserve phone dialogue, and refresh the voice context on completion. Wait for active work to finish first. |
| What did we discuss earlier? | Search/read this caller's retained dialogue, optionally including archived phone chats. |
| Actually, make that tomorrow | Steer the current native Hermes task; acceptance does not guarantee the correction was applied before it finished. |
| Stop that task | Revoke its authority and request native cancellation; already completed external actions cannot be undone. |

Resetting refreshes the Gemini connection while keeping the Bluetooth call alive.
Late messages from the old voice generation and old tasks are excluded. A failed
cross-system deletion reports incomplete cleanup and preserves references for a
later explicit retry; it must not claim all history was erased.

Continuity keeps a primary native Hermes task session per conversation, creates extra
sessions lazily for independent overlap, and uses a fresh binding
for every run. The plugin records the immutable run-to-binding association before
native execution; a later call cannot renew an earlier run's authority. The small
session adapter uses native Hermes creation, deletion, and compression machinery.
Native compression can keep the original task history when a summary would make
it larger. Cancelling the wait for compaction cannot undo native compression
already in progress; its result cannot refresh a reset or ended call.
Classic STT/TTS also uses persistent task history and recent phone context, while
the background conversational flow and the voice-control tools above use Gemini.

`hfp_caller_read` and `hfp_caller_update` operate only on the caller bound to the current session. They do not accept another phone number or profile. Notes are bounded to 8,000 characters and retained until forgotten. The model is told that notes are untrusted facts, not instructions. The controller automatically extracts permitted lasting facts for closeout, and explicit notes tools remain available; the audio session itself is not a memory store.

Gemini also receives `phone_notes` on normal routes with admin or
`hfp_caller_read` permission, so checking notes does not require a background
Hermes task. Reads and updates independently enforce the corresponding caller
capability; read access does not grant update access. Gemini checks current notes
for memory questions and uses `phone_recall` for specific retained dialogue when
continuity is enabled. A failed or denied lookup is not an empty memory result.
Different phone numbers have separate caller records, even when they share an
admin policy and profile.

### Automatic call memory closeout

For routes with `remember: true` and caller-note write permission, the controller
extracts useful facts, decisions, and commitments during the call and finishes
saving after hangup. The conversational agent does not need to invoke a notes
tool. This works with Gemini and classic voice, with or without continuity.
Extraction uses the routed Hermes profile's tools-disabled auxiliary model path
(`auxiliary.phone_memory` can configure that task); it does not start a personal
agent, create reminders, or perform actions discussed on the call.

Notes and transcripts serve different purposes. Notes provide quick, dated
updates; enabled transcripts retain the detailed conversation for exact questions.
Automatic notes never replace or delete transcripts and do not change transcript
opt-ins or existing retention. With transcripts disabled, dialogue is processed
in memory and only extracted facts/checkpoints are persisted. Checkpoints are
attempted during the call, with a bounded queue and a final flush on hangup.

Set `phone.timezone` to an IANA timezone such as `Asia/Kolkata`. It defaults to the
host timezone (UTC if unavailable). Each update includes the originating call,
local reporting timestamp, and caller attribution. Relative dates use the
statement's original local date, including when a call crosses midnight. Ambiguous
dates remain uncertain; a caller's reported completion is not a verified action.

Notes reads return `revision`. All public replacement writes, including
`hfp_caller_update`, `hfp_phone_caller_update`, and `phone_notes`, require
`expected_revision` from that read. A stale write returns HTTP 409 (or
`notes_conflict` in the caller tool): read again and merge. Existing notes are
preserved while dated additions and corrections are appended. An exact fact
already saved during the call receives its missing date/source annotation without
adding another copy of that fact. The 8,000-character
limit still applies; capacity failure leaves the old notes intact.

The controller status and persisted call summary include `memory_save` with
`pending`, `saved`, `skipped`, or `failed`, plus a reason and capture completeness.
`already_saved` means no additional write was necessary. `incomplete_capture`
means available updates may have been saved, but some dialogue was unavailable;
check `saved_updates`. Transcript-storage errors remain separate. No proactive
owner notification is sent by this feature.

Ordinary caller bindings are revoked at hangup. A private, call-scoped closeout
credential permits only extracting and merging that call's data, with a maximum
30-second final transcription flush and retries for up to 24 hours after hangup.
Restart recovery uses durable extracted checkpoints and still-permitted retained
transcripts, never an expired caller binding. A crash can lose untranscribed audio
or transient text; incomplete recovery is reported. Forgetting notes invalidates
pending closeouts, and route/permission changes are checked again before writes.
Internal gateway receipts are removed after their recovery window plus one day;
daemon save reports follow diagnostic retention. Saved caller notes remain until
forgotten. Unsupported gateways report `closeout_unavailable`; ordinary calls
continue. Upgrade both daemon and gateway plugin for this feature.

Withheld callers receive separate ephemeral identities and cannot save persistent notes. `remember: false` also disables persistence. Receptionist profiles must not copy caller details into global MEMORY.md, USER.md, or a shared external memory provider.

```bash
hfp-mcp caller inspect --profile default --number +919876543210
hfp-mcp caller forget --profile default --number +919876543210
# Use --store /absolute/path/callers.sqlite3 for a remote/copied or custom-home store.
```

Forgetting notes does not delete Hermes session history or downstream calendar records. Manage those through their owning system. Raw audio recording is off; daemon diagnostic retention remains 30 days. Hermes session retention is configured independently in Hermes.

Bindings expire after ten seconds unless renewed every two seconds. A lost renewal ends the call; expiry prevents later caller-tool access. Hangup revokes the binding before stopping Hermes work. Hermes stop is cooperative and cannot undo an external operation already committed. External integration handlers must call `binding["check_active"]()` immediately before a write and provide their own operation-level idempotency.

## Full call transcripts and timing

Set `HFP_FULL_TRANSCRIPTS=true` in the daemon's private environment file and
restart `hfp-mcp.service` while no call is active. For Gemini, leave both
`HFP_GEMINI_INPUT_TRANSCRIPTION` and `HFP_GEMINI_OUTPUT_TRANSCRIPTION` enabled
(their default). Classic voice saves Hermes STT input and generated TTS text.
This records text, not raw audio. Recognition can contain errors; an output
transcript describes generated speech, which may have been interrupted before
the caller heard all of it.

Opted-in transcript events are saved as turns arrive in the private
`~/.local/state/hfp-mcp/calls.db`, or the configured daemon database. They survive
daemon restarts and the bounded in-memory display history. They expire under
`HFP_RETENTION_DAYS` (30 by default); disabling transcripts stops capture and
blocks API access but does not immediately delete previously saved records.
Enabling now cannot recover text redacted during earlier calls.

From the repository root:

```bash
.venv/bin/hfp-mcp transcript list
.venv/bin/hfp-mcp transcript show                         # latest retained call
.venv/bin/hfp-mcp transcript show --call-id <call-id>
.venv/bin/hfp-mcp transcript show --call-id <call-id> --output ~/call-transcript.json
```

Exports contain all pages and use private mode 0600; existing output files are
not overwritten. Owner Hermes chats can use `hfp_phone_transcripts` to list/read
calls. Authenticated MCP clients have `list_call_transcripts` and
`get_call_transcript(session_id, after_id, limit)`; follow `next_after_id` while
`has_more` is true. The corresponding authenticated HTTP endpoints are
`/v1/phone/transcripts/calls` and `/v1/phone/transcripts?call_id=…&after_id=…`.
Transcript tools are owner controls, not restricted caller capabilities.

Gemini's `phone_status` tool checks the current authenticated Hermes endpoint
without launching a model run. Action requests still use Hermes and preserve
its normal approvals. Status and hangup remain available during a pending task.
A cancelled request releases its admission slot, but the controller waits for
its authority cleanup before executing the next task. An additional task while
one is still pending receives a busy response; it is not queued or executed.
A status-check timeout does not establish a permission failure or task outcome.
The voice prompt requires the actual function call in the same response, with an
optional brief acknowledgement; it cannot guarantee speech while a synchronous
tool is running.
`phone_timing` audit records capture setup and Hermes request timings, including
cancellation outcomes. `voice_metrics` records and `get_live_ai_status` expose
first provider/input/output audio timings. Caller bindings retain their ten-second
expiry. Heartbeats tolerate temporary gateway delays only within the last confirmed
lease (with a half-second safety margin); rejection or failure to renew before
that deadline stops the call. A single three-second delay no longer triggers a hangup.
Sending audio to HFP is not an exact
measurement of when the handset plays it. No tool prompts or transcript text
are included in timing records.

The voice prompt distinguishes fictional role-play from real actions and requires
actual function calls, rather than speaking function names or arguments. The
`ask_hermes` handoff forwards both the task and its optional conversation context
(up to 8,000 characters) as caller data, preserving language and constraints.
Hermes does not automatically hear the whole call. With continuity enabled, the
plugin restores bounded recent dialogue and supplies context for tasks. Neither context nor
a spoken claim changes the route's permissions or supplies an approval.

The call's initial context describes the access configured by its route. Admin
calls can request the selected Hermes profile's available host-system tools under
its existing approval rules; restricted callers retain their explicit capability
limits. RAM/CPU, Docker, ping, and package requests refer to the Hermes host unless
the caller specifies another system. Testing the assistant does not imply fiction.
Only caller-established pretend scenarios should be described as fictional in a
handoff, including when the conversation is in Malayalam or another language.

Without continuity, Gemini cancellation of a pending function revokes that request's
binding and stops its Hermes run. With continuity, a task is owned independently
of the initial Gemini function response: submission returns promptly, reporting
whether admission is still pending or confirmed, and completion is delivered as a
separate task update. Ordinary interruptions stop speech without cancelling the
task. Hangup stops call-bound work; authorized continued work retains its separate
bounded grant. Explicit cancellation, confirmed reset, or expiry stops selected work.
`phone.max_phone_tasks` defaults to two simultaneous tasks, including continued work across gateways on this host (atomic slots in the existing daemon ledger),
with native status, steering, and stop controls and no custom task queue. Neither mode silently replays actions or treats cancellation
as successful completion. The prompt changes
reduce unnecessary handoffs and misleading claims, but cannot guarantee model
compliance or eliminate recognition errors and provider interruptions.

## Natural task handling

`ask_hermes` infers `relationship`, an optional original `task_id`, and
`continue_after_call` from the conversation. Ordinary and long requests use the
primary session when free. A clearly independent overlapping request may use a
second session; follow-ups and corrections stay with their original session.
Ambiguous/conflicting requests need clarification. Status questions never submit
work. At capacity, offer cancellation/replacement; nothing is queued. Replacement
first stops the selected run, then checks it has settled before new submission.

To enable continuation for a known admin caller, set its policy's
`background_tasks: true`. Configure that Hermes profile's existing Telegram home
channel first. `phone.background_task_minutes` defaults to 30 minutes, measured
from designation to continue, and is not extended by later calls or status checks.
Normal Hermes approvals remain in force. Substantial work can be designated from
ordinary wording; no special background phrase or confirmation ritual is needed.
The tool response must confirm the designation before voice promises Telegram.
Use `hermes_task continue` to designate an already running task.

The existing gateway observes native Runs and persists task IDs, session IDs,
status, results and per-run grants in its existing caller database. After restart
it looks up recorded native run IDs; interrupted or uncertain work is reported,
never resubmitted. Completed outcomes remain queryable on callbacks, even when
their spoken announcement was interrupted. Voice announcements wait for a pause.
Owner `hfp_phone_status` and exact-request `hfp_phone_approval` also cover continued
work while no call is connected.

Telegram uses Hermes's existing messaging helper for a concise labeled completion
notice. Requested attachments use native `hermes send --to telegram` with
`MEDIA:/absolute/path`; `[[as_document]]` is available when a document is requested.
Keep the requested file count and format. A timeout means delivery is uncertain:
no test message, custom code/transport fallback, or automatic duplicate follows.
A notice attempt is recorded before sending, so a gateway crash cannot trigger
an automatic resend. Callback status shows whether delivery was confirmed.

New/clear confirmation explicitly includes cancellation of unfinished work,
including continued tasks. Delete removes all linked managed native sessions,
including additional task sessions. Reset receipts in refreshed voice context
prevent a question about the new chat from being treated as another reset.
Saved caller facts remain separate. Explicit physical-call hangup uses the
idempotent phone operation. A conservative finalized-transcript fallback recognizes
literal commands and excludes quoted, negated, hypothetical and fictional speech;
Gemini remains responsible for interpreting other natural wording.

## Calls and approvals

In owner chat, request a call to an explicit number; the plugin provides `hfp_phone_start_call(number)`. It reports success after the interactive voice path is ready. For outbound calls, the **destination number** selects the route and permissions. Add an explicit destination route, or a restricted default, before dialing. Never map every destination to your admin profile.

Inbound calls use the presented caller number. `auto_answer: false` leaves permitted incoming calls unanswered until answered through phone control. Once active, the controller attaches the selected voice backend. It still declines calls without a permitted route.

Check pending actions and resolve one exact approval request from the repository root:

```bash
.venv/bin/hfp-mcp phone status
.venv/bin/hfp-mcp phone approve --request-id <pending-request-id>
# Or deny that request:
.venv/bin/hfp-mcp phone deny --request-id <pending-request-id>
```

Use the request ID shown in status, not the call ID or run ID. The owner-chat `hfp_phone_approval` tool provides the same operation. Gemini does not grant approval. A rejected, cancelled or uncertain action must not be described as completed.

For commands shown as `hfp-mcp` elsewhere in this reference, activate the repository virtualenv (`source .venv/bin/activate`) or use `.venv/bin/hfp-mcp` explicitly.

## Integrations

Future booking, CRM or message-intake integrations can register restricted capabilities through:

```python
from hfp_mcp.hermes_bridge import register_caller_capability

register_caller_capability(
    ctx,
    name="appointments_for_caller",
    schema={
        "description": "List appointments owned by the current caller.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    handler=lambda binding, args: appointment_store.for_caller(
        profile=binding["profile"], caller_id=binding["caller_id"]
    ),
)
```

Register from a Hermes plugin loaded in the receiving profile. Add the exact tool name to the phone policy. The capability belongs to `hfp_caller`; newly installed tools are denied until explicitly added. Handlers must return a Hermes-compatible result (JSON string or dictionary), enforce ownership using the supplied binding and never trust a model-supplied resource owner. Namespaced suffix matching and wildcards are not supported.

No calendar backend or business workflow is bundled. Owner sessions can continue using their existing unrestricted Hermes integrations under normal approval rules.

## Multiple gateways

Separate profile gateway processes are the default. Give each endpoint its existing API URL and separate credentials. Only the HFP daemon owns Bluetooth.

For an already-enabled Hermes multiplexer, use `http://127.0.0.1:8642/p/<profile>` for named endpoints. Install the plugin in the default home and each receiving profile. The default plugin registers explicit mirrors for its custom binding/speech routes; Hermes supplies the native Runs routes. Named-profile requests use their own API and bridge credentials. Do not enable a second API listener for profiles served by the multiplexer. This installer never enables multiplexing.

Use HTTPS or an SSH tunnel between hosts. The router rejects plaintext non-loopback HTTP endpoints. Keep profile data and speech-provider credentials on the Hermes host. For owner controls from a different host/user, set `HFP_MCP_DAEMON_URL` to the authenticated daemon's `/mcp` URL and `HFP_MCP_BEARER_TOKEN` to its control token in a private HFP environment file on the Hermes host (the default is `~/.config/hfp-mcp.env`; override with `HFP_MCP_ENV_FILE`). Copy the phone routing YAML there and set `HFP_MCP_CONFIG` accordingly. The Gemini key is needed only on the daemon host; STT/TTS providers and `ffmpeg` are needed on the Hermes host.

## Reinstalling and removing the plugin

Rerun the plugin installer to install the current checkout. It stages the new files first, backs up the existing plugin and Hermes YAML under `<Hermes home>/hfp-phone-backups/<timestamp>/`, and installs one `hfp-phone` plugin. Obsolete awareness plugins and old discoverable phone-plugin backups are moved into that backup directory. Existing adapter-specific plugin settings are cleared; other plugins, model settings and caller notes are preserved.

Restart the selected Hermes gateway after installation. No gateway restart is performed by the plugin installer.

To remove phone assistance, stop the HFP daemon between calls, disable `hfp-phone` in the selected Hermes profile, and remove the `phone` section from the HFP YAML. Start the daemon again if you still need low-level MCP calling. Caller notes are retained under `<Hermes home>/hfp-phone/`; delete them separately only if you want to erase that data. The daemon's authenticated MCP tools remain independently usable.

For the complete update policy, backup locations and full manual uninstall procedure, see [Maintenance](maintenance.md).

## Hardware acceptance

Run these on the actual phone/controller before unattended use:

- Incoming owner call: correct personal profile, usable two-way audio, normal pending approval and exact-request resolution from owner chat/CLI.
- Known caller: correct restricted profile; notes survive a second call; another caller cannot read them.
- Unmatched/withheld caller: declined without a default, or routed to the configured guest profile.
- Speech interruption: playback clears; stopped/failed work is not reported as completed.
- Outbound interactive call: success only after voice readiness. One-shot announcement: no later AI attachment.
- Remote hangup, daemon restart, gateway outage and Gemini reconnect: audio lease released, caller authority revoked/expired, no repeated external action.
- Both voice backends: measure answer-to-audio readiness, end-of-speech-to-first-reply latency and intelligibility on the same sample calls. No latency improvement is claimed without those measurements.
- Continuity: share a detail in ordinary conversation, hang up, call back and ask
  about it. Repeat after a daemon restart. A different number/profile must not
  receive it; withheld callers must start fresh.
- During a Hermes task, keep chatting, give a correction and check its actual
  result. Try explicit cancellation and call-bound hangup; neither may produce a late
  success announcement in a later call or new chat.
- Say “new chat,” confirm in the next reply, and continue without hanging up.
  Old details should require explicit archived recall. Test explicit phone-history
  deletion on disposable dialogue and verify saved caller facts stay separate.
- Compact a task history and continue speaking while it runs. Check that retained
  phone dialogue remains accessible after the voice context refresh.

Relevant upstream references: [Hermes Runs API](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server), [profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles), [plugin APIs](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins), [voice](https://hermes-agent.nousresearch.com/docs/user-guide/features/voice-mode), [Gemini Live tools](https://ai.google.dev/gemini-api/docs/live-api/tools).
