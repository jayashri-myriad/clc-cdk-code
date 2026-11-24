"""ASL definitions kept separate so transitions can be tested without deployment."""


def lambda_task(function_arn: str, payload: dict) -> dict:
    """Build a Step Functions task that invokes Lambda and waits for its response.

    Its 130-second timeout allows slightly longer than Lambda's 120-second limit;
    it does not wait for the full CLC analysis to finish.
    """
    return {
        "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
        "Parameters": {"FunctionName": function_arn, "Payload": payload},
        "TimeoutSeconds": 130,
    }


def failure_recorder(status_arn: str) -> dict:
    """Build a task that marks monitoring as failed, then enters a Fail state.

    Keep the existing job context even if recording fails. This never cancels
    CLC or releases capacity for a job whose actual outcome is still unknown.
    """
    return {
        **lambda_task(status_arn, {
            "action": "record_monitoring_failure",
            "request_id.$": "$.analysis.request_id",
            "job_id.$": "$.analysis.job_id",
        }),
        "ResultPath": None, "Next": "MonitoringFailed",
        "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": None, "Next": "MonitoringFailed"}],
    }


def monitor_definition(status_arn: str) -> dict:
    """Build the child workflow: wait, check one job, then choose the next action.

    Return on completion/timeout or after 100 running checks. Breaking a long
    polling session into segments avoids the execution-history event limit.
    The caller must pass the same process ID and deadline into each new segment.
    """
    result_fields = (
        "request_id", "sample_id", "job_id", "deadline", "status", "wait_seconds",
    )
    return {
        "StartAt": "InitializeCount", "TimeoutSeconds": 86400,
        "States": {
            "InitializeCount": {
                "Type": "Pass", "Result": {"value": 0},
                "ResultPath": "$.checks", "Next": "WaitForNextCheck",
            },
            "WaitForNextCheck": {
                # Step Functions waits here, so no Lambda stays running between checks.
                "Type": "Wait", "SecondsPath": "$.analysis.wait_seconds",
                "Next": "CheckAnalysisStatus",
            },
            "CheckAnalysisStatus": {
                **lambda_task(status_arn, {
                    "request_id.$": "$.analysis.request_id",
                    "job_id.$": "$.analysis.job_id",
                    "deadline.$": "$.analysis.deadline",
                }),
                # Keep the sample, process ID, and deadline together for the next check.
                "ResultSelector": {f"{field}.$": f"$.Payload.{field}" for field in result_fields},
                "ResultPath": "$.analysis", "Next": "EvaluateStatus",
                "Retry": [{
                    "ErrorEquals": [
                        "TransientStatusError", "Lambda.ServiceException",
                        "Lambda.AWSLambdaException", "Lambda.SdkClientException",
                        "Lambda.TooManyRequestsException",
                    ],
                    "IntervalSeconds": 5, "MaxAttempts": 3,
                    "BackoffRate": 2, "JitterStrategy": "FULL",
                }],
                "Catch": [{
                    "ErrorEquals": ["States.ALL"], "ResultPath": None,
                    "Next": "RecordMonitoringFailure",
                }],
            },
            "EvaluateStatus": {
                "Type": "Choice",
                "Choices": [
                    {"Variable": "$.analysis.status", "StringEquals": "running", "Next": "IncrementCount"},
                    *[
                        {"Variable": "$.analysis.status", "StringEquals": status, "Next": "SegmentComplete"}
                        for status in ("successful", "failed", "timeout")
                    ],
                ],
                "Default": "RecordMonitoringFailure",
            },
            "IncrementCount": {
                "Type": "Pass",
                "Parameters": {"value.$": "States.MathAdd($.checks.value, 1)"},
                "ResultPath": "$.checks", "Next": "CheckSegmentLimit",
            },
            "CheckSegmentLimit": {
                # Return after 100 running checks; the outer workflow starts a fresh segment.
                "Type": "Choice",
                "Choices": [{
                    "Variable": "$.checks.value", "NumericGreaterThanEquals": 100,
                    "Next": "SegmentComplete",
                }],
                "Default": "WaitForNextCheck",
            },
            "SegmentComplete": {"Type": "Succeed"},
            "RecordMonitoringFailure": failure_recorder(status_arn),
            "MonitoringFailed": {
                "Type": "Fail", "Error": "CLCMonitoringFailed",
                "Cause": "Monitoring failed; the CLC job may still be running. Reconcile the ledger.",
            },
        },
    }


