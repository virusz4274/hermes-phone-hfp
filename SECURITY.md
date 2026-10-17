# Security

This public preview receives fixes on the main branch. Upgrade to the latest
preview before reporting a problem. There is no supported older-release branch.

Use GitHub's **Security → Report a vulnerability** on this repository for private
reports when available. If private reporting is unavailable, open an issue asking
the maintainer for a private reporting channel, without exploit details or private
data. Do not post credentials, caller numbers, transcripts, or database files.

Include the affected revision, configuration with secrets removed, impact, and a
minimal reproduction using synthetic callers. No response-time guarantee is made.

## Trust and data

- The paired phone's Bluetooth-reported caller number is the trusted routing and
  caller-memory identity. There is no additional caller verification step. This
  does not prove who is speaking: spoofed or reassigned numbers reported as a
  known number are indistinguishable under this design. An admin route permits
  that profile's tools, subject to Hermes approvals. Keep normal approvals enabled.
- Restricted callers need dedicated profiles and integrations that enforce caller
  ownership. Profiles are not operating-system sandboxes.
- The control API uses bearer authentication and defaults to loopback. Use an SSH
  tunnel or authenticated TLS deployment for remote access.
- Gemini Live sends voice to Google. Classic voice sends speech to the selected
  STT/TTS providers, and Hermes sends task content to its configured model provider.
  Local transcript opt-out does not disable provider processing.
- Gemini startup briefly buffers up to five seconds of raw audio in memory while
  its first connection is being established. It is sent once to that connection;
  unsent startup audio is discarded on disconnect, reset or call end. It is not
  written to disk or retained as a recording.
- Full transcripts are off by default. Route-level continuity is a separate opt-in
  that stores dialogue; active dialogue lasts until reset and archived dialogue has
  its own retention. Caller notes remain until explicitly forgotten. See
  [data retention](docs/phone-routing.md#conversation-continuity).
- Local files are private to the service account, not encrypted against that
  account or a machine administrator. Backups may retain deleted live data.

Admin lookup defaults to the caller's configured endpoint. `phone.owner_access`
explicitly grants access to additional existing endpoints; grants are directional.
Both daemon and gateway recheck live admin bindings, including delegated runs,
before returning data. Full transcript retention still controls transcript lookup.
Trusted call-source metadata (call ID, routed number, caller hash, profile and
endpoint) follows diagnostic retention even when conversation continuity is off.
Legacy calls without reliable endpoint attribution are omitted from scoped lookup.
No additional lookup grant is inferred from guest configuration or saved notes.

Automatic note closeout uses a separate private credential created from an
admitted call's binding. It cannot run tools, select another caller/profile, or
renew caller authority. Ordinary caller bindings remain revoked after hangup.
Extracted checkpoints may be merged for a bounded recovery period; current
routing, note-write permission, and forget generations are checked before commit.
Notes and dialogue remain untrusted input to a tools-disabled extraction model.
Checkpoint credentials stay in the private daemon ledger and are never included
in voice context or public status. Forgetting notes removes pending gateway
checkpoints and their credentials, so recovery cannot restore forgotten facts.

## Phone workflow authority and records

Hermes owns native call schedules, bounded follow-up decisions, and delivery.
The plugin checks current routing and narrow request authority before executing
an attempt and before releasing its result. A scheduled request does not revive
an expired caller binding. The outcome-decision tool is limited to the exact
admitted native run; recipient speech and retained evidence do not authorize
unrelated tools, arbitrary recipients, or messages. Cancellation prevents future
attempts; active-call hangup remains separate.

Each profile's private `hfp-phone/workflows.sqlite3` stores purposes, deadlines,
authority, attempt evidence, decisions, and report claims. The daemon's `calls.db`
contains `phone_workflow_execution` receipts. Native Hermes stores cron execution
and decision-session records separately. Generated mode-0600 scripts in the
profile's `scripts/` directory contain workflow identifiers, not credentials or
transcripts. Protect all of these and their backups as private data.

Workflow pruning runs on gateway startup after the configured retention interval
(default 30 days) beyond the request deadline, provided no attempt is still
admitted, observing, or deciding. Daemon execution receipts follow diagnostic
retention from their start time. Native cron/session stores have their own
policies; generated scripts and backups need operator cleanup. Transcript
excerpts are collected only when full transcript retention is enabled. Forgetting
caller notes does not delete workflow evidence, native sessions/jobs, generated
scripts, or backups. Do not remove linked state while attempts or report workers
are active. See [workflow lifecycle](docs/hermes-call-workflows.md) and
[maintenance](docs/maintenance.md) for coordinated backup/removal.
