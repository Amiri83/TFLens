"""Per-service extractor tests against tests/fixtures/<service>/."""

from __future__ import annotations

from conftest import span


def test_ec2_instance(load):
    m = load("ec2")
    web = m.nodes["aws_instance.web"]
    assert web["service"] == "ec2"
    assert web["properties"]["instance_type"] == "t3.micro"
    assert web["location"] == {"file": "main.tf", "start_line": 63, "end_line": 72}

    ami = m.edge("RUNS_ON", "aws_instance.web", "data.aws_ami.al2023")
    assert span(ami["evidence"][0]) == ("main.tf", 64, 64)
    assert "data.aws_ami.al2023.id" in ami["evidence"][0]["snippet"]
    m.edge("RUNS_ON", "aws_instance.legacy", "external:ami:ami-0123456789abcdef0")

    # instance -> instance profile -> role -> policy (literal ARNs matched to the bucket)
    access = m.edge("ACCESS", "aws_instance.web", "aws_s3_bucket.artifacts")
    assert access["attributes"]["actions"] == ["s3:GetObject", "s3:ListBucket"]
    assert access["via"] == [
        "aws_iam_instance_profile.web",
        "aws_iam_role.web",
        "aws_iam_role_policy.web_artifacts",
    ]
    assert [span(e) for e in access["evidence"]] == [
        ("main.tf", 66, 66),
        ("main.tf", 47, 47),
        ("main.tf", 37, 41),
    ]
    m.edge("ACCESS", "external:aws_service:ec2.amazonaws.com", "aws_iam_role.web")

    m.edge("NETWORK", "aws_instance.web", "aws_security_group.web")
    ingress = m.edge("NETWORK", "external:cidr:0.0.0.0/0", "aws_security_group.web")
    assert ingress["label"] == "ingress tcp 443"
    assert ingress["attributes"]["public"] is True


def test_lambda(load):
    m = load("lambda")
    fn = "aws_lambda_function.worker"
    props = m.nodes[fn]["properties"]
    assert props["runtime"] == "nodejs20.x"
    assert props["environment_variables"] == ["LOG_LEVEL", "TABLE_NAME"]

    m.edge("RUNS_ON", fn, "external:runtime:nodejs20.x")
    env = m.edge("CONFIG", fn, "aws_dynamodb_table.items")
    assert env["label"] == "env TABLE_NAME"
    assert env["attributes"]["env_var"] == "TABLE_NAME"
    m.edge("CONFIG", fn, "aws_iam_role.fn")

    trig = m.edge("TRIGGER", "aws_sqs_queue.jobs", fn)
    assert trig["label"] == "event source mapping"
    assert trig["via"] == ["aws_lambda_event_source_mapping.jobs"]
    assert trig["attributes"]["batch_size"] == "10"

    perm = m.edge("ACCESS", "aws_s3_bucket.uploads", fn)
    assert perm["attributes"]["actions"] == ["lambda:InvokeFunction"]
    assert perm["attributes"]["principal"] == "s3.amazonaws.com"

    table = m.edge("ACCESS", fn, "aws_dynamodb_table.items")
    assert table["attributes"]["derived_via_role"] == "aws_iam_role.fn"
    assert span(table["evidence"][-1]) == ("main.tf", 41, 45)
    queue = m.edge("ACCESS", fn, "aws_sqs_queue.jobs")
    assert span(queue["evidence"][-1]) == ("main.tf", 46, 50)

    m.edge("NETWORK", fn, "aws_security_group.fn")


def test_sqs(load):
    m = load("sqs")
    q = "aws_sqs_queue.orders"
    assert m.nodes[q]["properties"]["dead_letter_queue"] == ["aws_sqs_queue.dlq"]
    assert m.edge("CONFIG", q, "aws_sqs_queue.dlq")["label"] == "redrive_policy"

    svc = m.edge("ACCESS", "external:aws_service:sns.amazonaws.com", q)
    assert svc["label"] == "Allow: sqs:SendMessage"
    assert svc["attributes"]["resource_policy"] is True
    assert svc["attributes"]["sid"] == "AllowTopic"
    assert svc["via"] == ["aws_sqs_queue_policy.orders"]
    assert span(svc["evidence"][0]) == ("main.tf", 24, 33)
    # aws:SourceArn condition links the topic itself
    m.edge("ACCESS", "aws_sns_topic.events", q)


def test_sns(load):
    m = load("sns")
    topic = "aws_sns_topic.alerts"
    sqs = m.edge("TRIGGER", topic, "aws_sqs_queue.alerts")
    assert sqs["label"] == "subscription (sqs)"
    assert sqs["attributes"]["raw_message_delivery"] == "true"
    m.edge("TRIGGER", topic, "aws_lambda_function.notifier")
    # literal topic ARN is matched to the topic by name
    email = m.edge("TRIGGER", topic, "external:email_endpoint:alerts@example.com")
    assert email["via"] == ["aws_sns_topic_subscription.email"]

    pub = m.edge("ACCESS", "external:aws_service:events.amazonaws.com", topic)
    assert pub["attributes"]["actions"] == ["sns:Publish"]
    # aws_iam_policy_document statement block is the evidence
    assert span(pub["evidence"][0]) == ("main.tf", 37, 46)


