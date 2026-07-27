"""AWS API hallucination detector.

botocore ships the authoritative machine-readable definition of every AWS
service - 426 of them locally, with full operation lists. That makes it possible
to check, without a model and without guessing, whether the APIs an article
tells you to call actually exist.

An invented API is the cleanest possible proof that nobody ran the code. Real
execution fails loudly on a fake operation name; only unexecuted prose keeps it.

Precision matters more than recall here. A false accusation of hallucination is
much worse than a miss, so every check is anchored to a syntactic context that
is unambiguously an AWS call (a boto3 client bound to a known service, or an
`aws` CLI invocation). Ambiguous PascalCase in prose is reported separately as
`suspect`, never as `invalid`.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import Any

import botocore.session
from botocore.exceptions import UnknownServiceError

from .markdown_tools import extract_code_blocks, strip_code


# --------------------------------------------------------------------------
# botocore index
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ServiceIndex:
    """Operation names for one service, in the forms they appear in the wild."""

    name: str
    operations: frozenset[str]  # PascalCase, e.g. CreateBucket
    snake_operations: frozenset[str]  # boto3 method, e.g. create_bucket
    cli_operations: frozenset[str]  # CLI subcommand, e.g. create-bucket


def _snake(pascal: str) -> str:
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", pascal)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    return s.lower()


@functools.lru_cache(maxsize=1)
def _session() -> botocore.session.Session:
    return botocore.session.get_session()


@functools.lru_cache(maxsize=1)
def known_services() -> frozenset[str]:
    """Every service id botocore knows, plus the CLI aliases boto3 accepts."""
    svcs = set(_session().get_available_services())
    # `s3api` is a CLI-only name for the raw S3 API surface.
    svcs.add("s3api")
    return frozenset(svcs)


@functools.lru_cache(maxsize=512)
def service_index(service: str) -> ServiceIndex | None:
    canonical = "s3" if service == "s3api" else service
    try:
        model = _session().get_service_model(canonical)
    except (UnknownServiceError, Exception):
        return None
    ops = frozenset(model.operation_names)
    return ServiceIndex(
        name=service,
        operations=ops,
        snake_operations=frozenset(_snake(o) for o in ops),
        cli_operations=frozenset(_snake(o).replace("_", "-") for o in ops),
    )


@functools.lru_cache(maxsize=1)
def all_operations() -> frozenset[str]:
    """Union of every operation name across every service."""
    out: set[str] = set()
    for svc in _session().get_available_services():
        idx = service_index(svc)
        if idx:
            out |= idx.operations
    return frozenset(out)


# CLI commands implemented by the CLI itself rather than by a botocore
# operation. Checking these against botocore would produce false positives.
CLI_CUSTOMIZATIONS: dict[str, set[str]] = {
    "s3": {"cp", "ls", "mb", "mv", "presign", "rb", "rm", "sync", "website"},
    "logs": {"tail", "start-live-tail"},
    "ecr": {"get-login-password", "get-login"},
    "ecr-public": {"get-login-password"},
    "eks": {"update-kubeconfig", "get-token"},
    "deploy": {"push", "install", "uninstall", "register"},
    "emr": {"ssh", "socks", "get", "put", "create-cluster", "install-applications"},
    "cloudformation": {"deploy", "package"},
    "gamelift": {"upload-build", "get-game-session-log"},
    "opsworks": {"register"},
    "rds": {"generate-db-auth-token"},
    "dynamodb": {"wizard"},
    "configure": {"set", "get", "list", "add-model", "import", "sso", "sso-session"},
    "sso": {"login", "logout"},
    "ssm": {"start-session"},
    "codeartifact": {"login"},
    "history": {"list", "show"},
    "cloudtrail": {"validate-logs", "create-subscription"},
}
_META_CLI = {"help", "configure", "history", "sso", "--version"}


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------
# x = boto3.client("s3")   /   boto3.Session(...).resource('dynamodb')
BOTO_BIND_RE = re.compile(
    r"""(?:(?P<var>[A-Za-z_]\w*)\s*=\s*)?
        \b(?:boto3|session|sess|Session\(\)|boto3\.Session\([^)]*\))
        \s*\.\s*(?:client|resource)\s*\(\s*["'](?P<svc>[a-z0-9.\-]+)["']""",
    re.X,
)
# Any .client("x") / .resource("x") call, to catch service names we missed above.
ANY_CLIENT_RE = re.compile(
    r"\.\s*(?:client|resource)\s*\(\s*[\"']([a-z0-9.\-]+)[\"']"
)
METHOD_CALL_RE = re.compile(r"\b(?P<var>[A-Za-z_]\w*)\s*\.\s*(?P<meth>[a-z][a-z0-9_]{2,})\s*\(")
AWS_CLI_RE = re.compile(
    r"(?:^|[\n;|&(`$])\s*aws\s+(?:--[\w-]+(?:[ =]\S+)?\s+)*"
    r"(?P<svc>[a-z][a-z0-9-]{1,40})\s+(?P<cmd>[a-z][a-z0-9-]{1,60})"
)
# Backticked multi-word PascalCase: `CreateDetector`, `GetFindings`.
BACKTICK_PASCAL_RE = re.compile(r"`([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+){1,6})`")

# AWS operation names are verb-first and the verb set is small. Requiring one
# keeps CloudWatch metric names, IAM condition keys and class names out of the
# suspect bucket - they look like operations but were never meant to be.
API_VERBS = (
    "Accept|Activate|Add|Allocate|Assign|Associate|Attach|Authorize|Batch|Cancel|Claim|Clone|"
    "Complete|Confirm|Connect|Copy|Create|Deactivate|Declare|Decrease|Delete|Deregister|"
    "Describe|Detach|Disable|Disassociate|Discover|Enable|Estimate|Evaluate|Execute|Export|"
    "Generate|Get|Grant|Import|Increase|Initiate|Invoke|Issue|Join|List|Lookup|Modify|Move|"
    "Poll|Prepare|Provision|Publish|Purchase|Put|Query|Reboot|Rebuild|Record|Redeem|Register|"
    "Reject|Release|Remove|Rename|Renew|Replace|Report|Request|Reset|Resolve|Restore|Resume|"
    "Retrieve|Revoke|Run|Scan|Search|Select|Send|Set|Start|Stop|Submit|Subscribe|Suspend|"
    "Sync|Tag|Terminate|Test|Transfer|Unassign|Unsubscribe|Untag|Update|Upgrade|Upload|"
    "Validate|Verify|Wait|Write"
)
API_SHAPED_RE = re.compile(rf"^(?:{API_VERBS})[A-Z]")
# Names that are shaped like operations but are something else entirely.
NON_API_SUFFIXES = ("Exception", "Error", "Warning", "Fault", "Failure")

# boto3 methods that are real but are not service operations.
NON_OPERATION_METHODS = {
    "get_paginator", "get_waiter", "can_paginate", "close", "paginate", "wait",
    "meta", "exceptions", "generate_presigned_url", "generate_presigned_post",
    "upload_file", "download_file", "upload_fileobj", "download_fileobj",
    "copy", "load", "reload", "all", "filter", "batch_writer", "put_item_batch",
}


@dataclass
class ApiCheck:
    services_referenced: list[str] = field(default_factory=list)
    invalid_services: list[str] = field(default_factory=list)
    calls_checked: int = 0
    invalid_calls: list[str] = field(default_factory=list)
    cli_checked: int = 0
    invalid_cli: list[str] = field(default_factory=list)
    suspect_api_names: list[str] = field(default_factory=list)

    @property
    def total_checked(self) -> int:
        return self.calls_checked + self.cli_checked

    @property
    def total_invalid(self) -> int:
        return len(self.invalid_calls) + len(self.invalid_cli) + len(self.invalid_services)

    @property
    def invalid_ratio(self) -> float:
        return self.total_invalid / self.total_checked if self.total_checked else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "services_referenced": sorted(set(self.services_referenced)),
            "invalid_services": sorted(set(self.invalid_services)),
            "calls_checked": self.calls_checked,
            "invalid_calls": sorted(set(self.invalid_calls)),
            "cli_checked": self.cli_checked,
            "invalid_cli": sorted(set(self.invalid_cli)),
            "suspect_api_names": sorted(set(self.suspect_api_names)),
            "total_checked": self.total_checked,
            "total_invalid": self.total_invalid,
            "invalid_ratio": round(self.invalid_ratio, 3),
        }


