# AWS Cloud Report Agent

This project deploys an AWS architecture that sends **daily and weekly cloud reports** by email (SES). Reports cover cost optimization (Trusted Advisor cost pillar), security findings (Trusted Advisor security pillar), and infrastructure hygiene (unused EBS, snapshots, EIPs, idle NAT/ALB, empty log groups). The stack uses Amazon Bedrock agents (Supervisor + CostAnalysis, CostOptimization, Hygiene, Security), Lambda functions, and EventBridge schedules.

## Deploying to another account (audit, management)

The same CloudFormation template can be used in multiple accounts (e.g. sandbox, audit, management). No code changes are required; only parameters and optionally the stack name change.

### 1. Verify SES in the target account

Before reports can be sent, the **sender email** (and optionally recipient addresses, if the account is in SES sandbox) must be verified in Amazon SES in the same account/region where you deploy.

- In **Amazon SES** (same region as the stack): verify the email (or domain) you will use as `SESFromEmail`.
- If the account is in SES sandbox, also verify each recipient email, or move the account out of sandbox.

### 2. Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `EnvironmentName` | Environment identifier; used in resource names and email subject | `sandbox`, `audit`, `management` |
| `ReportRecipientEmails` | Comma-separated list of email addresses to receive daily and weekly reports | `team@example.com,ops@example.com` |
| `DailyReportSchedule` | EventBridge cron for daily report (UTC) | `cron(0 8 * * ? *)` (08:00 UTC daily) |
| `WeeklyReportSchedule` | EventBridge cron for weekly report (UTC) | `cron(0 9 ? * MON *)` (Monday 09:00 UTC) |
| `SESFromEmail` | Verified sender email address in SES for report emails | `reports@example.com` |
| `UserEmail` | FinOps user email (Cognito); optional if not using the chat UI | `user@example.com` |
| `SupervisorFoundationModel` | Bedrock model for Supervisor agent | `amazon.nova-pro-v1:0` |
| `SubAgentFoundationModel` | Bedrock model for sub-agents | `amazon.nova-micro-v1:0` |

### 3. Deploy

```bash
aws cloudformation deploy \
  --stack-name cloud-report-agent-audit \
  --template-file cfn-finops-bedrock-multiagent-nova.yaml \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    EnvironmentName=audit \
    ReportRecipientEmails=team@example.com \
    SESFromEmail=reports@example.com \
    UserEmail=user@example.com
```

Use a different `--stack-name` per account (e.g. `cloud-report-agent-sandbox`, `cloud-report-agent-audit`, `cloud-report-agent-management`) if you deploy the same template to multiple accounts.

### 4. CI/CD (optional)

To deploy to multiple accounts from GitHub Actions, add a matrix or separate workflow that runs `aws cloudformation deploy` with different `parameter-overrides` and `stack-name` per account. No changes to the template are required.

## Source layout

- `src/lambdas/report_orchestrator/` – Report Orchestrator Lambda (invokes Supervisor agent, sends SES email).
- `src/lambdas/trusted_advisor_security/` – Trusted Advisor security pillar Lambda (SecurityAgent).
- `src/lambdas/hygiene_scanner/` – Hygiene scanner Lambda (HygieneAgent).
- `src/lambdas/trusted_advisor_list_recommendations/` – Trusted Advisor cost pillar Lambda (CostOptimizationAgent).
- `src/lambdas/trusted_advisor_list_recommendation_resources/` – TA resource list Lambda.

Lambda code is also inlined in the CloudFormation template (`ZipFile`) for deployment. To change behavior, update the source file and sync the same logic into the template’s `ZipFile` (or switch to S3-packaged code).

## Testing the report

1. Trigger the Report Orchestrator Lambda manually from the AWS Lambda console with a test event: `{"detail":{"reportType":"daily"}}` or `{"detail":{"reportType":"weekly"}}`.
2. Ensure `ReportRecipientEmails` and `SESFromEmail` are set and verified in SES.
3. Check CloudWatch Logs for the Lambda and your inbox for the report email.
