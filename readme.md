# 🔍 AWS Cloud Cost Monitor

A serverless AWS Lambda function that automatically monitors your AWS cloud spending, detects significant cost changes, and generates clean HTML reports saved directly to S3.

---

## 📋 Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Setup & Deployment](#setup--deployment)
- [Environment Variables](#environment-variables)
- [How It Works](#how-it-works)
- [Report Output](#report-output)
- [Free Tier & Estimated Cost](#free-tier--estimated-cost)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Overview

AWS Cloud Cost Monitor is a lightweight, serverless solution for keeping track of your AWS spending without any dashboards or third-party tools. It runs automatically every day, checks if your costs have changed significantly, and generates a styled HTML report uploaded to your S3 bucket — all within the AWS free tier (except Cost Explorer API calls at ~$0.30/month).

---

## Features

- 📊 Fetches daily cost breakdown by service via AWS Cost Explorer
- 🔁 Full pagination support — handles large accounts with many services
- 🧠 Smart report skipping — only generates a report when costs change significantly
- 🚨 Budget alerts when spending exceeds your configured threshold
- 🗑️ Automatic cleanup of reports older than a configurable retention period
- ☁️ Reports saved to S3 as styled HTML files
- 📝 All activity logged to CloudWatch
- 🔐 Credential/permission errors caught and returned with clear messages

---

## Architecture

```
EventBridge (daily cron)
         │
         ▼
    AWS Lambda
    (lambda_handler.py)
         │
         ├──► AWS Cost Explorer API  ──► Fetch daily costs by service
         │
         ├──► S3 Bucket
         │      ├── cost-reports/cost_report_YYYYMMDD_HHMMSS.html
         │      └── cost-reports/last_run_metadata.json
         │
         └──► CloudWatch Logs
```

---

## Project Structure

```
aws-cloud-cost-monitor/
├── lambda_handler.py   # All Lambda logic — single file deployment
├── README.md
└── LICENSE
```

---

## Prerequisites

- An AWS account (free tier works)
- Python 3.12 installed locally (for packaging dependencies)
- An S3 bucket to store reports
- An IAM role for Lambda with the following permissions:
  - `ce:GetCostAndUsage`
  - `s3:PutObject`
  - `s3:GetObject`
  - `s3:DeleteObject`
  - `s3:ListBucket`
  - `logs:CreateLogGroup`
  - `logs:CreateLogStream`
  - `logs:PutLogEvents`

---

## Setup & Deployment

### 1. Create an S3 Bucket

1. Go to the AWS Console → S3 → **Create bucket**
2. Give it a unique name (e.g. `my-aws-cost-reports-2026`)
3. Choose your region (e.g. `eu-north-1`)
4. Keep **Block all public access** enabled
5. Click **Create bucket**

---

### 2. Create an IAM Role for Lambda

1. Go to IAM → **Roles** → **Create role**
2. Select **AWS service** → **Lambda**
3. Attach the following permissions:
   - `AWSLambdaBasicExecutionRole` (for CloudWatch Logs)
   - `AWSBillingReadOnlyAccess` (this includes `ce:GetCostAndUsage`)
   - S3 permissions on your bucket only:
     - `s3:GetObject`
     - `s3:PutObject`
     - `s3:DeleteObject`
     - `s3:ListBucket`
4. Name the role `CostMonitorLambdaRole` and create it

---

### 3. Package the Code (Linux-compatible)

```bash
# 1. Create packaging directory and copy handler
mkdir -p package
cp lambda_handler.py package/

# 2. Install dependencies for Lambda runtime
cd package
pip install pandas jinja2 \
  --target . \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all:

# 3. Create zip from inside the package folder (critical)
zip -r ../deployment-package.zip .

# 4. Verify structure - lambda_handler.py MUST be at the root
echo "=== Zip contents ==="
unzip -l ../deployment-package.zip | grep -E '\.py|pandas|jinja2'
```

**Important:** `lambda_handler.py` must appear directly at the root of the zip file (not inside any folder).

> ⚠️ **Windows users:** Use PowerShell with `Compress-Archive -Path * -DestinationPath ..\deployment-package.zip` from inside the `package` folder.

---

### 4. Create the Lambda Function

1. Go to AWS Console → Lambda → **Create function**
2. Choose **Author from scratch**
3. Set:
   - **Function name:** `CostMonitorLambda`
   - **Runtime:** `Python 3.12`
   - **Architecture:** `x86_64`
4. Under **Permissions** → **Use an existing role** → select `CostMonitorLambdaRole`
5. Click **Create function**

---

### 5. Upload the Code

1. On the Lambda function page → **Code** tab
2. Click **Upload from** → **.zip file**
3. Upload `deployment-package.zip`
4. Scroll down to **Runtime settings** → **Edit**
5. Set the Handler to exactly:
   ```
   lambda_handler.lambda_handler
   ```

---

### 6. Set Environment Variables

Go to **Configuration** tab → **Environment variables** → **Edit** and add:

| Key | Value |
|---|---|
| `S3_BUCKET` | Your bucket name (e.g. `my-aws-cost-reports-2026`) |
| `BUDGET_THRESHOLD` | `50.0` |
| `REGION` | `eu-north-1` |
| `CHANGE_THRESHOLD_ABSOLUTE` | `10.0` |
| `CHANGE_THRESHOLD_PERCENT` | `15.0` |
| `RETENTION_DAYS` | `30` |

---

### 7. Increase Timeout and Memory

Go to **Configuration** → **General configuration** → **Edit**:

- **Timeout:** `1 min 0 sec`
- **Memory:** `256 MB`

---

### 8. Schedule Daily Runs

1. Go to **Configuration** → **Triggers** → **Add trigger**
2. Select **EventBridge (CloudWatch Events)**
3. Choose **Create a new rule**
4. Set:
   - **Rule name:** `CostMonitorDailySchedule`
   - **Rule type:** Schedule expression
   - **Expression:** `cron(0 8 * * ? *)` ← runs daily at 8:00 AM UTC
5. Click **Add**

---

### 9. Test It

1. Go to your Lambda function → **Test** tab
2. Create a new test event with an empty JSON body `{}`
3. Click **Test**

Expected success response:
```json
{
  "statusCode": 200,
  "body": "{\"message\": \"Report generated\", \"total_cost\": 12.34, \"alert\": false, \"skipped\": false}"
}
```

> ℹ️ If you just enabled Cost Explorer for the first time, AWS needs up to **24 hours** to populate data. You may see a `DataUnavailableException` until then — this is normal.

---

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `S3_BUCKET` | S3 bucket name to store reports and metadata | **Required** |
| `BUDGET_THRESHOLD` | Alert threshold in USD | `50.0` |
| `REGION` | AWS region | `eu-north-1` |
| `CHANGE_THRESHOLD_ABSOLUTE` | Minimum $ change to trigger a new report | `10.0` |
| `CHANGE_THRESHOLD_PERCENT` | Minimum % change to trigger a new report | `15.0` |
| `RETENTION_DAYS` | Auto-delete reports older than this many days | `30` |
| `SEND_EMAIL` | Set to `true` to enable SES email alerts (code ready but requires verified SES identities) | `false` |
| `TO_EMAIL` | Recipient email for alerts (future use) | — |
| `FROM_EMAIL` | Sender email for alerts (must be verified in SES) | — |

---

## How It Works

1. **Lambda is triggered** by EventBridge on a daily schedule
2. **Cost data is fetched** from AWS Cost Explorer for the last 30 days, grouped by service, with full pagination
3. **Previous total cost** is read from `last_run_metadata.json` in S3
4. **Change detection:** if the cost difference is below both the absolute and percentage thresholds, the run is skipped and no report is generated
5. **If significant change detected:** a styled HTML report is generated and uploaded to S3
6. **Old reports** older than `RETENTION_DAYS` are deleted from S3
7. **Budget alert** is included in the report if total cost exceeds `BUDGET_THRESHOLD`
8. **Metadata is saved** to S3 for comparison on the next run

---

## Report Output

Reports are saved to your S3 bucket at:
```
s3://your-bucket-name/cost-reports/cost_report_YYYYMMDD_HHMMSS.html
```

The HTML report includes:
- Total cost for the last 30 days
- Budget alert banner (if threshold exceeded)
- Full cost breakdown table sorted by most expensive service
- Report generation timestamp

---

## Free Tier & Estimated Cost

| Service | Estimated Monthly Usage | Cost |
|---|---|---|
| AWS Lambda | 30 executions | Free ✅ |
| Amazon S3 | < 1 MB (HTML + metadata) | Free ✅ |
| CloudWatch Logs | Very low | Free ✅ |
| Cost Explorer | 30 API calls | **~$0.30** ⚠️ |

**Total estimated monthly cost: ~$0.30**

> ⚠️ Cost Explorer charges **$0.01 per API request**. At one run per day, expect roughly **$0.30/month**. This is the only cost associated with running this project.

---

## Troubleshooting

**`statusCode: 403` — "AWS credentials or permissions error"**
- Make sure the Lambda role has `AWSBillingReadOnlyAccess` (or at least `ce:GetCostAndUsage`).
- If using temporary credentials, the session may have expired.

**`Runtime.ImportModuleError: No module named 'lambda_handler'`**
- Your zip file has the wrong structure. Make sure `lambda_handler.py` is at the root of the zip, not inside a subfolder. Zip from *inside* the `package/` folder using `zip -r ../deployment-package.zip .`

**`No module named 'pandas'` or `No module named 'jinja2'`**
- Dependencies were not packaged correctly. Rebuild the zip using the exact commands in the "Package the Code" section above.

**`Unable to import required dependency numpy`**
- Dependencies were installed for the wrong platform. Re-install using `--platform manylinux2014_x86_64 --only-binary=:all:` as shown in the packaging step above.

**`DataUnavailableException: Data is not available`**
- Cost Explorer was recently enabled on your account. AWS takes up to 24 hours to populate data. Wait and try again.

**`statusCode: 500` — Internal error**
- Check CloudWatch logs: Lambda → Monitor tab → View CloudWatch logs → latest log stream.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
