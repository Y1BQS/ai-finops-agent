import json
import os
from datetime import datetime, timezone, timedelta

import boto3


ec2 = boto3.client("ec2")
cloudwatch = boto3.client("cloudwatch")
logs = boto3.client("logs")
eks = boto3.client("eks")
elb = boto3.client("elbv2")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _days_between(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 86400))


def _build_finding(
    *,
    resource_type: str,
    resource_id: str,
    region: str,
    estimated_monthly_cost: float,
    age_days: int | None = None,
    tags: dict | None = None,
    risk_level: str = "LOW",
    recommended_action: str = "",
    extra: dict | None = None,
) -> dict:
    finding = {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "region": region,
        "estimated_monthly_cost": round(float(estimated_monthly_cost), 4),
        "age_days": age_days,
        "tags": tags or {},
        "risk_level": risk_level,
        "recommended_action": recommended_action,
    }
    if extra:
        finding.update(extra)
    return finding


def _get_volume_tags(volume: dict) -> dict:
    return {t["Key"]: t["Value"] for t in volume.get("Tags", [])}


def _scan_unattached_ebs(region: str) -> list[dict]:
    """Detect unattached EBS volumes."""
    regional_ec2 = boto3.client("ec2", region_name=region)
    findings: list[dict] = []
    paginator = regional_ec2.get_paginator("describe_volumes")
    for page in paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
        for vol in page.get("Volumes", []):
            volume_id = vol["VolumeId"]
            size_gb = vol.get("Size", 0)
            create_time = vol.get("CreateTime")
            age_days = _days_between(create_time, _now_utc()) if isinstance(create_time, datetime) else None

            # Very rough, fixed EBS gp3 price approximation in USD per GB-month (can be tuned)
            price_per_gb_month = float(os.getenv("HYGIENE_EBS_GB_MONTH_PRICE", "0.08"))
            estimated_cost = size_gb * price_per_gb_month

            findings.append(
                _build_finding(
                    resource_type="EBS_VOLUME",
                    resource_id=volume_id,
                    region=region,
                    estimated_monthly_cost=estimated_cost,
                    age_days=age_days,
                    tags=_get_volume_tags(vol),
                    risk_level="MEDIUM",
                    recommended_action="Delete volume if no longer needed",
                    extra={"size_gb": size_gb},
                )
            )
    return findings


def _scan_old_snapshots(region: str, min_age_days: int = 3) -> list[dict]:
    """Detect old EBS snapshots owned by this account."""
    regional_ec2 = boto3.client("ec2", region_name=region)
    findings: list[dict] = []
    paginator = regional_ec2.get_paginator("describe_snapshots")
    for page in paginator.paginate(OwnerIds=["self"]):
        for snap in page.get("Snapshots", []):
            snap_id = snap["SnapshotId"]
            start_time = snap.get("StartTime")
            age_days = _days_between(start_time, _now_utc()) if isinstance(start_time, datetime) else None
            if age_days is None or age_days < min_age_days:
                continue

            # Simple approximation: snapshot size ~ volume size, 0.05 USD / GB-month
            size_gb = snap.get("VolumeSize", 0)
            price_per_gb_month = float(os.getenv("HYGIENE_SNAPSHOT_GB_MONTH_PRICE", "0.05"))
            estimated_cost = size_gb * price_per_gb_month

            findings.append(
                _build_finding(
                    resource_type="EBS_SNAPSHOT",
                    resource_id=snap_id,
                    region=region,
                    estimated_monthly_cost=estimated_cost,
                    age_days=age_days,
                    tags={t["Key"]: t["Value"] for t in snap.get("Tags", [])},
                    risk_level="LOW",
                    recommended_action="Review and delete stale snapshot if no longer required",
                    extra={"size_gb": size_gb},
                )
            )
    return findings


