# Phone request authorization repair

The November 13 call reached Hermes with an admin binding, but the plugin used
the gateway conversation routing key as a Runs API request ID. Authorization
failed before system tools ran. The earlier October 18 RAM check succeeded.

1. Preserve the connected phone: do not restart Bluetooth, the HFP daemon,
   WirePlumber, or the host. Record their process identities before deployment.
2. Expose the exact admitted Runs API request ID in Hermes's existing task-local
   API execution context. Keep the normal conversation/approval routing unchanged.
   Never derive authority from dialogue, model arguments, or a search for the
   latest request in a conversation.
3. Update the plugin's compatibility adapter to read that trusted execution ID,
   retaining support for older Hermes execution paths. Use it consistently for
   tool authorization, caller tools and follow-up decisions. Preserve caller,
   profile, task-lifetime, revocation and native approval checks.
4. Reproduce the failure before the fix. Cover valid admin requests, restricted
   callers, cancellation/hangup, separate conversations, sequential requests and
   late callbacks. Extend the real-Hermes check to cover canonical admission.
5. Publish through the existing managed plugin/runtime installer with backups.
   Restart only the Hermes gateway, then verify plugin/API readiness and confirm
   that Bluetooth and HFP process identities and the phone connection persist.
6. Leave actual dialing and live audio testing to the owner. Later, ask for RAM
   usage and a PC ping, repeat in a fresh session, and verify that the values come
   from executed tools rather than saved readings. Check an approval-requiring
   action separately without weakening approvals.

Rollback uses the saved plugin/runtime generation and another gateway-only
restart. Do not reset pairing, disconnect the phone, or restart the HFP daemon.

## Verification

The implementation repairs the plugin lookup and carries the exact native
admission identity through Hermes's task-local execution context. The standalone
companion patch is retained at `setup/hermes-api-run-identity.patch`; preserve it
when upgrading Hermes until the equivalent native interface is available.

- Phone repository: 794 tests passed.
- Native Hermes: 21 tests passed across canonical API projection, admission
  behavior and the new execution-identity regression tests.
- Native code checks: all 13 passed, with no blocking or advisory health findings.
- Isolated integration: real canonical admission, TurnRunner approval scope and
  plugin hooks passed, including sequential requests and late-worker revocation.
- No model requests, calls or messages were used for the isolated checks.

The host rebooted between work sessions at 16:18 IST on November 13, removing the
first temporary validation environment. Bluetooth was running but the phone was
not connected when work resumed. No reconnect or Bluetooth/HFP restart was
attempted; deployment compares against the current boot's service baseline.
Actual phone audio and spoken RAM/ping requests remain owner acceptance checks.

## Deployment result

Published and activated on November 13 at approximately 20:58 IST through the
existing managed plugin publication flow. Installed plugin and Hermes modules
were verified against the repaired sources before restarting only
`hermes-gateway.service`. The restarted gateway served authenticated native and
phone capability checks successfully, with sessions, compaction and parallel
phone tasks ready and no missing native interfaces.

Bluetooth, HFP and WirePlumber retained the same process IDs and start times
throughout deployment. The phone remained disconnected, as observed after the
earlier host reboot. No dialing, reconnection, Bluetooth reset, HFP restart or
host reboot was requested or executed during this repair.

Rollback copies are saved privately in the existing Hermes phone backup area
under `authorization-20261113-161240`; validation logs are in the repository's
ignored `.local-backups/phone-auth-validation/` directory.
