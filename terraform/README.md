# QMS Terraform

Infrastructure for the Quota Monitoring Solution v6: a Cloud Run **job** that
collects quota usage across the organization into BigQuery, and a Cloud Run
**service** that serves the dashboard from precomputed views.

```
terraform/
  modules/qms/     the module -- everything lives here
  example/         a root module that instantiates it for one project
```

## Prerequisites

- Terraform >= 1.5 (declarative `import` blocks are used during adoption).
- `hashicorp/google` ~> 8.0.
- Application Default Credentials for a principal with, at minimum:
  - `roles/owner` (or an equivalent set) on the target project, and
  - `roles/resourcemanager.organizationAdmin` **or** a role that can grant the
    three read-only org roles below. Org-level IAM is the one thing a project
    owner cannot do for itself.

### On the corp network

`registry.terraform.io` is blocked by the proxy (403). Use the internal mirror:

```bash
export TF_GOOGLE_USE_INTERNAL_REGISTRY=true
terraform -chdir=terraform/example init
```

## What this creates

| Resource | Name |
|---|---|
| Host Project APIs (`google_project_service.this`) | `artifactregistry`, `bigquery`, `cloudasset`, `cloudbuild`, `cloudquotas`, `cloudresourcemanager`, `cloudscheduler`, `iam`, `iap`, `logging`, `monitoring`, `run`, `storage` (`.googleapis.com`) |
| Service accounts | `qms-collector`, `qms-dashboard`, `qms-build`, `qms-scheduler` |
| BigQuery dataset | `quota_monitoring` (regional, `var.region`) |
| Artifact Registry | `qms` (Docker) |
| GCS bucket | `${project_id}-qms-build-source` -- Cloud Build source staging |
| Cloud Run job | `qms-collector` |
| Cloud Run service | `qms-dashboard` |
| Cloud Scheduler | `qms-daily-collect` |

> **APIs on Monitored Projects:** Each monitored project in the organization only needs `monitoring.googleapis.com` enabled (which is enabled by default on Google Cloud projects) so PromQL can read `serviceruntime.googleapis.com/quota/*` time series. `cloudquotas.googleapis.com` only needs to be enabled on the **Host Project** because `qms-collector` sends `x-goog-user-project: <HOST_PROJECT_ID>` on all Cloud Quotas API requests.

Plus the IAM below. The dataset is created **without**
`default_partition_expiration_ms`; v5 set it to one day, which silently deleted
every partition older than 24h and is the reason historical peaks were empty.

## IAM model

Every grant is at the narrowest scope the platform supports.

| Principal | Role | Scope |
|---|---|---|
| `qms-collector` | `roles/monitoring.viewer` | **organization** |
| `qms-collector` | `roles/cloudquotas.viewer` | **organization** |
| `qms-collector` | `roles/browser` | **organization** |
| `qms-collector` | `roles/bigquery.dataEditor` | **dataset** `quota_monitoring` |
| `qms-collector` | `roles/bigquery.jobUser` | project (no narrower scope exists) |
| `qms-dashboard` | `roles/bigquery.dataViewer` | **dataset** `quota_monitoring` |
| `qms-dashboard` | `roles/bigquery.jobUser` | project (no narrower scope exists) |
| `qms-build` | `roles/storage.objectViewer` | **bucket** `…-qms-build-source` |
| `qms-build` | `roles/artifactregistry.writer` | **repository** `qms` |
| `qms-build` | `roles/logging.logWriter` | project (no narrower scope exists) |
| Cloud Run service agent | `roles/artifactregistry.reader` | **repository** `qms` |
| `qms-scheduler` | `roles/run.invoker` | **job** `qms-collector` |
| `var.dashboard_invokers` | `roles/run.invoker` | **service** `qms-dashboard` |

The three org roles are read-only. The collector never writes to any project it
scans; its only write target is its own dataset.

`var.dashboard_invokers` has a validation block that **refuses** `allUsers` and
`allAuthenticatedUsers`. The dashboard renders organization-wide quota posture
and must never be public.

## Two-phase bootstrap

A Cloud Run job cannot be created without an image, and the image cannot be
pushed without a repository. So the first deployment into a clean project is
two applies:

```bash
export TF_GOOGLE_USE_INTERNAL_REGISTRY=true
cd terraform/example

# Phase 1 -- APIs, repository, bucket, accounts.
terraform init
terraform apply \
  -target=module.qms.google_project_service.this \
  -target=module.qms.google_artifact_registry_repository.qms \
  -target=module.qms.google_storage_bucket.build_source \
  -target=module.qms.google_service_account.build \
  -target=module.qms.google_artifact_registry_repository_iam_member.build_pushes \
  -target=module.qms.google_storage_bucket_iam_member.build_reads_source \
  -target=module.qms.google_project_iam_member.build_logs

# Phase 2 -- build the image, from the repo root.
cd ../..
gcloud builds submit \
  --config cloudbuild.yaml \
  --service-account projects/PROJECT/serviceAccounts/qms-build@PROJECT.iam.gserviceaccount.com \
  --gcs-source-staging-dir gs://PROJECT-qms-build-source/source \
  --region REGION .

# Phase 3 -- everything else.
cd terraform/example
terraform apply
```

`terraform output build_command` prints the exact `gcloud builds submit` line
with the project, region and bucket already substituted.

> Cloud Build with a user-specified `--service-account` **requires** an explicit
> logging destination. `cloudbuild.yaml` sets `options.logging:
> CLOUD_LOGGING_ONLY`; do not remove it or the build is rejected before it
> starts.

## Adopting an existing hand-built deployment

`example/imports.tf` contains `import` blocks for the 17 resources that were
created imperatively during the first deployment of this project. With that file
in place, `terraform plan` should show **no destroys** and only the intended
additions (the source bucket and the dataset-scoped IAM).

It deliberately does **not** import four over-broad bindings that the module
does not reproduce:

```
qms-build     roles/storage.objectAdmin      (project)
qms-build     roles/artifactregistry.writer  (project)
qms-collector roles/bigquery.dataEditor      (project)
qms-dashboard roles/bigquery.dataViewer      (project)
```

Terraform cannot revoke a binding it never knew about, and importing them would
put them under management and then *keep* them. Remove them after the apply:

```bash
../../scripts/revoke-legacy-iam.sh PROJECT_ID
```

The script removes each binding and then re-reads the policy to confirm, exiting
non-zero if any survives. Run it **after** the apply, so the narrow replacements
already exist and nothing loses access mid-flight.

Once `terraform plan` is clean, delete `imports.tf`.

## Verifying

```bash
terraform output dashboard_url
gcloud run jobs execute qms-collector --region REGION --wait
```

The dashboard is IAM-gated with no public ingress path for unauthenticated
callers, so reach it with:

```bash
terraform output -raw proxy_command   # gcloud run services proxy …
```

`ingress` is `INGRESS_TRAFFIC_ALL` on purpose: `gcloud run services proxy` calls
the public endpoint with a signed token, and internal-only ingress would break
it. Authorization is still enforced by `roles/run.invoker`.

## Destroying

`delete_dataset_contents_on_destroy` defaults to `false`, so `terraform destroy`
will fail while the dataset holds tables. That is intentional -- the dataset is
the only stateful thing here and 400 days of history is not recoverable. Flip it
explicitly if you really mean it.