def _scan_unused_eips(region: str) -> list[dict]:
    """Detect Elastic IPs not currently associated."""
    regional_ec2 = boto3.client("ec2", region_name=region)
    findings: list[dict] = []
    addresses = regional_ec2.describe_addresses().get("Addresses", [])
    for addr in addresses:
        if addr.get("AssociationId"):
            continue

        allocation_id = addr.get("AllocationId") or addr.get("PublicIp")
        # Very rough EIP cost estimate (~3.5 USD / month when unused)
        estimated_cost = float(os.getenv("HYGIENE_EIP_MONTH_PRICE", "3.5"))

        findings.append(
            _build_finding(
                resource_type="ELASTIC_IP",
                resource_id=str(allocation_id),
                region=region,
                estimated_monthly_cost=estimated_cost,
                age_days=None,
                tags={},
                risk_level="MEDIUM",
                recommended_action="Release unused Elastic IP",
            )
        )
    return findings


def _metric_has_traffic(namespace: str, metric_name: str, dimensions: list[dict], region: str) -> bool:
    regional_cw = boto3.client("cloudwatch", region_name=region)
    end_time = _now_utc()
    start_time = end_time - timedelta(days=7)
    response = regional_cw.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        StartTime=start_time,
        EndTime=end_time,
        Period=3600,
        Statistics=["Sum"],
    )
    for datapoint in response.get("Datapoints", []):
        if datapoint.get("Sum", 0) > 0:
            return True
    return False


def _scan_idle_nat_gateways(region: str) -> list[dict]:
    """Detect NAT gateways with no recent traffic."""
    regional_ec2 = boto3.client("ec2", region_name=region)
    findings: list[dict] = []
    nats = regional_ec2.describe_nat_gateways().get("NatGateways", [])
    for nat in nats:
        nat_id = nat["NatGatewayId"]
        # For simplicity, we treat NAT as idle if BytesOutToDestination has been zero for last 7 days
        try:
            has_traffic = _metric_has_traffic(
                namespace="AWS/NATGateway",
                metric_name="BytesOutToDestination",
                dimensions=[{"Name": "NatGatewayId", "Value": nat_id}],
                region=region,
            )
        except Exception:
            # If metrics are unavailable, do not flag
            continue

        if has_traffic:
            continue

        # Very rough NAT Gateway price (~32 USD / month)
        estimated_cost = float(os.getenv("HYGIENE_NAT_MONTH_PRICE", "32.0"))

        findings.append(
            _build_finding(
                resource_type="NAT_GATEWAY",
                resource_id=nat_id,
                region=region,
                estimated_monthly_cost=estimated_cost,
                age_days=None,
                tags={},
                risk_level="HIGH",
                recommended_action="Remove or downsize idle NAT Gateway",
            )
        )
    return findings


def _scan_idle_load_balancers(region: str) -> list[dict]:
    """Detect Application / Network Load Balancers with no traffic."""
    regional_elb = boto3.client("elbv2", region_name=region)
    findings: list[dict] = []

    paginator = regional_elb.get_paginator("describe_load_balancers")
    for page in paginator.paginate():
        for lb_desc in page.get("LoadBalancers", []):
            lb_arn = lb_desc["LoadBalancerArn"]
            lb_name = lb_desc["LoadBalancerName"]
            lb_type = lb_desc.get("Type", "application").upper()

            metric_name = "RequestCount" if lb_type == "APPLICATION" else "ActiveFlowCount"
            try:
                has_traffic = _metric_has_traffic(
                    namespace="AWS/ApplicationELB" if lb_type == "APPLICATION" else "AWS/NetworkELB",
                    metric_name=metric_name,
                    dimensions=[{"Name": "LoadBalancer", "Value": lb_arn.split("loadbalancer/")[-1]}],
                    region=region,
                )
            except Exception:
                continue

            if has_traffic:
                continue

            # Rough ALB / NLB fixed-price approximation (does not include LCU usage)
            estimated_cost = float(os.getenv("HYGIENE_ELB_MONTH_PRICE", "18.0"))

            findings.append(
                _build_finding(
                    resource_type="LOAD_BALANCER",
                    resource_id=lb_arn,
                    region=region,
                    estimated_monthly_cost=estimated_cost,
                    age_days=None,
                    tags={},
                    risk_level="MEDIUM",
                    recommended_action="Delete or consolidate idle load balancer",
                    extra={"name": lb_name, "type": lb_type},
                )
            )
    return findings


