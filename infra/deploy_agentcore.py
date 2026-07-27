#!/usr/bin/env python3
"""Deploy the scoring fleet and the MCP server to Bedrock AgentCore Runtime.

AgentCore runtimes are not CloudFormation resources, so they are created through
the control plane API rather than in the CDK stack. Two runtimes are created:

    shouldiread_scorer   invoke-style runtime wrapping the Strands fleet
    shouldiread_mcp      MCP protocol runtime exposing the reading-queue tools

Usage:
    python infra/deploy_agentcore.py build      build and push both images
    python infra/deploy_agentcore.py deploy     create or update the runtimes
    python infra/deploy_agentcore.py status     show current runtime status
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
PROFILE = "heisenberg"
REGION = "us-east-1"

RUNTIMES = {
    "shouldiread_scorer": {
        "dockerfile": "Dockerfile.scorer",
        "repo": "shouldiread-scorer",
        "protocol": "HTTP",
    },
    "shouldiread_mcp": {
        "dockerfile": "Dockerfile.mcp",
        "repo": "shouldiread-mcp",
        "protocol": "MCP",
    },
}

EXECUTION_ROLE_NAME = "ShouldIReadAgentCoreRole"

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

PERMISSIONS = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            "Resource": [
                "arn:aws:bedrock:*::foundation-model/amazon.nova-*",
                "arn:aws:bedrock:*:*:inference-profile/*amazon.nova-*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:Scan"],
            "Resource": "arn:aws:dynamodb:*:*:table/ShouldIRead*",
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject"],
            "Resource": "arn:aws:s3:::shouldiread*/*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "ecr:GetAuthorizationToken",
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
                # AgentCore validates the image at CreateAgentRuntime time and
                # needs layer availability as well as the manifest.
                "ecr:BatchCheckLayerAvailability",
                "ecr:DescribeImages",
                "ecr:DescribeRepositories",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": "arn:aws:logs:*:*:*",
        },
    ],
}


def session() -> boto3.Session:
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


def run(cmd: list[str], **kw) -> str:
    print(f"  $ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)
    return proc.stdout.strip()


# --------------------------------------------------------------------------
def ensure_role(sess: boto3.Session) -> str:
    iam = sess.client("iam")
    try:
        role = iam.get_role(RoleName=EXECUTION_ROLE_NAME)["Role"]
        print(f"  role exists: {role['Arn']}")
    except iam.exceptions.NoSuchEntityException:
        print(f"  creating role {EXECUTION_ROLE_NAME}")
        role = iam.create_role(
            RoleName=EXECUTION_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
            Description="Execution role for ShouldIRead AgentCore runtimes",
        )["Role"]
        time.sleep(10)  # IAM propagation

    iam.put_role_policy(
        RoleName=EXECUTION_ROLE_NAME,
        PolicyName="ShouldIReadAgentCorePermissions",
        PolicyDocument=json.dumps(PERMISSIONS),
    )
    return role["Arn"]


def ensure_repo(sess: boto3.Session, name: str) -> str:
    ecr = sess.client("ecr")
    try:
        repo = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]
    except ecr.exceptions.RepositoryNotFoundException:
        repo = ecr.create_repository(
            repositoryName=name,
            imageScanningConfiguration={"scanOnPush": True},
            encryptionConfiguration={"encryptionType": "AES256"},
        )["repository"]
    return repo["repositoryUri"]


def docker_login(sess: boto3.Session) -> str:
    ecr = sess.client("ecr")
    auth = ecr.get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    endpoint = auth["proxyEndpoint"]
    subprocess.run(
        ["docker", "login", "--username", user, "--password-stdin", endpoint],
        input=password,
        text=True,
        check=True,
        capture_output=True,
    )
    return endpoint


def cmd_build(_args: argparse.Namespace) -> int:
    sess = session()
    docker_login(sess)
    for name, spec in RUNTIMES.items():
        print(f"\nbuilding {name}")
        uri = ensure_repo(sess, spec["repo"])
        tag = f"{uri}:latest"
        # AgentCore Runtime requires linux/arm64.
        run(
            [
                "docker", "buildx", "build", "--platform", "linux/arm64",
                "-f", str(ROOT / spec["dockerfile"]), "-t", tag, "--push", str(ROOT),
            ]
        )
        print(f"  pushed {tag}")
    return 0


def stack_outputs(sess: boto3.Session) -> dict[str, str]:
    """Names of the resources the CDK stack created, for the runtime env."""
    try:
        stack = sess.client("cloudformation").describe_stacks(StackName="ShouldIRead")
        return {o["OutputKey"]: o["OutputValue"] for o in stack["Stacks"][0].get("Outputs", [])}
    except Exception:
        return {}


def cmd_deploy(_args: argparse.Namespace) -> int:
    sess = session()
    role_arn = ensure_role(sess)
    control = sess.client("bedrock-agentcore-control")

    # Without these the runtimes fall back to the (empty) local score store and
    # every reading-queue call comes back with nothing.
    out = stack_outputs(sess)
    env = {
        k: v
        for k, v in {
            "SCORES_TABLE": out.get("ScoresTableName", ""),
            "PREFS_TABLE": out.get("PreferencesTableName", ""),
            "SIR_AWS_REGION": REGION,
        }.items()
        if v
    }
    print(f"  runtime env: {sorted(env)}")

    existing = {
        r["agentRuntimeName"]: r
        for r in control.list_agent_runtimes(maxResults=100).get("agentRuntimes", [])
    }

    for name, spec in RUNTIMES.items():
        uri = f"{ensure_repo(sess, spec['repo'])}:latest"
        payload = {
            "agentRuntimeName": name,
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": uri}},
            "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": spec["protocol"]},
            "roleArn": role_arn,
            "environmentVariables": env,
        }
        if name in existing:
            print(f"updating {name}")
            control.update_agent_runtime(
                agentRuntimeId=existing[name]["agentRuntimeId"], **{
                    k: v for k, v in payload.items() if k != "agentRuntimeName"
                }
            )
        else:
            print(f"creating {name}")
            control.create_agent_runtime(**payload)

    return cmd_status(_args)


def cmd_status(_args: argparse.Namespace) -> int:
    control = session().client("bedrock-agentcore-control")
    runtimes = control.list_agent_runtimes(maxResults=100).get("agentRuntimes", [])
    print(f"\n{'runtime':32}{'status':12}arn")
    for r in runtimes:
        if r["agentRuntimeName"] in RUNTIMES:
            print(f"{r['agentRuntimeName']:32}{r.get('status',''):12}{r.get('agentRuntimeArn','')}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("build").set_defaults(func=cmd_build)
    sub.add_parser("deploy").set_defaults(func=cmd_deploy)
    sub.add_parser("status").set_defaults(func=cmd_status)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
