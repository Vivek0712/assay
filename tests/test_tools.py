"""Tool-belt tests.

The load-bearing assertions are the precision ones: `check_aws_api_names` must
flag invented APIs and must NOT flag real ones or ordinary non-AWS Python.
A false hallucination accusation is far worse than a miss.
"""

from __future__ import annotations

import pytest

from shouldiread.tools import (
    check_aws_api_names,
    cross_post_check,
    engagement_ratio,
    extract_code_blocks,
    extract_links,
    known_services,
    placeholder_density,
    structure_stats,
    validate_code,
)
from shouldiread.tools.dedup import DuplicateIndex, minhash, similarity
from shouldiread.tools.links import classify_host


# ---------------------------------------------------------------- markdown ---
def test_extract_code_blocks_handles_both_fence_styles():
    md = "intro\n```python\nx = 1\n```\ntext\n~~~bash\nls -la\n~~~\n"
    blocks = extract_code_blocks(md)
    assert [b.lang for b in blocks] == ["python", "bash"]
    assert blocks[0].body == "x = 1"


def test_extract_code_blocks_tolerates_unclosed_final_fence():
    md = "intro\n```python\nx = 1\ny = 2\n"
    blocks = extract_code_blocks(md)
    assert len(blocks) == 1
    assert "y = 2" in blocks[0].body


def test_bold_pseudo_headings_detected():
    """`**Introduction**` as a heading is a real tell found in live content."""
    md = "**Introduction**\nSome prose here.\n\n**Conclusion**\nMore prose.\n"
    stats = structure_stats(md)
    assert stats.bold_pseudo_headings == 2
    assert stats.atx_headings == 0


def test_real_headings_not_counted_as_pseudo():
    stats = structure_stats("## Real Heading\n\ntext\n\n### Another\n\nmore\n")
    assert stats.atx_headings == 2
    assert stats.bold_pseudo_headings == 0


def test_strip_code_excludes_fenced_content_from_prose_counts():
    md = "prose word\n```\nSECRETCODEWORD\n```\nmore prose\n"
    stats = structure_stats(md)
    assert stats.code_blocks == 1
    assert stats.links == 0


def test_placeholder_density_counts_only_code():
    md = "Set YOUR_BUCKET in prose.\n```bash\naws s3 ls s3://YOUR_BUCKET_NAME\n```\n"
    count, density = placeholder_density(md)
    assert count >= 1
    assert density > 0


def test_terminal_evidence_detected():
    md = "```\nTraceback (most recent call last):\nbotocore.exceptions.ClientError\n```\n"
    assert structure_stats(md).terminal_evidence >= 1


def test_measurements_detected_in_prose():
    md = "Cold start dropped from 1200 ms to 340 ms and cost $0.42 per million.\n"
    assert structure_stats(md).measurements >= 3


# ----------------------------------------------------------------- aws api ---
def test_botocore_index_is_populated():
    svcs = known_services()
    assert len(svcs) > 400
    assert {"s3", "ec2", "lambda", "dynamodb", "bedrock-runtime"} <= svcs


def test_flags_invented_operation():
    """The headline capability: an API that does not exist is caught."""
    md = """
```python
import boto3
s3 = boto3.client("s3")
s3.turbo_upload_object(Bucket="b", Key="k")
```
"""
    r = check_aws_api_names(md)
    assert "s3.turbo_upload_object" in r.invalid_calls
    assert r.total_invalid == 1


def test_accepts_real_operations():
    md = """
```python
import boto3
s3 = boto3.client("s3")
s3.put_object(Bucket="b", Key="k", Body=b"x")
s3.get_object(Bucket="b", Key="k")
ddb = boto3.client("dynamodb")
ddb.put_item(TableName="t", Item={})
```
"""
    r = check_aws_api_names(md)
    assert r.calls_checked == 3
    assert r.invalid_calls == []


def test_ignores_non_aws_method_calls():
    """Anchoring to boto3-bound variables keeps ordinary Python out."""
    md = """
```python
import pandas as pd
import boto3
df = pd.read_csv("x.csv")
df.drop_duplicates()
df.group_by_magic()
s3 = boto3.client("s3")
s3.list_buckets()
```
"""
    r = check_aws_api_names(md)
    assert r.invalid_calls == []
    assert r.calls_checked == 1