def _scan_empty_log_groups(region: str) -> list[dict]:
    """Detect CloudWatch log groups with zero stored bytes."""
    regional_logs = boto3.client("logs", region_name=region)
    findings: list[dict] = []
    paginator = regional_logs.get_paginator("describe_log_groups")
    for page in paginator.paginate():
        for lg in page.get("logGroups", []):
            if lg.get("storedBytes", 0) != 0:
                continue

            name = lg["logGroupName"]
            # CloudWatch logs pricing is usage-based; empty group cost is effectively zero, but we keep entry for hygiene.
            findings.append(
                _build_finding(
                    resource_type="CLOUDWATCH_LOG_GROUP",
                    resource_id=name,
                    region=region,
                    estimated_monthly_cost=0.0,
                    age_days=None,
                    tags={},
                    risk_level="LOW",
                    recommended_action="Delete unused empty log group",
                )
            )
    return findings


def _scan_empty_eks_namespaces(region: str) -> list[dict]:
    """
    Placeholder for EKS namespace hygiene.

    Detecting empty Kubernetes namespaces requires querying the Kubernetes API
    using cluster credentials, which is environment-specific. This function is
    intentionally conservative and returns an empty list by default.

    You can extend this by:
    - Using `eks.describe_cluster` to obtain cluster endpoints
    - Generating kubeconfig / auth and using the Kubernetes Python client
    """
    # For now, we do not flag EKS namespaces automatically to avoid false positives.
    return []


def run_hygiene_scan(regions: list[str] | None = None) -> dict:
    if not regions:
        # Default to the current region only; multi-region can be passed explicitly
        regions = [os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))]

    all_findings: list[dict] = []
    for region in regions:
        all_findings.extend(_scan_unattached_ebs(region))
        all_findings.extend(_scan_old_snapshots(region))
        all_findings.extend(_scan_unused_eips(region))
        all_findings.extend(_scan_idle_nat_gateways(region))
        all_findings.extend(_scan_idle_load_balancers(region))
        all_findings.extend(_scan_empty_log_groups(region))
        all_findings.extend(_scan_empty_eks_namespaces(region))

    total_estimated_savings = sum(f.get("estimated_monthly_cost", 0.0) or 0.0 for f in all_findings)

    return {
        "findings": all_findings,
        "summary": {
            "total_estimated_savings": round(total_estimated_savings, 4),
            "total_resources": len(all_findings),
        },
    }


def lambda_handler(event, context):
    """
    Entry point for the Hygiene Scanner Lambda.

    Expects to be invoked by an Amazon Bedrock Agent action group, and returns
    a response body matching the required JSON structure in TEXT format.
    """
    try:
        regions = None
        # Allow optional region override via parameters or top-level event key
        params = event.get("parameters") or []
        for param in params:
            if param.get("name") == "regions":
                value = param.get("value")
                if isinstance(value, list):
                    regions = value
                elif isinstance(value, str):
                    regions = [r.strip() for r in value.split(",") if r.strip()]
                break

        scan_result = run_hygiene_scan(regions)

        response_body = {
            "TEXT": {
                "body": json.dumps(scan_result)
            }
        }

        function_response = {
            "messageVersion": "1.0",
            "response": {
                "actionGroup": event.get("actionGroup", "hygiene_scan"),
                "function": event.get("function", "run_scan"),
                "functionResponse": {
                    "responseBody": response_body
                },
            },
        }

        return function_response

    except Exception as exc:
        error_body = {
            "TEXT": {
                "body": json.dumps(
                    {
                        "error": str(exc),
                        "message": "Hygiene scan failed",
                    }
                )
            }
        }
        return {
            "messageVersion": "1.0",
            "response": {
                "actionGroup": event.get("actionGroup", "hygiene_scan"),
                "function": event.get("function", "run_scan"),
                "responseState": "FAILED",
                "functionResponse": {
                    "responseBody": error_body
                },
            },
        }

