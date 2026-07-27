"""Deterministic analysis tools.

Everything in this package is exact and model-free. The agents in
`shouldiread.agents` call these first and reason over the results, so that a
verdict traces back to a measurement rather than to a model's impression.
"""

from .artifacts import ArtifactReport, find_artifacts
from .aws_footprint import AwsFootprint, aws_footprint
from .aws_api import ApiCheck, check_aws_api_names, known_services, service_index
from .code_validation import CodeReport, validate_code
from .dedup import DuplicateIndex, DuplicateReport, cross_post_check, minhash, similarity
from .engagement import EngagementReport, engagement_ratio
from .links import LinkReport, classify_host, extract_links, verify_links
from .markdown_tools import (
    CodeBlock,
    MarkdownStats,
    extract_code_blocks,
    placeholder_density,
    strip_code,
    structure_stats,
)

__all__ = [
    "ApiCheck",
    "ArtifactReport",
    "AwsFootprint",
    "CodeBlock",
    "CodeReport",
    "DuplicateIndex",
    "DuplicateReport",
    "EngagementReport",
    "LinkReport",
    "MarkdownStats",
    "check_aws_api_names",
    "aws_footprint",
    "find_artifacts",
    "classify_host",
    "cross_post_check",
    "engagement_ratio",
    "extract_code_blocks",
    "extract_links",
    "known_services",
    "minhash",
    "placeholder_density",
    "service_index",
    "similarity",
    "strip_code",
    "structure_stats",
    "validate_code",
    "verify_links",
]
