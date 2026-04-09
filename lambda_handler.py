import boto3
import os
import json
from datetime import datetime, timedelta
import pandas as pd
from jinja2 import Template
from botocore.exceptions import ClientError
import logging
# ==============================================================
logger = logging.getLogger()
logger.setLevel(logging.INFO)
# ==============================================================
ce_client = boto3.client('ce')
s3_client = boto3.client('s3')
# ==============================================================
REGION = os.getenv("REGION", "eu-north-1")
BUDGET_THRESHOLD = float(os.getenv("BUDGET_THRESHOLD", 50.0))
TO_EMAIL = os.getenv("TO_EMAIL")
FROM_EMAIL = os.getenv("FROM_EMAIL")
SEND_EMAIL = os.getenv("SEND_EMAIL", "false").lower() == "true"
S3_BUCKET = os.getenv("S3_BUCKET")

# Cost Optimization thresholds
CHANGE_THRESHOLD_ABSOLUTE = float(os.getenv("CHANGE_THRESHOLD_ABSOLUTE", 10.0))
CHANGE_THRESHOLD_PERCENT = float(os.getenv("CHANGE_THRESHOLD_PERCENT", 15.0))

# Retention settings
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", 30))   # Delete reports older than this

if not S3_BUCKET:
    logger.warning("S3_BUCKET not set. Reports and metadata will not be persisted.")

METADATA_KEY = "cost-reports/last_run_metadata.json"
REPORTS_PREFIX = "cost-reports/"

_CREDENTIAL_ERROR_CODES = {
    'UnrecognizedClientException',
    'InvalidClientTokenId',
    'ExpiredTokenException',
    'AccessDeniedException',
    'AccessDenied',
    'ExpiredToken',
}


def get_previous_total_cost():
    try:
        response = s3_client.get_object(Bucket=S3_BUCKET, Key=METADATA_KEY)
        data = json.loads(response['Body'].read().decode('utf-8'))
        return data.get("total_cost", 0.0)
    except ClientError as e:
        if e.response['Error']['Code'] in ('NoSuchKey', '404'):
            return None
        logger.warning(f"Could not read previous metadata: {e}")
        return None
    except Exception:
        return None


def save_current_total_cost(total_cost):
    if not S3_BUCKET:
        return
    try:
        metadata = {
            "total_cost": round(total_cost, 4),
            "timestamp": datetime.utcnow().isoformat(),
            "report_date": datetime.now().strftime("%Y-%m-%d %H:%M UTC")
        }
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=METADATA_KEY,
            Body=json.dumps(metadata),
            ContentType='application/json'
        )
    except Exception as e:
        logger.warning(f"Failed to save metadata: {e}")


def cleanup_old_reports():
    """Delete reports older than RETENTION_DAYS"""
    if not S3_BUCKET:
        return
    try:
        cutoff_date = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
        objects_to_delete = []

        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=REPORTS_PREFIX):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('.html') and obj['LastModified'].replace(tzinfo=None) < cutoff_date:
                    objects_to_delete.append({'Key': key})

        if objects_to_delete:
            # Batch delete (max 1000 per call)
            for i in range(0, len(objects_to_delete), 1000):
                batch = objects_to_delete[i:i+1000]
                s3_client.delete_objects(Bucket=S3_BUCKET, Delete={'Objects': batch})
            logger.info(f"Cleaned up {len(objects_to_delete)} old reports (older than {RETENTION_DAYS} days)")
        else:
            logger.info("No old reports to clean up")
    except Exception as e:
        logger.warning(f"Cleanup failed: {e}")


def is_significant_change(current_total, previous_total):
    if previous_total is None:
        logger.info("First run - generating initial report")
        return True

    abs_change = current_total - previous_total
    abs_value = abs(abs_change)

    if previous_total > 0:
        percent_change = (abs_value / previous_total) * 100
    else:
        percent_change = 0

    direction = "increased" if abs_change > 0 else "decreased"
    if abs_change == 0:
        logger.info(f"Cost unchanged (${current_total:.2f})")
    else:
        logger.info(f"Cost {direction} by ${abs_value:.2f} ({percent_change:.1f}%) from previous day")

    if abs_value > CHANGE_THRESHOLD_ABSOLUTE or percent_change > CHANGE_THRESHOLD_PERCENT:
        logger.info("→ Significant change detected - will generate report")
        return True

    logger.info(f"→ No significant change (thresholds: ${CHANGE_THRESHOLD_ABSOLUTE} or {CHANGE_THRESHOLD_PERCENT}%)")
    return False


