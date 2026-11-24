import json
from pathlib import Path
import sys
import tempfile
import unittest

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis_stack import AnalysisStack


class InfrastructureTests(unittest.TestCase):
    def test_private_deployment_and_restricted_execution_permissions(self):
        with tempfile.TemporaryDirectory() as output:
            app = cdk.App(outdir=output, context={
                "vpc_id": "vpc-0123456789abcdef0", "subnet_ids": ["subnet-0123456789abcdef0"],
                "subnet_azs": ["us-west-2a"], "route_table_ids": ["rtb-0123456789abcdef0"],
                "clc_port": 7777, "clc_workflow": "approved-workflow", "clc_preset": "approved-preset",
            })
            stack = AnalysisStack(app, "TestAnalysis", env=cdk.Environment(
                account="123456789012", region="us-west-2",
            ))
            template = Template.from_stack(stack)
            template.resource_count_is("AWS::EC2::Instance", 0)
            template.resource_count_is("AWS::Lambda::Function", 2)
            template.resource_count_is("AWS::StepFunctions::StateMachine", 2)
            template.has_resource_properties("AWS::EC2::SecurityGroupIngress", {
                "FromPort": 7777, "ToPort": 7777, "IpProtocol": "tcp",
                "SourceSecurityGroupId": Match.any_value(),
                "GroupId": {"Ref": "ClcSecurityGroupId"},
            })
            template.has_resource_properties("AWS::Lambda::Function", {
                "PackageType": "Image", "Architectures": ["x86_64"],
                "Timeout": 120,
                "Environment": {"Variables": Match.object_like({
                    "CLC_HOST": {"Ref": "ClcHost"}, "CLC_PORT": "7777",
                    "CLC_PASSWORD": {"Ref": "ClcPassword"}, "MAX_ACTIVE_ANALYSES": "10",
                })},
                "VpcConfig": Match.object_like({"SubnetIds": ["subnet-0123456789abcdef0"]}),
            })
            template.has_resource_properties("AWS::DynamoDB::Table", {
                "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
                "SSESpecification": {"SSEEnabled": True},
            })
            template.has_parameter("ClcPassword", {"NoEcho": True})
            machines = template.find_resources("AWS::StepFunctions::StateMachine")
            for machine in machines.values():
                self.assertFalse(machine["Properties"]["LoggingConfiguration"]["IncludeExecutionData"])
                self.assertTrue(machine.get("DependsOn"))
                definition = machine["Properties"]["DefinitionString"]
                parts = definition["Fn::Join"][1]
                asl = json.loads("".join(part if isinstance(part, str) else "resolved-arn" for part in parts))
                for state in asl["States"].values():
                    for catch in state.get("Catch", []):
                        self.assertIn("ResultPath", catch)
                        self.assertIsNone(catch["ResultPath"])


if __name__ == "__main__":
    unittest.main()