def test_dynamodb(load):
    m = load("dynamodb")
    t = "aws_dynamodb_table.orders"
    props = m.nodes[t]["properties"]
    assert props["hash_key"] == "customer_id"
    assert props["range_key"] == "order_id"
    assert props["global_secondary_indexes"] == ["by-order"]
    assert props["stream_enabled"] is True
    assert props["point_in_time_recovery"] is True

    # stream_arn -> table; literal function name -> lambda
    m.edge("TRIGGER", t, "aws_lambda_function.projector")

    role = m.edge("ACCESS", "aws_iam_role.reader", t)
    assert role["via"] == ["aws_iam_role_policy_attachment.reader", "aws_iam_policy.orders_read"]
    assert "dynamodb:Query" in role["attributes"]["actions"]
    assert role["evidence"][1]["note"] == "policy attachment"
    m.edge("ACCESS", "aws_lambda_function.projector", t)


def test_s3(load):
    m = load("s3")
    b = "aws_s3_bucket.data"
    props = m.nodes[b]["properties"]
    assert props["versioning"] == "Enabled"
    assert props["public_access_block"]["block_public_policy"] is True

    # heredoc bucket policy with interpolations
    pol = m.edge("ACCESS", "aws_iam_role.analytics", b)
    assert pol["attributes"]["actions"] == ["s3:GetObject"]
    assert pol["attributes"]["sid"] == "AnalyticsRead"
    assert span(pol["evidence"][0]) == ("main.tf", 32, 38)
    # plain JSON string trust policy
    m.edge("ACCESS", "external:aws_service:glue.amazonaws.com", "aws_iam_role.analytics")

    fn = m.edge("TRIGGER", b, "aws_lambda_function.thumbs")
    assert fn["attributes"]["events"] == ["s3:ObjectCreated:*"]
    assert fn["attributes"]["filter_prefix"] == "images/"
    assert span(fn["evidence"][0]) == ("main.tf", 59, 63)
    m.edge("TRIGGER", b, "aws_sqs_queue.ingest")


def test_iam(load):
    m = load("iam")
    role = "aws_iam_role.app"
    m.edge("ACCESS", "external:aws_service:ec2.amazonaws.com", role)
    m.edge("ACCESS", "external:aws_account:123456789012", role)

    logs = m.edge("ACCESS", role, "external:wildcard:*")
    assert logs["attributes"]["actions"] == ["logs:CreateLogStream", "logs:PutLogEvents"]

    managed = m.edge(
        "ACCESS", role, "external:aws_managed_policy:arn:aws:iam::aws:policy/ReadOnlyAccess"
    )
    assert "not expanded" in managed["label"]
    assert (
        m.nodes["external:aws_managed_policy:arn:aws:iam::aws:policy/ReadOnlyAccess"]["name"]
        == "ReadOnlyAccess"
    )

    to_bucket = m.edges("ACCESS", role, "aws_s3_bucket.reports")
    assert sorted(e["attributes"]["effect"] for e in to_bucket) == ["Allow", "Deny"]
    deny = next(e for e in to_bucket if e["attributes"]["effect"] == "Deny")
    assert deny["label"] == "Deny: s3:DeleteObject"
    assert span(deny["evidence"][0]) == ("main.tf", 46, 51)

    m.edge("ACCESS", "aws_iam_user.ci", "aws_s3_bucket.reports")
    m.edge(
        "ACCESS",
        "aws_iam_group.ops",
        "external:aws_managed_policy:arn:aws:iam::aws:policy/AdministratorAccess",
    )
    assert m.nodes["aws_iam_policy_attachment.reports"]["category"] == "relationship"
    assert m.nodes["aws_iam_policy.reports"]["category"] == "policy"


def test_security_groups(load):
    m = load("security_groups")
    app, lb = "aws_security_group.app", "aws_security_group.lb"
    assert m.edge("NETWORK", lb, app)["label"] == "ingress tcp 8080"
    assert m.edge("NETWORK", app, app)["label"] == "ingress tcp 7946"
    egress = m.edge("NETWORK", app, "external:cidr:0.0.0.0/0")
    assert egress["label"] == "egress all traffic"
    assert egress["attributes"]["public"] is True

    ssh = m.edge("NETWORK", "external:cidr:10.20.0.0/16", app)
    assert ssh["label"] == "ingress tcp 22"
    assert ssh["via"] == ["aws_security_group_rule.app_ssh"]
    assert ssh["attributes"]["public"] is False
    assert span(ssh["evidence"][0]) == ("main.tf", 38, 45)

    assert m.edge("NETWORK", "aws_vpc.main", app)["label"] == "ingress tcp 9090-9100"
    https = m.edge("NETWORK", "external:cidr:0.0.0.0/0", lb)
    assert https["via"] == ["aws_vpc_security_group_ingress_rule.lb_https"]
    m.edge("NETWORK", "aws_instance.app", app)


def test_vpc_endpoint(load):
    m = load("vpc_endpoint")
    s3 = m.edge("NETWORK", "aws_vpc_endpoint.s3", "external:aws_service:com.amazonaws.us-east-1.s3")
    assert s3["label"] == "private Gateway endpoint"
    sqs = m.edge(
        "NETWORK", "aws_vpc_endpoint.sqs", "external:aws_service:com.amazonaws.us-east-1.sqs"
    )
    assert sqs["label"] == "private Interface endpoint"
    assert m.nodes["aws_vpc_endpoint.sqs"]["properties"]["private_dns_enabled"] is True

    m.edge("NETWORK", "aws_vpc_endpoint.sqs", "aws_security_group.endpoints")
    m.edge("CONFIG", "aws_vpc_endpoint.s3", "aws_route_table.private")
    pol = m.edge("ACCESS", "external:principal:*", "aws_vpc_endpoint.sqs")
    assert pol["attributes"]["actions"] == ["sqs:SendMessage", "sqs:ReceiveMessage"]
