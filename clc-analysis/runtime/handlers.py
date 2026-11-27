"""Lambda entry points for per-sample submission and one-shot monitoring."""

import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urlsplit

from botocore.exceptions import BotoCoreError, ClientError

from cli import (
    ConfigurationError, Connection, InvalidInput, MonitoringError,
    TransientStatusError, check_status, submit, validate_job_id,
)
from ledger import Ledger, ReconciliationRequired, RequestConflict

POLL_INTERVAL_SECONDS = 300


class LambdaContext(Protocol):
    invoked_function_arn: str


class LedgerUnavailable(Exception):
    pass


def now() -> datetime:
    """Return the current UTC time used to calculate monitoring deadlines."""
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    """Format a UTC time as a JSON-friendly deadline, such as 2026-11-25T16:00:00Z."""
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def deadline_value(value: object) -> datetime:
    """Convert a deadline string to a time that can be compared with the current time.

    Reject missing, invalid, or non-UTC deadlines instead of guessing a timezone.
    """
    if not isinstance(value, str):
        raise InvalidInput("A UTC monitoring deadline is required.")
    try:
        deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidInput("Invalid monitoring deadline.") from None
    if deadline.tzinfo is None or deadline.utcoffset() != timedelta(0):
        raise InvalidInput("Monitoring deadline must use UTC.")
    return deadline


def identifier(event: dict, key: str) -> str:
    """Read a request/sample ID from the input and reject unsupported characters.

    Return a valid ID of up to 128 characters; raise InvalidInput otherwise.
    """
    value = event.get(key)
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise InvalidInput(f"{key} must be a non-empty, safe correlation identifier.")
    return value


def s3_location(value: object, prefixes: list[str], destination: bool = False) -> str:
    """Check that an S3 path is inside one of the configured allowed prefixes.

    Output destinations must end in '/'. Return the accepted path without checking
    object existence or granting access to the bucket.
    """
    if not isinstance(value, str):
        raise InvalidInput("S3 locations must be strings.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/")
        or parsed.query or parsed.fragment or any(character.isspace() for character in value)
        or not any(value.startswith(prefix) for prefix in prefixes)
        or (destination and not value.endswith("/"))
    ):
        raise InvalidInput("Use an approved S3 location and a destination ending in '/'.")
    return value


def prefixes(name: str) -> list[str]:
    """Read an allowed-prefix list from a Lambda environment variable.

    For example, INPUT_PREFIXES contains a JSON array of approved S3 locations.
    Missing or invalid configuration stops processing rather than allowing all paths.
    """
    try:
        values = json.loads(os.environ[name])
    except (KeyError, json.JSONDecodeError):
        raise ConfigurationError(f"Configure {name} as a JSON list of S3 prefixes.") from None
    if (
        not isinstance(values, list) or not values
        or any(
            not isinstance(value, str) or not value.startswith("s3://")
            or not value.endswith("/") or not urlsplit(value).netloc
            for value in values
        )
    ):
        raise ConfigurationError(f"{name} requires non-empty S3 prefixes ending in '/'.")
    return values


def environment_integer(name: str, minimum: int, maximum: int) -> int:
    """Read a required numeric setting, such as the active-job limit.

    Return it as an integer only if it is within the allowed range.
    """
    value = os.environ.get(name, "")
    if not value.isdecimal() or not minimum <= int(value) <= maximum:
        raise ConfigurationError(f"{name} is missing or outside its allowed range.")
    return int(value)


def server_identity(connection: Connection) -> str:
    """Create a stable tracking key from the server address, port, and service user.

    This keeps records/capacity separate for different server/account combinations.
    The password is not part of this key.
    """
    return hashlib.sha256(
        json.dumps([connection.host, connection.port, connection.user]).encode()
    ).hexdigest()


