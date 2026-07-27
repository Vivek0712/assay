#!/usr/bin/env python3
"""CDK app for ShouldIRead.

One stack. The scoring fleet itself runs on Bedrock AgentCore Runtime (deployed
separately by `infra/deploy_agentcore.py`, since AgentCore is not a CloudFormation
resource); everything here is the surrounding plumbing - ingest schedule, score
store, and the public read surfaces.
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from stacks.shouldiread_stack import ShouldIReadStack

app = cdk.App()

ShouldIReadStack(
    app,
    "ShouldIRead",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="Agentic read-quality triage for AWS Builder Center content",
)

app.synth()
