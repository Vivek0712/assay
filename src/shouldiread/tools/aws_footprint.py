"""How deeply does the article actually engage with AWS?

This is Builder Center. An article can be a perfectly good piece of engineering
writing, name a single service in passing, and tell an AWS reader nothing they
could use. The original rubric could not tell that apart from a piece that gets
into IAM conditions, service quotas and the parameter that actually matters.

The distinction being measured is **naming a service versus operating it**.
Everything here is countable, and botocore supplies the authoritative service
vocabulary, so "does this article mention AWS" is never a judgement call.

Naming:      "we used Amazon Bedrock for the LLM"
Operating:   an inference profile id, a converse() call, the throttling you hit
             and the retry config that fixed it
"""

from __future__ import annotations

import functools
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import botocore.session

from .markdown_tools import extract_code_blocks, strip_code

# --------------------------------------------------------------------------
# service vocabulary, straight from the SDK
# --------------------------------------------------------------------------
# Service ids that are also ordinary English words. Matching these bare turns
# any sentence containing "config" or "connect" into an AWS service reference -
# which it did, on a rover article that mentions none of them.
AMBIGUOUS = {
    "config", "connect", "translate", "comprehend", "detective", "inspector",
    "organizations", "outposts", "personalize", "polly", "proton", "rekognition",
    "resiliencehub", "route53", "s3control", "scheduler", "schemas", "shield",
    "signer", "support", "synthetics", "textract", "timestream", "transfer",
    "waf", "budgets", "chatbot", "chime", "cleanrooms", "cloudsearch", "compute",
    "controltower", "dataexchange", "datasync", "deadline", "devicefarm",
    "discovery", "drs", "entityresolution", "evidently", "finspace", "forecast",
    "frauddetector", "gamelift", "grafana", "greengrass", "health", "honeycode",
    "imagebuilder", "inspector2", "iot", "ivs", "kendra", "keyspaces", "lakeformation",
    "license-manager", "location", "lookoutvision", "m2", "macie2", "marketplace",
    "mediaconvert", "medialive", "mgn", "monitoring", "mq", "neptune", "network-firewall",
    "notifications", "oam", "omics", "opensearch", "panorama", "payment-cryptography",
    "pinpoint", "pipes", "pricing", "qbusiness", "qldb", "ram", "rbin", "rolesanywhere",
    "rum", "sms", "snowball", "sso", "swf", "wisdom", "workdocs", "workmail",
}

# Bare names that are unambiguous in an AWS context, so they need no prefix.
UNAMBIGUOUS_BARE = {
    "s3": "s3", "ec2": "ec2", "dynamodb": "dynamodb", "cloudfront": "cloudfront",
    "cloudwatch": "cloudwatch", "cloudformation": "cloudformation", "iam": "iam",
    "ecs": "ecs", "eks": "eks", "ecr": "ecr", "sqs": "sqs", "sns": "sns",
    "rds": "rds", "vpc": "ec2", "kms": "kms", "efs": "efs", "emr": "emr",
    "fargate": "ecs", "bedrock": "bedrock", "sagemaker": "sagemaker",
    "redshift": "redshift", "athena": "athena", "glue": "glue", "kinesis": "kinesis",
    "aurora": "rds", "cognito": "cognito-idp", "appsync": "appsync",
    "amplify": "amplify", "lambda": "lambda", "step functions": "stepfunctions",
    "eventbridge": "events", "codebuild": "codebuild", "codepipeline": "codepipeline",
    "secrets manager": "secretsmanager", "systems manager": "ssm",
    "lightsail": "lightsail", "route 53": "route53", "api gateway": "apigateway",
    "opensearch": "opensearch", "elasticache": "elasticache", "agentcore": "bedrock-agentcore",
}


@functools.lru_cache(maxsize=1)
def service_names() -> dict[str, str]:
    """Prose form (lowercased) -> canonical service id.

    Built from botocore's own metadata so it tracks the SDK, but only the
    *prefixed* official names ("Amazon Bedrock", "AWS Lambda") are matched in
    prose. Bare service ids are far too collision-prone: `config`, `connect` and
    `translate` are all real services and all ordinary English.
    """
    session = botocore.session.get_session()
    names: dict[str, str] = dict(UNAMBIGUOUS_BARE)
    for service in session.get_available_services():
        try:
            meta = session.get_service_model(service).metadata
        except Exception:
            continue
        for key in ("serviceId", "serviceFullName", "serviceAbbreviation"):
            value = (meta.get(key) or "").strip()
            if not value:
                continue
            low = value.lower()
            if low.startswith(("amazon ", "aws ")):
                names[low] = service          # "amazon bedrock", "aws lambda"
            elif low not in AMBIGUOUS and len(low) > 4:
                # Prefix it ourselves rather than accepting the bare word.
                names[f"amazon {low}"] = service
                names[f"aws {low}"] = service
    return names


