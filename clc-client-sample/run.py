#!/usr/bin/env python3
"""Run the standalone CLC paired-read workflow."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

PROJECT = Path(__file__).resolve().parent
REQUIRED_SETTINGS = (
    "CLC_HOST", "CLC_PORT", "CLC_USER", "CLC_PASSWORD", "CLC_PRESET",
    "CLC_WORKFLOW", "CLC_INSTALLER", "CLC_INSTALL_DIR", "CLC_INPUT_FILE",
)


class RunnerError(Exception):
    """Report a configuration, job input, or client installation error."""

    pass


def read_settings() -> dict[str, str]:
    """Load required settings from this folder's .env and the shell environment.

    Shell values take precedence. Return the required settings as strings,
    or raise RunnerError if any setting is missing or empty.
    """
    # Shell variables override the local .env, including Vault-supplied credentials.
    values = {**dotenv_values(PROJECT / ".env", interpolate=False), **os.environ}
    settings = {}
    for name in REQUIRED_SETTINGS:
        value = values.get(name)
        if not value:
            raise RunnerError(f"Set {name} in {PROJECT / '.env'} or the environment.")
        settings[name] = value
    return settings


def read_job(input_file: Path) -> tuple[str, str, str]:
    """Read the job JSON and return (read1, read2, destination).

    Require a JSON object with three non-empty string fields; leave URL and
    workflow-specific validation to CLC. Raise RunnerError for unreadable,
    invalid, or incomplete input.
    """
    try:
        job = json.loads(input_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RunnerError(f"Cannot read job input: {input_file}") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RunnerError("Job input must contain valid JSON.") from exc
    if not isinstance(job, dict) or not all(
        isinstance(job.get(key), str) and job[key].strip()
        for key in ("read1", "read2", "destination")
    ):
        raise RunnerError("Job input requires non-empty read1, read2, and destination strings.")
    return job["read1"], job["read2"], job["destination"]


def get_client(installer: Path, install_dir: Path, env: dict[str, str]) -> Path:
    """Return the executable clcserver path, installing it if needed.

    Reuse an existing executable. Otherwise, run the Bash installer quietly
    with the supplied environment and verify the installed executable.
    Raise RunnerError on installation failure or an incomplete installation;
    never overwrite an existing installation directory.
    """
    client = install_dir / "clcserver"
    if client.is_file() and os.access(client, os.X_OK):
        return client
    if install_dir.exists() or install_dir.is_symlink():
        raise RunnerError(
            "CLC installation directory exists but has no executable clcserver; inspect it before retrying."
        )
    if not installer.is_file():
        raise RunnerError("Cannot read the CLC Server Command Line Tools installer.")
    print("Installing CLC Server Command Line Tools...", file=sys.stderr)
    result = subprocess.run(
        ["bash", str(installer), "-q", "-dir", str(install_dir)],
        stdin=subprocess.DEVNULL, stdout=sys.stderr, env=env, check=False,
    )
    if result.returncode:
        raise RunnerError(f"CLC installer failed with status {result.returncode}.")
    if not client.is_file() or not os.access(client, os.X_OK):
        raise RunnerError("CLC installer finished without creating an executable clcserver.")
    return client


def main() -> int:
    """Load configuration and job input, then run the paired-read CLC workflow.

    Parse the optional installer override and output flag, locate or install
    the client, and wait for its command to finish without retrying. Suppress
    raw output unless --show-test-output is set.

    Return 0 on success, the CLI failure status (128 + signal if terminated
    by a signal), or 1 for configuration, input, installation, or OS errors.
    """
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("installer", nargs="?", type=Path, help="Override CLC_INSTALLER.")
        parser.add_argument(
            "--show-test-output", action="store_true",
            help="Display unfiltered CLC output; use only with synthetic, non-sensitive test data.",
        )
        args = parser.parse_args()
        settings = read_settings()

        # Resolve configured paths beside this script, not the working directory.
        installer = args.installer or PROJECT / settings["CLC_INSTALLER"]
        install_dir = PROJECT / settings["CLC_INSTALL_DIR"]
        read1, read2, destination = read_job(PROJECT / settings["CLC_INPUT_FILE"])
        # The CLI requires -W; do not also expose the password in its environment.
        env = os.environ.copy()
        env.pop("CLC_PASSWORD", None)
        client = get_client(installer, install_dir, env)
        command = [
            str(client), "-S", settings["CLC_HOST"], "-U", settings["CLC_USER"],
            "-W", settings["CLC_PASSWORD"], "-P", settings["CLC_PORT"],
            "-L", settings["CLC_PRESET"], "-A", settings["CLC_WORKFLOW"],
            "--reads-import-command", "ngs_import_illumina",
            "--reads-select-files", read1, "--reads-select-files", read2,
            "--reads-paired-reads", "true", "--destination-cloud", destination,
        ]
        # Wait for completion without logging potentially sensitive CLI output.
        status = subprocess.run(
            command, stdin=subprocess.DEVNULL, env=env, check=False,
            stdout=None if args.show_test_output else subprocess.DEVNULL,
            stderr=None if args.show_test_output else subprocess.STDOUT,
        ).returncode
        if status == 0:
            print("CLC command completed successfully.")
            return 0
        if status < 0:
            status = 128 - status
        print(
            f"CLC command failed with status {status}. "
            "Check CLC job history and restricted server logs before retrying.",
            file=sys.stderr,
        )
        return status
    except RunnerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Cannot run CLC tools: {exc}. Check CLC job history before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
