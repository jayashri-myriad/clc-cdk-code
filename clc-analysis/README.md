# Standalone CLC analysis orchestration

This is a separate CDK application. It does not import, modify, or deploy the
existing server application or client sample.

## What it deploys

- Two Linux/x86-64 container Lambdas using one image: submission and status checking.
- A retained, encrypted DynamoDB ledger with point-in-time recovery.
- A per-sample Step Functions Standard workflow and a private polling-segment
  Standard workflow.
- A Lambda security group and an ingress rule on the existing server security
  group for TCP **7777**.
- A DynamoDB gateway endpoint unless an existing endpoint is explicitly selected.
- Retained logs, an encrypted SNS alert topic, and an EventBridge failure rule.

It does not create or replace the CLC EC2 instance, fetch FASTQs, retrieve results,
export VCFs, or integrate QCII/Reporting/Auto-Pipeline. It does not deploy a parent
sample Map workflow.

## Execution

```text
Start per-sample execution
    |
Validate configuration/input and check ledger
    |
Reserve request + active capacity slot atomically
    |                         |
    |                         +-- full: wait 30 seconds, then try admission again
    |
Submit CLC workflow once with -Y
    |
Persist CLC process ID and absolute monitoring deadline
    |
Start polling segment synchronously
    |
    +-- Wait -> Lambda: one -I request -> Choice
    |                                    |
    |                                    +-- running: repeat
    |                                    +-- terminal/timeout: return
    |
After 100 running checks, start a new segment with the same ID/deadline
    |
Successful -> Succeed
Analysis failed / monitoring error / timeout -> Fail
```

Step Functions persists each sample's ID in `analysis.job_id`; no Lambda needs
to remember a previous invocation. A queued job is reported as `running`.

Defaults:

| Setting | Value |
| --- | --- |
| Region | `us-west-2` |
| CLC server port | `7777`, the server-side port of the local SSM tunnel |
| Poll interval | 30 seconds after each completed running check |
| Monitoring budget | 24 hours after accepted submission |
| CLI request timeout | At most 60 seconds, clamped to remaining monitoring time |
| Lambda timeout / memory | 120 seconds / 2048 MiB |
| Active analyses | At most 10 tracked submissions per configured server/account |
| Capacity admission | At most 120 waits, approximately one hour plus request overhead |
| Per-sample execution guard | 27 hours, including admission and monitoring overhead |

Waits are clamped to the remaining monitoring budget. Status requests recheck the
deadline before contacting CLC, including after retries. Only read-only status
timeouts and selected transient Lambda service errors have bounded automatic
retries (three retries, exponential backoff from five seconds, full jitter).

**Why two state machines?** A 24-hour loop at 30-second intervals can exceed
Standard Step Functions' 25,000-event history limit. Each polling segment makes
at most 100 successful running checks, then returns control to the per-sample
workflow. The deadline is not reset between segments. Do not start the internal
polling-segment machine directly.

Stopping/timing out orchestration never cancels the CLC job.

## Prerequisites

- Python 3.12+, Node.js supported by CDK, AWS CDK CLI compatible with
  `requirements.txt`, and Docker with Linux image build support.
- AWS credentials authorized to deploy to the target account and a bootstrapped
  CDK environment.
- An approved Linux CLI installer and permission to package/distribute it in
  your private ECR repository.
- Actual server private IP, server security group ID, and a least-privileged CLC
  service account.
- Approved S3 input/output prefixes. The CLC/cloud execution roles, not just
  Lambda, need S3/KMS access.

The existing server must listen on its private interface on port 7777.
Validate its protocol and certificate trust requirements before deployment.
There is no active SSM tunnel dependency. Do not disable certificate validation.
If the current server's certificate requires a DNS name rather than its private
IP, arrange a matching private name/certificate before live testing.

## Prepare and validate locally

PowerShell commands from this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Supply an authorized installer to the ignored build directory:

