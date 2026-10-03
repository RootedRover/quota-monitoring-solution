# CodeMender integration

[CodeMender](https://docs.cloud.google.com/gemini-enterprise-agent-platform/codemender)
is Google's AI code security agent. Unlike a conventional scanner it works in
three phases: it **scans** for vulnerabilities, **verifies** each one by
building and executing a real proof-of-concept exploit in a sandbox, and then
**remediates** by proposing a patch. The verification step is the interesting
part, because it is what removes false positives.

## Why this is not a per-commit gate

The blog announcement says CodeMender "can integrate with existing CI/CD
workflows". In practice, as of 2026-09-15, that means: run the `cm` binary in
your own container and emit SARIF. There is **no GitHub Action, no GitHub App,
no Cloud Build integration and no customer-facing REST API**.

Four further constraints shaped the design here:

| Constraint | Consequence |
|---|---|
| Allowlist-gated Public Preview | Cannot be a required check — access can lapse |
| Billed per Vertex AI token | Running on every push would be costly and mostly redundant |
| Docs recommend 10–50 files per scan | Whole-repo scanning is discouraged |
| Executes real exploits | Needs a disposable sandbox, never a workstation |
| Terms permit evaluation only, not production use | Cannot be load-bearing |

So the **blocking** gate is CodeQL, OSV-Scanner, Ruff `S`, Trivy and zizmor.
CodeMender runs as a **deep scan**: weekly, on demand, or on a pull request
labelled `codemender` — where it scans only that PR's changed files, which fits
the 10–50 file guidance naturally.

## Enabling it

### 1. Get the GCP project allowlisted

The gate is the **project number**, not the repo or the user.

- Internal / Argolis projects: **`go/cmoc-allowlist-internal-project-request`**
- External customer projects: **`go/cmoc-allowlist-request`** (filed *by a
  Googler* on the customer's behalf — never send the form to the customer)

For this project: `krishngupt-argolis`, project number **`114680847754`**.

Onboarding runs in weekly batches that land on Mondays; expect roughly three
days. Org-level allowlisting is being piloted, so ask about covering the whole
Argolis org rather than one project.

### 2. Prepare the project

```bash
gcloud services enable aiplatform.googleapis.com cloudresourcemanager.googleapis.com \
  --project krishngupt-argolis
```

Billing must be enabled — CodeMender bills as ordinary Vertex AI token usage
(\$0.75/1M input, \$3.75/1M output until 2026-12-31, doubling after). There is
no separate SKU or licence.

### 3. Set up Workload Identity Federation

There is deliberately **no service-account JSON key** in this repository; a
key in a public repo would be exposed to every fork PR.

```bash
PROJECT_ID=krishngupt-argolis
PROJECT_NUMBER=114680847754
REPO=RootedRover/quota-monitoring-solution

gcloud iam service-accounts create codemender-ci \
  --project "$PROJECT_ID" --display-name "CodeMender CI"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:codemender-ci@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role roles/aiplatform.user

gcloud iam workload-identity-pools create github \
  --project "$PROJECT_ID" --location global --display-name "GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github \
  --project "$PROJECT_ID" --location global \
  --workload-identity-pool github \
  --display-name "GitHub" \
  --issuer-uri "https://token.actions.githubusercontent.com" \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition "assertion.repository == '${REPO}'"

gcloud iam service-accounts add-iam-policy-binding \
  "codemender-ci@${PROJECT_ID}.iam.gserviceaccount.com" \
  --project "$PROJECT_ID" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}"
```

> The `attribute-condition` pinning the provider to this exact repository is
> essential. Without it, any GitHub repository in the world could mint tokens
> for this service account.

### 4. Set the repository variables

These are **variables**, not secrets — none of them is sensitive, and keeping
them visible makes the configuration auditable.

```bash
gh variable set CODEMENDER_ENABLED --body "true"
gh variable set GCP_PROJECT_ID     --body "krishngupt-argolis"
gh variable set GCP_SERVICE_ACCOUNT --body "codemender-ci@krishngupt-argolis.iam.gserviceaccount.com"
gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER \
  --body "projects/114680847754/locations/global/workloadIdentityPools/github/providers/github"
```

Until `CODEMENDER_ENABLED` is `true`, every job in the workflow no-ops with an
explanatory message rather than failing.

### 5. Create the opt-in label

```bash
gh label create codemender \
  --description "Run a CodeMender deep scan on this PR's changed files" \
  --color 1a73e8
```

## Running it

```bash
# On demand, against a specific path
gh workflow run codemender.yml -f paths="collector/sources" -f model="gemini-3.7-flash"

# On a pull request: add the label
gh pr edit <N> --add-label codemender
```

Findings appear under **Security → Code scanning** with the `codemender`
category, and the raw SARIF is retained as a build artifact for 30 days.

## Running it locally

Do **not** run this on your workstation or Cloudtop — CodeMender builds and
launches working exploits. Use a disposable VM:

```bash
gcloud compute instances create codemender-sandbox \
  --project krishngupt-argolis --zone us-west1-a \
  --machine-type e2-highmem-4 \
  --image-family debian-12 --image-project debian-cloud \
  --no-address                              # egress via Cloud NAT only

gcloud compute ssh codemender-sandbox --tunnel-through-iap --zone us-west1-a
```

Then on the VM:

```bash
gcloud auth application-default login
git clone https://github.com/RootedRover/quota-monitoring-solution
cd quota-monitoring-solution
bash security/codemender/scan.sh findings.sarif collector
```

## Notes and gotchas

- **`vcs.type` must be set** in `~/.codemender/config.yaml` or scans fail.
  `scan.sh` writes this for you.
- **The default extension include list omits YAML, JSON, shell and IaC files.**
  They are skipped silently, which is indistinguishable from "no findings".
  `scan.sh` adds them back.
- **Sandbox networking is all-or-nothing** (`permissive-closed` or
  `permissive-open`) — there is no domain allowlist. Pre-install dependencies
  before scanning, or `build.command` will fail.
- `cm report --format sarif` is the only integration surface; everything else
  in this directory exists to feed it.
- Sessions are retained at most 7 days. Source code is never used for training
  and never leaves the runner — the agent does not clone your repo server-side.

## Related

- Sibling product, **no allowlist required**: the
  [SecureCoder IDE extension](https://open-vsx.org/extension/Google/securecoder),
  which works in Antigravity or any agentic IDE.
- Announcement: [Find and fix software vulnerabilities with CodeMender](https://cloud.google.com/blog/products/identity-security/find-and-fix-software-vulnerabilities-with-codemender)
