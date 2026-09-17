# Maintenance

For this public preview, installations and updates are explicitly invoked and full removal is manual. There is no background auto-update or automatic full uninstall.

## Installer behavior

| Installer | Changes when run | Service behavior |
| --- | --- | --- |
| `sudo bash setup/install.sh [--with-gemini]` | Installs/updates dependencies and the daemon package, refreshes its service and Bluetooth configuration, preserves existing environment settings and reconciles control credentials | Enables the HFP user service, restarts Bluetooth and the daemon, and checks daemon liveness |
| `python3 setup/install_hermes.py --home … --python …` | Installs/updates the supporting Python package in Hermes, backs up and replaces the phone plugin, removes obsolete phone-plugin copies from discovery, enables the new plugin, and connects both environment files to the routing YAML (creating an empty-route starter if missing) | Does not restart Hermes |

The Hermes installer automates the routing file path and starter YAML. Neither installer chooses caller numbers/routes, creates Hermes profiles, provisions Hermes/Gemini provider credentials or configures business integrations. The plugin installer also leaves Hermes platform toolset selection to the operator. Follow [Installation](install.md) to complete those steps.

## Updating an installation

1. Finish active calls and check `hfp-mcp phone status` for unfinished continued tasks. Settle or cancel those tasks before an update; restart reconciles their native IDs without replaying actions. Choose the checkout revision you intend to install. Save any local configuration changes.
2. Stop the affected Hermes gateway while replacing its plugin. For an installed default gateway service, use `systemctl --user stop hermes-gateway.service`; use the corresponding unit for a named profile or stop a foreground gateway in its terminal.
3. Run the Bluetooth installer, retaining `--with-gemini` if you use Gemini. Run the Hermes installer for each participating profile with its existing home and interpreter.
4. Validate the routing YAML with `.venv/bin/hfp-mcp route validate` and check each role with `route explain <number>`.
5. Start the affected Hermes gateway. Check `.venv/bin/hfp-mcp phone status`, run `doctor`, and perform the relevant [hardware acceptance checks](phone-routing.md#hardware-acceptance).

Package installation can update dependencies. Plugin file backups do not roll back Python dependencies or OS packages; reinstall a chosen package revision separately if needed.

## Backups

The Hermes installer automatically backs up replaced plugin files and `config.yaml` under `<Hermes home>/hfp-phone-backups/<timestamp>/`. That directory is outside plugin discovery. The Bluetooth installer separately backs up its host configuration and daemon environment under `/var/lib/hfp-mcp/backups/`.

Manual backups can be kept in this checkout's `.local-backups/`, which is excluded from Git. Use directory mode `0700` and file mode `0600` for credentials and configuration.

Before removing data, retain the files you need:

- `~/.config/hfp-mcp.env` and `~/.config/hfp-mcp/config.yaml` for daemon configuration and credentials.
- `~/.local/state/hfp-mcp/` for the control token, call records and action state.
- The selected Hermes home's `config.yaml`, `.env`, `plugins/hfp-phone/` and `hfp-phone/` directory.
- The adjacent key file with `callers.sqlite3`, so restored caller IDs still match their notes.
- The selected Hermes home's `state.db` for native task sessions linked from the
  daemon's `calls.db`. Preserve these databases together when backing up continuity.

Copy databases after calls and the services using them have stopped. A plugin/config backup is not a full Hermes backup; preserve other Hermes data through your normal backup process.

Conversation continuity is opt-in per route (`continuity: true`). Upgrading does
not import old test-call transcripts. To disable it, set the route flag to false
and restart the daemon after the call ends; stored conversations remain available
if it is re-enabled. The additive database changes do not require deleting the
existing ledger. Voice history deletion removes managed live records, not copies
in operator backups; handle those backups separately. Saved caller facts require
their own explicit forget request.

## Paired phone will not reconnect

The daemon enables the adapter's `Connectable` setting at startup where BlueZ
supports it. This permits incoming connections from an already-paired handset;
it does not enable discovery or open a pairing window. A powered adapter can
still have incoming connections disabled. See the
[BlueZ adapter API](https://github.com/bluez/bluez/blob/5.82/doc/org.bluez.Adapter.rst).

Daemon-initiated reconnects request the HFP profile explicitly. A generic BlueZ
device connection can select a different bearer/profile on a dual-mode phone
and time out even though HFP itself works. Existing pairing records need not be
removed to fix this condition.

`phone.auto_reconnect: true` in the routing YAML makes the daemon retry the
configured handset when disconnected. With the default `false`, reconnect from
the phone's Bluetooth settings or use the daemon's `connect_and_wait` MCP tool.
Restart `hfp-mcp.service` after changing the routing YAML. Profile readiness or
an HTTP health response alone does not prove the handset is connected.

## Manual full removal

This procedure removes an installed phone integration. It does not delete the new source checkout or unrelated Hermes profiles.

1. Finish calls, stop affected Hermes gateways and disable the phone daemon:

   ```bash
   systemctl --user disable --now hfp-mcp.service
   ```

2. Back up the HFP files listed above. Remove the installed `~/.config/systemd/user/hfp-mcp.service` and any HFP-specific drop-ins. If an older setup added `hermes-gateway.service.d/20-hfp-phone.conf`, remove that file so Hermes no longer starts the old daemon or loads its environment.
3. In each participating Hermes profile, remove `hfp-phone` and obsolete `hfp-call-awareness` plugin entries, HFP MCP registrations, and `hfp_phone`/`hfp_caller` references from `platform_toolsets` and `known_plugin_toolsets`. Remove only phone-related entries. Remove HFP-specific environment variables and stale phone-operation skills. Remove the installed phone-plugin directories from `plugins/`, including obsolete disabled or backup copies that remain discoverable. If no other profile using that interpreter needs the phone plugin, uninstall the supporting package with `<hermes-python> -m pip uninstall hfp-mcp`; keep shared dependencies.
4. Archive or remove the daemon environment, phone YAML, daemon state directory and the profile's caller-note directory. Preserve any unrelated sections in a shared YAML file. Historical Hermes chats and external calendar records are separate data and are not removed by uninstalling this integration.
5. Remove only HFP-specific host overrides if present: `/etc/dbus-1/system.d/hfp-mcp.conf`, WirePlumber's `90-hfp-mcp.conf` or `90-hfp-mcp.lua`, and the obsolete `99-hfp-audio.conf`. Check both `/etc/wireplumber/` and your user WirePlumber configuration directories. Leave shared Bluetooth packages, pairing records and unrelated audio settings intact.
6. Reload the systemd user configuration with `systemctl --user daemon-reload`. If the D-Bus policy changed, run `sudo systemctl reload dbus.service`. If WirePlumber overrides changed, restart the active WirePlumber user service. Start the affected Hermes gateways again.
7. Verify that `systemctl --user show hfp-mcp.service -p LoadState -p ActiveState` reports `not-found` and `inactive`, the Hermes gateways are healthy, and no HFP plugin/toolset registrations remain in their configuration.

Shared dependencies and user lingering may also be used by Hermes or other services; do not remove them as part of phone-only cleanup. The source virtualenv is separate from the installed service registration and can remain for development.

## Recovering a failed installation

The Bluetooth installer validates prerequisites before changing packages or host
configuration. Run `sudo bash setup/install.sh --check` to repeat that validation.
A real install first prints a snapshot under `/var/lib/hfp-mcp/backups/`. Each
snapshot is private to root and includes `manifest.json` plus numbered file copies.
The manifest records the original path, whether it existed, its owner/group, mode,
and modification time. Backups can contain credentials; keep them private.

1. Finish calls and stop `hfp-mcp.service` before restoring configuration.
2. Inspect the chosen manifest with `sudo cat <snapshot>/manifest.json` locally;
   do not post it or its file copies to an issue.
3. For each entry with `existed: true`, restore its numbered copy to `path`, then
   restore the recorded `uid`, `gid`, `mode`, and modification time. A mode is stored
   as a JSON integer (for example, 384 means octal 0600). Preserve the current file
   separately if it contains later configuration you need.
4. For `existed: false`, remove only the corresponding HFP-specific file created by
   the failed installation. Do not remove parent directories or unrelated settings.
5. Reload D-Bus if its policy changed, restart Bluetooth/WirePlumber if their
   configuration changed, and run `systemctl --user daemon-reload`. Restore a known
   package revision if needed before restarting the phone service.
6. Run `hfp-mcp doctor` and the relevant hardware checks.

A snapshot restores configuration, not OS/Python packages, pairing/trust records,
group membership, user lingering, or service enablement. Those changes may remain
after a failed install. Shared dependencies must not be removed automatically.