CFN_RESOURCE_RE = re.compile(r"\bAWS::([A-Za-z0-9]+)::([A-Za-z0-9]+)")
TF_RESOURCE_RE = re.compile(r'resource\s+"(aws_[a-z0-9_]+)"')
CDK_IMPORT_RE = re.compile(r"\b(?:from|import)\s+aws_cdk|@aws-cdk/|aws-cdk-lib")
ARN_RE = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:")
IAM_STATEMENT_RE = re.compile(r'"(?:Effect|Action|Resource|Principal|Condition)"\s*:')
# Numbers attached to a service limit are a strong tell of operating experience.
QUOTA_RE = re.compile(
    r"\b(quota|limit|throttl|burst|concurrency|rps|tps|provisioned|reserved|"
    r"cold start|p9\d|timeout|retry|backoff|partition key|hot key)\w*\b",
    re.I,
)
REGION_RE = re.compile(r"\b(?:us|eu|ap|sa|ca|me|af|il)-(?:[a-z]+)-\d\b")


@dataclass
class AwsFootprint:
    services: list[str] = field(default_factory=list)
    """Canonical service ids referenced anywhere."""

    services_in_code: list[str] = field(default_factory=list)
    """Services actually constructed or invoked in code, not just named."""

    operations_invoked: int = 0
    cfn_resources: list[str] = field(default_factory=list)
    terraform_resources: list[str] = field(default_factory=list)
    uses_cdk: bool = False
    arns: int = 0
    iam_statements: int = 0
    quota_mentions: int = 0
    regions: list[str] = field(default_factory=list)

    @property
    def breadth(self) -> int:
        return len(self.services)

    @property
    def operated(self) -> int:
        """Count of signals that mean 'operated', not merely 'named'."""
        return (
            len(self.services_in_code)
            + self.operations_invoked
            + len(self.cfn_resources)
            + len(self.terraform_resources)
            + int(self.uses_cdk)
            + min(self.arns, 5)
            + min(self.iam_statements, 5)
        )

    @property
    def names_only(self) -> bool:
        """Services appear in prose but nothing shows them being configured."""
        return self.breadth > 0 and self.operated == 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["breadth"] = self.breadth
        d["operated"] = self.operated
        d["names_only"] = self.names_only
        return d


def aws_footprint(markdown: str) -> AwsFootprint:
    """Measure how far past naming a service the article goes."""
    from .aws_api import check_aws_api_names

    report = AwsFootprint()
    prose = strip_code(markdown)
    code = "\n".join(b.body for b in extract_code_blocks(markdown))

    # What the code actually touches, reusing the checker's anchored extraction.
    api = check_aws_api_names(markdown)
    report.services_in_code = sorted(set(api.services_referenced))
    report.operations_invoked = api.total_checked

    found: set[str] = set(report.services_in_code)
    lowered = prose.lower()
    import re as _re

    for display, service in service_names().items():
        # Word-boundary match: "s3" must not fire inside "s3cure", and the
        # prefixed forms must appear as written.
        if _re.search(rf"(?<![\w-]){_re.escape(display)}(?![\w-])", lowered):
            found.add(service)
    report.services = sorted(found)

    report.cfn_resources = sorted({f"AWS::{a}::{b}" for a, b in CFN_RESOURCE_RE.findall(markdown)})
    report.terraform_resources = sorted(set(TF_RESOURCE_RE.findall(code)))
    report.uses_cdk = bool(CDK_IMPORT_RE.search(markdown))
    report.arns = len(ARN_RE.findall(markdown))
    report.iam_statements = len(IAM_STATEMENT_RE.findall(code))
    report.quota_mentions = len(QUOTA_RE.findall(prose))
    report.regions = sorted(set(REGION_RE.findall(markdown)))
    return report
