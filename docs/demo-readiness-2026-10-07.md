# Gemini phone demo readiness — 2026-10-07

Historical build/rehearsal evidence; retain its original observations and limits.
For current scheduling and deployment evidence, see
[native phone workflows](hermes-call-workflows.md) and
[the subsequent workflow verification](hermes-owner-flow-verification-2026-10-08.md).
Older scheduler/reminder statements below do not describe the current lifecycle.

Update on 8 October: incoming rehearsal exposed a Gemini tool-schema startup
failure. See the [startup fix and replacement build](gemini-startup-fix-2026-10-08.md).
The subsequent [caller-first opening change](caller-first-greeting-2026-10-08.md)
removes the automatic incoming greeting at the user's request and keeps outgoing
introductions after recipient speech.
The user confirmed working incoming conversation on 8 October; the follow-up
report records the two successful call audits. Broader live acceptance remains pending.
The latest [cached outgoing opening](cached-outgoing-opening-2026-10-08.md) plays
the selected spoken clip after outgoing/callback answer; that deployment supersedes
the outgoing recipient-first behavior. Its real-call rehearsal remains pending.
Two later incoming calls exposed lost pre-ready speech and separate Bluetooth
control-link timeouts. See the [early-hello investigation and fix](early-hello-fix-2026-10-08.md);
the user subsequently confirmed quick answering and response to the first hello.
That incoming check passes; broader live acceptance remains pending.
The build digest below records the earlier deployment and is superseded.

The scoped implementation and coordinated deployment are complete. Real-call
acceptance remains pending. No rehearsal calls or demonstration reminders were
placed by this verification run. Existing caller permissions, configuration and
credentials were retained; no guest route was added.

## Tested and deployed build

- Package: `hfp-mcp 0.1.1rc1`, built from the working tree (not a published release).
- Wheel SHA-256: `6ae707917d1c71fbe4923a36463f157029323b4e76c5998ec5904175b2746a77`.
- Artifacts: `/tmp/hfp-demo-build-6and9c1h/` (wheel and source distribution).
- Daemon: `.venv/lib/python3.13/site-packages/hfp_mcp`.
- Hermes: `/home/virusz4274/.hermes/hermes-agent/venv/lib/python3.11/site-packages/hfp_mcp`.
- All 51 Python module hashes in both environments match the tested wheel.
- Private rollback backup: `.local-backups/gemini-demo-20261007-230234/`.
  It includes linked SQLite snapshots, caller key, cron state, configuration,
  plugin files, installed modules/metadata, dependency inventories and tracked HEAD.
- Both services restarted after the phone was idle and native phone tasks settled.
  The handset was reconnected through authenticated `connect_and_wait` using its
  existing configured address. Bluetooth configuration was not replaced.

## Verification evidence

| Procedure | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q -o faulthandler_timeout=60` | 596 passed, one existing Starlette test-client deprecation warning; 46.65 seconds |
| Targeted tasks/reminders/callbacks regressions | 79 passed |
| Native Hermes check: Hermes interpreter running `scripts/check_hermes.py --hermes-root /home/virusz4274/.hermes/hermes-agent` | Passed against checkout `130b8f2c5d…`, including fenced native reminder updates/read-back in disposable state with network blocked |
| `python -m build`, `twine check` | Wheel and source distribution built; both metadata checks passed |
| `scripts/check_artifacts.py --source . <wheel> <sdist>` | Contents passed; no high-confidence credential/runtime-data matches; Git history not scanned |
| `scripts/smoke_packages.py <wheel> <sdist>` | Both installed in fresh portable environments; dependency checks, imports and CLI passed |
| Installed-code regressions: tasks, rescheduling, owner lookup, greeting and conversation tests | 93 passed, one existing warning; 17.03 seconds; includes previously failing late-correction cases |
| Runtime doctor, route validation, authenticated Hermes capabilities and owner authority | All readiness checks passed; owner lookup, callback rescheduling, conversation sessions and parallel tasks advertised |
| Read-only installed owner lookup for notes/calls/transcripts, limit 1 | All returned `ok`, bounded records and continuation; private content omitted from this report |
| SQLite schema inspection | Callback revision, verification and reminder-attempt columns, durable operations and trusted call-source table present |
| Runtime and Telegram inspection | Both services active, handset connected, call idle, API healthy, Telegram connected with home destination configured |
| Historical task/configuration comparison against backup | All 24 task IDs, native run IDs, statuses and delivery claims unchanged; configuration and credential files byte-identical |

Hermes `pip check` passed. The daemon environment inherits system Python packages;
its broad `pip check` reports existing unrelated distro/type-stub dependencies
(Flask/Pillow/etc.). HFP's required installed dependencies and native imports
passed separately. Installing the wheel with `--no-deps` retained dependency
versions in both environments. These system-package warnings are not a phone
plugin regression and were not repaired by changing unrelated packages.

Schedule changes retain IDs and revisions, read back both schedules, hold callbacks
while reconciliation is unresolved and persist uncertain outcomes across restart.
Repeat callback creation durably claims reminder creation before I/O and does not
create another reminder after an ambiguous result. Task notifications lead with
the observed schedule receipt and explicitly report unverified corrections.

Admin lookup is restricted to the caller's configured endpoint unless directional
`phone.owner_access` grants additional endpoints. Tests use disposable guest and
cross-profile configuration, including revoked/expired/delegated authority and
delayed results crossing a caller/session boundary. Guest absence in the deployed
configuration is not a failure of guest routing.

Incoming greeting tests cover one send across reconnect/reset and no outgoing
greeting; existing interruption tests remain green. Explicit wait-for-confirmation
blocks assistant hangup while authoritative tasks are busy. Remote hangup, caller
retraction and configured call limits still apply.

## Pending real-call acceptance

| User-initiated rehearsal | Evidence still needed |
| --- | --- |
| Incoming call and interruption | One audible brief introduction; caller speech interrupts naturally; no replay |
| Outgoing call requested through Hermes | Recipient hello precedes assistant introduction; all supplied purpose details heard |
| Callback plus time correction | Original callback/reminder IDs preserved; both actual times agree; original time does not fire; actual call and Telegram delivery recorded |
| Later-call memory recall and owner lookup | Save receipt, caller-scoped recall, accurate scoped admin lookup and transcript pages |
| Background work, cancellation and hangup | Correct continuation limits, verified final outcome/delivery, explicit wait honored |
| Confirmed session reset | Fresh context after explicit confirmation without greeting replay or cross-caller data |

The rehearsal request was presented to the user after deployment. No real-call
result has yet been supplied or observed. Voice readiness is not proof that a
recipient heard a message. No response-time claim is made without measurement.
Calendar booking requires separately configured Hermes integration and successful
provider receipts; this update adds no calendar provider.

Classic STT/TTS, recording, contacts, split configuration, office/inbox work and
other unrelated backlog items remain outside this delivery. The wider implementation
backlog is intentionally not marked complete.
