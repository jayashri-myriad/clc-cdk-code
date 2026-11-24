"""Bounded CLC CLI requests without exposing commands or private output."""

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class InvalidInput(Exception):
    pass


class ConfigurationError(Exception):
    pass


class SubmissionUncertain(Exception):
    pass


class MonitoringError(Exception):
    pass


class TransientStatusError(Exception):
    pass


def validate_job_id(value: object) -> str:
    """Return a usable process ID, rejecting blank IDs and CLI control commands."""
    # Reject values that the CLI could interpret as options or cancellation commands.
    if (
        not isinstance(value, str) or not value
        or any(character.isspace() for character in value)
        or value.startswith("-") or value in {"cancel", "cancel-all"}
    ):
        raise InvalidInput("Invalid CLC process ID.")
    return value


def response_field(output: str, name: str) -> str:
    """Extract one named field from CLC text output, such as Process ID.

    Accept plain fields and the Message wrapper. Missing, empty, or duplicate
    fields raise an error because the response cannot be interpreted safely.
    """
    values = []
    for line in output.splitlines():
        field, separator, value = line.strip().partition(":")
        if field == "Message":
            # CLC sometimes wraps fields as "Message: Process ID: <id>".
            field, separator, value = value.strip().partition(":")
        if separator and field.strip() == name:
            values.append(value.strip())
    if len(values) != 1 or not values[0]:
        raise ValueError(f"No unique {name} field.")
    return values[0]


@dataclass(frozen=True)
class Connection:
    host: str
    port: str
    user: str
    password: str = field(repr=False)
    executable: str

    @classmethod
    def from_environment(cls) -> "Connection":
        """Load the server login and executable path from Lambda configuration.

        Check that required values exist and the CLI is executable. This does not
        connect to the server or prove that the credentials are accepted.
        """
        names = ("CLC_HOST", "CLC_PORT", "CLC_USER", "CLC_PASSWORD")
        if any(not os.environ.get(name) for name in names):
            raise ConfigurationError("CLC connection settings are incomplete.")
        port = os.environ["CLC_PORT"]
        if not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ConfigurationError("CLC_PORT must be a valid TCP port.")
        executable = os.environ.get("CLC_EXECUTABLE", "/opt/clc/clcserver")
        if not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise ConfigurationError("The installed CLC executable is unavailable.")
        return cls(
            os.environ["CLC_HOST"], port, os.environ["CLC_USER"],
            os.environ["CLC_PASSWORD"], executable,
        )

    def command(self) -> list[str]:
        """Build the common CLI connection arguments used by submit/status requests.

        The vendor requires the password in -W, so never log this argument list.
        """
        return [
            self.executable, "-S", self.host, "-P", self.port,
            "-U", self.user, "-W", self.password,
        ]


def execute(connection: Connection, arguments: list[str], timeout: float) -> str:
    """Run one CLI request within the supplied timeout and return its stdout.

    Capture stdout/stderr without printing them. A nonzero exit raises an error;
    this helper never retries or decides whether an analysis finished.
    """
    environment = os.environ.copy()
    environment.pop("CLC_PASSWORD", None)
    # Capture output instead of logging private server details or the command's password.
    result = subprocess.run(
        [*connection.command(), *arguments],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        check=False, env=environment,
    )
    if result.returncode:
        raise ValueError("CLC returned a nonzero exit status; consult restricted job history.")
    return result.stdout


def submit(
    connection: Connection, workflow: str, preset: str,
    read1: str, read2: str, destination: str,
) -> str:
    """Start the paired-read cloud workflow asynchronously and return its process ID.

    Send one -Y request, bounded to 60 seconds. If the ID cannot be confirmed,
    raise SubmissionUncertain rather than trying again and risking a duplicate.
    """
    try:
        # -Y returns after submission, without waiting for the analysis to finish.
        output = execute(connection, [
            "-Y", "-L", preset, "-A", workflow,
            "--reads-import-command", "ngs_import_illumina",
            "--reads-select-files", read1, "--reads-select-files", read2,
            "--reads-paired-reads", "true", "--destination-cloud", destination,
        ], 60)
        return validate_job_id(response_field(output, "Process ID"))
    except (OSError, subprocess.TimeoutExpired, ValueError, InvalidInput):
        # A missing response does not prove rejection; retrying could create a second job.
        raise SubmissionUncertain(
            "CLC may have accepted the job. Reconcile job history before retrying."
        ) from None


def check_status(connection: Connection, job_id: str, timeout: float) -> str:
    """Query one process ID and return running, successful, or failed.

    Check that CLC returned the same ID and valid completion/error fields.
    A request timeout is retryable; other query/response failures stop monitoring.
    No waiting loop, cancellation, or result retrieval happens here.
    """
    validate_job_id(job_id)
    try:
        # -I only reads status; result retrieval with -R belongs in a separate step.
        output = execute(connection, ["-I", job_id], timeout)
        if response_field(output, "Job ID") != job_id:
            raise ValueError("Mismatched job ID.")
        done = response_field(output, "Job is done").casefold()
        errors = response_field(output, "Job has errors").casefold()
        if done not in {"yes", "no", "true", "false"} or errors not in {
            "yes", "no", "true", "false",
        }:
            raise ValueError("Unrecognized status.")
    except subprocess.TimeoutExpired:
        raise TransientStatusError("CLC status request timed out.") from None
    except (OSError, ValueError):
        raise MonitoringError(
            "Cannot validate CLC status; consult restricted server logs."
        ) from None
    if done in {"no", "false"}:
        return "running"
    return "failed" if errors in {"yes", "true"} else "successful"
