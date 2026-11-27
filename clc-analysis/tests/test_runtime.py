import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import cli
import handlers
from ledger import CapacityUnavailable, Ledger, ReconciliationRequired, RequestConflict


@mock_aws
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "AWS_DEFAULT_REGION": "us-west-2",
            "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_EC2_METADATA_DISABLED": "true",
            "LEDGER_TABLE": "requests", "CLC_WORKFLOW": "workflow",
            "CLC_PRESET": "preset", "CLC_PASSWORD": "fake-secret",
            "INPUT_PREFIXES": '["s3://example-input/reads/"]',
            "OUTPUT_PREFIXES": '["s3://example-output/results/"]',
            "MONITORING_SECONDS": "86400", "MAX_ACTIVE_ANALYSES": "10",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.connection = cli.Connection("10.0.0.5", "7777", "service", "fake-secret", "/opt/clc/clcserver")
        connection_patch = patch.object(cli.Connection, "from_environment", return_value=self.connection)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)
        self.clock = datetime(2026, 11, 24, 16, tzinfo=timezone.utc)
        clock_patch = patch.object(handlers, "now", return_value=self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        boto3.client("dynamodb").create_table(
            TableName="requests", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ledger = Ledger("requests", handlers.server_identity(self.connection))
        self.context = SimpleNamespace(invoked_function_arn="arn:aws:lambda:us-west-2:123456789012:function:test")
        self.event = {
            "request_id": "run-001-sample-A", "sample_id": "sample-A",
            "read1": "s3://example-input/reads/A_R1.fastq.gz",
            "read2": "s3://example-input/reads/A_R2.fastq.gz",
            "destination": "s3://example-output/results/run-001/A/",
        }

    def response(self, output, code=0):
        return subprocess.CompletedProcess([], code, output, "private output fake-secret")

    def submit(self):
        with patch.object(cli.subprocess, "run", return_value=self.response(
            "Message: Process ID: process-101\n",
        )) as command:
            result = handlers.submit_handler(self.event, self.context)
        return result, command

    def active(self):
        return int(self.ledger.client.get_item(
            TableName="requests",
            Key={"pk": {"S": f"CAPACITY#{self.ledger.server_id}"}},
        )["Item"]["active"]["N"])

    def status_event(self, result):
        return {key: result[key] for key in ("request_id", "job_id", "deadline")}

    def test_submit_persists_id_and_returns_without_polling(self):
        result, command = self.submit()
        self.assertEqual(result["job_id"], "process-101")
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["wait_seconds"], 300)
        self.assertEqual(result["deadline"], "2026-11-25T16:00:00Z")
        arguments = command.call_args.args[0]
        self.assertIn("-Y", arguments)
        self.assertIn("7777", arguments)
        self.assertNotIn("-I", arguments)
        self.assertNotIn("CLC_PASSWORD", command.call_args.kwargs["env"])
        self.assertNotIn("fake-secret", str(result))
        self.assertEqual(self.ledger.get(self.event["request_id"])["job_id"]["S"], result["job_id"])
        self.assertEqual(self.active(), 1)
        command.assert_called_once()

    def test_duplicate_returns_recorded_id_and_original_deadline_without_dispatch(self):
        result, _ = self.submit()
        with patch.object(cli.subprocess, "run") as command:
            duplicate = handlers.submit_handler(self.event, self.context)
        self.assertEqual(duplicate["job_id"], result["job_id"])
        self.assertEqual(duplicate["deadline"], result["deadline"])
        self.assertEqual(self.active(), 1)
        command.assert_not_called()

    def test_conflicting_inputs_rejected_without_dispatch(self):
        self.submit()
        with patch.object(cli.subprocess, "run") as command:
            with self.assertRaises(RequestConflict):
                handlers.submit_handler({**self.event, "sample_id": "another"}, self.context)
        command.assert_not_called()

    def test_uncertain_submission_keeps_reservation_and_never_resubmits(self):
        for response in (
            self.response("No ID"),
            self.response("Process ID: one\nProcess ID: two\n"),
            self.response("Process ID: accepted", 1),
            subprocess.TimeoutExpired(["clcserver", "-W", "fake-secret"], 60),
        ):
            request_id = f"request-{self.active()}" if self.ledger.get(self.event["request_id"]) else self.event["request_id"]
            event = {**self.event, "request_id": request_id}
            with self.subTest(response=response), patch.object(cli.subprocess, "run", side_effect=[response]) as command:
                with self.assertRaises(cli.SubmissionUncertain) as error:
                    handlers.submit_handler(event, self.context)
                self.assertNotIn("fake-secret", str(error.exception))
                with self.assertRaises(ReconciliationRequired):
                    handlers.submit_handler(event, self.context)
                command.assert_called_once()
                self.assertTrue(self.ledger.get(request_id)["slot_held"]["BOOL"])

    def test_failure_to_persist_accepted_id_does_not_allow_resubmission(self):
        error = ClientError({"Error": {"Code": "InternalServerError", "Message": "unavailable"}}, "UpdateItem")
        with (
            patch.object(Ledger, "submitted", side_effect=error),
            patch.object(cli.subprocess, "run", return_value=self.response("Process ID: accepted")),
        ):
            with self.assertRaises(handlers.LedgerUnavailable):
                handlers.submit_handler(self.event, self.context)
        with patch.object(cli.subprocess, "run") as command:
            with self.assertRaises(ReconciliationRequired):
                handlers.submit_handler(self.event, self.context)
        command.assert_not_called()
        self.assertEqual(self.active(), 1)

    def test_capacity_limit_is_transactional_and_terminal_release_is_idempotent(self):
        for index in range(10):
            self.ledger.reserve(f"request-{index}", "sample", "hash", "execution", "deadline", 10)
        with self.assertRaises(CapacityUnavailable):
            self.ledger.reserve("overflow", "sample", "hash", "execution", "deadline", 10)
        self.assertEqual(self.active(), 10)
        self.assertEqual(self.ledger.get("overflow"), {})
        self.ledger.submitted("request-0", "job-0", "deadline")
        self.ledger.complete("request-0", "job-0", "successful")
        self.ledger.complete("request-0", "job-0", "successful")
        self.assertEqual(self.active(), 9)
        self.ledger.reserve("replacement", "sample", "hash", "execution", "deadline", 10)
        self.assertEqual(self.active(), 10)

    def test_repeated_reservation_does_not_increment_capacity(self):
        self.ledger.reserve("request", "sample", "hash", "execution", "deadline", 10)
        with self.assertRaises(RequestConflict):
            self.ledger.reserve("request", "sample", "hash", "execution", "deadline", 10)
        self.assertEqual(self.active(), 1)

    def test_status_checks_once_and_only_terminal_completion_releases_slot(self):
        result, _ = self.submit()
        for done, errors, expected in (("No", "No", "running"), ("true", "false", "successful")):
            with patch.object(cli.subprocess, "run", return_value=self.response(
                f"Job ID: process-101\nJob is done: {done}\nJob has errors: {errors}",
            )) as command:
                response = handlers.status_handler(self.status_event(result), self.context)
            self.assertEqual(response["status"], expected)
            self.assertEqual(command.call_args.args[0][-2:], ["-I", "process-101"])
            command.assert_called_once()
            self.assertEqual(self.active(), 1 if expected == "running" else 0)
        with patch.object(cli.subprocess, "run") as command:
            self.assertEqual(handlers.status_handler(self.status_event(result), self.context)["status"], "successful")
        command.assert_not_called()

    def test_failed_analysis_releases_capacity(self):
        result, _ = self.submit()
        with patch.object(cli.subprocess, "run", return_value=self.response(
            "Job ID: process-101\nJob is done: Yes\nJob has errors: Yes",
        )):
            self.assertEqual(handlers.status_handler(self.status_event(result), self.context)["status"], "failed")
        self.assertEqual(self.active(), 0)

    def test_invalid_cli_status_never_releases_capacity_or_defaults_to_success(self):
        result, _ = self.submit()
        for output in (
            "Job ID: other\nJob is done: Yes\nJob has errors: No",
            "Job ID: process-101\nJob is done: Unknown\nJob has errors: No",
            "Job ID: process-101\nJob is done: Yes",
        ):
            with patch.object(cli.subprocess, "run", return_value=self.response(output)):
                with self.assertRaises(cli.MonitoringError):
                    handlers.status_handler(self.status_event(result), self.context)
        self.assertEqual(self.active(), 1)

    def test_expired_deadline_does_not_query_or_release_capacity(self):
        result, _ = self.submit()
        result["deadline"] = handlers.timestamp(self.clock)
        with patch.object(cli.subprocess, "run") as command:
            response = handlers.status_handler(self.status_event(result), self.context)
        self.assertEqual(response["status"], "timeout")
        self.assertEqual(response["wait_seconds"], 0)
        self.assertEqual(self.active(), 1)
        command.assert_not_called()

    def test_running_status_waits_five_minutes_unless_deadline_is_nearer(self):
        result, _ = self.submit()
        with patch.object(cli.subprocess, "run", return_value=self.response(
            "Job ID: process-101\nJob is done: No\nJob has errors: No",
        )):
            response = handlers.status_handler(self.status_event(result), self.context)
        self.assertEqual(response["status"], "running")
        self.assertEqual(response["wait_seconds"], 300)

        result["deadline"] = handlers.timestamp(self.clock + timedelta(seconds=42))
        with patch.object(cli.subprocess, "run", return_value=self.response(
            "Job ID: process-101\nJob is done: No\nJob has errors: No",
        )):
            response = handlers.status_handler(self.status_event(result), self.context)
        self.assertEqual(response["status"], "running")
        self.assertEqual(response["wait_seconds"], 42)

    def test_query_timeout_is_bounded_by_remaining_budget(self):
        result, _ = self.submit()
        result["deadline"] = handlers.timestamp(self.clock + timedelta(seconds=7))
        with patch.object(cli.subprocess, "run", side_effect=subprocess.TimeoutExpired(["clcserver"], 7)) as command:
            with self.assertRaises(cli.TransientStatusError):
                handlers.status_handler(self.status_event(result), self.context)
        self.assertEqual(command.call_args.kwargs["timeout"], 7)
        self.assertEqual(self.active(), 1)

    def test_monitor_mode_needs_no_fastqs_or_workflow_and_never_submits(self):
        result, _ = self.submit()
        os.environ.pop("CLC_WORKFLOW")
        os.environ.pop("CLC_PRESET")
        with (
            patch.object(handlers, "now", return_value=self.clock + timedelta(days=2)),
            patch.object(cli.subprocess, "run") as command,
        ):
            response = handlers.submit_handler({
                "mode": "monitor", "request_id": self.event["request_id"],
            }, self.context)
        self.assertEqual(response["job_id"], result["job_id"])
        self.assertGreater(response["deadline"], result["deadline"])
        command.assert_not_called()

    def test_multiple_samples_keep_independent_process_ids_and_capacity(self):
        first, _ = self.submit()
        second_event = {
            **self.event, "request_id": "run-001-sample-B", "sample_id": "sample-B",
            "destination": "s3://example-output/results/run-001/B/",
        }
        with patch.object(cli.subprocess, "run", return_value=self.response("Process ID: process-202")):
            second = handlers.submit_handler(second_event, self.context)
        self.assertEqual(self.active(), 2)
        for result, done, expected in ((second, "No", "running"), (first, "Yes", "successful")):
            with patch.object(cli.subprocess, "run", return_value=self.response(
                f"Job ID: {result['job_id']}\nJob is done: {done}\nJob has errors: No",
            )) as command:
                status = handlers.status_handler(self.status_event(result), self.context)
            self.assertEqual(command.call_args.args[0][-2:], ["-I", result["job_id"]])
            self.assertEqual(status["job_id"], result["job_id"])
            self.assertEqual(status["sample_id"], result["sample_id"])
            self.assertEqual(status["status"], expected)
        self.assertEqual(self.active(), 1)

    def test_job_id_must_match_ledger_and_cancellation_tokens_are_rejected(self):
        result, _ = self.submit()
        for job_id in ("other", "cancel", "cancel-all", "--cancel", ""):
            with self.subTest(job_id=job_id), patch.object(cli.subprocess, "run") as command:
                with self.assertRaises(cli.InvalidInput):
                    handlers.status_handler({**self.status_event(result), "job_id": job_id}, self.context)
                command.assert_not_called()

    def test_unapproved_or_identical_reads_rejected_before_reservation(self):
        for update in (
            {"read1": "s3://example-input/reads-other/file.gz"},
            {"read1": self.event["read2"]},
            {"destination": "s3://example-output/results/no-trailing-slash"},
            {"request_id": "unsafe request"},
        ):
            with self.subTest(update=update), patch.object(cli.subprocess, "run") as command:
                with self.assertRaises(cli.InvalidInput):
                    handlers.submit_handler({**self.event, **update}, self.context)
                command.assert_not_called()
        self.assertEqual(self.ledger.get(self.event["request_id"]), {})


if __name__ == "__main__":
    unittest.main()
