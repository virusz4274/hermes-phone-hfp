# Maintenance

Installations and updates are explicitly invoked, and full removal is manual.
There is no background auto-update or automatic full uninstall. `main` is the
normal install source; there is no separate release branch or per-fix tagging process.

## Installer behavior

| Installer | Changes when run | Service behavior |
| --- | --- | --- |
| `sudo bash setup/install.sh [--with-gemini]` | Installs/updates dependencies and the daemon package, refreshes its service and Bluetooth configuration, preserves existing environment settings and reconciles control credentials | Enables the HFP user service, restarts Bluetooth and the daemon, and checks daemon liveness |
| `python3 setup/install_hermes.py --home … --python …` | Installs/updates the supporting Python package in Hermes, backs up and replaces the phone plugin, removes obsolete phone-plugin copies from discovery, enables the new plugin, and connects both environment files to the routing YAML (creating an empty-route starter if missing) | Does not restart Hermes |

The Hermes installer automates the routing file path and starter YAML. Neither installer chooses caller numbers/routes, creates Hermes profiles, provisions Hermes/Gemini provider credentials or configures business integrations. The plugin installer also leaves Hermes platform toolset selection to the operator. Follow [Installation](install.md) to complete those steps.

## Updating an installation

1. Finish active calls and inspect `hfp-mcp phone status` for continued tasks and native phone workflows. Settle or cancel affected work and wait for native call/decision/report workers to finish before stopping services. Cancellation prevents future attempts; hangup is separate. Choose the intended revision and save local configuration changes.
2. Stop the affected Hermes gateway while replacing its plugin. For an installed default gateway service, use `systemctl --user stop hermes-gateway.service`; use the corresponding unit for a named profile or stop a foreground gateway in its terminal.
3. Run the Bluetooth installer, retaining `--with-gemini` if you use Gemini. Run the Hermes installer for each participating profile with its existing home and interpreter.
4. Validate the routing YAML with `.venv/bin/hfp-mcp route validate` and check each role with `route explain <number>`.
5. Start the affected Hermes gateway. Check `.venv/bin/hfp-mcp phone status`, run `doctor`, and perform the relevant [hardware acceptance checks](phone-routing.md#hardware-acceptance).

Package installation can update dependencies. Plugin file backups do not roll back Python dependencies or OS packages; reinstall a chosen package revision separately if needed.

## Editing configuration

Keep caller routes and permissions in `~/.config/hfp-mcp/config.yaml`, and credentials/provider settings in `~/.config/hfp-mcp.env` and the selected Hermes profile's `.env`. Use the single [routing example](../setup/phone.example.yaml) and [environment example](../setup/hfp-mcp.env.example) as references; merge changes into your existing files.

Keep optional environment settings commented out with their explanations so they remain easy to configure later. If a key is already active, edit it in place instead of adding a second assignment. Existing installations retain their environment files during updates; installers do not automatically refresh all comments or add every optional example. Copy missing commented options from the current example as needed, preserving active values and credentials. Keep private files at mode `0600` and [back them up](#backups) before editing.

Comment-only edits need no service restart. After changing active values, restart the affected daemon/Hermes gateways when calls and background tasks are idle. Validate routing with `hfp-mcp route validate`, then use `hfp-mcp phone status` and `hfp-mcp doctor` to check readiness.

## Branches and package versions

Use `main` as the source for installs and fixes. The `development` branch may be
used to prepare a group of changes before opening a pull request to `main`; it is
not a separate user-facing release channel. After stopping services as described
above, update the checkout with `git pull --ff-only origin main`, then rerun both
installers from that same revision before starting the gateways. Updating the
checkout alone does not update installed components.

There is no release ceremony or tag for each maintenance change. The package
version remains build metadata; it does not identify every change installed from
the moving `main` branch. Record the commit SHA when tracking a specific
installation or reporting a problem. Upgrade the daemon and Hermes plugin together.
Public note replacements require `expected_revision` from the preceding read;
stale revisions cause a conflict and omitted revisions fail validation. Existing
notes and transcripts are preserved by the automatic database additions.

The Gemini demo updates add callback revisions, durable schedule operations,
reminder-creation claims and trusted call-source attribution through additive
SQLite migrations. Upgrade daemon and participating Hermes environments from the
same tested distribution, then restart both services after calls and continued
tasks settle. For an existing configured installation, installing the same wheel
with `pip install --no-deps --force-reinstall <wheel>` in both environments avoids
changing Bluetooth configuration or dependency versions. Confirm module hashes,
new authenticated capabilities and migrated columns after restart; a matching
package version alone is insufficient. No guest route or owner-access grant is
created automatically.

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
- Its `cron/jobs.json` and all linked native cron execution/claim state for phone jobs, report-only jobs, and ordinary text reminders.
- Each participating profile's `hfp-phone/workflows.sqlite3`, alongside its caller store/key, and generated phone executor/report scripts in its `scripts/` directory. Keep the daemon `calls.db` (including execution receipts), native `state.db`, cron state, and profile stores from one quiescent snapshot.

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

1. Cancel future phone workflow attempts while the plugin is available, settle active call/decision/report workers, then stop affected Hermes gateways and disable the phone daemon:

   ```bash
   systemctl --user disable --now hfp-mcp.service
   ```

2. Back up the HFP files listed above. Remove the installed `~/.config/systemd/user/hfp-mcp.service` and any HFP-specific drop-ins. If an older setup added `hermes-gateway.service.d/20-hfp-phone.conf`, remove that file so Hermes no longer starts the old daemon or loads its environment.
3. In each participating Hermes profile, remove `hfp-phone` and obsolete `hfp-call-awareness` plugin entries, HFP MCP registrations, and `hfp_phone`/`hfp_caller` references from `platform_toolsets` and `known_plugin_toolsets`. Remove only phone-related entries. Remove HFP-specific environment variables and stale phone-operation skills. Remove the installed phone-plugin directories from `plugins/`, including obsolete disabled or backup copies that remain discoverable. If no other profile using that interpreter needs the phone plugin, uninstall the supporting package with `<hermes-python> -m pip uninstall hfp-mcp`; keep shared dependencies.
4. Archive or remove the daemon environment, phone YAML/state and the profile's `hfp-phone/` stores according to your retention needs. Remove only identified phone-workflow native cron jobs and generated executor/report scripts after their workers have settled; leave unrelated jobs/scripts alone. Preserve unrelated sections in shared YAML. Historical Hermes chats, native delivery history and external calendar records require separate management in their owning systems.
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

Gemini outgoing openings now default to the packaged cached spoken greeting.
Keep `hfp_mcp/audio/assets/outgoing-hello.wav` with the package during upgrades;
release and portable-install checks verify it is present and readable. Set
`HFP_GEMINI_OUTGOING_GREETING=off` to retain recipient-first outgoing behavior or
`chime` for a brief cue. Restart after configuration/package changes when calls
and background tasks have settled. Incoming calls continue to wait for the caller.

## Native phone workflow upgrade

Phone scheduling now belongs to native Hermes cron. See [the lifecycle and upgrade guide](hermes-call-workflows.md) before restarting: preserve linked workflow, call, native session, cron and script stores together. Legacy pending daemon callbacks are held for review, and terminal history is retained.

For rollback, stop both runtimes and native phone workers, then restore compatible
packages and the complete linked snapshot: daemon ledger/execution receipts,
profile caller key/store and workflow database, native sessions/cron state,
generated scripts, configuration, and plugin. Do not combine an older workflow
store with newer execution claims or resume historical uncertain calls. If external
actions occurred after the snapshot, reconcile them before re-enabling jobs; a
filesystem restore cannot undo calls or delivered messages. Never enable the retired
daemon callback timer alongside native scheduling. Review held legacy pending
callbacks and their former reminders explicitly before migration or cancellation.

Workflow requests are pruned at gateway startup after the retention interval
(default 30 days) beyond their deadline when no attempt remains active. Generated
scripts, backups, native cron records and Hermes sessions have separate cleanup
lifecycles. Forgetting caller notes does not clear those records. For exact privacy
boundaries see [Security](../SECURITY.md#phone-workflow-authority-and-records).