def check_aws_api_names(md: str) -> ApiCheck:
    """Verify every AWS API the article tells you to call actually exists."""
    result = ApiCheck()
    blocks = extract_code_blocks(md)
    code = "\n".join(b.body for b in blocks)
    prose = strip_code(md)
    services = known_services()

    # 1. Bind variables to services, and validate the service names themselves.
    var_to_svc: dict[str, str] = {}
    for m in BOTO_BIND_RE.finditer(code):
        svc = m.group("svc")
        if svc in services:
            result.services_referenced.append(svc)
            if m.group("var"):
                var_to_svc[m.group("var")] = svc
        else:
            result.invalid_services.append(svc)

    for svc in ANY_CLIENT_RE.findall(code):
        if svc in services:
            result.services_referenced.append(svc)
        elif svc not in result.invalid_services:
            result.invalid_services.append(svc)

    # 2. Method calls on those bound variables only. Anchoring to a known
    #    client variable is what keeps `df.head()` out of the results.
    for m in METHOD_CALL_RE.finditer(code):
        var, meth = m.group("var"), m.group("meth")
        svc = var_to_svc.get(var)
        if not svc or meth in NON_OPERATION_METHODS:
            continue
        idx = service_index(svc)
        if not idx:
            continue
        result.calls_checked += 1
        if meth not in idx.snake_operations:
            result.invalid_calls.append(f"{svc}.{meth}")

    # 3. AWS CLI invocations - only where a command can actually live. Scanning
    #    free prose matched English like "aws builder center" as `aws <service>
    #    <command>` and reported two nonexistent services on an article that
    #    contained no commands at all.
    from .markdown_tools import INLINE_CODE_RE

    command_text = "\n".join(
        [code] + [f"\n{m}" for m in INLINE_CODE_RE.findall(strip_code(md))]
    )
    for m in AWS_CLI_RE.finditer(command_text):
        svc, cmd = m.group("svc"), m.group("cmd")
        if svc in _META_CLI or cmd.startswith("-"):
            continue
        if svc not in services:
            result.invalid_services.append(svc)
            continue
        result.services_referenced.append(svc)
        if cmd in CLI_CUSTOMIZATIONS.get(svc, set()) or cmd == "help" or cmd == "wait":
            continue
        idx = service_index(svc)
        if not idx:
            continue
        result.cli_checked += 1
        if cmd not in idx.cli_operations:
            result.invalid_cli.append(f"aws {svc} {cmd}")

    # 4. Backticked PascalCase in prose. Low confidence by construction, so this
    #    is only ever "suspect" - the model adjudicates, the score never
    #    penalises on this alone.
    if result.services_referenced:
        ops = all_operations()
        for name in BACKTICK_PASCAL_RE.findall(prose):
            if name in ops:
                continue
            if name.endswith(NON_API_SUFFIXES):
                continue  # AccessDeniedException is an error, not an operation
            if not API_SHAPED_RE.match(name):
                continue  # metric names and class names are not operations
            result.suspect_api_names.append(name)

    return result
