import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_async


class AsyncRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.results_file = self.path / "results.txt"
        self.settings = {
            "CLC_HOST": "test-host", "CLC_PORT": "7778", "CLC_USER": "test-user",
            "CLC_PASSWORD": "fake password", "CLC_PRESET": "test-preset",
            "CLC_WORKFLOW": "test-workflow", "CLC_INSTALLER": "installer.sh",
            "CLC_INSTALL_DIR": "install", "CLC_INPUT_FILE": "job.json",
        }
        self.job = {
            "read1": "s3://example/read1.fastq.gz",
            "read2": "s3://example/read2.fastq.gz",
            "destination": "s3://example/results/",
        }
        (self.path / "job.json").write_text(json.dumps(self.job), encoding="utf-8")
        for patcher in (
            patch.object(run_async, "PROJECT", self.path),
            patch.object(run_async, "read_settings", return_value=self.settings),
            patch.object(run_async, "get_client", return_value=self.path / "clcserver"),
            patch.dict(os.environ, {"CLC_PASSWORD": "fake password"}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def invoke(self, *args, responses):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("sys.argv", ["run_async.py", *args]),
            patch.object(run_async.subprocess, "run", side_effect=responses) as execute,
            patch.object(run_async.time, "sleep") as sleep,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = run_async.main()
        return status, stdout.getvalue(), stderr.getvalue(), execute, sleep

    def response(self, output, code=0):
        return subprocess.CompletedProcess([], code, output, "Private CLI details")

    def status_response(self, done="false", errors="false", job_id="test-job"):
        return self.response(f"Job ID: {job_id}\nJob is done: {done}\nJob has errors: {errors}\n")

    def poll_args(self):
        return ("poll", "test-job", "--results-file", str(self.results_file))

    def test_submit_returns_id_and_sends_async_workflow_without_exposing_output(self):
        status, stdout, stderr, execute, _ = self.invoke(
            "submit", responses=[self.response("Message: private\nProcess ID: test-job\n")]
        )
        self.assertEqual(status, 0, stderr)
        self.assertEqual(json.loads(stdout), {"job_id": "test-job", "status": "submitted"})
        self.assertEqual(execute.call_args.args[0], [
            str(self.path / "clcserver"), "-S", "test-host", "-U", "test-user",
            "-W", "fake password", "-P", "7778", "-Y",
            "-L", "test-preset", "-A", "test-workflow",
            "--reads-import-command", "ngs_import_illumina",
            "--reads-select-files", self.job["read1"],
            "--reads-select-files", self.job["read2"],
            "--reads-paired-reads", "true", "--destination-cloud", self.job["destination"],
        ])
        self.assertNotIn("CLC_PASSWORD", execute.call_args.kwargs["env"])
        self.assertNotIn("private", stdout + stderr)
        execute.assert_called_once()

    def test_uncertain_or_failed_submission_is_not_retried(self):
        for response in (
            self.response("Process ID: test-job", code=42),
            self.response("Message: no ID"),
            self.response("Process ID: one\nProcess ID: two"),
            self.response("Process ID: cancel-all"),
        ):
            with self.subTest(response=response.stdout):
                status, stdout, stderr, execute, _ = self.invoke("submit", responses=[response])
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertNotIn("Private CLI details", stderr)
                execute.assert_called_once()

    def test_submit_accepts_cli_message_wrapped_process_id(self):
        status, stdout, stderr, execute, _ = self.invoke(
            "submit", responses=[self.response(
                "Message: Submitting workflow\nMessage: Process ID: test-job\n"
            )],
        )
        self.assertEqual(status, 0, stderr)
        self.assertEqual(json.loads(stdout), {"job_id": "test-job", "status": "submitted"})
        execute.assert_called_once()

    def test_submit_rejects_duplicate_ids_across_plain_and_wrapped_fields(self):
        status, stdout, _, execute, _ = self.invoke(
            "submit", responses=[self.response(
                "Process ID: one\nMessage: Process ID: two\n"
            )],
        )
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        execute.assert_called_once()

    def test_poll_waits_then_retrieves_once_and_saves_results(self):
        (self.path / "job.json").unlink()
        status, stdout, stderr, execute, sleep = self.invoke(
            *self.poll_args(), responses=[
                self.status_response(), self.status_response(done="true"),
                self.response("Name: test result\nClcUrl: clc://example/result\n"),
            ],
        )
        self.assertEqual(status, 0, stderr)
        self.assertEqual(json.loads(stdout), {
            "job_id": "test-job", "status": "successful",
            "results_file": str(self.results_file),
        })
        self.assertEqual(
            [call.args[0][-2:] for call in execute.call_args_list],
            [["-I", "test-job"], ["-I", "test-job"], ["-R", "test-job"]],
        )
        sleep.assert_called_once_with(30)
        self.assertIn("ClcUrl: clc://example/result", self.results_file.read_text())
        if os.name != "nt":
            self.assertEqual(self.results_file.stat().st_mode & 0o777, 0o600)

    def test_failed_job_does_not_retrieve_results(self):
        status, stdout, stderr, execute, _ = self.invoke(
            *self.poll_args(), responses=[self.status_response(done="true", errors="true")]
        )
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stdout)["status"], "failed")
        self.assertIn("Job failed", stderr)
        execute.assert_called_once()
        self.assertFalse(self.results_file.exists())

    def test_poll_accepts_cli_yes_no_status_fields(self):
        for errors, expected_status, expected_code in (
            ("No", "successful", 0), ("Yes", "failed", 1),
        ):
            with self.subTest(errors=errors):
                responses = [
                    self.status_response(done="No", errors="No"),
                    self.status_response(done="Yes", errors=errors),
                ]
                if errors == "No":
                    responses.append(self.response("Name: test result\n"))
                status, stdout, stderr, execute, _ = self.invoke(
                    *self.poll_args(), responses=responses,
                )
                self.assertEqual(status, expected_code, stderr)
                self.assertEqual(json.loads(stdout)["status"], expected_status)
                self.assertEqual(execute.call_count, len(responses))

    def test_bad_status_is_never_reported_as_success(self):
        for response in (
            self.status_response(job_id="other-job"),
            self.status_response(done="unknown"),
            self.response("Job ID: test-job\nJob is done: true"),
            self.response("Job ID: test-job\nJob is done: true\nJob is done: false\nJob has errors: false"),
            self.response("Private CLI details", code=42),
        ):
            with self.subTest(response=response.stdout):
                status, stdout, stderr, execute, _ = self.invoke(*self.poll_args(), responses=[response])
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertNotIn("Private CLI details", stderr)
                execute.assert_called_once()

    def test_poll_deadline_limits_requests_and_does_not_cancel_or_retrieve(self):
        with patch.object(run_async.time, "monotonic", side_effect=[0, 0, 10, 10]):
            status, stdout, stderr, execute, sleep = self.invoke(
                *self.poll_args(), "--timeout", "10", responses=[self.status_response()]
            )
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(stdout)["status"], "timeout")
        self.assertIn("may still be running", stderr)
        self.assertEqual(execute.call_args.kwargs["timeout"], 10)
        execute.assert_called_once()
        sleep.assert_called_once_with(0)

    def test_existing_output_is_not_overwritten_or_consumed(self):
        self.results_file.write_text("previous results", encoding="utf-8")
        status, stdout, stderr, execute, _ = self.invoke(
            *self.poll_args(), responses=[self.status_response(done="true")]
        )
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Cannot run CLC tools or write results", stderr)
        self.assertEqual(self.results_file.read_text(), "previous results")
        execute.assert_called_once()

    def test_overall_timeout_during_status_request_reports_timeout(self):
        exception = subprocess.TimeoutExpired(["clcserver", "-I", "test-job"], 10)
        with patch.object(run_async.time, "monotonic", side_effect=[0, 0, 10]):
            status, stdout, stderr, execute, _ = self.invoke(
                *self.poll_args(), "--timeout", "10", responses=[exception]
            )
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(stdout)["status"], "timeout")
        self.assertIn("may still be running", stderr)
        self.assertFalse(self.results_file.exists())
        execute.assert_called_once()

    def test_overall_timeout_during_retrieval_preserves_reserved_file(self):
        exception = subprocess.TimeoutExpired(["clcserver", "-R", "test-job"], 10)
        with patch.object(run_async.time, "monotonic", side_effect=[0, 0, 0, 10]):
            status, stdout, _, execute, _ = self.invoke(
                *self.poll_args(), "--timeout", "10",
                responses=[self.status_response(done="true"), exception],
            )
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(stdout)["status"], "timeout")
        self.assertTrue(self.results_file.exists())
        self.assertEqual(execute.call_count, 2)

    def test_result_failure_is_not_retried_or_reported_as_success(self):
        status, stdout, stderr, execute, _ = self.invoke(
            *self.poll_args(), responses=[
                self.status_response(done="true"), self.response("private", code=42),
            ],
        )
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Result retrieval failed", stderr)
        self.assertEqual(execute.call_count, 2)

    def test_request_timeout_does_not_leak_password_or_retry(self):
        exception = subprocess.TimeoutExpired(["clcserver", "-W", "fake password"], 60)
        status, stdout, stderr, execute, _ = self.invoke("submit", responses=[exception])
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("outcome is unknown", stderr)
        self.assertNotIn("fake password", stderr)
        execute.assert_called_once()

    def test_cancellation_tokens_are_rejected_before_any_request(self):
        for job_id in ("cancel", "cancel-all", " ", "--cancel"):
            with self.subTest(job_id=job_id):
                status, stdout, stderr, execute, _ = self.invoke(
                    "poll", "--results-file", str(self.results_file), "--", job_id, responses=[]
                )
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("Invalid CLC job ID", stderr)
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