def get_ledger(connection: Connection) -> Ledger:
    """Open the configured DynamoDB tracking table for this server/account.

    Return a Ledger helper; this does not create the table or submit a CLC job.
    """
    name = os.environ.get("LEDGER_TABLE")
    if not name:
        raise ConfigurationError("LEDGER_TABLE is required.")
    return Ledger(name, server_identity(connection))


def verify_record(record: dict, ledger: Ledger) -> None:
    """Require a saved request for this server/account with a known CLC process ID.

    If the ID is missing, require an operator to check CLC job history. Starting
    another job could duplicate a submission that already reached the server.
    """
    if not record or record.get("server_id") != {"S": ledger.server_id}:
        raise ReconciliationRequired("No matching request exists for this server/account.")
    if "job_id" not in record:
        raise ReconciliationRequired(
            "Submission has no recorded process ID. Reconcile CLC job history; do not resubmit."
        )


def analysis_response(request_id: str, record: dict, deadline: str) -> dict:
    """Build the job context that Step Functions passes between polling steps.

    Include the sample ID, CLC process ID, saved status, deadline, and next wait.
    This only builds a response; it does not query CLC or change the saved record.
    """
    # Shorten the next wait when less than five minutes remain in the deadline.
    return {
        "request_id": request_id, "sample_id": record["sample_id"]["S"],
        "job_id": record["job_id"]["S"], "deadline": deadline,
        "status": record["analysis_status"]["S"],
        "wait_seconds": min(
            POLL_INTERVAL_SECONDS,
            max(0, math.ceil((deadline_value(deadline) - now()).total_seconds())),
        ),
    }


def prepare(event: dict, context: LambdaContext) -> dict:
    """Submit a new analysis safely, or return an existing job for monitoring.

    Monitor mode uses a saved process ID with a fresh monitoring budget.
    Submit mode validates FASTQ paths and checks for duplicates, then reserves a
    capacity slot before calling CLC. Save the returned ID before reporting success.
    """
    request_id = identifier(event, "request_id")
    connection = Connection.from_environment()
    ledger = get_ledger(connection)
    budget = environment_integer("MONITORING_SECONDS", 60, 86400)
    mode = event.get("mode", "submit")
    if mode not in {"submit", "monitor"}:
        raise InvalidInput("Mode must be submit or monitor.")
    record = ledger.get(request_id)
    if mode == "monitor":
        # Resume a recorded job with a new monitoring budget; never submit it again.
        verify_record(record, ledger)
        return analysis_response(request_id, record, timestamp(now() + timedelta(seconds=budget)))

    sample_id = identifier(event, "sample_id")
    read1 = s3_location(event.get("read1"), prefixes("INPUT_PREFIXES"))
    read2 = s3_location(event.get("read2"), prefixes("INPUT_PREFIXES"))
    destination = s3_location(event.get("destination"), prefixes("OUTPUT_PREFIXES"), True)
    if read1 == read2:
        raise InvalidInput("Paired read locations must be distinct.")
    workflow, preset = os.environ.get("CLC_WORKFLOW"), os.environ.get("CLC_PRESET")
    if not workflow or not preset:
        raise ConfigurationError("Configure the approved workflow and preset.")
    # Detect accidental reuse of a request ID for different inputs or workflow settings.
    fingerprint = hashlib.sha256(json.dumps([
        sample_id, read1, read2, destination, workflow, preset, ledger.server_id,
    ]).encode()).hexdigest()
    if record:
        if record.get("fingerprint") != {"S": fingerprint}:
            raise RequestConflict("Request ID was used with different inputs/configuration.")
        verify_record(record, ledger)
        # A normal duplicate keeps the original job and deadline.
        return analysis_response(request_id, record, record["deadline"]["S"])

    deadline = timestamp(now() + timedelta(seconds=budget))
    execution_arn = event.get("execution_arn", context.invoked_function_arn)
    ledger.reserve(
        request_id, sample_id, fingerprint, execution_arn, deadline,
        environment_integer("MAX_ACTIVE_ANALYSES", 1, 10),
    )
    # Keep this slot after an uncertain submission: CLC may already be running the job.
    job_id = submit(connection, workflow, preset, read1, read2, destination)
    deadline = timestamp(now() + timedelta(seconds=budget))
    ledger.submitted(request_id, job_id, deadline)
    return {
        "request_id": request_id, "sample_id": sample_id, "job_id": job_id,
        "deadline": deadline, "status": "submitted",
        "wait_seconds": POLL_INTERVAL_SECONDS,
    }


