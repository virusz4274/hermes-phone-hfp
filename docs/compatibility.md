# Preview compatibility

This release targets Hermes users on Linux. A tested software interface does not
establish support for every Bluetooth adapter or handset.

| Component | Preview target | Evidence / limitation |
| --- | --- | --- |
| Python | 3.11–3.13 | CI matrix; local release validation uses 3.13 |
| Hermes | `819988acb750836387fbb9d5d76203a9b3f530f4` | Native plugin/session compatibility check; this is a commit, not a promised minimum version |
| MCP SDK | 1.x, >=1.27 | Daemon extra only; MCP 2 renamed FastMCP and is not a daemon target |
| Bluetooth host | Debian-family Linux, systemd; Raspberry Pi OS Bookworm/Trixie | Installer targets; fresh-machine validation is required on each platform |
| Phone / Bluetooth controller | HFP Audio Gateway + SCO over HCI | No universal handset/adapter claim; publish results with model and OS versions |
| Call audio | CVSD, 8 kHz mono PCM | Wideband mSBC is not implemented |
| Gemini Live | `gemini-3.1-flash-live-preview`, google-genai 2.x >=2.16 | Local SDK/configuration checks; cloud access and audio quality require a live call |
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
