import os

import aws_cdk as cdk
from dotenv import load_dotenv

from clc_server_stack import ClcServerStack

load_dotenv()

app = cdk.App()

ClcServerStack(
    app,
    "jayashri-ClcServerStack",
    analytics_reporting=False,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    ),
)

app.synth()