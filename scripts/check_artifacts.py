"""Check release completeness and high-confidence private-data patterns (no Git history)."""

from __future__ import annotations
import argparse
from pathlib import Path, PurePosixPath
import re
import tarfile
import zipfile

SECRET = re.compile(
    rb"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|AIza[0-9A-Za-z_-]{35}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,})"
)
ROOTS = ("src", "hermes_phone_plugin", "setup", "docs", "tests", "scripts")


def inspect_files(files):
    errors = []
    for name, content in files:
        parts = PurePosixPath(name).parts
        base = parts[-1]
        if any(
            p
            in {
                ".git",
                ".codex",
                ".agents",
                ".local-backups",
                "__pycache__",
                ".venv",
                "transcripts",
                "exports",
            }
            for p in parts
        ):
            errors.append(f"Private/generated path: {name}")
        if base in {".env", "control.token"} or base.endswith(
            (".db", ".sqlite3", ".sqlite", ".pyc", ".log", ".key", ".pem")
        ):
            errors.append(f"Unexpected runtime file: {name}")
        if SECRET.search(content):
            errors.append(f"Potential credential in {name} (value withheld)")
    return errors


def check(path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            files = [
                (n, archive.read(n)) for n in archive.namelist() if not n.endswith("/")
            ]
        names = {n for n, _ in files}
        required = {
            "hfp_mcp/hermes_bridge.py",
            "hfp_mcp/hermes_sessions.py",
            "hfp_mcp/phone_tasks.py",
            "hfp_mcp/http_routes.py",
            "hfp_mcp/audio/gemini_playback.py",
        }
        if not any(n.endswith("/licenses/LICENSE") for n in names):
            required.add("MIT license in wheel metadata")
    else:
        with tarfile.open(path) as archive:
            files = [
                (m.name.split("/", 1)[1], archive.extractfile(m).read())
                for m in archive.getmembers()
                if m.isfile() and "/" in m.name
            ]
        names = {n for n, _ in files}
        required = {
            "LICENSE",
            "README.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "CHANGELOG.md",
            "pyproject.toml",
            "hermes_phone_plugin/plugin.yaml",
            "hermes_phone_plugin/__init__.py",
            "setup/install.sh",
            "setup/install_hermes.py",
            "setup/host_config.py",
            "setup/phone.example.yaml",
            "docs/install.md",
            "scripts/check_hermes.py",
            "src/hfp_mcp/phone_tasks.py",
            "src/hfp_mcp/hermes_sessions.py",
        }
    return inspect_files(files) + [
        f"Missing release asset: {n}" for n in sorted(required - names)
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="*", type=Path)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    errors = []
    if args.source:
        files = [
            p
            for root in ROOTS
            for p in (args.source / root).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        ]
        files += [
            p
            for p in args.source.iterdir()
            if p.is_file() and p.suffix in {".md", ".toml"}
        ]
        errors += inspect_files(
            (str(p.relative_to(args.source)), p.read_bytes()) for p in files
        )
    for artifact in args.artifacts:
        errors += check(artifact)
    if not args.source and not args.artifacts:
        parser.error("Supply --source or release artifacts")
    if errors:
        raise SystemExit("\n".join(errors))
    print(
        "Release contents checked; no high-confidence credential/runtime-data matches. Git history was not scanned."
    )


if __name__ == "__main__":
    main()
