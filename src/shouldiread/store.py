"""DynamoDB persistence for scores and preferences.

Falls back to the local JSONL files when the tables are not configured, so the
same code path works on a laptop and in Lambda without branching at call sites.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from .corpus import load_scores
from .preferences import Preferences

SCORES_TABLE = os.environ.get("SCORES_TABLE")
PREFS_TABLE = os.environ.get("PREFS_TABLE")
RAW_BUCKET = os.environ.get("RAW_BUCKET")


def _resource():
    return boto3.resource("dynamodb")


def _decimals_to_floats(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_decimals_to_floats(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _decimals_to_floats(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        f = float(obj)
        return int(f) if f.is_integer() else f
    return obj


def _floats_to_decimals(obj: Any) -> Any:
    """DynamoDB rejects floats. Round-trip through JSON to normalise everything."""
    return json.loads(json.dumps(obj), parse_float=Decimal, parse_int=Decimal)


# --------------------------------------------------------------------------
def put_score(score: dict[str, Any]) -> None:
    if not SCORES_TABLE:
        return
    item = dict(score)
    # Sort key for the GSI: RQS descending within a verdict, newest first on ties.
    item["rqs_published"] = f"{1000 - int(score.get('rqs', 0)):04d}#{score.get('published_at') or ''}"
    _resource().Table(SCORES_TABLE).put_item(Item=_floats_to_decimals(item))


def get_score(article_id: str) -> dict[str, Any] | None:
    if not SCORES_TABLE:
        for s in load_scores():
            if s["article_id"] == article_id:
                return s
        return None
    res = _resource().Table(SCORES_TABLE).get_item(Key={"article_id": article_id})
    item = res.get("Item")
    return _decimals_to_floats(item) if item else None


def all_scores(limit: int = 2000) -> list[dict[str, Any]]:
    """Every scored article. Small corpus, so a paginated scan is fine."""
    if not SCORES_TABLE:
        return load_scores()

    table = _resource().Table(SCORES_TABLE)
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {}
    while len(items) < limit:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        key = page.get("LastEvaluatedKey")
        if not key:
            break
        kwargs["ExclusiveStartKey"] = key
    return _decimals_to_floats(items[:limit])


def top_by_verdict(verdict: str = "READ", limit: int = 50) -> list[dict[str, Any]]:
    """Best articles for a verdict, straight off the GSI - no scan."""
    if not SCORES_TABLE:
        scores = [s for s in load_scores() if s["verdict"] == verdict]
        scores.sort(key=lambda s: s["rqs"], reverse=True)
        return scores[:limit]

    res = _resource().Table(SCORES_TABLE).query(
        IndexName="by-verdict-rqs",
        KeyConditionExpression=Key("verdict").eq(verdict),
        Limit=limit,
    )
    return _decimals_to_floats(res.get("Items", []))


def scored_ids() -> set[str]:
    return {s["article_id"] for s in all_scores()}


# --------------------------------------------------------------------------
def put_preferences(subscriber_id: str, prefs: Preferences) -> None:
    if not PREFS_TABLE:
        return
    _resource().Table(PREFS_TABLE).put_item(
        Item=_floats_to_decimals({"subscriber_id": subscriber_id, **prefs.to_dict()})
    )


def get_preferences(subscriber_id: str = "default") -> Preferences:
    if not PREFS_TABLE:
        return Preferences()
    res = _resource().Table(PREFS_TABLE).get_item(Key={"subscriber_id": subscriber_id})
    item = res.get("Item")
    if not item:
        return Preferences()
    return Preferences.from_dict(_decimals_to_floats(item))


# --------------------------------------------------------------------------
def put_raw(article_id: str, payload: dict[str, Any]) -> None:
    if not RAW_BUCKET:
        return
    boto3.client("s3").put_object(
        Bucket=RAW_BUCKET,
        Key=f"articles/{article_id}.json",
        Body=json.dumps(payload, ensure_ascii=False).encode(),
        ContentType="application/json",
    )
