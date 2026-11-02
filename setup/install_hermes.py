#!/usr/bin/env python3
"""Install the optional phone plugin into an EXISTING Hermes home. No restart."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path


def managed_script_command(launcher, script: Path, arguments, *, env):
    """Keep Hermes's complete bootstrap, then execute a source installer/check."""
    resolved = subprocess.run(
        [str(launcher), "--print-runtime-command", "--module", "stat", "--",
         str(script), *arguments],
        env=env, check=True, capture_output=True, text=True,
    )
    command = json.loads(resolved.stdout)
    if (not isinstance(command, list) or not all(isinstance(arg, str) for arg in command)
            or len(command) < 5 or command[1:3] != ["-I", "-c"]):
        raise ValueError("Unsupported managed Hermes launcher command")
    # stat is a silent stdlib module. Let the published command bootstrap and
    # lease its dependencies unchanged before running our file (runpy's own
    # command-line interface accepts module names, not filesystem paths).
    command[3] += "; sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
    return command


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
            line if not line.strip() or line.lstrip().startswith("#") else "  # " + line[2:]
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


def stage_plugin(source: Path, destination: Path):
    """Make a buildable plugin whose supporting package travels with PM snapshots."""
    root = source.resolve().parent
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(
        root / "src/hfp_mcp", destination / "src/hfp_mcp",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    project = metadata["project"]
    # Only the portable package belongs in Hermes; extras and daemon CLI entry
    # points remain in the Bluetooth service's own environment.
    sections = {"build-system": metadata["build-system"], "project": {
        key: project[key] for key in (
            "name", "version", "description", "readme", "license", "license-files",
            "requires-python", "dependencies",
        )
    }, "tool.hatch.build.targets.wheel": metadata["tool"]["hatch"]["build"]["targets"]["wheel"]}
    text = "\n\n".join(
        f"[{name}]\n" + "\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items())
        for name, values in sections.items()
    )
    (destination / "pyproject.toml").write_text(text + "\n")
    for name in ("README.md", "LICENSE"):
        shutil.copy2(root / name, destination / name)
    if not (destination / "plugin.yaml").is_file() or not (destination / "__init__.py").is_file():
        raise ValueError("Incomplete phone plugin source")


def publish_plugin(home: Path, staged: Path):
    """PM owns code publication and the corresponding dependency generation."""
    from pm import sync_venv
    from pm.plugin_inputs import StagedUpdate
    from pm.store import tree_digest

    destination = home / "plugins/hfp-phone"
    metadata_path = home / "plugins/.install-metadata.json"
    previous = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    # This generated local payload is updated by this installer, not git-pulled
    # independently of the matching supporting package.
    record = {"installer": "hfp-mcp", "revision": tree_digest(staged)}
    sync_venv(explicit=True, plugins=StagedUpdate({
        "staged": str(staged), "target": str(destination),
        "old_metadata": previous, "new_metadata": {**previous, "hfp-phone": record},
        "target_digest": tree_digest(destination) if destination.exists() else None,
    }))


def select_plugin(home: Path, plugins: dict):
    from pm import sync_venv
    from pm.plugin_inputs import Selection

    sync_venv(explicit=True, plugins=Selection({
        "home": str(home), "enabled": plugins["enabled"],
        "disabled": plugins.get("disabled", []),
    }))


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
    destination = home / "plugins" / "hfp-phone"
    backup_root = home / "hfp-phone-backups" / str(time.time_ns())
    backup_root.mkdir(mode=0o700, parents=True)
    backup_root.parent.chmod(0o700)
    shutil.copy2(config_path, backup_root / "config.yaml")
    (backup_root / "config.yaml").chmod(0o600)
    # Backups are outside discovery. Leave the live target in place so PM can
    # compare-and-swap it and preserve its selection if dependency resolution fails.
    obsolete = [destination.parent / "hfp-call-awareness"]
    obsolete.extend(destination.parent.glob("hfp-phone.backup-*"))
    for old in [destination, *obsolete]:
        if old.exists():
            shutil.copytree(old, backup_root / old.name)
    with tempfile.TemporaryDirectory(prefix=".hfp-install-", dir=home) as staging:
        staged = Path(staging) / "hfp-phone"
        stage_plugin(source, staged)
        publish_plugin(home, staged)
        select_plugin(home, plugins)
        # Selection writes enabled/disabled lists transactionally. Read its
        # resulting config before changing only this plugin's obsolete settings.
        current = yaml.safe_load(config_path.read_text()) or {}
        current_plugins = current.setdefault("plugins", {})
        current_entries = current_plugins.setdefault("entries", {})
        current_entries.pop("hfp-call-awareness", None)
        # Adapter settings are not part of the API plugin's configuration.
        current_entries["hfp-phone"] = {"allow_tool_override": False}
        prepared_config = Path(staging) / "config.yaml"
        prepared_config.write_text(yaml.safe_dump(current, sort_keys=False))
        prepared_config.chmod(0o600)
        prepared_config.replace(config_path)
        for old in obsolete:
            if old.exists():
                shutil.rmtree(old)

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
        "--hermes", default="hermes", help="Managed Hermes launcher (default: hermes on PATH)"
    )
    parser.add_argument("--python", help=argparse.SUPPRESS)
    parser.add_argument("--managed-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--phone-config",
        type=Path,
        help="Routing YAML path; defaults to an existing setting or ~/.config/hfp-mcp/config.yaml",
    )
    args = parser.parse_args()
    if args.python:
        parser.error("--python is no longer supported; use --hermes with a managed Hermes launcher")
    if not (args.home.expanduser() / "config.yaml").is_file():
        parser.error("--home must name an existing Hermes profile")
    root = Path(__file__).resolve().parents[1]
    home = args.home.expanduser().resolve()
    if not args.managed_child:
        launcher = str(Path(args.hermes).expanduser()) if "/" in args.hermes else args.hermes
        arguments = ["--managed-child", "--home", str(home)]
        if args.phone_config:
            arguments.extend(["--phone-config", str(args.phone_config.expanduser().resolve())])
        env = {**os.environ, "HERMES_HOME": str(home)}
        try:
            command = managed_script_command(launcher, Path(__file__).resolve(), arguments, env=env)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            parser.error(f"Managed Hermes with --print-runtime-command support is required; check --hermes ({exc})")
        subprocess.run(command, env=env, check=True)
        return
    try:
        from pm import sync_venv
        from pm.plugin_inputs import Selection, StagedUpdate
    except ImportError as exc:
        parser.error(f"Managed Hermes with plugin publication support is required: {exc}")
    print(install_plugin(home, root / "hermes_phone_plugin"))
    print("Phone routing config:", configure_phone(
        home, root / "setup/phone.example.yaml", phone_config=args.phone_config,
    ))
    print(
        "Installed without restarting Hermes. Configure API/binding credentials and phone routes, then restart this profile's gateway. See docs/install.md."
    )


if __name__ == "__main__":
    main()
