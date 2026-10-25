# Validation and compatibility

`main` is the install source and the branch checked by pull requests. CI runs
automated checks on pull requests to `main`, pushes to `main`, and manual runs.
It does not publish packages or create a release.

## Automated checks

Create the development environment as described in [Contributing](../CONTRIBUTING.md).
The optional test constraints reproduce the ASGI transport combination used for
local validation without constraining an existing Hermes environment:

```bash
.venv/bin/python -m pip install -e '.[daemon,dev,gemini-live]' -c setup/test-constraints.txt
.venv/bin/python -m pytest -q
bash -n setup/install.sh
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
.venv/bin/python scripts/check_artifacts.py --source . dist/*
python3 scripts/smoke_packages.py dist/*
```

The CI packaging job builds distributions on pull requests to `main` and on
manual runs. Its artifacts are checks for that commit, not published releases.
CI tests the daemon suite on Python 3.13; the supported Python range is
3.11–3.13, but the full range is not currently tested in CI.

Use the interpreter containing the Hermes installation to run the compatibility
check against that Hermes checkout:

```bash
<hermes-python> scripts/check_hermes.py --hermes-root /path/to/hermes-agent
```

This isolated check covers plugin registration and native phone workflow APIs.
It blocks external connections and does not place calls or send messages. It does
not establish live model quality or delivery behavior.

## Hardware and provider checks

Automated checks do not certify Bluetooth adapters, phones, providers, or call
quality. Before unattended use, perform the [hardware acceptance checks](phone-routing.md#hardware-acceptance)
and record the phone model and OS, controller, host OS, Hermes commit, voice
backend, and observed call behavior. Do not include caller IDs, Bluetooth
addresses, transcripts, or credentials in public reports.

The [compatibility table](compatibility.md) lists the supported software targets
and known limits. Dated local evidence is preserved in
[native workflow verification](hermes-owner-flow-verification-2026-10-08.md) and
[the demo readiness report](demo-readiness-2026-10-07.md); it describes those
specific checkouts and does not guarantee current `main` or hosted CI results.
