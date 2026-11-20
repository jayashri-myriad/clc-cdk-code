import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.config = self.path / ".env"
        self.config.write_text(
            "CLC_HOST=test-host\nCLC_PORT=7778\nCLC_USER=test-user\n"
            "CLC_PASSWORD='fake password'\nCLC_PRESET=test-preset\n"
            "CLC_WORKFLOW=test-workflow\nCLC_INSTALLER=installer.sh\n"
            "CLC_INSTALL_DIR=install\nCLC_INPUT_FILE=job.json\n",
            encoding="utf-8",
        )
        self.job = {
            "read1": "s3://example/input_R1.fastq.gz",
            "read2": "s3://example/input_R2.fastq.gz",
            "destination": "s3://example/output/",
        }
        self.input_file = self.path / "job.json"
        self.input_file.write_text(json.dumps(self.job), encoding="utf-8")
        self.install_dir = self.path / "install"
        self.install_dir.mkdir()
        self.client = self.install_dir / "clcserver"
        self.client.touch()
        self.client.chmod(0o700)
        self.installer = self.path / "installer.sh"
        self.installer.touch()
        for patcher in (
            patch.object(run, "PROJECT", self.path),
            patch.dict(os.environ, {}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_sample(self, *args, status=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("sys.argv", ["run.py", *args]),
            patch.object(run.subprocess, "run", return_value=subprocess.CompletedProcess([], status)) as execute,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = run.main()
        return result, stdout.getvalue(), stderr.getvalue(), execute

    def test_reads_env_and_builds_synchronous_command(self):
        status, stdout, stderr, execute = self.run_sample()
        self.assertEqual(status, 0, stderr)
        self.assertIn("completed successfully", stdout)
        self.assertEqual(execute.call_args.args[0], [
            str(self.client), "-S", "test-host", "-U", "test-user",
            "-W", "fake password", "-P", "7778", "-L", "test-preset",
            "-A", "test-workflow", "--reads-import-command", "ngs_import_illumina",
            "--reads-select-files", self.job["read1"],
            "--reads-select-files", self.job["read2"],
            "--reads-paired-reads", "true", "--destination-cloud", self.job["destination"],
        ])
        self.assertEqual(execute.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(execute.call_args.kwargs["stderr"], subprocess.STDOUT)

    def test_shell_overrides_env_without_passing_password_in_environment(self):
        os.environ.update(CLC_PASSWORD="override", CLC_HOST="other-host")
        status, _, stderr, execute = self.run_sample()
        self.assertEqual(status, 0, stderr)
        command = execute.call_args.args[0]
        self.assertEqual(command[command.index("-W") + 1], "override")
        self.assertEqual(command[command.index("-S") + 1], "other-host")
        self.assertNotIn("CLC_PASSWORD", execute.call_args.kwargs["env"])

    def test_missing_settings_prevent_execution(self):
        for contents in ("", "CLC_PASSWORD="):
            with self.subTest(contents=contents):
                self.config.write_text(contents, encoding="utf-8")
                status, _, stderr, execute = self.run_sample()
                self.assertEqual(status, 1)
                self.assertIn("Set CLC_", stderr)
                execute.assert_not_called()

    def test_blank_password_has_no_fallback(self):
        self.config.write_text(
            self.config.read_text(encoding="utf-8").replace("'fake password'", ""),
            encoding="utf-8",
        )
        status, _, stderr, execute = self.run_sample()
        self.assertEqual(status, 1)
        self.assertIn("Set CLC_PASSWORD", stderr)
        execute.assert_not_called()

    def test_invalid_input_prevents_execution(self):
        for contents in ("{", "[]", "{}", '{"read1": 123}', json.dumps({**self.job, "read2": " "})):
            with self.subTest(contents=contents):
                self.input_file.write_text(contents, encoding="utf-8")
                status, _, stderr, execute = self.run_sample()
                self.assertEqual(status, 1)
                self.assertIn("Job input", stderr)
                execute.assert_not_called()
        self.input_file.unlink()
        status, _, stderr, execute = self.run_sample()
        self.assertEqual(status, 1)
        self.assertIn("Cannot read job input", stderr)
        execute.assert_not_called()

    def test_cli_handles_url_validation_and_extra_fields_are_allowed(self):
        self.input_file.write_text(
            json.dumps({**self.job, "read1": "s3://example/read1", "note": "test"}),
            encoding="utf-8",
        )
        status, _, stderr, _ = self.run_sample()
        self.assertEqual(status, 0, stderr)

    def test_installs_once_and_reuses_client(self):
        self.client.unlink()
        self.install_dir.rmdir()

        def install(command, **kwargs):
            self.assertEqual(command, ["bash", str(self.installer), "-q", "-dir", str(self.install_dir)])
            self.assertNotIn("CLC_PASSWORD", kwargs["env"])
            self.install_dir.mkdir()
            self.client.touch()
            self.client.chmod(0o700)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(run.subprocess, "run", side_effect=install) as execute:
            self.assertEqual(run.get_client(self.installer, self.install_dir, {}), self.client)
            self.assertEqual(run.get_client(self.installer, self.install_dir, {}), self.client)
        execute.assert_called_once()

    def test_incomplete_installation_is_not_overwritten(self):
        self.client.unlink()
        status, _, stderr, execute = self.run_sample()
        self.assertEqual(status, 1)
        self.assertIn("exists but has no executable clcserver", stderr)
        execute.assert_not_called()

    def test_installer_failures_do_not_submit_jobs(self):
        self.client.unlink()
        self.install_dir.rmdir()
        status, _, stderr, execute = self.run_sample(status=42)
        self.assertEqual(status, 1)
        self.assertIn("installer failed with status 42", stderr)
        execute.assert_called_once()

        status, _, stderr, execute = self.run_sample()
        self.assertEqual(status, 1)
        self.assertIn("without creating an executable clcserver", stderr)
        execute.assert_called_once()

        self.installer.unlink()
        status, _, stderr, execute = self.run_sample()
        self.assertEqual(status, 1)
        self.assertIn("Cannot read", stderr)
        execute.assert_not_called()

    def test_cli_failure_is_reported_without_credentials(self):
        status, stdout, stderr, _ = self.run_sample(status=42)
        self.assertEqual(status, 42)
        self.assertNotIn("successfully", stdout)
        self.assertIn("Check CLC job history", stderr)
        self.assertNotIn("fake password", stderr)

    def test_test_output_inherits_streams_and_preserves_failure(self):
        status, _, _, execute = self.run_sample("--show-test-output", status=42)
        self.assertEqual(status, 42)
        self.assertIsNone(execute.call_args.kwargs["stdout"])
        self.assertIsNone(execute.call_args.kwargs["stderr"])

    def test_launch_error_is_reported(self):
        with patch.object(run.subprocess, "run", side_effect=OSError("cannot execute")):
            with patch("sys.argv", ["run.py"]), contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(run.main(), 1)
        self.assertIn("Cannot run CLC tools", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