def test_flags_invented_service():
    md = '```python\nimport boto3\nc = boto3.client("quantumledger9000")\n```\n'
    assert "quantumledger9000" in check_aws_api_names(md).invalid_services


def test_cli_customizations_not_flagged():
    """`aws s3 cp` is a CLI-only command with no botocore operation behind it."""
    md = "```bash\naws s3 cp a.txt s3://bucket/\naws s3 sync . s3://bucket/\naws logs tail /aws/lambda/fn\n```\n"
    r = check_aws_api_names(md)
    assert r.invalid_cli == []


def test_flags_invented_cli_subcommand():
    md = "```bash\naws ec2 describe-instances\naws ec2 teleport-instance --id i-123\n```\n"
    r = check_aws_api_names(md)
    assert "aws ec2 teleport-instance" in r.invalid_cli
    assert r.cli_checked == 2


def test_paginator_helpers_not_treated_as_operations():
    md = """
```python
import boto3
s3 = boto3.client("s3")
p = s3.get_paginator("list_objects_v2")
```
"""
    assert check_aws_api_names(md).invalid_calls == []


def test_no_aws_content_yields_nothing():
    r = check_aws_api_names("Just prose about cooking.\n")
    assert r.total_checked == 0
    assert r.total_invalid == 0


# --------------------------------------------------------- code validation ---
def test_valid_python_passes():
    md = "```python\nimport os\n\n\ndef f(x):\n    return x + 1\n```\n"
    rep = validate_code(md)
    assert rep.checked == 1 and rep.passed == 1
    assert rep.complete_files == 1


def test_broken_python_fails():
    md = "```python\ndef f(:\n    return\n```\n"
    rep = validate_code(md)
    assert rep.failed == 1 and rep.pass_rate == 0.0


def test_broken_json_fails():
    rep = validate_code('```json\n{"a": 1,,}\n```\n')
    assert rep.failed == 1


def test_cloudformation_short_tags_accepted():
    md = """```yaml
Resources:
  Bucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Ref MyParam
```
"""
    rep = validate_code(md)
    assert rep.checked == 1 and rep.passed == 1


def test_console_output_block_not_penalised():
    """Terminal transcripts are evidence, not code that should parse."""
    md = "```bash\n$ terraform apply\nApply complete! Resources: 3 added.\n$ echo done\ndone\n```\n"
    rep = validate_code(md)
    assert rep.output_blocks == 1
    assert rep.failed == 0


def test_bracketed_root_prompt_recognised_as_transcript():
    """Real EC2 sessions print `[root@ip-172-31-44-77 ~]#`. Found live in the
    corpus, and originally missed - the strongest evidence in the whole post."""
    md = (
        "```bash\n[root@ip-172-31-44-77 ~]# pvs\n"
        "  PV         VG         Fmt  Attr PSize\n"
        "  /dev/sdb   vghanadata lvm2 a--  <8.00g\n```\n"
    )
    rep = validate_code(md)
    assert rep.output_blocks == 1
    assert rep.failed == 0
    assert structure_stats(md).terminal_evidence >= 1


def test_user_at_host_prompt_recognised():
    md = "```bash\nubuntu@ip-10-0-1-5:~$ kubectl get pods\nNAME  READY  STATUS\n```\n"
    assert validate_code(md).output_blocks == 1
    assert structure_stats(md).terminal_evidence >= 1


def test_bash_comment_is_not_a_prompt():
    """A leading `#` is a comment, not a root prompt - must stay checkable."""
    md = "```bash\n# create the bucket\naws s3 mb s3://demo\n```\n"
    rep = validate_code(md)
    assert rep.output_blocks == 0
    assert rep.checked == 1 and rep.passed == 1


# -------------------------------------------------------------------- links --
def test_classify_host():
    assert classify_host("https://docs.aws.amazon.com/s3/index.html") == "primary"
    assert classify_host("https://github.com/foo/bar") == "code"
    assert classify_host("https://dev.to/x/y") == "community"
    assert classify_host("https://twitter.com/x") == "social"
    assert classify_host("https://media2.dev.to/img.png") == "image_cdn"


