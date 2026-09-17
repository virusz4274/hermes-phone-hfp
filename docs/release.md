# Public preview release validation

Version: **0.1.0rc1**. The preview targets Hermes users. Release artifacts and this
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

CI runs the daemon suite on Python 3.11–3.13, builds distributions, installs each
artifact in a fresh environment outside the checkout, and checks that the portable
package imports without MCP or native Bluetooth bindings. It also runs the Hermes
integration script against the commit in [compatibility](compatibility.md).

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

## Local validation record

Validated locally on Linux aarch64 with Python 3.13.5:

- 399 tests passed with no warnings using the test transport constraints.
- Real Hermes checks passed at the pinned commit: plugin registration, authenticated
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
