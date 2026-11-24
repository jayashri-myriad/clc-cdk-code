from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workflow import analysis_definition, monitor_definition


class WorkflowTests(unittest.TestCase):
    def test_submit_has_no_retry_and_capacity_is_only_automatic_resubmit_route(self):
        states = analysis_definition("submit", "status", "monitor")["States"]
        self.assertNotIn("Retry", states["PrepareAnalysis"])
        catches = states["PrepareAnalysis"]["Catch"]
        self.assertEqual(catches[0]["ErrorEquals"], ["CapacityUnavailable"])
        self.assertEqual(catches[0]["Next"], "CheckAdmissionLimit")
        self.assertEqual(states["IncrementAdmissionAttempts"]["Next"], "PrepareAnalysis")
        self.assertEqual(states["CheckAdmissionLimit"]["Choices"][0]["NumericGreaterThanEquals"], 120)
        self.assertEqual(catches[-1]["Next"], "SubmissionNeedsReview")

    def test_nested_segments_retain_analysis_state_and_do_not_repeat_submission(self):
        states = analysis_definition("submit", "status", "monitor")["States"]
        task = states["MonitorSegment"]
        self.assertEqual(task["Parameters"]["Input"]["analysis.$"], "$.analysis")
        self.assertEqual(task["ResultSelector"]["analysis.$"], "$.Output.analysis")
        self.assertEqual(task["Next"], "EvaluateAnalysis")
        routes = {route["StringEquals"]: route["Next"] for route in states["EvaluateAnalysis"]["Choices"]}
        self.assertEqual(routes["running"], "MonitorSegment")
        self.assertEqual(routes["successful"], "AnalysisSucceeded")
        self.assertEqual(states["AnalysisFailed"]["Type"], "Fail")
        self.assertEqual(states["MonitoringTimedOut"]["Type"], "Fail")

    def test_poll_loop_is_bounded_and_only_status_reads_are_retried(self):
        states = monitor_definition("status")["States"]
        task = states["CheckAnalysisStatus"]
        self.assertEqual(task["Parameters"]["Payload"]["job_id.$"], "$.analysis.job_id")
        self.assertEqual(task["Parameters"]["Payload"]["deadline.$"], "$.analysis.deadline")
        self.assertEqual(states["WaitForNextCheck"]["SecondsPath"], "$.analysis.wait_seconds")
        errors = task["Retry"][0]["ErrorEquals"]
        self.assertIn("TransientStatusError", errors)
        self.assertNotIn("States.ALL", errors)
        self.assertNotIn("MonitoringError", errors)
        self.assertEqual(states["CheckSegmentLimit"]["Choices"][0]["NumericGreaterThanEquals"], 100)

    def test_all_transition_targets_exist(self):
        for definition in (analysis_definition("submit", "status", "monitor"), monitor_definition("status")):
            states = definition["States"]
            for state in states.values():
                transitions = [state, *state.get("Choices", []), *state.get("Catch", [])]
                for transition in transitions:
                    if "Next" in transition:
                        self.assertIn(transition["Next"], states)
                if "Default" in state:
                    self.assertIn(state["Default"], states)


if __name__ == "__main__":
    unittest.main()
