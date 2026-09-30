import os

import aws_cdk as cdk
from dotenv import load_dotenv

from clc_server_stack import ClcServerStack

# override=True: the cdk CLI pre-populates CDK_DEFAULT_ACCOUNT/REGION with its
# own resolved defaults, so .env values must take precedence explicitly here.
load_dotenv(override=True)

app = cdk.App()

ClcServerStack(
    app,
    "jayashri-clc-server-stack",
    analytics_reporting=False,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    ),
)

app.synth()