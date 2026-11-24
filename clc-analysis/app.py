import os

import aws_cdk as cdk

from analysis_stack import AnalysisStack


app = cdk.App()
# Deploy analysis resources separately from the existing CLC server stack.
AnalysisStack(
    app, "clc-analysis",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=app.node.try_get_context("region") or "us-west-2",
    ),
    analytics_reporting=False,
)
app.synth()
