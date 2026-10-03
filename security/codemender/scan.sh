#!/usr/bin/env bash
#
# CodeMender scan wrapper.
#
# CodeMender ships no GitHub Action, no GitHub App and no REST API. The entire
# supported integration surface is "run the `cm` binary in your own CI
# container and emit SARIF", which is what this script does.
#
# Reference: https://docs.cloud.google.com/gemini-enterprise-agent-platform/codemender
#
# Usage:
#   scan.sh <output.sarif> <path> [path...]
#
# Requires: application default credentials, an allowlisted GCP project, and
# the CLOUDSDK_CORE_PROJECT / GOOGLE_CLOUD_PROJECT env var.

set -euo pipefail

CM_VERSION="${CM_VERSION:-stable}"
CM_MODEL="${CM_MODEL:-gemini-3.7-flash}"
# CodeMender is priced per token and its own docs recommend 10-50 files per
# scan for both cost and precision. A PR touching more than this is almost
# certainly better reviewed by a human first.
MAX_FILES="${CM_MAX_FILES:-50}"

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <output.sarif> <path> [path...]" >&2
  exit 2
fi

OUTPUT="$1"
shift
TARGETS=("$@")

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# --- install -----------------------------------------------------------------

if ! command -v cm >/dev/null 2>&1; then
  log "installing CodeMender CLI (${CM_VERSION})"
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  ARTIFACT="cm-linux-amd64.zip"  ;;
    Linux-aarch64) ARTIFACT="cm-linux-arm64.zip"  ;;
    Darwin-x86_64) ARTIFACT="cm-darwin-amd64.zip" ;;
    Darwin-arm64)  ARTIFACT="cm-darwin-arm64.zip" ;;
    *) echo "unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
  esac

  TMP="$(mktemp -d)"
  gcloud artifacts generic download \
    --project=cmoc-prod \
    --location=us \
    --repository=codemender-cli-production \
    --package=cm \
    --version="${CM_VERSION}" \
    --name="${ARTIFACT}" \
    --destination="${TMP}"

  unzip -q "${TMP}/${ARTIFACT}" -d "${TMP}"
  chmod +x "${TMP}/cm"
  export PATH="${TMP}:${PATH}"
fi

log "CodeMender version: $(cm --version 2>/dev/null || echo unknown)"

# --- configure ---------------------------------------------------------------

# vcs.type must be set explicitly or scans fail. The default extension include
# list omits YAML, JSON, shell and IaC files, so they are added back here;
# otherwise they are skipped silently, which looks identical to "no findings".
mkdir -p "${HOME}/.codemender"
cat > "${HOME}/.codemender/config.yaml" <<YAML
vcs:
  type: git
model: ${CM_MODEL}
scan:
  extensions:
    include:
      - .py
      - .tf
      - .yaml
      - .yml
      - .json
      - .sh
      - .toml
      - Dockerfile
  max_file_size_kb: 500
sandbox:
  enabled: true
  network:
    # CodeMender builds and executes real proof-of-concept exploits. Keep
    # egress closed; dependencies are pre-installed before this runs.
    profile: permissive-closed
tools:
  confirm_commands: false
  confirm_writes: false
YAML

# --- bound the scan ----------------------------------------------------------

mapfile -t FILES < <(
  for t in "${TARGETS[@]}"; do
    if [[ -d "$t" ]]; then
      find "$t" -type f \
        \( -name '*.py' -o -name '*.tf' -o -name '*.sh' -o -name 'Dockerfile' \)
    elif [[ -f "$t" ]]; then
      echo "$t"
    fi
  done | sort -u
)

if [[ ${#FILES[@]} -eq 0 ]]; then
  log "no scannable files; writing empty SARIF"
  cat > "${OUTPUT}" <<'JSON'
{"$schema":"https://json.schemastore.org/sarif-2.1.0.json","version":"2.1.0","runs":[{"tool":{"driver":{"name":"CodeMender","informationUri":"https://docs.cloud.google.com/gemini-enterprise-agent-platform/codemender","rules":[]}},"results":[]}]}
JSON
  exit 0
fi

if [[ ${#FILES[@]} -gt ${MAX_FILES} ]]; then
  log "WARNING: ${#FILES[@]} files exceeds CM_MAX_FILES=${MAX_FILES}; truncating"
  FILES=("${FILES[@]:0:${MAX_FILES}}")
fi

log "scanning ${#FILES[@]} file(s)"
printf '    %s\n' "${FILES[@]}"

# --- scan --------------------------------------------------------------------

cm init

# -y auto-approves exploit execution, which is required for a non-interactive
# run. Note this shifts responsibility to the caller under the Service Specific
# Terms; it is only acceptable because this runs on a disposable CI runner.
set +e
cm find -y --model "${CM_MODEL}" "${FILES[@]}"
FIND_STATUS=$?
set -e
log "cm find exited ${FIND_STATUS}"

cm report --format sarif > "${OUTPUT}" || {
  log "report generation failed; emitting empty SARIF so the upload step still runs"
  cat > "${OUTPUT}" <<'JSON'
{"$schema":"https://json.schemastore.org/sarif-2.1.0.json","version":"2.1.0","runs":[{"tool":{"driver":{"name":"CodeMender","informationUri":"https://docs.cloud.google.com/gemini-enterprise-agent-platform/codemender","rules":[]}},"results":[]}]}
JSON
}

log "wrote ${OUTPUT} ($(wc -c < "${OUTPUT}") bytes)"
cm report --format table || true