def submit_handler(event: dict, context: LambdaContext) -> dict:
    """Handle a Submit Lambda invocation from Step Functions or a direct caller.

    Unwrap the workflow input and call prepare(). Report tracking-table failures
    without retrying submission, since CLC may already have accepted the job.
    """
    try:
        if not isinstance(event, dict):
            raise InvalidInput("Request must be a JSON object.")
        if "request" in event:
            # Step Functions wraps the sample input with its execution ARN for tracking.
            request = event["request"]
            if not isinstance(request, dict):
                raise InvalidInput("Request must be a JSON object.")
            event = {**request, "execution_arn": event["execution_arn"]}
        return prepare(event, context)
    except (BotoCoreError, ClientError):
        raise LedgerUnavailable(
            "Cannot confirm the request ledger. Reconcile before resubmission."
        ) from None


def monitor(event: dict) -> dict:
    """Perform one monitoring action for a request's recorded CLC process ID.

    Return a saved terminal result, report an expired deadline, or query CLC once.
    Confirmed completion frees capacity; monitoring errors/timeouts do not.
    The failure-recording action only updates monitoring status, without querying CLC.
    """
    if not isinstance(event, dict):
        raise InvalidInput("Status request must be a JSON object.")
    request_id = identifier(event, "request_id")
    job_id = validate_job_id(event.get("job_id"))
    connection = Connection.from_environment()
    ledger = get_ledger(connection)
    record = ledger.get(request_id)
    verify_record(record, ledger)
    if record["job_id"]["S"] != job_id:
        raise InvalidInput("Process ID does not match the recorded request.")
    action = event.get("action", "check")
    if action == "record_monitoring_failure":
        ledger.monitoring(request_id, "failed")
        return {"job_id": job_id, "status": "monitoring_failed"}
    if action != "check":
        raise InvalidInput("Unknown monitoring action.")
    deadline = timestamp(deadline_value(event.get("deadline")))
    remaining = (deadline_value(deadline) - now()).total_seconds()
    response = analysis_response(request_id, record, deadline)
    if record.get("slot_held") == {"BOOL": False}:
        # A confirmed terminal result is already saved; no further CLI query is needed.
        return response
    if remaining <= 0:
        # Stop monitoring, but keep capacity reserved because the job may still be running.
        ledger.monitoring(request_id, "timed_out")
        return {**response, "status": "timeout", "wait_seconds": 0}
    try:
        # Make one query only; Step Functions decides when to call again.
        status = check_status(connection, job_id, min(60, remaining))
    except TransientStatusError:
        raise
    except MonitoringError:
        ledger.monitoring(request_id, "failed")
        raise
    if status in {"successful", "failed"}:
        ledger.complete(request_id, job_id, status)
    elif (deadline_value(event.get("deadline")) - now()).total_seconds() <= 0:
        ledger.monitoring(request_id, "timed_out")
        status = "timeout"
    else:
        ledger.monitoring(request_id, "active")
    response = analysis_response(request_id, record, deadline)
    response["status"] = status
    if status == "timeout":
        response["wait_seconds"] = 0
    return response


def status_handler(event: dict, context: LambdaContext) -> dict:
    """Handle a Status Lambda invocation and return one monitoring result.

    Let Step Functions handle retryable status errors. Convert tracking-table
    failures to a clear error; never submit or cancel an analysis here.
    """
    try:
        return monitor(event)
    except (BotoCoreError, ClientError):
        raise LedgerUnavailable("Cannot persist or verify CLC monitoring state.") from None
