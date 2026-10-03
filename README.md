# Google Cloud Quota Monitoring Solution (QMS v6)

<p align="left">
  <img src="dashboard/static/logo.png" alt="Cloud Quota Monitoring" width="90">
</p>

> Organization-wide Google Cloud quota monitoring, 7-day and 30-day peak utilization tracking, Quota Adjuster posture visibility, and a self-hosted Cloud Console-style dashboard on Cloud Run protected by Direct Cloud Run Identity-Aware Proxy (IAP) and per-user `cloudquotas.viewer` authorization.

---

## 1. Overview

Google Cloud enforces [quotas](https://cloud.google.com/docs/quota) on resource usage across projects, folders, and organizations. **Quota Monitoring Solution (QMS v6)** provides an automated, low-cost, organization-wide quota observability platform built on **Cloud Run**, **BigQuery**, the **Cloud Quotas API**, and **Cloud Monitoring PromQL**.

![QMS v6 Dashboard](img/qms-v6-dashboard.png)

### Key Capabilities in v6

* **Accurate Quota Utilization Semantics**: Normalizes rate-quota consumption (`serviceruntime.googleapis.com/quota/rate/net_usage`) to the exact enforcement interval (`refreshInterval`: per-minute vs. per-day on `US/Pacific` boundaries) and joins usage to authoritative limits via `QuotaInfo.quotaId ≡ limit_name` from the **Cloud Quotas API** (`cloudquotas.googleapis.com`).
* **Comparability Guardrails**: Automatically detects and withholds non-comparable pairings (such as project-aggregate usage vs. per-user limits `dimensions: ["user"]`) and normalizes unlimited quota sentinels (`9223372036854775807` and `-1`), surfacing full telemetry in a dedicated **Data Quality & Guardrails** tab.
* **7-Day & 30-Day Peak Tracking**: Stores daily peak and current usage in a partitioned, clustered BigQuery table (`quota_daily`, 400-day retention) backed by six precomputed BigQuery views (`quota_latest`, `quota_peaks`, `quota_risk`, `quota_movers`, `quota_hierarchy`, `quota_quality`).
* **Self-Hosted Cloud Console UI (`qms-dashboard`)**: Fast, zero-dependency Cloud Console design featuring cascading searchable dropdowns (`Project`, `Service`, `Quota Metric`), instant threshold filters (`All`, `>= 50%`, `>= 80%`, `>= 90%`), a 30-day interactive trend drawer, **7d Movers**, and **Org & Folder Hierarchy** rollups (including read-only **Quota Adjuster** status per project).
* **Direct Cloud Run IAP + Per-User IAM Scoping**: Authenticates users at Google's edge via Direct Cloud Run IAP (no External Load Balancer required) and dynamically filters dashboard data to the exact Organization, Folder, or Project scopes where the logged-in user holds `roles/cloudquotas.viewer` (or `cloudquotas.quotaInfos.list`).
* **Lightweight Zero-Config Alerting**:
  * **In-Browser Alert Bell & Desktop Push Notifications**: Alerts badge (`>= 80%`) in the top Console header with native Web Push notifications when a daily collection detects quota threshold breaches.
  * **1-Click Cloud Console Real-Time Alerting**: Every quota's detail drawer generates a ready-to-paste PromQL condition (`>= 80%` of the authoritative limit) and direct deep links to create a Cloud Monitoring Alert Policy or manage the quota in Google Cloud Console.
  * **Structured Collector Summary & Optional Webhook**: Emits a structured `QMS_QUOTA_THRESHOLD_SUMMARY` JSON event to Cloud Logging after each run and supports an optional Slack or Google Chat incoming webhook (`QMS_ALERT_WEBHOOK_URL`).

---

## 2. Architecture

![QMS v6 Architecture](img/qms-v6-architecture.jpg)

One container image (`Dockerfile`) powers two serverless Cloud Run workloads in a single host project:

1. **`qms-collector` (Cloud Run Job)**: Triggered daily by **Cloud Scheduler** (`qms-daily-collect`). Walks active projects across the organization (`HierarchySource`), queries Cloud Monitoring PromQL (`MonitoringSource`) concurrently across a bounded worker pool (`QMS_COLLECTOR_WORKERS=8`), fetches authoritative quota definitions and Quota Adjuster settings from the Cloud Quotas API (`CloudQuotasSource`) using a thread-safe token-bucket rate limiter (`14 RPS` / `840 RPM`, staying safely below the host project's `1,200 RPM` `ReadRequestsPerMinute` quota with exponential backoff on `HTTP 429`/`5xx`), normalizes daily peaks, and loads rows into BigQuery (`BigQuerySink`) via free batch load jobs.
2. **`qms-dashboard` (Cloud Run Service)**: Read-only FastAPI service (`min_instance_count = 0`, `512 MiB` RAM) behind **Direct Cloud Run IAP**:
   * **Partition-Pruned Precomputed Views**: All six BigQuery views (`quota_latest`, `quota_peaks`, `quota_risk`, `quota_movers`, `quota_hierarchy`, `quota_quality`) enforce `usage_date_utc` partition pruning (`30–35` days) so refreshes scan only active partitions rather than the full 400-day retention window.
   * **Uncapped Facet Catalog + Bounded Memory (`QMS_RISK_CACHE_LIMIT=15000`)**: Populates the **Project**, **Service**, and **Quota Metric** searchable dropdowns from a complete `(project_id, service, quota_metric)` facet index across all 100–500+ projects while capping DOM rendering at 500 rows and dynamically fetching filtered slices via `GET /api/risk`.
   * **Single-RPC Org IAM Fast Path + SWR Cache**: Evaluates per-user access in a single `organizations/{org_id}:analyzeIamPolicy` (`expandResources=true, expandGroups=true`) call when Cloud Asset Inventory is available, falling back to bounded concurrent Cloud Resource Manager `getIamPolicy` checks only for fresh unindexed grants or custom roles, backed by Stale-While-Revalidate (SWR) caching.

---

## 3. Repository Layout

```text
collector/            # Python package for quota collection, normalization, and BigQuery views
  sources/            #   monitoring.py (PromQL + retry), cloud_quotas.py (14 RPS limiter + retry), hierarchy.py
  sinks/              #   bigquery.py (batch load jobs), views.py (6 partition-pruned SQL views)
  alerts.py           #   Zero-config threshold evaluation, webhook digest, and PromQL builder
  model.py            #   Immutable domain types and comparability flags
  normalise.py        #   Enforcement interval and unlimited-sentinel normalization
  rollup.py           #   Daily peak/current rollup across UTC and US/Pacific boundaries
  cli.py              #   Parallel CLI entry point: `collect` | `backfill` | `verify` | `views`
dashboard/            # Self-hosted FastAPI + Jinja2 Cloud Console UI
  app.py              #   HTTP routes, health checks, /api/risk, and /api/alerts
  authz.py            #   IAP ES256 JWT verification, 1-RPC Org CAI fast path, and SWR authz cache
  queries.py          #   Concurrent BigQuery view reader with uncapped facet index & SWR caching
  static/             #   QMS logo and browser tab favicons
  templates/          #   base.html and index.html (Risk, Movers, Hierarchy, Quality, Drawer)
terraform/            # Terraform >= 1.5 / google provider ~> 8.0
  modules/qms/        #   Reusable QMS module (Cloud Run Job + Service, BigQuery, Scheduler, IAM)
  example/            #   Example root module
tests/                # Golden-file and unit test suite (pytest, 108 tests)
```

---

## 4. Deployment Guide

See [`terraform/README.md`](terraform/README.md) for the complete infrastructure reference and IAM table.

### 4.1 Prerequisites

* **Host Project**: A Google Cloud project to host BigQuery, Artifact Registry, Cloud Scheduler, and the two Cloud Run workloads.
* **Target Organization**: Organization ID (e.g., `957650833838`) to scan.
* **Tools**: `gcloud` CLI, `terraform >= 1.5`, and `uv` (for local development/testing).
* **Permissions to deploy**:
  * Project Owner (or Editor + Project IAM Admin + Run Admin + Service Account Admin) on the **Host Project**.
  * Organization IAM Admin on the **Target Organization** to grant read-only viewer roles (`roles/monitoring.viewer`, `roles/cloudquotas.viewer`, `roles/browser`, `roles/cloudasset.viewer`, `roles/iam.securityReviewer`) to the collector and dashboard service accounts.

### 4.2 Deploy with Terraform & Cloud Build

```bash
# 1. Authenticate with Google Cloud
gcloud auth login
gcloud auth application-default login
gcloud config set project <HOST_PROJECT_ID>

# 2. Configure Terraform variables
cd terraform/example
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your project_id, organization_id, region, and dashboard_invokers

# 3. Phase 1: Provision APIs, Artifact Registry, staging bucket, and build service account
terraform init
terraform apply \
  -target=module.qms.google_project_service.this \
  -target=module.qms.google_artifact_registry_repository.qms \
  -target=module.qms.google_storage_bucket.build_source \
  -target=module.qms.google_service_account.build \
  -target=module.qms.google_artifact_registry_repository_iam_member.build_writer \
  -target=module.qms.google_storage_bucket_iam_member.build_source_admin \
  -target=module.qms.google_project_iam_member.build_log_writer

# 4. Phase 2: Build and push the container image via Cloud Build
cd ../..
gcloud builds submit \
  --region=<REGION> \
  --config=cloudbuild.yaml \
  --gcs-source-staging-dir=gs://<HOST_PROJECT_ID>-qms-build-source/source \
  --service-account=projects/<HOST_PROJECT_ID>/serviceAccounts/qms-build@<HOST_PROJECT_ID>.iam.gserviceaccount.com \
  --project=<HOST_PROJECT_ID>

# 5. Phase 3: Provision BigQuery dataset, Cloud Run Job & Service (with Direct IAP), and Scheduler
cd terraform/example
terraform apply
```

### 4.3 Create Views & Run Initial 30-Day Backfill

```bash
# Create or replace the 6 BigQuery views
uv run python -m collector.cli \
  --billing-project <HOST_PROJECT_ID> \
  --dataset quota_monitoring \
  --bq-location <REGION> \
  views

# Execute the collector job (or run a 30-day backfill)
gcloud run jobs execute qms-collector --region=<REGION> --project=<HOST_PROJECT_ID> --wait
```

### 4.4 Granting Dashboard Access to Users

1. **IAP Access to the Cloud Run Service**: Grant `roles/iap.httpsResourceAccessor` on `qms-dashboard` to the users or Google Groups who should be able to open the dashboard URL.
2. **Workload Quota Visibility**: Each signed-in user automatically sees only the projects where they hold `roles/cloudquotas.viewer` (or a custom role containing `cloudquotas.quotaInfos.list`) at the **Organization**, **Folder**, or **Project** level.

---

## 5. Local Development & Testing

```bash
# Run formatter, linter (including flake8-bandit security rules), and unit tests
uv run ruff format --check .
uv run ruff check .
uv run pytest

# Reconcile sample quota ratios against Cloud Console
uv run python -m collector.cli \
  --billing-project <HOST_PROJECT_ID> \
  --organization <ORG_ID> \
  --days 7 \
  verify --limit 20
```

---

## 6. Cost

QMS v6 is designed to run at minimal operational cost by combining scale-to-zero serverless workloads, free BigQuery batch load jobs, in-memory stale-while-revalidate view caching, and Direct Cloud Run IAP (avoiding the fixed \~$18/month cost of an External Application Load Balancer).

### 6.1 Cost Components

* **Cloud Run Job (`qms-collector`)**: `1 vCPU`, `1 GiB` memory, executed once daily (`30 2 * * *`). Parallel 8-worker execution with `14 RPS` rate limiting keeps wall-clock runtime short (\~1.5 minutes/day for 100 projects).
* **Cloud Run Service (`qms-dashboard`)**: `1 vCPU`, `512 MiB` memory with `min_instance_count = 0` (scales to zero when idle). Client-side filtering/sorting and a 5-minute Stale-While-Revalidate (SWR) in-memory cache minimize active CPU time.
* **BigQuery (`quota_monitoring`)**:
  * **Ingestion**: Uses batch load jobs (`load_table_from_json`), which are **$0.00 (free)**.
  * **Storage**: Partitioned by `usage_date_utc` and clustered by `(project_id, service, quota_metric)` with 400-day retention (\~1.4 GB logical storage for 100 projects; partitions older than 90 days automatically drop to Long-Term Storage pricing).
  * **Queries**: Views (`quota_latest`, `quota_peaks`, `quota_movers`) prune `quota_daily` to the most recent 30–35 partitions, and the dashboard caches view snapshots in memory for 5 minutes rather than issuing per-click queries.
* **Cloud Monitoring, Cloud Quotas, Cloud Asset & CRM APIs**: Cloud Quotas, Cloud Asset Inventory, and Cloud Resource Manager API calls are free; Cloud Monitoring API reads (`query_range` on GCP `serviceruntime` quota metrics) include 1,000,000 free API read calls/month per billing account ($0.01 per 1,000 calls thereafter).
* **Direct Cloud Run IAP, Cloud Scheduler & Artifact Registry**: Direct Cloud Run IAP has no hourly load-balancer fee; Cloud Scheduler is $0.10/month for the single daily cron job; Artifact Registry stores one \~180 MB container image (\~$0.02/month).

### 6.2 Estimated Monthly Cost & Scaling by Project Count

Because `QuotaInfo` limit definitions are cached per GCP service (`O(distinct services)` rather than `O(projects × services)`), `quota_daily` views are partition-pruned, and the dashboard serves cached view snapshots in memory, total cost scales gently as the number of monitored projects grows:

| Monitored Projects | Daily Collector Runtime | Cloud Run (`qms-collector` + `qms-dashboard`) | BigQuery (Storage + Pruned View Queries) | Monitoring API, Scheduler & Artifact Registry | **Estimated Total Monthly Cost (Gross List Price)** |
| --- | --- | ---: | ---: | ---: | ---: |
| **100 Projects** | \~1.5 min / day | \~$1.45 – $2.00 | \~$0.20 – $1.10 | \~$0.20 – $0.25 | **\~$1.85 – $3.35 / month** |
| **500 Projects** | \~7 min / day | \~$2.10 – $2.80 | \~$0.95 – $2.50 | \~$0.50 – $0.65 | **\~$3.55 – $5.95 / month** |
| **1,000 Projects** | \~14 min / day | \~$3.00 – $3.90 | \~$1.90 – $4.50 | \~$0.90 – $1.10 | **\~$5.80 – $9.50 / month** |

*(When GCP monthly free tiers for Cloud Run, BigQuery 1 TiB query / 10 GB storage, and Cloud Monitoring 1M API reads are available on the billing account, net cost for a 100-project deployment is typically under **$0.25 / month**.)*

> **Disclaimer:** Costs are indicatory and to be used purely for estimation. Actual costs should be monitored for accuracy.

---

## 7. Getting Support & Contributing

* [Contributing guidelines](CONTRIBUTING.md)
* [Code of conduct](code-of-conduct.md)
* Quota Monitoring Solution is an open-source solution and is not officially covered by Google Cloud product support.
