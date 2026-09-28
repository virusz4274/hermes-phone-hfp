# Public preview release validation

Version: **0.1.1rc1**. The preview targets Hermes users. Release artifacts and this
checklist prepare publication; no publishing action is part of these scripts.

## Reproduce automated checks

Create the development environment as described in [Contributing](../CONTRIBUTING.md).
The optional test constraints reproduce the ASGI transport combination used for
preview validation without constraining an existing Hermes environment:

```bash
.venv/bin/python -m pip install -e '.[daemon,dev,gemini-live]' -c setup/test-constraints.txt
.venv/bin/python -m pytest -q
bash -n setup/install.sh
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
.venv/bin/python scripts/check_artifacts.py --source . dist/*
python3 scripts/smoke_packages.py dist/*
```

Use the Python interpreter containing the tested Hermes installation:

```bash
<hermes-python> scripts/check_hermes.py --hermes-root /path/to/hermes-agent
```

The integration script uses a new temporary home, removes inherited credentials,
blocks external connections, and exercises real plugin registration, authenticated
capabilities, session creation/resumption, caller revocation, compaction lineage,
and deletion. Summaries are synthetic; it does not test model summary quality.

CI runs the daemon suite on Python 3.13. On PRs targeting `main`, release tags,
and manual runs, it also builds distributions, installs each artifact in a fresh
environment outside the checkout, and checks that the portable package imports
without MCP or native Bluetooth bindings. Python 3.11–3.13 is the supported range;
the current workflow does not test that full range or run the Hermes integration
script. Run the latter separately and record the exact Hermes commit tested.

## Release workflow

Feature PRs target `development`. Prepare each release there by updating package
and plugin versions, current documentation, and validation-script version checks,
then open a `development` → `main` PR titled
**Release 0.1.1rc1 — Public preview**. `main` remains the reviewed public preview
branch; a version tag provides an optional reproducible installation point.

`0.1.1rc1` is the first candidate for 0.1.1. Further candidates use `0.1.1rc2`,
`0.1.1rc3`, and so on; use `0.1.1` when ready for a final release. Keep the Git tag
(`v0.1.1rc1`) aligned with the package and plugin version (`0.1.1rc1`).

Preparing the PR does not merge it or publish anything. After release review:

1. Merge the approved PR using a merge commit to preserve the shared `development`/`main` history, then
   synchronize `development` with `main` before starting the next release cycle.
2. Tag the merged `main` commit as `v0.1.1rc1`; never move an already published tag.
3. Require successful checks on that tagged revision, including packaging.
4. Publish **0.1.1rc1 — Public preview** as a GitHub prerelease, with the changelog,
   upgrade requirements, validation evidence, known limits, wheel, and source archive.

Publication to PyPI is not part of this workflow. See [Maintenance](maintenance.md)
for the coordinated daemon/plugin upgrade and notes API compatibility change.

## Release gates

- Include all intended source and tests in the release commit, including new modules
  from the development checkout. Validate artifacts from that exact committed revision.
- Require successful CI before publishing. Local checks do not prove hosted CI passed.
- Inspect artifact contents and the current release source for private data. The
  supplied scanner detects high-confidence key patterns and runtime files; it is
  not a guarantee that no secret exists. Git history is deliberately not scanned.
- Keep the source archive: the wheel contains runtime modules, while host installers,
  plugin source, examples and documentation live in the source archive.
- Mark the GitHub release as a prerelease and include the changelog and these limits.
- Enable GitHub private vulnerability reporting if available to the repository owner.

## Hardware acceptance

Fresh-machine installation, live telephone calls, both voice backends, and latency/
intelligibility checks remain unverified for this preparation. Complete the
[hardware checklist](phone-routing.md#hardware-acceptance) before unattended use.
Record actual results in the compatibility table; do not turn an unperformed check
into a support claim.

## Local validation: 0.1.1rc1

Validated on 2026-09-28 in an isolated checkout on Linux aarch64 with Python 3.13.5:

- 524 tests passed using the test transport constraints.
- Native Hermes compatibility passed at `130b8f2c5dbca93a81aa396dd2ba44420d78f6f0`:
  plugin registration, capability/auth gates, session persistence/resumption, caller
  revocation, compaction lineage, and deletion in a disposable home with external
  connections blocked.
- Wheel/source builds and Twine validation passed. Both artifacts installed into
  separate fresh virtualenvs outside the checkout; `pip check`, version assertions,
  portable imports, and CLI help passed without MCP, dbus, or gi installed.
- Shell syntax and source/artifact content checks passed. Every runtime Python
  module is in the wheel; source, test, script, and plugin Python files are in the
  source archive. No high-confidence credential/runtime-data matches were found;
  Git history was not scanned.
- Package metadata, plugin manifest, runtime fallback, and validation-script version
  references agree on `0.1.1rc1`.

Hosted CI results belong to the final release PR revision and must be checked
separately. No host installer, service restart, live call, provider-backed memory
quality check, or successful end-to-end callback voice delivery was performed for
this preparation. The full live hardware acceptance matrix remains pending.

## Historical validation: 0.1.0rc1

Validated locally on Linux aarch64 with Python 3.13.5:

- 399 tests passed with no warnings using the test transport constraints.
- Real Hermes checks passed at `819988acb750836387fbb9d5d76203a9b3f530f4`: plugin registration, authenticated
  capabilities, native session persistence/resumption, revoked caller authority,
  no-op compaction, atomic compression-child lineage, and history deletion.
- Both wheel and source archive installed into fresh virtualenvs outside the checkout;
  `pip check`, portable imports, and CLI help passed without MCP, dbus, or gi installed.
- Wheel/source builds and Twine validation passed using Core Metadata 2.4.
- Installer `--check`, shell syntax, source/artifact content checks passed.
- Git-history scanning was skipped as requested. Hosted CI and live hardware/audio
  acceptance have not been run as part of this local preparation.

The native compression publication check uses synthetic summary text. Actual model
summarization quality and a full provider-backed compaction remain live checks.