def test_extract_links_skips_images_and_code():
    md = "See [docs](https://docs.aws.amazon.com/a).\n![img](https://x.com/i.png)\n```\nhttps://in-code.example\n```\n"
    urls = extract_links(md)
    assert urls == ["https://docs.aws.amazon.com/a"]


@pytest.mark.asyncio
async def test_verify_links_offline_still_classifies():
    from shouldiread.tools import verify_links

    md = "[a](https://docs.aws.amazon.com/x) and [b](https://github.com/y)\n"
    rep = await verify_links(md, check_network=False)
    assert rep.total == 2
    assert rep.primary_sources == 2
    assert rep.checked == 0


# -------------------------------------------------------------------- dedup --
def test_minhash_identical_and_different():
    a = "the quick brown fox jumps over the lazy dog " * 20
    b = "completely unrelated text about databases and indexing strategies " * 20
    assert similarity(minhash(a), minhash(a)) == 1.0
    assert similarity(minhash(a), minhash(b)) < 0.2


def test_index_finds_foreign_duplicate():
    body = "This article explains how to configure a VPC endpoint step by step. " * 30
    idx = DuplicateIndex()
    idx.add("orig", body, title="Original", author="alice")
    rep = cross_post_check(body, article_id="copy", author_alias="bob", index=idx)
    assert rep.max_similarity > 0.9
    assert rep.has_foreign_duplicate


def test_same_author_repost_is_not_foreign():
    body = "Repeated content about Lambda cold starts and provisioned concurrency. " * 30
    idx = DuplicateIndex()
    idx.add("first", body, title="First", author="alice")
    rep = cross_post_check(body, article_id="second", author_alias="alice", index=idx)
    assert rep.max_similarity > 0.9
    assert not rep.has_foreign_duplicate


def test_declared_cross_post():
    rep = cross_post_check("body", external_canonical_url="https://dev.to/a/b")
    assert rep.is_cross_post and rep.canonical_host == "dev.to"


# --------------------------------------------------------------- engagement --
def test_high_engagement_on_thin_post_is_suspicious():
    rep = engagement_ratio(likes=60, comments=10, words=350, code_blocks=0, code_loc=0)
    assert rep.suspicious


def test_engagement_never_rewards():
    """No input combination produces a positive signal - only a flag or nothing."""
    rep = engagement_ratio(likes=5000, comments=900, words=4000, code_blocks=12, code_loc=400)
    assert not rep.suspicious
    assert not hasattr(rep, "bonus")


def test_low_engagement_substantial_post_is_clean():
    rep = engagement_ratio(likes=1, comments=0, words=2500, code_blocks=8, code_loc=250, links=6)
    assert not rep.suspicious
    assert rep.substance_score > 0.7


def test_prose_mentioning_aws_is_not_parsed_as_a_cli_command():
    """Regression: found live in the corpus.

    "aws builder center" in running prose was matched as `aws <service> <cmd>`
    and reported two nonexistent services on an article containing no commands.
    """
    md = "I read about aws builder center yesterday, and aws itself keeps changing.\n"
    r = check_aws_api_names(md)
    assert r.invalid_services == []
    assert r.cli_checked == 0


def test_cli_command_in_a_code_block_is_still_checked():
    md = "```bash\naws ec2 teleport-instance --id i-1\n```\n"
    assert "aws ec2 teleport-instance" in check_aws_api_names(md).invalid_cli


def test_cli_command_in_inline_code_is_checked():
    md = "Run `aws ec2 teleport-instance --id i-1` to finish.\n"
    assert "aws ec2 teleport-instance" in check_aws_api_names(md).invalid_cli


def test_piped_and_chained_commands_are_found():
    md = "```bash\ncat x | aws ec2 describe-instances && aws s3 ls\n```\n"
    r = check_aws_api_names(md)
    assert r.cli_checked >= 1
    assert r.invalid_cli == []


