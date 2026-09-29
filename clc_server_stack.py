from aws_cdk import RemovalPolicy, Stack, aws_sqs as sqs
from constructs import Construct


class ClcServerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        sqs.Queue(
            self,
            "DemoQueue",
            removal_policy=RemovalPolicy.DESTROY,
        )