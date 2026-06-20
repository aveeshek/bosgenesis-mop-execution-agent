#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-bosgenesis}"
K8S_SECRET_NAME="${K8S_SECRET_NAME:-bosgenesis-k8s-inspector-mcp-secret}"
HELM_SECRET_NAME="${HELM_SECRET_NAME:-bosgenesis-helm-manager-mcp-secret}"
SECRET_KEY="${SECRET_KEY:-BOSGENESIS_API_KEY}"

log() {
  printf '\n[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 127
  fi
}

generate_key() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(32))'
    return
  fi
  if command -v python >/dev/null 2>&1; then
    python -c 'import secrets; print(secrets.token_hex(32))'
    return
  fi
  echo "Required command not found: openssl, python3, or python" >&2
  exit 127
}

upsert_secret() {
  local name="$1"
  local value="$2"

  log "Creating/updating secret ${name} in namespace ${NAMESPACE}"
  kubectl create secret generic "${name}" \
    -n "${NAMESPACE}" \
    --from-literal="${SECRET_KEY}=${value}" \
    --dry-run=client \
    -o yaml | kubectl apply -f -
}

require_cmd kubectl

log "Ensuring namespace ${NAMESPACE} exists"
kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${NAMESPACE}"

K8S_API_KEY="${K8S_API_KEY:-$(generate_key)}"
HELM_API_KEY="${HELM_API_KEY:-$(generate_key)}"

upsert_secret "${K8S_SECRET_NAME}" "${K8S_API_KEY}"
upsert_secret "${HELM_SECRET_NAME}" "${HELM_API_KEY}"

log "Verification"
kubectl get secret "${K8S_SECRET_NAME}" "${HELM_SECRET_NAME}" -n "${NAMESPACE}"

cat <<EOF

Use these Helm values for bosgenesis-mop-execution-agent:

external:
  k8sInspectorApiKeySecret:
    name: ${K8S_SECRET_NAME}
    key: ${SECRET_KEY}
  helmManagerApiKeySecret:
    name: ${HELM_SECRET_NAME}
    key: ${SECRET_KEY}

If the K8s Inspector or Helm Manager deployments already use different API-key
secrets, point these values at those existing secrets instead of creating new keys.
EOF