```powershell
New-Item -ItemType Directory -Force runtime\vendor
Copy-Item 'C:\approved-installers\CLCServerCommandLineTools_26_0_7_64.sh' runtime\vendor\
```

The installer is not committed or downloaded automatically. The build context
contains only runtime code, dependency/build files, and the named installer;
never place credentials or sample input there.

Validate the container on a Docker host:

```powershell
docker build --platform linux/amd64 -t clc-analysis-local runtime
docker run --rm --read-only --tmpfs /tmp --entrypoint /bin/bash clc-analysis-local -c 'test -x /opt/clc/clcserver && python -c "import handlers"'
```

The second command checks the executable exists and the handlers import with a
read-only filesystem. It does not execute an analysis or prove server
connectivity/workflow compatibility. Test the actual CLI against the approved
non-production server before production use.

## Configure and deploy

Review [cdk.json](./cdk.json). Its VPC/subnet/route-table defaults reference the
existing server environment; verify them against your target account.

CloudFormation parameters:

| Parameter | Required value |
| --- | --- |
| `ClcHost` | Existing server private IP or trusted private DNS name |
| `ClcSecurityGroupId` | Existing server security group ID |
| `ClcUser` | Service user; NoEcho parameter |
| `ClcPassword` | Password; NoEcho parameter |
| `InputPrefixes` | JSON array, e.g. `["s3://example-input/reads/"]` |
| `OutputPrefixes` | JSON array, e.g. `["s3://example-output/results/"]` |

Both functions receive credentials as Lambda environment variables, as requested.
No secret values are included in source or synthesis. Deliver required
CloudFormation parameters through your approved secure deployment channel.
NoEcho masks parameter display; it does not make Lambda environment variables
inaccessible to privileged users. Do not put credentials in command history,
checked-in parameter files, CDK context, outputs, or deployment logs.
Use Secrets Manager/Vault before production if required by organizational policy.

From this directory, with the virtual environment active:

```powershell
cdk synth --quiet
cdk diff
```

Then deploy **this application only** with `cdk deploy clc-analysis`, supplying the
six required parameters through your approved deployment process. Deployment
builds and pushes the image to the bootstrapped private ECR asset repository.
It must run on a host with Docker and the installer present.

If these route tables already have a DynamoDB gateway endpoint, use
`cdk synth -c existing_dynamodb_endpoint=true` and the same context on diff/deploy.
Verify that endpoint's policy allows this ledger table and that all Lambda
subnets' route tables are associated. Creating another endpoint for the same
service/route tables will fail.

The stack adds a standalone ingress resource referencing the server security
group; it does not alter the original server stack's source or existing 8443
rules. Confirm ownership with the infrastructure team. On EC2 replacement, pass
the new private IP/security group and redeploy this application. Do not change
server address or service account while outstanding jobs need reconciliation:
the ledger binds requests to the configured host, port, and service user.

Stack outputs:

- `StateMachineArn`: public per-sample entry point.
- `LedgerTable`: request/capacity tracking table.
- `AlertsTopicArn`: subscribe an approved operational destination and confirm it.
  Until a subscription is configured, SNS is not an operator notification channel.

## Start one sample

Use the shape in [input.example.json](./input.example.json), replacing the
synthetic locations with approved inputs.

```powershell
aws stepfunctions start-execution --region us-west-2 --state-machine-arn '<StateMachineArn>' --input file://input.example.json
```

`request_id` must be stable for the same intended analysis across retries, and
unique for intentional reanalysis. Request/sample correlation IDs accept letters,
digits, underscore, hyphen, and period, up to 128 characters. Use non-sensitive
correlation IDs; do not encode patient information.

Successful execution output:

```json
{
  "analysis": {
    "request_id": "run-001-sample-A",
    "sample_id": "synthetic-sample-A",
    "job_id": "<clc-process-id>",
    "deadline": "<UTC deadline>",
    "status": "successful",
    "wait_seconds": 0
  }
}
```

