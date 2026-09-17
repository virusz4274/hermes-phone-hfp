# Installation

This guide installs Hermes Phone from a fresh checkout on one Linux machine. Run commands from the repository root as the user who will own the phone service. Use `sudo` only for the Bluetooth installer.

```bash
git clone https://github.com/virusz4274/hermes-phone-hfp.git
cd hermes-phone-hfp
```

Use the reviewed preview revision or release source archive. The wheel contains
the runtime; the checkout/source archive also includes host setup and plugin files.
See [compatibility](compatibility.md) before installing into Hermes.

## 1. Prerequisites

- Debian-family Linux or Raspberry Pi OS Bookworm/Trixie, systemd and Python 3.11–3.13.
- A Bluetooth controller supporting SCO audio over HCI and a mobile phone supporting HFP.
- Hermes already installed with a working model/provider and at least its default profile configured.
- For Gemini Live: a Gemini API key. For classic voice or its optional fallback: working STT/TTS providers in the selected Hermes profile. `ffmpeg` is needed on the Hermes host as well as the Bluetooth host.

The daemon uses narrowband CVSD audio: 8 kHz, mono, signed 16-bit PCM. mSBC wideband audio is not implemented.

## 2. Install the Bluetooth daemon

For Gemini Live:

```bash
sudo bash setup/install.sh --check
sudo bash setup/install.sh --with-gemini
```

For classic STT/TTS only, omit `--with-gemini`.

This installs OS/Python dependencies, configures BlueZ and WirePlumber, generates a private control token, and starts `hfp-mcp.service` under your user account. It restarts Bluetooth during setup. It does not install or restart Hermes.

