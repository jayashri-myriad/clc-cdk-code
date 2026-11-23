#!/usr/bin/env python3
"""Submit a CLC cloud workflow asynchronously or poll an existing job."""

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from run import (
    PROJECT, RunnerError, connection_args, get_client, read_job,
    read_settings, workflow_args,
)

REQUEST_TIMEOUT = 60


def positive_seconds(value: str) -> float:
    """Parse a finite, positive polling interval or overall timeout in seconds."""
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use a positive number of seconds.") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Use a finite, positive number of seconds.")
    return seconds


def validate_job_id(job_id: str) -> str:
    """Reject empty IDs and values that CLC could interpret as options or cancellation."""
    if (
        not job_id or any(character.isspace() for character in job_id)
        or job_id.startswith("-") or job_id in {"cancel", "cancel-all"}
    ):
        raise RunnerError("Invalid CLC job ID.")
    return job_id


def response_field(output: str, name: str) -> str:
    """Read one CLI field, allowing the Message: wrapper used for process IDs."""
    values = []
    for line in output.splitlines():
        field, separator, value = line.strip().partition(":")
        if field == "Message":
            field, separator, value = value.strip().partition(":")
        if separator and field.strip() == name:
            values.append(value.strip())
    if len(values) != 1 or not values[0]:
        raise RunnerError(
            f"CLC response has no unique {name} field; check job history before retrying."
        )
    return values[0]


def execute(command: list[str], env: dict[str, str], timeout: float, action: str) -> str:
    """Run one bounded CLI request and return stdout only when its exit status is zero.

    Capture output instead of logging credentials or private job details.
    Never retry a request; let the caller handle OS errors and timeouts.
    """
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=env,
        timeout=timeout, check=False,
    )
    if result.returncode:
        raise RunnerError(
            f"{action} failed with status {result.returncode}; "
            "check CLC job history and restricted server logs before retrying."
        )
    return result.stdout


def submit(command: list[str], settings: dict[str, str], job: tuple[str, str, str],
           env: dict[str, str]) -> str:
    """Submit once with -Y and return the process ID, not a claim of job completion."""
    output = execute(
        [*command, "-Y", *workflow_args(settings, job)],
        env, REQUEST_TIMEOUT, "Submission",
    )
    return validate_job_id(response_field(output, "Process ID"))


def report(job_id: str, status: str, **details: str) -> None:
    """Print a machine-readable submission or terminal status response to stdout."""
    print(json.dumps({"job_id": job_id, "status": status, **details}), flush=True)


def report_timeout(job_id: str) -> int:
    """Report an expired polling deadline without claiming the server job stopped."""
    report(job_id, "timeout")
    print("Polling timed out; the job may still be running.", file=sys.stderr)
    return 2


def poll(command: list[str], job_id: str, env: dict[str, str], interval: float,
         timeout: float, results_file: Path) -> int:
    """Query -I until completion, failure, or the overall polling deadline.

    Verify the job ID and Yes/No status fields (also accept true/false). On error-free
    completion, retrieve results once with -R into a new owner-only file.
    Never overwrite an existing file or automatically retry result retrieval.
    Return 0 for success, 1 for job failure, or 2 for a polling timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return report_timeout(job_id)
        try:
            output = execute(
                [*command, "-I", job_id], env,
                min(REQUEST_TIMEOUT, remaining), "Status query",
            )
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                return report_timeout(job_id)
            raise
        if response_field(output, "Job ID") != job_id:
            raise RunnerError("CLC returned a different job ID; job outcome is unknown.")
        done = response_field(output, "Job is done").casefold()
        errors = response_field(output, "Job has errors").casefold()
        boolean_values = {"yes", "no", "true", "false"}
        if done not in boolean_values or errors not in boolean_values:
            raise RunnerError("CLC returned an unrecognized job status; outcome is unknown.")
        if done in {"yes", "true"}:
            if errors in {"yes", "true"}:
                report(job_id, "failed")
                print("Job failed; check CLC job history and restricted server logs.", file=sys.stderr)
                return 1
            # Reserve the output path before -R consumes the retrievable result record.
            descriptor = os.open(results_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output_file:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return report_timeout(job_id)
                try:
                    results = execute(
                        [*command, "-R", job_id], env,
                        min(REQUEST_TIMEOUT, remaining), "Result retrieval",
                    )
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        return report_timeout(job_id)
                    raise
                output_file.write(results)
            report(job_id, "successful", results_file=str(results_file))
            return 0
        print(f"Job {job_id} is queued or running.", file=sys.stderr)
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def main() -> int:
    """Load this folder's .env and run either submit or poll.

    Keep progress/errors on stderr and JSON responses on stdout. Return 2
    for runner or monitoring errors, or 130 for an interrupted client.
    Neither timeouts nor interruption cancel or resubmit the server job.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("submit", help="Submit the configured paired-read workflow once.")
    polling = modes.add_parser("poll", help="Wait for an existing job and retrieve its results.")
    polling.add_argument("job_id", help="Process ID returned by submit.")
    polling.add_argument("--interval", type=positive_seconds, default=30, help="Polling interval in seconds (default: 30).")
    polling.add_argument("--timeout", type=positive_seconds, default=86400, help="Overall timeout in seconds (default: 86400).")
    polling.add_argument("--results-file", type=Path, required=True, help="New local file for the final CLI result summary.")
    args = parser.parse_args()
    try:
        if args.mode == "poll":
            validate_job_id(args.job_id)
        settings = read_settings()
        job = read_job(PROJECT / settings["CLC_INPUT_FILE"]) if args.mode == "submit" else None
        env = os.environ.copy()
        env.pop("CLC_PASSWORD", None)
        client = get_client(
            PROJECT / settings["CLC_INSTALLER"],
            PROJECT / settings["CLC_INSTALL_DIR"], env,
        )
        command = [str(client), *connection_args(settings)]
        if job is not None:
            report(submit(command, settings, job, env), "submitted")
            return 0
        return poll(command, args.job_id, env, args.interval, args.timeout, args.results_file)
    except RunnerError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("CLC request timed out; job outcome is unknown. Check job history before retrying.", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            f"Cannot run CLC tools or write results ({exc.strerror}). "
            "Check local paths and job history before retrying.",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        print("Interrupted; the server job may still be running. Check job history before retrying.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
