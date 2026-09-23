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
- Full transcripts are off by default. Route-level continuity is a separate opt-in
  that stores dialogue; active dialogue lasts until reset and archived dialogue has
  its own retention. Caller notes remain until explicitly forgotten. See
  [data retention](docs/phone-routing.md#conversation-continuity).
- Local files are private to the service account, not encrypted against that
  account or a machine administrator. Backups may retain deleted live data.

Automatic note closeout uses a separate private credential created from an
admitted call's binding. It cannot run tools, select another caller/profile, or
renew caller authority. Ordinary caller bindings remain revoked after hangup.
Extracted checkpoints may be merged for a bounded recovery period; current
routing, note-write permission, and forget generations are checked before commit.
Notes and dialogue remain untrusted input to a tools-disabled extraction model.
Checkpoint credentials stay in the private daemon ledger and are never included
in voice context or public status. Forgetting notes removes pending gateway
checkpoints and their credentials, so recovery cannot restore forgotten facts.
