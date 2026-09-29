# CLC Server CDK

Python AWS CDK project for the future CLC Genomics Server EC2 deployment in an existing VPC. The stack currently creates a simple SQS queue that can be removed with the stack.

## Prerequisites

- Python 3.11 or later
- Node.js and npm
- AWS credentials and permissions for the target account to deploy

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
sudo npm install -g aws-cdk@2.1143.0
nvm exec 22 cdk synth
nvm exec 22 cdk deploy
```

The `cdk.json` file points the CDK CLI to the Python app. The CDK Toolkit writes generated templates to the ignored `cdk.out/` directory; `synth` does not deploy them. Deploying requires AWS credentials and a bootstrapped target account. To remove the stack and its queue later, run `nvm exec 22 cdk destroy`.

## Next step

Add EC2 resources to `clc_server_stack.py`, referencing the existing VPC without creating a new one. Keep account-specific IDs and secrets out of source control. Configure the required PRSDLC/Gatekeeper pull-request checks with the organization's reusable workflows when publishing this repository.