`--check` performs preflight only. Before changes, the installer prints a private
configuration backup location. See [recovery](maintenance.md#recovering-a-failed-installation).

The daemon is a systemd **user service**. The installer enables it and enables user lingering, so it starts at boot without an interactive login. Manage it with `systemctl --user`; there is no second HFP system service to start.

Pair your mobile phone if the installer did not adopt an already paired phone:

```bash
.venv/bin/hfp-mcp enroll --timeout 180
.venv/bin/hfp-mcp doctor
```

Follow the enrollment instructions and enable call audio for this Bluetooth connection on the phone. The phone supplies cellular service; its Bluetooth address identifies the handset, while caller numbers select Hermes profiles.

The installer creates `~/.config/hfp-mcp.env` from [the current environment example](../setup/hfp-mcp.env.example). `HFP_MCP_DAEMON_URL` is the URL used by MCP clients and Hermes owner tools; they share `HFP_MCP_BEARER_TOKEN`, so a second phone-plugin control token is unnecessary. Set `HFP_PHONE_SELF_NUMBER` there to the cellular number of the paired handset to prevent accidental calls to itself. Set `HFP_ADAPTER_ADDRESS` if multiple Bluetooth adapters are attached.

## 3. Install the one Hermes plugin

Select the Hermes home and Python interpreter used by your existing Hermes installation. For a typical default installation:

```bash
python3 setup/install_hermes.py \
  --home ~/.hermes \
  --python ~/.hermes/hermes-agent/venv/bin/python
```

Use your actual interpreter path if it differs; it must support `python -m pip`. The launcher itself needs only Python's standard library. The installer installs its dependencies and writes plugin configuration using the selected Hermes interpreter.

The installer copies **only `hermes_phone_plugin/`** to `~/.hermes/plugins/hfp-phone/`, installs the supporting Python package, and enables the plugin. It backs up replaced files outside plugin discovery. It preserves other plugins and the selected profile's model settings. It creates no profile and restarts no gateway.

The Hermes installer installs the portable base package with normal dependency resolution. The daemon-only MCP SDK and uvicorn are excluded, preserving Hermes’s own MCP dependency. Native `dbus-python` and `PyGObject` bindings belong to the `daemon` extra, which the Bluetooth installer selects automatically. Hermes does not need those native bindings, even when its Python version differs from the system Python. If an earlier plugin installation reported them as missing, rerun the current Hermes installer to replace the old package metadata.

## 4. Configure your first caller route

The Hermes installer automatically selects the routing YAML and writes its absolute path as `HFP_MCP_CONFIG` in both the daemon environment and the selected Hermes profile's `.env`. It preserves an existing path and file. If neither environment specifies a path, it uses `~/.config/hfp-mcp/config.yaml`.

If the file is missing, the installer creates a private starter configuration with `numbers: {}` and a commented example. It never grants admin access to the sample number. Open the file printed by the installer:

```bash
nano ~/.config/hfp-mcp/config.yaml
```

Set `phone.default_region` to your ISO country code (for example, `IN`, `US`, or
`GB`). It takes precedence over `HFP_DEFAULT_REGION`; both default to `IN` for
existing installations. Prefer full international E.164 numbers starting with `+`.

Inside `phone:`, replace `numbers: {}` with your own route, using your real calling number:

```yaml
  numbers:
    "+919876543210":
      endpoint: personal
      policy: admin
      voice: gemini_live
      continuity: true
```

Use **the number you will call from**, which is different from the paired handset's own SIM number. Keep the existing `endpoints` and `policies` sections. See [the full YAML example](../setup/phone.example.yaml) for context.

For a custom location, pass `--phone-config /absolute/path/to/calls.yaml` to `setup/install_hermes.py`. That explicit option updates both path references. If existing daemon and Hermes paths disagree, the installer reports the conflict instead of silently choosing one. Separate hosts need a local copy on each host, as described in [multiple gateways](phone-routing.md#multiple-gateways).

The example selects the existing `default` Hermes profile, admin permissions and Gemini Live. Your normal Hermes approval rules still apply. Number matching is the identity basis for this route; caller ID is not cryptographic authentication.

The example also sets `continuity: true` on that number route. This saves phone
dialogue in the daemon's existing database, resumes the caller's conversation,
and links a native Hermes task session. Existing routes remain unchanged until
you add this setting. Active dialogue remains until reset; archived dialogue is
retained for 30 days. See [conversation continuity](phone-routing.md#conversation-continuity)
for voice reset/delete controls and the required Gemini transcription settings.

For classic voice, set `voice: classic` and remove `fallback`. For Gemini startup fallback, add `fallback: classic` only after STT/TTS is configured in Hermes. An active conversation does not switch backends automatically.

There is no guest default in this initial configuration. To answer unknown callers, follow [guest and known-caller profiles](phone-routing.md#optional-guest-and-known-caller-profiles) after your own route works.

## 5. Set credentials

There are three separate credentials:

| Credential | Used by | Configuration |
| --- | --- | --- |
| Daemon control token | Owner tools/MCP → HFP daemon | Generated by the Bluetooth installer; `HFP_MCP_BEARER_TOKEN` and private `control.token` |
| Hermes API key | HFP daemon → Hermes Runs API | Same value in daemon `HFP_HERMES_API_KEY` and Hermes `API_SERVER_KEY` |
| Phone binding secret | HFP daemon → phone plugin | Same value in both environments under `HFP_HERMES_BRIDGE_KEY`; different from the API key |

Generate two separate random secrets of at least 32 characters using your password manager. Edit the existing environment files; do not replace their other settings or put real secrets in the repository.

In `~/.config/hfp-mcp.env`, add:

```dotenv
HFP_HERMES_API_KEY=<your-hermes-api-secret>
HFP_HERMES_BRIDGE_KEY=<your-different-binding-secret>
HFP_GEMINI_API_KEY=<your-gemini-api-key>
```

Omit `HFP_GEMINI_API_KEY` when using only classic voice. Keep the `HFP_MCP_CONFIG` line written by the installer; you do not need to enter that path again.

In the selected Hermes home's `.env` (normally `~/.hermes/.env`), add:

```dotenv
API_SERVER_ENABLED=true
API_SERVER_KEY=<same-value-as-HFP_HERMES_API_KEY>
HFP_HERMES_BRIDGE_KEY=<same-binding-secret-as-the-daemon>
```

```bash
chmod 600 ~/.config/hfp-mcp.env ~/.hermes/.env
```

The same-user plugin and local CLI read the private HFP environment and control token automatically. For separate hosts/users, explicitly provision the daemon URL and control credential on the Hermes host; see [multiple gateways](phone-routing.md#multiple-gateways).

Hermes serves its API at `http://127.0.0.1:8642` by default. Keep the route's endpoint URL aligned with your gateway. See the upstream [Hermes API setup](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server).

## 6. Select Hermes tools

The `hfp_phone` toolset provides owner chat controls. Enable it for the chat platforms where you want to start calls or resolve pending approvals. The `hfp_caller` toolset supplies caller-scoped notes and registered caller-aware integrations.

In the selected Hermes home's `config.yaml`, configure `platform_toolsets.api_server` with the tools you want available over phone calls. For personal admin use, copy the toolset selection from your owner chat platform and include `hfp_caller`. Preserve any existing platform entries. Admin routing permits those tools; it does not change Hermes's own tool availability or approval settings.

Public/receptionist profiles need the stricter configuration in [guest and known-caller profiles](phone-routing.md#optional-guest-and-known-caller-profiles). Do not point a guest route at an unrestricted personal profile.

## 7. Validate and start

For full transcripts, set `HFP_FULL_TRANSCRIPTS=true` in `~/.config/hfp-mcp.env`
before starting/restarting the daemon. This enables private, persistent text
records with the configured retention period; it does not record raw audio.
See [transcript access and exports](phone-routing.md#full-call-transcripts-and-timing).

Validate the configuration without contacting models or placing a call:

```bash
.venv/bin/hfp-mcp route validate
.venv/bin/hfp-mcp route explain +919876543210
```

Use your configured number in the second command. Check the selected profile, policy and voice backend.

Start the existing Hermes gateway with `hermes gateway` in a separate terminal and leave it running, or use `hermes gateway restart` if you already run its installed gateway service. The HFP installers do not install Hermes itself or create its gateway service. Then reload the daemon configuration:

```bash
systemctl --user restart hfp-mcp.service
.venv/bin/hfp-mcp phone status
.venv/bin/hfp-mcp doctor
```

Phone status should show `enabled: true` and `state: idle` between calls. This confirms that routing is loaded; run `doctor` to check gateway credentials, profile identity, required capabilities, and local voice prerequisites before a call. Provider access and audio quality still require a live test. Keep the Hermes gateway running while using phone assistance.

Call the paired handset from your configured number. Confirm that the right profile responds and two-way audio works. Then test an unknown caller and confirm it is declined, or handled by your explicitly configured guest profile. Complete the [hardware acceptance checks](phone-routing.md#hardware-acceptance) before unattended use.

`doctor --hermes-home /path/to/profile` checks a custom local Hermes home.
`doctor --offline` skips HTTP checks. Provider prerequisites being present does
not prove cloud credentials are accepted or that live audio will work.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Routing disabled | `phone.version: 1` exists in the YAML used by the daemon; restart after editing |
| Caller declined | `route explain` matches the normalized caller number; withheld numbers require a default route |
| Call ends while connecting | Hermes API/key, bridge secret, matching profile, required Runs API capabilities and plugin loading |
| Restricted profile rejected | Shared memory disabled, only caller-aware API tools enabled, no raw MCP servers |
| No owner phone tools | Enable `hfp_phone` in the appropriate Hermes chat platform's toolsets and restart the gateway |
| No audio | Phone call-audio setting, SCO-capable Bluetooth hardware, `doctor`, provider credentials |

```bash
journalctl --user -u hfp-mcp.service -n 80 --no-pager
hermes gateway status
```

If `journalctl --user` reports no journal files, read the service's entries in the system journal:

```bash
sudo journalctl _SYSTEMD_USER_UNIT=hfp-mcp.service -n 80 --no-pager
```

If an earlier installation failed with a read-only `/run/user/<uid>/hfp-mcp` error, rerun the current Bluetooth installer. Its service definition creates the private runtime directory before applying filesystem restrictions. Rerunning also recreates a deleted daemon environment file; re-enter any provider credentials that were in that file.

For adding profiles, caller notes, approval commands and extension APIs, continue with [Configuration and operations](phone-routing.md).

For subsequent installations, updates, backups or removal, follow [Maintenance](maintenance.md). Updates run only when you invoke an installer; full uninstall remains manual.

### Updating task coordination

Existing installations do not need Bluetooth re-enrollment for this update. Back up
`calls.db`, the selected Hermes home's `hfp-phone/` and `state.db` together inside
`.local-backups/`, then update the Python package in both runtimes (the daemon's
editable install already follows this checkout). Use the existing Hermes installer
if its plugin entry point is missing. Restart `hermes-gateway.service` and
`hfp-mcp.service` after calls and unfinished tasks have settled.

The changes are additive and preserve conversation/session history. Set
`phone.max_phone_tasks: 2` and `phone.background_task_minutes: 30`, and opt in with
`background_tasks: true` only on the intended admin policy after configuring the
profile's native Telegram home channel. No new service, sender plugin or cron job
is installed. See [natural task handling](phone-routing.md#natural-task-handling).