def test_exception_names_are_not_suspect_apis():
    """`AccessDeniedException` is an error class, not an operation."""
    md = """
```python
import boto3
c = boto3.client("bedrock-runtime")
c.converse(modelId="m", messages=[])
```
Handle `AccessDeniedException` and `ValidationException`, and watch for `TypeError`.
"""
    assert check_aws_api_names(md).suspect_api_names == []


def test_metric_names_are_not_suspect_apis():
    md = """
```python
import boto3
c = boto3.client("elasticache")
c.describe_cache_clusters()
```
Watch the `DurabilityLag` and `EffectiveDurability` metrics.
"""
    assert check_aws_api_names(md).suspect_api_names == []


def test_verb_shaped_unknown_api_is_still_suspect():
    md = """
```python
import boto3
c = boto3.client("s3")
c.list_buckets()
```
Then call `CreateQuantumBucket` to finish.
"""
    assert "CreateQuantumBucket" in check_aws_api_names(md).suspect_api_names


def test_percentages_counted_next_to_markup():
    """Regression: `\\b` after `%` meant "**64%**" in a table cell never counted."""
    assert structure_stats("| coverage | **64%** |\n").measurements >= 1
    assert structure_stats("dropped to 12%.\n").measurements >= 1
    assert structure_stats("about 7% (measured)\n").measurements >= 1
    assert structure_stats("plain 55% here\n").measurements >= 1


def test_unlabelled_bracketed_prompt_transcript_is_not_read_as_json():
    """Regression: `[root@host ~]# cmd` starts with "[", so a JSON check ahead of
    the prompt check classified the best execution evidence in the corpus as
    malformed JSON and counted it against the article."""
    md = (
        "```\n[root@ip-172-31-44-77 ~]# pvs\n"
        "  PV         VG         Fmt  Attr PSize  PFree\n"
        "  /dev/sdb   vghanadata lvm2 a--  <8.00g    0\n```\n"
    )
    rep = validate_code(md)
    assert rep.output_blocks == 1
    assert rep.failed == 0
    assert structure_stats(md).terminal_evidence >= 1


def test_unlabelled_real_json_is_still_json():
    rep = validate_code('```\n{"a": 1,,}\n```\n')
    assert rep.failed == 1


# ------------------------------------------------------------ aws footprint --
def test_ordinary_english_is_not_read_as_aws_services():
    """Regression: `config`, `connect` and `translate` are real service ids and
    ordinary English. Matching them bare reported three AWS services on a
    robotics article that mentions none."""
    from shouldiread.tools import aws_footprint

    fp = aws_footprint("We had to connect the parts, translate the frame and fix the config.")
    assert fp.services == []


def test_prefixed_and_unambiguous_service_names_are_found():
    from shouldiread.tools import aws_footprint

    fp = aws_footprint("We used Amazon Bedrock, wrote to S3 and an AWS Lambda read it back.")
    assert set(fp.services) >= {"bedrock", "s3", "lambda"}


def test_naming_a_service_is_not_operating_it():
    """The distinction the pillar exists to draw."""
    from shouldiread.tools import aws_footprint

    named = aws_footprint("Our pipeline uses Amazon Bedrock for inference.")
    assert named.services == ["bedrock"]
    assert named.operated == 0
    assert named.names_only is True

    operated = aws_footprint(
        '```python\nimport boto3\nc = boto3.client("bedrock-runtime")\n'
        'c.converse(modelId="m", messages=[])\n```\n'
    )
    assert operated.operated > 0
    assert operated.names_only is False


def test_infrastructure_as_code_counts_as_operating():
    from shouldiread.tools import aws_footprint

    fp = aws_footprint(
        "```yaml\nResources:\n  B:\n    Type: AWS::S3::Bucket\n```\n"
        '```hcl\nresource "aws_lambda_function" "f" {}\n```\n'
    )
    assert "AWS::S3::Bucket" in fp.cfn_resources
    assert "aws_lambda_function" in fp.terraform_resources
    assert fp.operated >= 2


def test_word_boundaries_are_respected():
    from shouldiread.tools import aws_footprint

    assert aws_footprint("The s3cure protocol and glued joints held.").services == []
