"""Install each release artifact in a fresh portable environment outside the checkout."""

from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import venv


def run(command, **kwargs):
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=300, **kwargs
    )
    if result.returncode:
        raise RuntimeError(result.stdout[-5000:] + result.stderr[-5000:])
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args()
    for artifact in args.artifacts:
        artifact = artifact.resolve()
        with tempfile.TemporaryDirectory(prefix="hfp-package-smoke-") as directory:
            root = Path(directory)
            target = root / "venv"
            venv.EnvBuilder(with_pip=True).create(target)
            python = str(target / "bin/python")
            env = {
                **os.environ,
                "HFP_MCP_ENV_FILE": str(root / "absent.env"),
                "HFP_MCP_CONFIG": str(root / "absent.yaml"),
                "HERMES_HOME": str(root),
                "PIP_CACHE_DIR": "/tmp/hfp-release-pip-cache",
            }
            env.pop("PYTHONPATH", None)
            print(f"Installing {artifact.name} into a fresh environment...", flush=True)
            run(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    str(artifact),
                ],
                cwd=root,
                env=env,
            )
            run([python, "-m", "pip", "check"], cwd=root, env=env)
            run(
                [
                    python,
                    "-c",
                    """import importlib.util
import hfp_mcp.hermes_bridge, hfp_mcp.phone_tasks, hfp_mcp.hermes_sessions
from importlib.metadata import version
assert version('hfp-mcp') == '0.1.0rc1'
for module in ('dbus', 'gi', 'mcp'):
    assert importlib.util.find_spec(module) is None, module
""",
                ],
                cwd=root,
                env=env,
            )
            run([str(target / "bin/hfp-mcp"), "doctor", "--help"], cwd=root, env=env)
            print(
                f"PASS: {artifact.name}: dependency check, portable imports, CLI",
                flush=True,
            )


if __name__ == "__main__":
    main()
