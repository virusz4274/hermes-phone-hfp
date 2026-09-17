#!/usr/bin/env python3
"""Install the optional phone plugin into an EXISTING Hermes home. No restart."""

import argparse
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def configure_phone(home: Path, example: Path, *, phone_config=None, service_env=None):
    """Choose one routing file, seed it without caller grants, and connect both envs."""
    home = home.expanduser().resolve()
    if not (home / "config.yaml").is_file():
        raise ValueError("Choose an existing Hermes home")
    service_env = (
        Path(service_env or os.getenv("HFP_MCP_ENV_FILE", "~/.config/hfp-mcp.env"))
        .expanduser()
        .resolve()
    )
    hermes_env = home / ".env"

    def configured_path(content):
        paths = []
        for line in content.splitlines():
            key, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator and key.strip() == "HFP_MCP_CONFIG":
                parsed = shlex.split(value, comments=True)
                if len(parsed) != 1:
                    raise ValueError("HFP_MCP_CONFIG must contain one path")
                paths.append(Path(parsed[0]).expanduser().resolve())
        if len(set(paths)) > 1:
            raise ValueError(
                "Conflicting HFP_MCP_CONFIG assignments; choose --phone-config explicitly"
            )
        return paths[-1] if paths else None

    contents = {
        path: path.read_text() if path.exists() else ""
        for path in (service_env, hermes_env)
    }
    configured = (
        []
        if phone_config
        else [configured_path(content) for content in contents.values()]
    )
    inherited = os.getenv("HFP_MCP_CONFIG")
    if inherited:
        configured.append(Path(inherited).expanduser().resolve())
    candidates = {path for path in configured if path}
    if phone_config is None and len(candidates) > 1:
        raise ValueError(
            "Daemon and Hermes use different routing paths; select --phone-config /absolute/path explicitly"
        )
    selected = (
        Path(phone_config).expanduser().resolve()
        if phone_config
        else next(iter(candidates), Path.home() / ".config/hfp-mcp/config.yaml")
    )
    if any(char in str(selected) for char in ("\n", "\r", "\x00")):
        raise ValueError("Invalid routing path")
    # Quoted assignment supports spaces in either environment-file format.
    value = str(selected).replace("\\", "\\\\").replace('"', '\\"')
    assignment = f'HFP_MCP_CONFIG="{value}"\n'
    if not selected.exists():
        selected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        template = example.read_text()
        # Keep the illustrative route visible, but never authorize a sample number.
        before, route = template.split("  numbers:\n", 1)
        number_lines, after = route.split("  # No default:", 1)
        template = (
            before
            + "  numbers: {}\n  # Add your real number here using this structure:\n  # numbers:\n"
        )
        template += "".join(
            "  # " + line[2:] if line.strip() else line
            for line in number_lines.splitlines(keepends=True)
        )
        template += "  # No default:" + after
        fd = os.open(selected, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(template)
    elif not selected.is_file():
        raise ValueError("Routing path must be a file")

    backup = home / "hfp-phone-backups" / ("routing-path-" + str(time.time_ns()))
    for path, original in contents.items():
        lines = original.splitlines(keepends=True)
        updated = "".join(
            line
            for line in lines
            if line.strip().removeprefix("export ").partition("=")[0].strip()
            != "HFP_MCP_CONFIG"
        )
        if updated and not updated.endswith("\n"):
            updated += "\n"
        updated += assignment
        if updated == original:
            continue
        if path.exists():
            backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            saved = backup / ("daemon.env" if path == service_env else "hermes.env")
            shutil.copy2(path, saved)
            saved.chmod(0o600)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".hfp-env-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(updated)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return selected


def install_plugin(home: Path, source: Path):
    import yaml

    home = home.expanduser().resolve()
    config_path = home / "config.yaml"
    if not config_path.is_file():
        raise ValueError(
            "Choose an existing Hermes home; this installer does not create profiles"
        )
    config = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(config, dict):
        raise ValueError("Hermes config must be a YAML mapping")
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("Hermes plugins config must be a mapping")
    enabled = plugins.get("enabled", [])
    disabled = plugins.get("disabled", [])
    entries = plugins.get("entries", {})
    if (
        not isinstance(enabled, list)
        or not isinstance(disabled, list)
        or not isinstance(entries, dict)
    ):
        raise ValueError("Invalid Hermes plugin lists or entries")
    plugins["enabled"] = [
        name for name in enabled if name not in {"hfp-phone", "hfp-call-awareness"}
    ] + ["hfp-phone"]
    if "disabled" in plugins:
        plugins["disabled"] = [
            name for name in disabled if name not in {"hfp-phone", "hfp-call-awareness"}
        ]
    entries.pop("hfp-call-awareness", None)
    # Adapter settings are not part of the API plugin's configuration.
    entries["hfp-phone"] = {"allow_tool_override": False}
    plugins["entries"] = entries
    destination = home / "plugins" / "hfp-phone"
    backup_root = home / "hfp-phone-backups" / str(time.time_ns())
    backup_root.mkdir(mode=0o700, parents=True)
    backup_root.parent.chmod(0o700)
    shutil.copy2(config_path, backup_root / "config.yaml")
    (backup_root / "config.yaml").chmod(0o600)
    moved = []
    installed = False
    # Stage the complete plugin before touching discoverable plugin directories.
    with tempfile.TemporaryDirectory(prefix=".hfp-install-", dir=home) as staging:
        staged = Path(staging) / "hfp-phone"
        shutil.copytree(
            source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        if (
            not (staged / "plugin.yaml").is_file()
            or not (staged / "__init__.py").is_file()
        ):
            raise ValueError("Incomplete phone plugin source")
        prepared_config = Path(staging) / "config.yaml"
        prepared_config.write_text(yaml.safe_dump(config, sort_keys=False))
        prepared_config.chmod(0o600)
        destination.parent.mkdir(parents=True, exist_ok=True)
        obsolete = [destination, destination.parent / "hfp-call-awareness"]
        obsolete.extend(destination.parent.glob("hfp-phone.backup-*"))
        try:
            for old in obsolete:
                if old.exists():
                    saved = backup_root / old.name
                    shutil.move(str(old), str(saved))
                    moved.append((old, saved))
            shutil.move(str(staged), str(destination))
            installed = True
            prepared_config.replace(config_path)
        except Exception:
            if installed:
                shutil.rmtree(destination)
            for old, saved in reversed(moved):
                shutil.move(str(saved), str(old))
            raise

    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home",
        required=True,
        type=Path,
        help="Existing Hermes home (default or named profile)",
    )
    parser.add_argument(
        "--python", required=True, type=Path, help="Hermes virtualenv Python executable"
    )
    parser.add_argument(
        "--phone-config",
        type=Path,
        help="Routing YAML path; defaults to an existing setting or ~/.config/hfp-mcp/config.yaml",
    )
    args = parser.parse_args()
    if not (args.home.expanduser() / "config.yaml").is_file():
        parser.error("--home must name an existing Hermes profile")
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [str(args.python.expanduser()), "-m", "pip", "install", str(root)],
        check=True,
    )
    # Configuration is written with the selected interpreter, where dependencies
    # were installed; the launcher may be a stock Python without PyYAML.
    subprocess.run(
        [
            str(args.python.expanduser()),
            "-c",
            "import runpy, sys; import hfp_mcp.hermes_bridge; from pathlib import Path; "
            "setup = runpy.run_path(sys.argv[1]); "
            "print('Phone routing config:', setup['configure_phone'](Path(sys.argv[2]), Path(sys.argv[4]), phone_config=sys.argv[5] or None)); "
            "print(setup['install_plugin'](Path(sys.argv[2]), Path(sys.argv[3])))",
            str(Path(__file__).resolve()),
            str(args.home.expanduser()),
            str(root / "hermes_phone_plugin"),
            str(root / "setup/phone.example.yaml"),
            str(args.phone_config) if args.phone_config else "",
        ],
        check=True,
    )
    print(
        "Installed without restarting Hermes. Configure API/binding credentials and phone routes, then restart this profile's gateway. See docs/install.md."
    )


if __name__ == "__main__":
    main()
