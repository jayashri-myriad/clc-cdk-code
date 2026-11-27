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


def analysis_definition(submit_arn: str, status_arn: str) -> dict:
    """Build the single per-sample workflow for submission and ongoing monitoring.

    Wait for capacity if necessary, submit once, then wait five minutes between
    status checks until the job finishes or the monitoring deadline expires.
    Uncertain submissions require review; they never trigger an automatic resubmit.
    """
    result_fields = (
        "request_id", "sample_id", "job_id", "deadline", "status", "wait_seconds",
    )
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
                    {"Variable": "$.analysis.status", "StringEquals": "submitted", "Next": "WaitForNextCheck"},
                    {"Variable": "$.analysis.status", "StringEquals": "running", "Next": "WaitForNextCheck"},
                ],
                "Default": "RecordMonitoringFailure",
            },
            "WaitForNextCheck": {
                # Wait in Step Functions; no Lambda runs during this five-minute delay.
                "Type": "Wait", "SecondsPath": "$.analysis.wait_seconds",
                "Next": "CheckAnalysisStatus",
            },
            "CheckAnalysisStatus": {
                **lambda_task(status_arn, {
                    "request_id.$": "$.analysis.request_id",
                    "job_id.$": "$.analysis.job_id",
                    "deadline.$": "$.analysis.deadline",
                }),
                # Replace only the analysis status fields and retain admission context.
                "ResultSelector": {f"{field}.$": f"$.Payload.{field}" for field in result_fields},
                "ResultPath": "$.analysis", "Next": "EvaluateAnalysis",
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
