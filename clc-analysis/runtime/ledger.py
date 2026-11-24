"""Transactional request reservations and server-wide active analysis limit."""

import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class CapacityUnavailable(Exception):
    pass


class RequestConflict(Exception):
    pass


class ReconciliationRequired(Exception):
    pass


class Ledger:
    def __init__(self, table_name: str, server_id: str) -> None:
        """Connect the helper to an existing tracking table and server/account key.

        Use short AWS request timeouts so database problems do not hang Lambda.
        Table creation is handled by CDK, not this constructor.
        """
        self.table_name = table_name
        self.server_id = server_id
        self.client = boto3.client(
            "dynamodb", config=Config(
                connect_timeout=3, read_timeout=5,
                retries={"mode": "standard", "total_max_attempts": 2},
            ),
        )

    def key(self, request_id: str) -> dict[str, dict[str, str]]:
        """Build the DynamoDB request key, keeping it separate from capacity keys.

        The S wrapper is the string format required by the low-level DynamoDB API.
        """
        return {"pk": {"S": f"REQUEST#{request_id}"}}

    def get(self, request_id: str) -> dict:
        """Read the latest request record, or return an empty dictionary if absent.

        Database errors still raise; an empty result means no record was found.
        """
        # Read the latest saved state before deciding whether submission is safe.
        return self.client.get_item(
            TableName=self.table_name, Key=self.key(request_id), ConsistentRead=True,
        ).get("Item", {})

    def reserve(
        self, request_id: str, sample_id: str, fingerprint: str,
        execution_arn: str, deadline: str, maximum: int,
    ) -> None:
        """Reserve a new request and one active-analysis slot in one transaction.

        Both writes succeed or neither does. Reject an already-reserved request;
        report CapacityUnavailable if the configured active count is full.
        This reserves permission to submit; it does not call CLC.
        """
        item = {
            **self.key(request_id), "sample_id": {"S": sample_id},
            "fingerprint": {"S": fingerprint}, "server_id": {"S": self.server_id},
            "execution_arn": {"S": execution_arn}, "deadline": {"S": deadline},
            "submission_state": {"S": "reserved"}, "slot_held": {"BOOL": True},
            "monitoring_status": {"S": "active"},
            "updated_at": {"N": str(int(time.time()))},
        }
        try:
            # Reserve the request and a capacity slot together, or change neither.
            self.client.transact_write_items(TransactItems=[
                {"Put": {
                    "TableName": self.table_name, "Item": item,
                    "ConditionExpression": "attribute_not_exists(pk)",
                }},
                {"Update": {
                    "TableName": self.table_name,
                    "Key": {"pk": {"S": f"CAPACITY#{self.server_id}"}},
                    "UpdateExpression": "ADD active :one",
                    "ConditionExpression": "attribute_not_exists(active) OR active < :maximum",
                    "ExpressionAttributeValues": {
                        ":one": {"N": "1"}, ":maximum": {"N": str(maximum)},
                    },
                }},
            ])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = exc.response.get("CancellationReasons", [])
            if any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons):
                if self.get(request_id):
                    raise RequestConflict("Request is already reserved; inspect its record.") from None
                raise CapacityUnavailable("CLC active analysis capacity is full.") from None
            raise

    def submitted(self, request_id: str, job_id: str, deadline: str) -> None:
        """Attach CLC's returned process ID and monitoring deadline to a reservation.

        Only update a still-reserved request that holds a slot. Keep that slot
        occupied while the analysis is queued or running.
        """
        self.client.update_item(
            TableName=self.table_name, Key=self.key(request_id),
            UpdateExpression=(
                "SET job_id = :job, submission_state = :submitted, "
                "analysis_status = :running, deadline = :deadline"
            ),
            ConditionExpression="submission_state = :reserved AND slot_held = :true",
            ExpressionAttributeValues={
                ":job": {"S": job_id}, ":submitted": {"S": "submitted"},
                ":running": {"S": "running"}, ":reserved": {"S": "reserved"},
                ":true": {"BOOL": True}, ":deadline": {"S": deadline},
            },
        )

    def monitoring(self, request_id: str, status: str) -> None:
        """Update only the monitoring state of an existing request.

        For example, timed_out means polling stopped, not that CLC stopped.
        Do not change the analysis outcome or active-analysis count here.
        """
        # Monitoring problems do not prove the CLC job stopped, so do not free its slot.
        self.client.update_item(
            TableName=self.table_name, Key=self.key(request_id),
            UpdateExpression="SET monitoring_status = :status",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":status": {"S": status}},
        )

    def complete(self, request_id: str, job_id: str, status: str) -> None:
        """Save confirmed analysis completion and release its capacity slot atomically.

        Match the saved process ID before changing anything. Repeating the same
        completion is safe: an already-released slot is not deducted again.
        """
        try:
            # Save the terminal result and free exactly one slot in the same transaction.
            self.client.transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": self.table_name, "Key": self.key(request_id),
                    "UpdateExpression": (
                        "SET slot_held = :false, analysis_status = :status, "
                        "monitoring_status = :complete"
                    ),
                    "ConditionExpression": "slot_held = :true AND job_id = :job",
                    "ExpressionAttributeValues": {
                        ":false": {"BOOL": False}, ":true": {"BOOL": True},
                        ":status": {"S": status}, ":job": {"S": job_id},
                        ":complete": {"S": "complete"},
                    },
                }},
                {"Update": {
                    "TableName": self.table_name,
                    "Key": {"pk": {"S": f"CAPACITY#{self.server_id}"}},
                    "UpdateExpression": "ADD active :minus_one",
                    "ConditionExpression": "active >= :one",
                    "ExpressionAttributeValues": {
                        ":minus_one": {"N": "-1"}, ":one": {"N": "1"},
                    },
                }},
            ])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            record = self.get(request_id)
            # Another check may have saved this result already; do not decrement twice.
            if (
                record.get("slot_held") == {"BOOL": False}
                and record.get("job_id") == {"S": job_id}
                and record.get("analysis_status") == {"S": status}
            ):
                return
            raise