def analysis_definition(submit_arn: str, status_arn: str, monitor_arn: str) -> dict:
    """Build the public per-sample workflow for submission and ongoing monitoring.

    Wait for capacity if necessary, prepare the job once, then run polling
    segments until an outcome is known. Uncertain submissions require review,
    and monitoring failures must not trigger a new CLC submission.
    """
    return {
        "StartAt": "InitializeRequest", "TimeoutSeconds": 97200,
        "States": {
            "InitializeRequest": {
                "Type": "Pass",
                "Parameters": {"request.$": "$", "admission": {"attempt": 0}},
                "Next": "PrepareAnalysis",
            },
            "PrepareAnalysis": {
                **lambda_task(submit_arn, {
                    "request.$": "$.request", "execution_arn.$": "$$.Execution.Id",
                }),
                "ResultSelector": {"analysis.$": "$.Payload"}, "ResultPath": "$",
                "Next": "EvaluateAnalysis",
                # Only a full capacity pool is retried here; uncertain submissions need review.
                "Catch": [
                    {"ErrorEquals": ["CapacityUnavailable"], "ResultPath": None, "Next": "CheckAdmissionLimit"},
                    {"ErrorEquals": ["InvalidInput", "ConfigurationError"], "ResultPath": None, "Next": "SubmissionRejected"},
                    {"ErrorEquals": ["States.ALL"], "ResultPath": None, "Next": "SubmissionNeedsReview"},
                ],
            },
            "CheckAdmissionLimit": {
                "Type": "Choice",
                "Choices": [{
                    "Variable": "$.admission.attempt", "NumericGreaterThanEquals": 120,
                    "Next": "CapacityTimedOut",
                }],
                "Default": "WaitForCapacity",
            },
            "WaitForCapacity": {"Type": "Wait", "Seconds": 30, "Next": "IncrementAdmissionAttempts"},
            "IncrementAdmissionAttempts": {
                "Type": "Pass",
                "Parameters": {"attempt.$": "States.MathAdd($.admission.attempt, 1)"},
                "ResultPath": "$.admission", "Next": "PrepareAnalysis",
            },
            "EvaluateAnalysis": {
                "Type": "Choice",
                "Choices": [
                    {"Variable": "$.analysis.status", "StringEquals": "successful", "Next": "AnalysisSucceeded"},
                    {"Variable": "$.analysis.status", "StringEquals": "failed", "Next": "AnalysisFailed"},
                    {"Variable": "$.analysis.status", "StringEquals": "timeout", "Next": "MonitoringTimedOut"},
                    {"Variable": "$.analysis.status", "StringEquals": "submitted", "Next": "MonitorSegment"},
                    {"Variable": "$.analysis.status", "StringEquals": "running", "Next": "MonitorSegment"},
                ],
                "Default": "RecordMonitoringFailure",
            },
            "MonitorSegment": {
                # Wait for the child workflow, then reuse its job ID and unchanged deadline.
                "Type": "Task", "Resource": "arn:aws:states:::states:startExecution.sync:2",
                "Parameters": {
                    "StateMachineArn": monitor_arn,
                    "Input": {
                        "analysis.$": "$.analysis",
                        "AWS_STEP_FUNCTIONS_STARTED_BY_EXECUTION_ID.$": "$$.Execution.Id",
                    },
                },
                "ResultSelector": {"analysis.$": "$.Output.analysis"},
                "ResultPath": "$", "Next": "EvaluateAnalysis",
                "Catch": [{
                    "ErrorEquals": ["States.ALL"], "ResultPath": None,
                    "Next": "RecordMonitoringFailure",
                }],
            },
            "RecordMonitoringFailure": failure_recorder(status_arn),
            "AnalysisSucceeded": {"Type": "Succeed"},
            "AnalysisFailed": {
                "Type": "Fail", "Error": "CLCAnalysisFailed",
                "Cause": "CLC reported analysis failure; see the request ledger.",
            },
            "MonitoringTimedOut": {
                "Type": "Fail", "Error": "CLCMonitoringTimedOut",
                "Cause": "Monitoring deadline expired. The CLC job may still be running.",
            },
            "MonitoringFailed": {
                "Type": "Fail", "Error": "CLCMonitoringFailed",
                "Cause": "Monitoring failed; reconcile the existing CLC job.",
            },
            "SubmissionRejected": {
                "Type": "Fail", "Error": "CLCSubmissionRejected",
                "Cause": "Input or deployment configuration is invalid.",
            },
            "SubmissionNeedsReview": {
                "Type": "Fail", "Error": "CLCSubmissionNeedsReview",
                "Cause": "Check request ledger and CLC job history. Do not blindly resubmit.",
            },
            "CapacityTimedOut": {
                "Type": "Fail", "Error": "CLCCapacityUnavailable",
                "Cause": "Capacity remained full after 120 waits. No job was submitted by this request.",
            },
        },
    }
