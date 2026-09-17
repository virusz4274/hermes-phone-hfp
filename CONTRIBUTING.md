# Contributing

Contributions are welcome, including bug reports, documentation, compatibility
reports, tests and new integrations. You do not need to be a Bluetooth expert to help.

For substantial changes, start with an issue describing the user-visible problem
or proposed behavior. Small fixes and documentation improvements can go straight
to a pull request. If you improve a fork or use Hermes Phone in another project,
please consider sharing generally useful changes here so other users can benefit.

For a fix, include a regression test that exercises the failure. Keep changes
focused and preserve caller isolation, exact-request approvals, cancellation,
and single ownership of Bluetooth/audio resources.

## Local development

Use Python 3.11–3.13 on Debian-family Linux. Install the native build dependencies
without running the Bluetooth installer or changing services:

```bash
sudo apt-get install python3-venv python3-dev build-essential pkg-config \
  libdbus-1-dev libglib2.0-dev libcairo2-dev libgirepository-2.0-dev
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[daemon,dev,gemini-live]' -c setup/test-constraints.txt
.venv/bin/python -m pytest -q
```

Bookworm ships older GLib bindings: install `python3-dbus python3-gi`, create the
venv with `--system-site-packages`, and use the distro bindings. The portable
Hermes plugin only needs `pip install -e '.[dev]'`; the full daemon suite needs
the native dependencies above. Tests use fake Bluetooth objects and need no phone.

Run `bash -n setup/install.sh` after installer edits. Test installer changes using
temporary directories and fake commands; do not run the real installer on CI.
See [release validation](docs/release.md) for artifact and Hermes integration checks.

## Pull requests

Explain the problem, changed behavior, and validation. Note any hardware checks
not performed. Do not commit real configuration, credentials, caller data, generated
artifacts, or local assistant settings. Public API changes need documentation and
compatibility tests. Contributions are provided under the project's MIT license.
