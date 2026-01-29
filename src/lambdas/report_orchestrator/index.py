"""
Report Orchestrator Lambda: triggered by EventBridge (daily/weekly).
Invokes SupervisorAgent for a cloud report, then sends the result via SES.
"""
import json
import os
import uuid
import boto3


def lambda_handler(event, context):
    recipients = (os.environ.get("REPORT_RECIPIENTS") or "").strip().split(",")
    recipients = [r.strip() for r in recipients if r.strip()]
    from_email = (os.environ.get("SES_FROM_EMAIL") or "").strip()
    if not recipients or not from_email:
        print("ReportRecipientEmails or SESFromEmail not set; skipping report.")
        return {"status": "skipped", "reason": "missing_config"}

    report_type = "daily"
    try:
        detail = event.get("detail") or {}
        if isinstance(detail, str):
            detail = json.loads(detail) if detail else {}
        report_type = detail.get("reportType", "daily")
    except Exception:
        pass
    if report_type not in ("daily", "weekly"):
        report_type = "daily"

    agent_id = os.environ.get("AGENT_ID")
    agent_alias_id = os.environ.get("AGENT_ALIAS_ID", "TSTALIASID")
    env_name = os.environ.get("ENVIRONMENT_NAME", "sandbox")
    prompt = (
        f"Generate a {report_type} cloud report for this AWS account. Include: "
        "1) Cost optimization opportunities (use CostOptimizationAgent). "
        "2) Security issues from Trusted Advisor (use SecurityAgent). "
        "3) Hygiene findings – unused EBS, snapshots, EIPs, idle NAT/ALB, empty log groups (use HygieneAgent). "
        "Format as clear sections with bullet points. Do not include internal reasoning; output only the final report."
    )

    client = boto3.client("bedrock-agent-runtime")
    session_id = str(uuid.uuid4())
    response = client.invoke_agent(
        agentId=agent_id,
        agentAliasId=agent_alias_id,
        sessionId=session_id,
        inputText=prompt,
    )

    completion = ""
    for event_stream in response.get("completion", []):
        if "chunk" in event_stream and event_stream["chunk"].get("bytes"):
            completion += event_stream["chunk"]["bytes"].decode("utf-8", errors="replace")

    if not completion.strip():
        completion = "(No report content generated.)"

    body_html = f"<html><body><pre style='white-space:pre-wrap;font-family:sans-serif;'>{completion}</pre></body></html>"
    ses = boto3.client("ses")
    subject = f"[{env_name}] AWS Cloud Report – {report_type.capitalize()}"
    ses.send_email(
        Source=from_email,
        Destinations=recipients,
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Html": {"Data": body_html, "Charset": "UTF-8"}},
        },
    )
    return {"status": "sent", "reportType": report_type, "recipientCount": len(recipients)}
