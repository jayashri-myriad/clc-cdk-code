import json
from pathlib import Path

from aws_cdk import (
    ArnFormat, Aws, CfnOutput, CfnParameter, Duration, RemovalPolicy, Stack,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    aws_stepfunctions as sfn,
)
from constructs import Construct

from workflow import analysis_definition, monitor_definition


class AnalysisStack(Stack):
    # This stack creates the AWS resources around an existing CLC server:
    # Lambda submits/checks jobs, Step Functions controls polling, and DynamoDB tracks jobs.
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        """Define the analysis infrastructure without provisioning another CLC server.

        Connect container Lambdas, tracking storage, polling workflows, and alerts
        to the existing VPC/server. Deployment still needs connection parameters.
        """
        super().__init__(scope, construct_id, **kwargs)
        # Read infrastructure settings from CDK context, normally supplied by cdk.json.
        # Entries at the same position in these lists must describe the same subnet.
        ctx = self.node.try_get_context
        subnet_ids = ctx("subnet_ids")
        azs = ctx("subnet_azs")
        route_tables = ctx("route_table_ids")
        if (
            not ctx("vpc_id") or not subnet_ids or not azs or not route_tables
            or not len(subnet_ids) == len(azs) == len(route_tables)
        ):
            raise ValueError("Configure matching subnet_ids, subnet_azs, route_table_ids and vpc_id.")
        # Reference the VPC and private subnets where Lambda will run.
        # This does not create or change those subnets, or create another CLC server.
        vpc = ec2.Vpc.from_vpc_attributes(
            self, "ExistingVpc", vpc_id=ctx("vpc_id"), availability_zones=azs,
            private_subnet_ids=subnet_ids, private_subnet_route_table_ids=route_tables,
        )
        # Ask for the existing server address and security group during deployment.
        # Use its private IP here, not localhost or the local SSM tunnel address.
        host = CfnParameter(self, "ClcHost", min_length=1, description="Existing CLC server private IP or trusted private DNS.")
        server_sg_id = CfnParameter(
            self, "ClcSecurityGroupId", allowed_pattern=r"sg-[a-f0-9]{8,17}",
            description="Existing CLC EC2 security group ID.",
        )
        # Supply the CLC login at deployment instead of storing it in this source file.
        # NoEcho masks CloudFormation parameter display. The login is still passed to
        # Lambda environment variables, which privileged AWS users may be able to read.
        user = CfnParameter(self, "ClcUser", no_echo=True, min_length=1, description="Least-privileged CLC service user.")
        password = CfnParameter(self, "ClcPassword", no_echo=True, min_length=1, description="CLC password; use an approved parameter delivery channel.")
        # These lists restrict which S3 paths the submit handler accepts.
        # They do not grant S3 access; the CLC/cloud execution roles need that separately.
        input_prefixes = CfnParameter(self, "InputPrefixes", min_length=1, description='JSON list of allowed S3 input prefixes ending in /.')
        output_prefixes = CfnParameter(self, "OutputPrefixes", min_length=1, description='JSON list of allowed S3 output prefixes ending in /.')
        # 7777 is the actual server port; 7778 is only the laptop's tunnel port.
        # The maximum controls tracked active analyses, not simultaneous Lambda calls.
        port = int(ctx("clc_port") if ctx("clc_port") is not None else 7777)
        maximum = int(ctx("max_active_analyses") if ctx("max_active_analyses") is not None else 10)
        if not 1 <= maximum <= 10 or not 1 <= port <= 65535:
            raise ValueError("Use 1-10 active analyses and a valid CLC TCP port.")

        # Attach this security group to both Lambdas so server access can be granted
        # to these clients as a group, instead of allowing an entire network range.
        client_sg = ec2.SecurityGroup(
            self, "LambdaSecurityGroup", vpc=vpc, allow_all_outbound=True,
            description="CLC analysis Lambda private connectivity",
        )
        # Add an inbound TCP rule to the existing CLC server's security group.
        # Lambda can then connect to the private server address on the configured port.
        # This does not start the CLC listener or open the server to the public internet.
        ec2.CfnSecurityGroupIngress(
            self, "ClcClientIngress", group_id=server_sg_id.value_as_string,
            source_security_group_id=client_sg.security_group_id,
            ip_protocol="tcp", from_port=port, to_port=port,
            description="CLC CLI from analysis Lambda only",
        )
        # Lambda needs DynamoDB access to save process IDs and reserve analysis slots.
        # A gateway endpoint provides that route without requiring internet/NAT access.
        # Set existing_dynamodb_endpoint=true when a working endpoint already covers
        # these route tables; otherwise creating another one can fail deployment.
        existing_endpoint = ctx("existing_dynamodb_endpoint")
        if existing_endpoint not in (None, True, False, "true", "false"):
            raise ValueError("existing_dynamodb_endpoint must be true or false.")
        if existing_endpoint not in (True, "true"):
            ec2.GatewayVpcEndpoint(
                self, "DynamoEndpoint", vpc=vpc,
                service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
                subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
            )
        # Store request-to-CLC-job mappings and the shared active-analysis counter.
        # Transactional writes in ledger.py prevent duplicate reservations and enforce
        # the capacity limit across executions using this table and server/account.
        # Keep the table on stack deletion, and enable recovery for accidental changes.
        table = dynamodb.Table(
            self, "Requests", partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        # Both handlers receive the same connection details and tracking-table name.
        # 86400 seconds gives each accepted submission a 24-hour monitoring budget.
        # Sample-specific FASTQ paths arrive in the invocation input, not these settings.
        environment = {
            "CLC_HOST": host.value_as_string, "CLC_PORT": str(port),
            "CLC_USER": user.value_as_string, "CLC_PASSWORD": password.value_as_string,
            "CLC_WORKFLOW": ctx("clc_workflow") or "",
            "CLC_PRESET": ctx("clc_preset") or "",
            "INPUT_PREFIXES": input_prefixes.value_as_string,
            "OUTPUT_PREFIXES": output_prefixes.value_as_string,
            "LEDGER_TABLE": table.table_name, "MAX_ACTIVE_ANALYSES": str(maximum),
            "MONITORING_SECONDS": "86400",
        }
        functions = []
        # Build the Linux image from runtime/Dockerfile, including the installed CLC CLI.
        # Submit calls submit_handler to start/resume a job; Status calls status_handler
        # for one status check. Lambda does not wait here for the whole analysis.
        for name, handler in (("Submit", "submit_handler"), ("Status", "status_handler")):
            function_logs = logs.LogGroup(
                self, f"{name}Logs", retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.RETAIN,
            )
            function = lambda_.DockerImageFunction(
                self, name,
                code=lambda_.DockerImageCode.from_image_asset(
                    str(Path(__file__).parent / "runtime"),
                    cmd=[f"handlers.{handler}"],
                    platform=ecr_assets.Platform.LINUX_AMD64,
                ),
                architecture=lambda_.Architecture.X86_64,
                timeout=Duration.seconds(120), memory_size=2048,
                environment=environment, vpc=vpc, security_groups=[client_sg],
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                log_group=function_logs,
            )
            # Allow the handler to read job records and atomically update reservations.
            table.grant_read_write_data(function)
            functions.append(function)
        submit_function, status_function = functions

        # The polling workflow can invoke only the Status Lambda, not the Submit Lambda.
        # It waits between checks and returns after at most 100 running checks so a
        # long analysis does not exceed Step Functions' execution-history event limit.
        monitor_role = iam.Role(self, "MonitorRole", assumed_by=iam.ServicePrincipal("states.amazonaws.com"))
        status_function.grant_invoke(monitor_role)
        monitor_machine = self.machine(
            "PollingSegment", monitor_role, monitor_definition(status_function.function_arn),
        )
        # This role is used by the per-sample workflow, not by Lambda or the CLC server.
        # It permits submission and recording monitoring failures through the handlers.
        analysis_role = iam.Role(self, "AnalysisRole", assumed_by=iam.ServicePrincipal("states.amazonaws.com"))
        submit_function.grant_invoke(analysis_role)
        status_function.grant_invoke(analysis_role)
        # Allow the per-sample workflow to start this specific polling state machine.
        # It waits for the segment's result before deciding whether another is needed.
        analysis_role.add_to_policy(iam.PolicyStatement(
            actions=["states:StartExecution"], resources=[monitor_machine.attr_arn],
        ))
        # The synchronous child-workflow integration needs to inspect/stop child executions.
        # The ARN pattern limits this permission to executions of the polling machine.
        # Stopping a child execution stops monitoring only; it does not cancel a CLC job.
        analysis_role.add_to_policy(iam.PolicyStatement(
            actions=["states:DescribeExecution", "states:StopExecution"],
            resources=[self.format_arn(
                service="states", resource="execution",
                resource_name=f"{monitor_machine.attr_name}:*",
                arn_format=ArnFormat.COLON_RESOURCE_NAME,
            )],
        ))
        # AWS uses this named EventBridge rule to notify the waiting per-sample workflow
        # when a child execution finishes. This is separate from the alert rule below.
        analysis_role.add_to_policy(iam.PolicyStatement(
            actions=["events:PutTargets", "events:PutRule", "events:DescribeRule"],
            resources=[self.format_arn(
                service="events", resource="rule",
                resource_name="StepFunctionsGetEventsForStepFunctionsExecutionRule",
            )],
        ))
        # This is the entry point callers start once per sample.
        # Its states submit once, run polling segments, and finish with success/failure.
        analysis_machine = self.machine(
            "Analysis", analysis_role,
            analysis_definition(
                submit_function.function_arn, status_function.function_arn, monitor_machine.attr_arn,
            ),
        )
        # Encrypt SNS alert messages with a dedicated key. Allow EventBridge in this
        # account to use the key so it can publish to the encrypted topic.
        alert_key = kms.Key(self, "AlertKey", enable_key_rotation=True, removal_policy=RemovalPolicy.RETAIN)
        alert_key.add_to_resource_policy(iam.PolicyStatement(
            principals=[iam.ServicePrincipal("events.amazonaws.com")],
            actions=["kms:Decrypt", "kms:GenerateDataKey"],
            resources=["*"], conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}},
        ))
        # SNS is the destination for failure notifications. Creating a topic does not
        # email anyone: an operator must add and confirm an appropriate subscription.
        topic = sns.Topic(self, "Alerts", master_key=alert_key, enforce_ssl=True)
        # Watch both state machines for failure, timeout, or manual stop events.
        # Successful executions do not match this rule.
        rule = events.Rule(
            self, "ExecutionFailures",
            event_pattern=events.EventPattern(
                source=["aws.states"], detail_type=["Step Functions Execution Status Change"],
                detail={
                    "stateMachineArn": [analysis_machine.attr_arn, monitor_machine.attr_arn],
                    "status": ["FAILED", "TIMED_OUT", "ABORTED"],
                },
            ),
        )
        # Publish only the affected execution ARN (its AWS identifier) and status.
        # Operators can use the ARN to investigate; sample input is not sent to SNS.
        rule.add_target(targets.SnsTopic(topic, message=events.RuleTargetInput.from_object({
            "execution_arn": events.EventField.from_path("$.detail.executionArn"),
            "status": events.EventField.from_path("$.detail.status"),
        })))
        # Show these identifiers after deployment so callers and operators can find
        # the per-sample entry point, tracking table, and notification topic.
        CfnOutput(self, "StateMachineArn", value=analysis_machine.attr_arn)
        CfnOutput(self, "LedgerTable", value=table.table_name)
        CfnOutput(self, "AlertsTopicArn", value=topic.topic_arn)

    def machine(self, name: str, role: iam.Role, definition: dict) -> sfn.CfnStateMachine:
        """Create a Standard state machine from a Python workflow definition.

        Attach its execution role and retained error logs, preserving explicit
        JSON nulls needed by error handlers. Return the resource for ARN references.
        """
        # Apply the same logging and role setup to both Standard state machines.
        # Standard workflows support the long waits needed for CLC analyses.
        log_group = logs.LogGroup(
            self, f"{name}HistoryLogs", retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        # Let Step Functions configure delivery of execution errors to CloudWatch Logs.
        # These log-delivery management operations require wildcard resource access.
        role.add_to_policy(iam.PolicyStatement(
            actions=[
                "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
                "logs:DeleteLogDelivery", "logs:ListLogDeliveries",
                "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
            ],
            resources=["*"],
        ))
        # Convert the Python state definitions to AWS States Language JSON.
        # Keep explicit JSON null values: ResultPath: null tells a Catch handler to
        # keep the original job context rather than replace it with an error object.
        machine = sfn.CfnStateMachine(
            self, name, role_arn=role.role_arn, state_machine_type="STANDARD",
            definition_string=json.dumps(definition),
            logging_configuration=sfn.CfnStateMachine.LoggingConfigurationProperty(
                # Log errors without copying state input/output into CloudWatch.
                # Step Functions execution history still stores that data, so restrict its access.
                level="ERROR", include_execution_data=False,
                destinations=[sfn.CfnStateMachine.LogDestinationProperty(
                    cloud_watch_logs_log_group=sfn.CfnStateMachine.CloudWatchLogsLogGroupProperty(
                        log_group_arn=log_group.log_group_arn,
                    ),
                )],
            ),
        )
        # Create the role and its policies before creating the state machine that uses them.
        machine.node.add_dependency(role)
        return machine
