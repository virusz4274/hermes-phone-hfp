# Hermes phone workflow verification — 2026-10-08

Implemented and installed the native Hermes scheduler and outcome-driven phone
workflow. This report distinguishes automated checks, installation verification
and live recipient acceptance.

## Build identity and automated evidence

- Base commit: `1d3a405`; the implementation is in the current working tree.
- Version: `0.1.1rc1`; version alone does not identify the build.
- Wheel SHA-256: `91b9a8ba453b82c42cca5e1e6f0ec6e4379d196026560ae678fa1a71949bb7d8`.
- Source, wheel, daemon environment and Hermes environment: all 56 package files
  match byte for byte, including the cached greeting asset.
- Source full suite: `PYTHONPATH=src .venv/bin/python -m pytest -q` — **658 passed**,
  one preexisting Starlette test-client deprecation warning, 58.04 seconds.
- Installed full suite: `env -u PYTHONPATH .venv/bin/python -m pytest -q` —
  **658 passed**, the same warning, 52.86 seconds.
- Isolated native Hermes compatibility check: passed. Real native job creation,
  schedule move/read-back, execution/firing-claim admission, cancellation and
  revoked execution authority were exercised in a disposable credential-free
  profile with external network access blocked. No call or message was executed.
- Wheel and sdist build, completeness/private-data inspection and Twine metadata
  checks: passed. Both artifacts passed portable installation/import/CLI and
  dependency smoke checks in fresh environments.
- Python AST, whitespace and the 154-item Markdown/HTML checklist catalog checks:
  passed.

New coverage includes duplicate creation/execution, lost creation responses,
restart recovery without dialing, native edits, stale revisions, failed schedule
moves and reconciliation, cancellation, firing ownership loss, profile isolation,
guest/expired caller denial, current route revocation, delayed decision authority,
recipient deferral, evidenced completion/refusal, exhausted attempts, calling
hours/deadlines, exact-call evidence isolation and durable report claims.
Final regressions cover cancellation arriving inside a native schedule move,
actual calling hours at catch-up admission, the original request date in retry
briefs, and rejection of assistant speech alone as completion evidence.
Existing startup audio, cached greeting, memory, transcript, owner lookup,
background work, late task correction and hangup regressions remain passing.

## Deployment and runtime evidence

Calls were idle; there were no active continued phone tasks or live native cron
workers. Legacy callback history contained three completed and two failed entries,
with no pending schedules to migrate. Both services were stopped before private
linked-store/configuration/native-session/cron/script/package backups. Backup
location and rollback package are recorded privately in the deployment manifest.

The same tested wheel was installed without dependency changes into the daemon
and Hermes environments. Both services restarted and remained active/running with
zero automatic restarts. Daemon health/readiness passed. The paired handset was
reconnected using the existing `connect_and_wait` operation and is connected/idle;
Bluetooth configuration and pairing were preserved. This is a runtime snapshot,
not evidence that earlier intermittent RFCOMM transport errors are eliminated.

Authenticated gateway capabilities report native phone workflows, scoped owner
lookup and callback rescheduling. Authenticated daemon MCP initialization and
43-tool listing passed. The daemon execution-receipt table and both profile-local
workflow tables exist. Telegram is enabled and its home destination is configured;
no test message was sent.

The requester timezone is `Asia/Kolkata`. Retry schedules and actual execution
both enforce automatic calling hours; clearly specified owner/recipient times
retain their documented exception. The final installation followed a second
private backup after the calling-hours and cancellation-race fixes.

## Existing backlog evidence

This change supplies further evidence for **TASK-01** (revision-safe schedule
changes), **TASK-02** (verified schedules and unresolved corrections), and
**TASK-03** (correction regressions). Native Hermes now supplies the single
schedule; compatibility callback/reminder time fields describe that same job.
Historical verification against the former dual-schedule design remains history.
Broader dependencies and unrelated backlog work remain pending.

## Live acceptance — pending

The following new-flow rehearsals have not been performed after this deployment:

- Successful call, cached opening, accurate objective completion and one final
  Telegram outcome/decision report.
- Unanswered/busy call followed by a verified bounded native retry.
- Evidenced “call tomorrow at 10” recipient deferral.
- Clear refusal closing the request with no future attempt.
- Cancellation of future attempts without forcing active-call hangup.
- Later-call memory recall and preservation of verified prior progress.

Use [the updated manual checklist](manual-feature-checklist.md), especially
R03–R26. Record actual call/job/attempt identities and Telegram delivery evidence
privately. No claim is made about recipient audibility, response latency, real
calendar booking, or successful automatic follow-up until observed live.

## Documentation reconciliation — 9 October 2026

The accepted plan now records TASK-01/02 against the single native schedule and
adds WORKFLOW-01. Superseded dual-schedule acceptance definitions/evidence remain
under `historical_acceptance` in the backlog; they do not count toward changed
native-workflow requirements. Partial reports/calling-hour support does not
complete REPORT-01 or OFFICE-02. New-flow live acceptance remains pending.

During this documentation-only update, the current source working tree based on
`1d3a405` passed:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_phone_workflows.py tests/test_workflow_execution.py tests/test_reminders.py tests/test_owner_notes.py
```

Result: **61 passed**, one existing Starlette warning, 2.58 seconds. These are
synthetic regression checks, not a new full-suite, native-Hermes, package build,
deployment, call, or delivery run. The build identity and 658-test evidence above
remain the 8 October record. Documentation validation is separate from runtime
acceptance; no live services or private records were changed by this update.

Documentation checks passed for 25 requirement IDs, 89 current acceptance IDs,
acyclic dependencies, statuses, source/evidence references, local Markdown links
and anchors, and SHA-256 formatting. All 154 manual check IDs agree between
Markdown, the browser catalog and rendered articles. Runtime/support file hashes
(124 files under source, tests, scripts, setup and plugin directories) were
unchanged during this task. Whitespace validation passed. Historical comparison
notes were updated locally and retain their existing Git exclusion.
