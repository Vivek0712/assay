#!/usr/bin/env python3
"""Push a scored run to the deployed site.

Uploads the leaderboard page, the extension's score index and the Atom feed to
the site bucket, seeds DynamoDB from the local run, and invalidates CloudFront.

    python scripts/publish_site.py --run full30
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shouldiread.corpus import load_scores  # noqa: E402
from shouldiread.publish import build_api_json, build_feed, build_page  # noqa: E402
from shouldiread.review_page import build_review_page  # noqa: E402

PROFILE = "heisenberg"
REGION = "us-east-1"
STACK = "ShouldIRead"

# Short TTL on the data files so a new run shows up without an invalidation;
# the page itself is invalidated explicitly below.
CACHE_CONTROL = {
    ".html": "public, max-age=60",
    ".json": "public, max-age=300",
    ".xml": "public, max-age=300",
    ".svg": "public, max-age=86400",
}


def outputs(session: boto3.Session) -> dict[str, str]:
    cf = session.client("cloudformation")
    stack = cf.describe_stacks(StackName=STACK)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", default="week")
    ap.add_argument("--skip-dynamo", action="store_true")
    ap.add_argument(
        "--replace",
        action="store_true",
        help="delete rows not present in this run, so the table matches the window",
    )
    args = ap.parse_args()

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    out = outputs(session)
    bucket, dist_id = out["SiteBucketName"], out["DistributionId"]
    print(f"site bucket   : {bucket}")
    print(f"distribution  : {dist_id}")
    print(f"site url      : {out['SiteUrl']}")

    scores = load_scores(args.run)
    if not scores:
        print(f"no scores for run {args.run!r}", file=sys.stderr)
        return 1
    print(f"scores        : {len(scores)}")

    # --- build artefacts -------------------------------------------------
    files = [
        build_page(run=args.run),
        build_api_json(run=args.run),
        build_feed(run=args.run, min_rqs=70.0),
        build_review_page(api_base=out["SiteUrl"]),
    ]
    # Brand assets are static; ship them alongside so the pages resolve the logo.
    files += [p for p in (Path("web/assay-logo.svg"), Path("web/assay-mark.svg"), Path("web/assay-banner.svg")) if p.exists()]

    s3 = session.client("s3")
    for path in files:
        ext = path.suffix
        s3.upload_file(
            str(path),
            bucket,
            path.name,
            ExtraArgs={
                "ContentType": mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
                "CacheControl": CACHE_CONTROL.get(ext, "public, max-age=300"),
            },
        )
        print(f"  uploaded {path.name} ({path.stat().st_size:,} bytes)")

    # --- seed DynamoDB ---------------------------------------------------
    if not args.skip_dynamo:
        import os

        os.environ["SCORES_TABLE"] = out["ScoresTableName"]
        from importlib import reload

        import shouldiread.store as store

        reload(store)

        table = session.resource("dynamodb").Table(out["ScoresTableName"])

        if args.replace:
            keep = {s["article_id"] for s in scores}
            stale = [
                i["article_id"]
                for i in store.all_scores()
                if i["article_id"] not in keep
            ]
            with table.batch_writer() as batch:
                for article_id in stale:
                    batch.delete_item(Key={"article_id": article_id})
            print(f"  removed {len(stale)} rows outside this run")

        with table.batch_writer(overwrite_by_pkeys=["article_id"]) as batch:
            for s in scores:
                item = dict(s)
                item["rqs_published"] = (
                    f"{1000 - int(s.get('rqs', 0)):04d}#{s.get('published_at') or ''}"
                )
                batch.put_item(Item=store._floats_to_decimals(item))
        print(f"  wrote {len(scores)} items to {out['ScoresTableName']}")

    # --- invalidate ------------------------------------------------------
    session.client("cloudfront").create_invalidation(
        DistributionId=dist_id,
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": f"publish-{args.run}-{len(scores)}",
        },
    )
    print("  invalidation requested")
    print(f"\nlive at {out['SiteUrl']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
