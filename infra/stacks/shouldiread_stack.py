"""ShouldIRead infrastructure.

    EventBridge (hourly)
        -> ingest Lambda   discover via /rss + sitemaps, fetch bodies, store raw
        -> score  Lambda   run the Strands fleet, write scores to DynamoDB
    API Gateway (HTTP)
        -> serve  Lambda   /scores.json, /feed.xml, /api/score/{id}, /api/queue
    CloudFront
        -> S3 (static leaderboard page)  and  /api/* -> API Gateway

Public by choice: the leaderboard, feed and MCP surface are all open. What that
buys is a demo anyone can click; what it costs is a scraping surface, so reads
are cached hard at the edge and the write path is not exposed at all.
"""

from __future__ import annotations

from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

BEDROCK_MODELS = [
    "arn:aws:bedrock:*::foundation-model/amazon.nova-*",
    "arn:aws:bedrock:*:*:inference-profile/*amazon.nova-*",
]


class ShouldIReadStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------- data
        scores_table = ddb.Table(
            self,
            "ScoresTable",
            partition_key=ddb.Attribute(name="article_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
        )
        # Query "best of the last N days" without scanning the table.
        scores_table.add_global_secondary_index(
            index_name="by-verdict-rqs",
            partition_key=ddb.Attribute(name="verdict", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="rqs_published", type=ddb.AttributeType.STRING),
        )

        prefs_table = ddb.Table(
            self,
            "PreferencesTable",
            partition_key=ddb.Attribute(name="subscriber_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        raw_bucket = s3.Bucket(
            self,
            "RawContent",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    # Raw markdown is only needed until an article is scored;
                    # keep a window for re-scoring after a prompt change.
                    expiration=Duration.days(90),
                    noncurrent_version_expiration=Duration.days(7),
                )
            ],
        )

        site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ------------------------------------------------------------- lambdas
        common_env = {
            "SCORES_TABLE": scores_table.table_name,
            "PREFS_TABLE": prefs_table.table_name,
            "RAW_BUCKET": raw_bucket.bucket_name,
            "SITE_BUCKET": site_bucket.bucket_name,
        }

        # Bundle dependencies into the asset. Zipping the source alone would
        # deploy a function that dies on `import strands`; the bundling image
        # pip-installs the project into the asset staging directory.
        code = lambda_.Code.from_asset(
            "..",
            exclude=[
                ".venv", "data", "infra", "extension", ".git", "tests",
                "**/__pycache__", "*.egg-info", "docs", "blog", "web",
            ],
            bundling=BundlingOptions(
                image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                platform="linux/arm64",
                command=[
                    "bash",
                    "-c",
                    " && ".join(
                        [
                            "pip install --no-cache-dir . -t /asset-output",
                            "cp -r handlers /asset-output/",
                            "find /asset-output -name '__pycache__' -type d -prune -exec rm -rf {} +",
                            "find /asset-output -name '*.dist-info' -type d -prune -exec rm -rf {} +",
                        ]
                    ),
                ],
            ),
        )

        def make_fn(name: str, handler: str, *, timeout: int, memory: int) -> lambda_.Function:
            fn = lambda_.Function(
                self,
                name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                handler=handler,
                code=code,
                timeout=Duration.minutes(timeout),
                memory_size=memory,
                environment=common_env,
                # An explicit log group, not the deprecated `log_retention`
                # property - that one provisions a custom resource per function.
                log_group=logs.LogGroup(
                    self,
                    f"{name}Logs",
                    retention=logs.RetentionDays.TWO_WEEKS,
                    removal_policy=RemovalPolicy.DESTROY,
                ),
            )
            scores_table.grant_read_write_data(fn)
            prefs_table.grant_read_write_data(fn)
            raw_bucket.grant_read_write(fn)
            return fn

        ingest_fn = make_fn("IngestFn", "handlers.ingest.handler", timeout=10, memory=1024)
        score_fn = make_fn("ScoreFn", "handlers.score.handler", timeout=15, memory=2048)
        serve_fn = make_fn("ServeFn", "handlers.serve.handler", timeout=1, memory=1024)

        score_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=BEDROCK_MODELS,
            )
        )
        site_bucket.grant_write(serve_fn)
        site_bucket.grant_write(score_fn)
        score_fn.grant_invoke(ingest_fn)
        ingest_fn.add_environment("SCORE_FUNCTION", score_fn.function_name)

        # ------------------------------------------------------------ schedule
        events.Rule(
            self,
            "HourlyIngest",
            schedule=events.Schedule.rate(Duration.hours(1)),
            targets=[targets.LambdaFunction(ingest_fn)],
            description="Poll the Builder Center Atom feed and score anything new",
        )

        # ----------------------------------------------------------------- api
        http_api = apigw.HttpApi(
            self,
            "Api",
            api_name="shouldiread",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.OPTIONS],
                allow_headers=["content-type"],
            ),
        )
        http_api.add_routes(
            path="/{proxy+}",
            methods=[apigw.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("ServeIntegration", serve_fn),
        )

        # ---------------------------------------------------------- cloudfront
        api_domain = f"{http_api.api_id}.execute-api.{self.region}.amazonaws.com"

        # Scores are recomputed hourly at most, so cache them hard and let the
        # ingest job invalidate. Cheap, and it keeps a public endpoint from
        # turning into a per-request Lambda bill.
        api_cache = cloudfront.CachePolicy(
            self,
            "ApiCachePolicy",
            default_ttl=Duration.minutes(10),
            min_ttl=Duration.minutes(1),
            max_ttl=Duration.hours(1),
            query_string_behavior=cloudfront.CacheQueryStringBehavior.all(),
            enable_accept_encoding_gzip=True,
            enable_accept_encoding_brotli=True,
        )

        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                compress=True,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(api_domain),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=api_cache,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                    compress=True,
                ),
            },
            default_root_object="index.html",
            comment="ShouldIRead - Builder Center triage",
        )

        for fn in (serve_fn, score_fn):
            fn.add_environment("DISTRIBUTION_ID", distribution.distribution_id)
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["cloudfront:CreateInvalidation"],
                    resources=[
                        f"arn:aws:cloudfront::{self.account}:distribution/"
                        f"{distribution.distribution_id}"
                    ],
                )
            )

        # -------------------------------------------------------------- output
        CfnOutput(self, "SiteUrl", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "ScoresTableName", value=scores_table.table_name)
        CfnOutput(self, "PreferencesTableName", value=prefs_table.table_name)
        CfnOutput(self, "SiteBucketName", value=site_bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
