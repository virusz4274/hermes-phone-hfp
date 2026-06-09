# HFP Role-Based Phone Permissions Implementation Plan

> **For Hermes:** This plan can be implemented directly or handed to Codex. Use TDD for behavior changes.

**Goal:** Replace the current HFP “unknown caller gets Hermes pairing code” behavior with a role-aware phone flow: admins can use full Hermes, while unknown callers get limited receptionist/appointment behavior.

**Architecture:** Keep Bluetooth pairing separate from Hermes authorization. The HFP adapter should assign caller identity/role metadata and enforce its own intake policy for limited callers, while the gateway should avoid generic DM pairing-code responses for the HFP phone platform. Start with a minimal safe implementation: suppress HFP pairing-code replies, add HFP caller role classification metadata, and add config/env switches for restart notifications and unknown caller behavior.

**Tech Stack:** Python, Hermes gateway platform adapter, pytest.

---

## Current context

- Repo: `/home/virusz4274/phone-bluetooth-hfp-mcp`
- HFP adapter: `hermes_platforms/hfp_phone/adapter.py`
- HFP plugin manifest: `hermes_platforms/hfp_phone/plugin.yaml`
- Tests: `tests/test_hfp_phone_plugin.py`
- Gateway auth behavior lives in Hermes source: `/home/virusz4274/.hermes/hermes-agent/gateway/run.py` and `/home/virusz4274/.hermes/hermes-agent/gateway/authz_mixin.py`
- Observed issue: gateway startup/shutdown notifications call the HFP home channel, then the HFP session can be treated as unauthorized and the generic Hermes DM pairing-code flow speaks a pairing code over the phone.

## Proposed minimal implementation

1. Do not call the phone for gateway restart notifications by default.
   - Set HFP plugin config default `gateway_restart_notification: false` if possible via plugin YAML/config bridge, or document/env it.
2. Make the HFP phone adapter an own-policy adapter.
   - Add `enforces_own_access_policy = True` to `HFPPhoneAdapter` so messages that reach the gateway are treated as adapter-authorized unless explicit gateway allowlists override.
3. Add caller role classification inside the HFP adapter.
   - `HFP_PHONE_ADMIN_CALLERS`: comma-separated caller IDs / Bluetooth addresses with full access.
   - `HFP_PHONE_TRUSTED_CALLERS`: comma-separated caller IDs / Bluetooth addresses with trusted limited access.
   - Unknown callers get role `unknown`.
   - Current HFP available identity is Bluetooth address (`22:22:D2:F8:01:7A`); future phone-number caller ID can be plugged in when exposed by the MCP state.
4. Add role metadata to HFP `MessageEvent.raw_message`.
   - Example: `{"source": "hfp-phone", "hfp_role": "unknown", "hfp_caller_id": "22:22:D2:F8:01:7A"}`.
5. Add unknown-caller prompt shaping.
   - For unknown callers, prefix transcribed text with a short system-like instruction for receptionist behavior, or attach metadata for a future gateway policy layer.
   - Minimal safe version: metadata only plus an HFP-specific default prompt string constant used in `_event` raw_message. Avoid over-broad tool-policy refactor now.
6. Tests.
   - Verify `_classify_caller_role` returns `admin`, `trusted`, or `unknown`.
   - Verify `HFPPhoneAdapter.enforces_own_access_policy` is true.
   - Verify `_event()` includes role metadata and caller ID.
   - Verify default plugin config disables gateway restart notifications if plugin YAML supports it.

## Files likely to change

- `hermes_platforms/hfp_phone/adapter.py`
- `hermes_platforms/hfp_phone/plugin.yaml`
- `tests/test_hfp_phone_plugin.py`
- Possibly `README.md` if documenting config.

## Tests / validation

Run targeted tests:

```bash
python -m pytest tests/test_hfp_phone_plugin.py -q
```

If dependencies permit, run broader repo tests:

```bash
python -m pytest tests/ -q
```

Manual verification after deployment:

```bash
hermes gateway restart
journalctl --user -u hfp-mcp -n 100 --no-pager
bluetoothctl info 22:22:D2:F8:01:7A
```

Expected behavior:

- Restarting gateway does not call HFP home phone by default.
- Unknown HFP caller no longer hears generic Hermes “pairing code” message.
- HFP event metadata contains caller role so future agent/policy prompts can restrict capabilities.

## Risks / tradeoffs

- Full per-role tool enforcement is a gateway-wide authorization feature and may require changes in Hermes core tool dispatch, not just this HFP plugin.
- Current HFP state exposes Bluetooth address, not cellular caller number. True unknown/external caller identification may require parsing phone CLCC/CLIP indicators from the HFP stack.
- `enforces_own_access_policy=True` means the adapter must be careful not to forward arbitrary unknown calls into full Hermes. Unknown callers should be handled by limited prompt/tool policy before enabling auto-answer broadly.

## Codex handoff recommendation

Use Hermes directly for this minimal plugin-level implementation. Hand to Codex if we decide to implement full gateway-wide role-based tool allowlists, because that crosses Hermes core authorization, tool registry, gateway session context, and tests.
