# Compatibility

Hermes Phone targets Hermes users on Linux. A tested software interface does not
establish support for every Bluetooth adapter or handset.

| Component | Current target | Evidence / limitation |
| --- | --- | --- |
| Python | 3.11–3.13 | Supported range; current CI tests 3.13 only; local validation uses 3.13 |
| Hermes | `130b8f2c5dbca93a81aa396dd2ba44420d78f6f0` | Native plugin/session compatibility check; this is a commit, not a promised minimum version |
| MCP SDK | 1.x, >=1.27 | Daemon extra only; MCP 2 renamed FastMCP and is not a daemon target |
| Bluetooth host | Debian-family Linux, systemd; Raspberry Pi OS Bookworm/Trixie | Installer targets; fresh-machine validation is required on each platform |
| Phone / Bluetooth controller | HFP Audio Gateway + SCO over HCI | No universal handset/adapter claim; publish results with model and OS versions |
| Call audio | CVSD, 8 kHz mono PCM | Wideband mSBC is not implemented |
| Gemini Live | Default `gemini-3.1-flash-live-preview`, google-genai 2.x >=2.16 | `HFP_GEMINI_LIVE_MODEL` may override it; dated October rehearsals used `gemini-3.8-live`. Local configuration checks and individual rehearsals do not certify every model/audio scenario |
| Classic voice | Hermes STT/TTS and ffmpeg | Checks local shims; provider credentials and audio quality require live validation |

The phone plugin's base package does not require the MCP SDK, uvicorn, or native
D-Bus/GLib bindings. This avoids replacing Hermes's own MCP dependency. Install
`hfp-mcp[daemon]` in the Bluetooth daemon's separate environment.

`hfp-mcp doctor` checks each configured endpoint's authentication, bridge secret,
profile identity and Runs API capabilities. Continuity requires compatible native
sessions and task support. Compaction is optional and produces an explicit error
on unsupported runtimes. A passing local prerequisite check is not a live provider
health check.

The plugin uses Hermes's native session storage and internal compaction interface.
Upgrades can change these interfaces. Before changing Hermes revisions, rerun
`scripts/check_hermes.py` using that revision's interpreter and a disposable home
(created automatically), then perform the relevant hardware acceptance checks.

Record hardware results with phone model/OS, controller chipset, host OS/kernel,
BlueZ/WirePlumber versions, Hermes commit, voice backend, incoming/outgoing calls,
interruption, hangup/reconnect and restart behavior. Do not include caller IDs,
Bluetooth addresses, transcripts, or credentials in public reports.

Automatic call memory uses Hermes's `agent.auxiliary_client.async_call_llm` under
the routed profile scope, with no action tools. The bridge advertises
`call_memory_closeout` only when that interface exists. Provider availability and
extraction quality still require a live check. The dedicated `auxiliary.phone_memory`
task uses normal Hermes auxiliary routing and can be configured in the profile.

Notes APIs now return `revision` and require `expected_revision` for replacement
writes. Upgrade daemon and plugin together. Old clients that omit the revision
receive a validation error; stale revisions receive a conflict instead of
silently overwriting concurrent updates. Database additions are automatic and
preserve existing notes and transcripts.

## Native phone workflows

Phone objectives require Hermes native one-shot `no_agent` scripts, private profile `scripts/`, durable cron execution identities and firing fences, plus native Runs API sessions and idempotent admission. Missing capabilities leave requests unverified; there is no daemon-timer fallback. The isolated compatibility check exercises native phone job creation, revision-safe moves and cancellation without dialing or delivering messages.

Configurable reminder delivery also uses Hermes's native default home-destination
resolver and configured delivery-target discovery. The compatibility adapter
isolates these internal APIs. The isolated check covers a non-Telegram destination,
linked notification/callback moves and durable notification claims. Call-only
reminders need no gateway; notification jobs retain native delivery receipts and
show failures through phone status. Existing scheduled jobs are not rewritten.

See [the dated workflow evidence](hermes-owner-flow-verification-2026-10-08.md)
for the tested installation and pending real-call scenarios. A package version
alone does not identify a working-tree build; record its artifact digest and the
Hermes revision used for compatibility checks.