`wait_seconds` is an internal scheduling field and can be nonzero in terminal
output. Only `successful` means analysis finished without CLC-reported errors.
It does not mean results were retrieved. No handler calls `-R`.

## Duplicate protection and capacity

The table has a string primary key, `pk`:

- `REQUEST#<request_id>` stores sample/execution IDs, an input fingerprint, server
  identity, CLC ID, deadline, submission/analysis/monitoring state, and slot ownership.
- `CAPACITY#<server-identity>` stores the active count.

Reservation atomically creates the request and increments capacity only if below
the limit. A duplicate with identical inputs and a recorded job ID returns that
ID without submission. Different inputs/configuration under the same request ID
are rejected. An unresolved reservation stops for reconciliation.

Confirmed terminal CLC status atomically releases the slot and records the
outcome; repeating that operation does not release twice.
Monitoring failure, workflow abort, and uncertain submission retain their slots.
This intentionally favors avoiding more than 10 active submissions over automatic
recovery of apparent stale slots.

The limit covers executions sharing this deployment's table and configured
server/account, not manual CLC submissions or other independent deployments.
Do not delete/expire reservations to free capacity while jobs may still run.

## Recovery

### Known process ID

For a request with an already recorded job ID, start the public workflow with:

```json
{
  "mode": "monitor",
  "request_id": "run-001-sample-A"
}
```

This reads the process ID from the ledger, never submits, and establishes a new
24-hour monitoring budget. It does not require FASTQ locations or a workflow
preset. A normal duplicate submission instead retains its original deadline.
Already recorded terminal outcomes are returned without contacting CLC.

### Accepted submission but no recorded process ID

1. Stop automatic redrive/resubmission and inspect the request record.
2. Locate the verified job using restricted CLC job history on the same server
   and account. Correlate carefully; do not guess an ID or use an AWS Batch ID.
3. An authorized operator can conditionally update the existing reserved record:
   set `job_id` to the verified process ID, `submission_state` to `submitted`,
   and `analysis_status` to `running`, only while it is still reserved and holds
   its slot. Preserve the fingerprint, server identity, and capacity count.
4. Start monitor mode for that request.

If it is conclusively established that no job was submitted, an authorized
operator must transactionally remove/resolve the reservation and decrement its
capacity exactly once. There is deliberately no automatic stale-lock expiry.
Do not manually decrement the counter without a matching conditional request
update. Do not resume a request against a different host/account.

### Monitoring failure or deadline expiry

Preserve the ID and capacity. Check the actual server job and resume monitoring
as above. Terminal monitoring will release capacity. The workflow never cancels
the CLC job.

## Logging and access

- Logs omit raw CLI stdout/stderr and full commands; errors use sanitized messages.
- The password is still passed as a process argument because the vendor CLI
  requires `-W`. Do not enable command tracing or raw process output.
- Step Functions CloudWatch logging is ERROR-only with execution data excluded.
  Standard execution history still contains state input/output and must be
  access-controlled, including initial sample/S3 input.
- EventBridge sends only execution ARN/status to the alert topic, not sample input.
- Ledger and log groups are retained on stack removal. Define approved retention
  and deletion procedures for them before production.

## Validation status

The tests use mocked subprocesses and an in-memory DynamoDB emulator; infrastructure
tests synthesize real CDK resources without deploying them. They cover submission,
uncertain outcomes, duplicate protection, the ten-job limit, idempotent slot
release, single status queries, failure/timeout behavior, monitor-only recovery,
state-machine wiring, and private network/IAM resource configuration.

Live acceptance still requires:

1. Building and starting the image on Linux Docker.
2. Verifying CLI/runtime compatibility and any certificate trust setup.
3. Deploying with the actual private IP, security group, and credentials.
4. Confirming Lambda-to-server and DynamoDB endpoint connectivity.
5. Running approved synthetic paired FASTQs through submission and terminal polling.
6. Confirming failure alerts reach an operational subscriber.

No test FASTQs are currently available, so end-to-end analysis is not yet verified.