def get_cost_and_usage(days=30):
    """Fetch cost and usage with full pagination support and specific credential error handling."""
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days)
    data = []
    next_page_token = None

    try:
        while True:
            params = {
                'TimePeriod': {'Start': start_date.isoformat(), 'End': end_date.isoformat()},
                'Granularity': 'DAILY',
                'Metrics': ['UnblendedCost'],
                'GroupBy': [{'Type': 'DIMENSION', 'Key': 'SERVICE'}]
            }
            if next_page_token:
                params['NextPageToken'] = next_page_token

            response = ce_client.get_cost_and_usage(**params)

            for result in response.get('ResultsByTime', []):
                for group in result.get('Groups', []):
                    service = group['Keys'][0]
                    cost = float(group['Metrics']['UnblendedCost']['Amount'])
                    data.append({'Date': result['TimePeriod']['Start'], 'Service': service, 'Cost': cost})

            next_page_token = response.get('NextPageToken')
            if not next_page_token:
                break

        logger.info(f"Fetched {len(data)} cost entries")
        return pd.DataFrame(data).astype({'Cost': 'float32'})

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code in _CREDENTIAL_ERROR_CODES:
            # Raise a clear ValueError so lambda_handler can return a 403 with a helpful message
            raise ValueError(
                f"AWS credentials or permissions error ({error_code}). "
                "Ensure the Lambda execution role has the 'ce:GetCostAndUsage' permission "
                "and that the role/session token has not expired."
            ) from e
        logger.error(f"AWS ClientError in get_cost_and_usage: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in get_cost_and_usage: {e}")
        raise


def generate_html_report(df, total_cost):
    service_summary = df.groupby('Service')['Cost'].sum().sort_values(ascending=False).to_dict()
    alert = total_cost > BUDGET_THRESHOLD
    report_date = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    report_filename = f"cost-reports/cost_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"

    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AWS Cost Report</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; background: #f8f9fa; color: #333; }
            h1 { color: #232f3e; border-bottom: 4px solid #ff9900; padding-bottom: 10px; }
            .total { font-size: 2em; color: #ff9900; font-weight: bold; }
            .alert { color: #d93025; background: #fce8e6; padding: 15px; border-left: 6px solid #d93025; margin: 20px 0; }
            table { border-collapse: collapse; width: 100%; margin: 25px 0; background: white; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            th, td { padding: 14px; text-align: left; border-bottom: 1px solid #ddd; }
            th { background-color: #232f3e; color: white; }
            tr:nth-child(even) { background-color: #f9f9f9; }
            .footer { margin-top: 40px; color: #777; font-size: 0.9em; }
        </style>
    </head>
    <body>
        <h1>AWS Cloud Cost Monitor Report</h1>
        <p><strong>Report Date:</strong> {{ report_date }}</p>
        <h2 class="total">Total Cost (Last {{ period_days }} days): ${{ "%.2f"|format(total_cost) }}</h2>
        {% if alert %}
        <div class="alert">⚠️ <strong>BUDGET ALERT:</strong> Exceeded threshold of ${{ "%.2f"|format(threshold) }}</div>
        {% endif %}
        <h3>Top Services by Cost</h3>
        <table>
            <tr><th>Service</th><th>Cost (USD)</th></tr>
            {% for service, cost in service_summary.items() %}
            <tr><td>{{ service }}</td><td>${{ "%.2f"|format(cost) }}</td></tr>
            {% endfor %}
        </table>
        <div class="footer">Generated by AWS Cloud Cost Monitor • {{ report_date }}</div>
    </body>
    </html>
    """

    template = Template(html_template)
    html_content = template.render(
        report_date=report_date,
        total_cost=total_cost,
        period_days=30,
        service_summary=service_summary,
        threshold=BUDGET_THRESHOLD,
        alert=alert
    )

    report_url = None
    try:
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=report_filename,
            Body=html_content,
            ContentType='text/html'
        )
        report_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{report_filename}"
        logger.info(f"Report uploaded successfully: {report_url}")
    except Exception as e:
        logger.error(f"S3 upload failed: {e}")

    return {
        "report_date": report_date,
        "total_cost": round(total_cost, 2),
        "alert": alert,
        "report_url": report_url
    }


def lambda_handler(event, context):
    logger.info("=== AWS Cloud Cost Monitor Lambda Started ===")

    try:
        df = get_cost_and_usage(days=30)

        if df.empty:
            logger.warning("No cost data found")
            return {"statusCode": 200, "body": json.dumps({"message": "No cost data"})}

        total_cost = df['Cost'].sum()
        previous_total = get_previous_total_cost()

        if not is_significant_change(total_cost, previous_total):
            save_current_total_cost(total_cost)
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "No significant cost change. Report skipped.",
                    "total_cost": round(total_cost, 2),
                    "skipped": True
                })
            }

        logger.info("Generating new report due to significant cost change")
        report = generate_html_report(df, total_cost)
        save_current_total_cost(total_cost)

        cleanup_old_reports()

        if report["alert"]:
            logger.warning(f"BUDGET ALERT triggered: ${report['total_cost']} > ${BUDGET_THRESHOLD}")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Report generated",
                "total_cost": report["total_cost"],
                "alert": report["alert"],
                "report_url": report.get("report_url"),
                "skipped": False
            })
        }

    except ValueError as e:
        logger.error(f"Credential/permission error: {e}")
        return {
            "statusCode": 403,
            "body": json.dumps({"error": str(e)})
        }
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": "Internal error. Check CloudWatch logs."})}
