#!/usr/bin/env bash
#
# Revoke the over-broad IAM bindings created during the first, imperative
# deployment of QMS. Run this AFTER `terraform apply` has succeeded and the
# narrower replacements are in place.
#
# Why this exists as a script rather than Terraform: Terraform can only remove
# bindings it created. These four were granted by hand with gcloud, so they are
# invisible to state. Importing them would be worse than useless -- it would put
# them under management and then faithfully keep them.
#
#   qms-build     roles/storage.objectAdmin      -> objectViewer on one bucket
#   qms-build     roles/artifactregistry.writer  -> same role, repo-scoped
#   qms-collector roles/bigquery.dataEditor      -> same role, dataset-scoped
#   qms-dashboard roles/bigquery.dataViewer      -> same role, dataset-scoped
#
# Each removal is verified afterwards; the script exits non-zero if any binding
# survives, so a partial revocation cannot be mistaken for success.

set -euo pipefail

PROJECT="${1:-}"
if [[ -z "${PROJECT}" ]]; then
  echo "usage: $0 PROJECT_ID" >&2
  exit 2
fi

SA_SUFFIX="${PROJECT}.iam.gserviceaccount.com"

# member:role pairs to remove.
LEGACY=(
  "qms-build@${SA_SUFFIX}:roles/storage.objectAdmin"
  "qms-build@${SA_SUFFIX}:roles/artifactregistry.writer"
  "qms-collector@${SA_SUFFIX}:roles/bigquery.dataEditor"
  "qms-dashboard@${SA_SUFFIX}:roles/bigquery.dataViewer"
)

echo "Revoking legacy project-level bindings in ${PROJECT}"
echo

for entry in "${LEGACY[@]}"; do
  sa="${entry%%:*}"
  role="${entry##*:}"

  printf '  %-56s %s ... ' "${role}" "${sa%%@*}"

  if gcloud projects remove-iam-policy-binding "${PROJECT}" \
      --member="serviceAccount:${sa}" \
      --role="${role}" \
      --condition=None \
      --quiet >/dev/null 2>&1; then
    echo "revoked"
  else
    # Already absent is the desired end state, so this is not an error.
    echo "not present"
  fi
done

echo
echo "Verifying no legacy binding survives..."

policy="$(gcloud projects get-iam-policy "${PROJECT}" --format=json)"
failed=0

for entry in "${LEGACY[@]}"; do
  sa="${entry%%:*}"
  role="${entry##*:}"

  if echo "${policy}" \
     | python3 -c "
import json, sys
policy = json.load(sys.stdin)
role, member = sys.argv[1], 'serviceAccount:' + sys.argv[2]
for binding in policy.get('bindings', []):
    if binding['role'] == role and member in binding.get('members', []):
        sys.exit(0)
sys.exit(1)
" "${role}" "${sa}"; then
    echo "  STILL PRESENT: ${role} on ${sa}" >&2
    failed=1
  fi
done

if [[ "${failed}" -ne 0 ]]; then
  echo >&2
  echo "Revocation incomplete. Do not consider the migration done." >&2
  exit 1
fi

echo "  clean"
echo
echo "Legacy bindings removed. Remaining access for these accounts is"
echo "dataset-scoped, repository-scoped or bucket-scoped; see terraform/modules/qms/iam.tf."
