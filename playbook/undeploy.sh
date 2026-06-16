#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

APP_NAME="${APP_NAME:-bosgenesis-mop-execution-agent}"
NAMESPACE="${NAMESPACE:-bosgenesis}"
DEPLOY_METHOD="${DEPLOY_METHOD:-helm}"
HELM_RELEASE="${HELM_RELEASE:-bosgenesis-mop-execution-agent}"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-bosgenesis-mop-execution-agent}"
IMAGE_TAG="${IMAGE_TAG:-0.1.0}"
IMAGE="${IMAGE_REPOSITORY}:${IMAGE_TAG}"
IMAGE_TAR="${IMAGE_REPOSITORY}-${IMAGE_TAG}.tar"
REMOTE_USER="${REMOTE_USER:-taieuser}"
REMOTE_HOST="${REMOTE_HOST:-10.99.52.165}"
REMOTE_TMP_DIR="${REMOTE_TMP_DIR:-/tmp}"
REMOTE_IMAGE_TAR="${REMOTE_TMP_DIR}/${IMAGE_TAR}"
DELETE_NAMESPACE="${DELETE_NAMESPACE:-false}"
DELETE_REMOTE_IMAGE="${DELETE_REMOTE_IMAGE:-false}"
DELETE_REMOTE_TAR="${DELETE_REMOTE_TAR:-false}"

log() {
  printf '\n[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 127
  fi
}

delete_resource() {
  local kind="$1"
  local name="$2"
  log "Deleting ${kind}/${name} from namespace ${NAMESPACE}"
  kubectl delete "${kind}" "${name}" -n "${NAMESPACE}" --ignore-not-found=true
}

require_cmd kubectl

if [ "${DEPLOY_METHOD}" = "helm" ]; then
  require_cmd helm
  log "Uninstalling Helm release ${HELM_RELEASE} from namespace ${NAMESPACE}"
  helm uninstall "${HELM_RELEASE}" --namespace "${NAMESPACE}" --ignore-not-found
else
  log "Uninstalling raw Kubernetes resources for ${APP_NAME}"
  delete_resource deployment "${HELM_RELEASE}"
  delete_resource deployment "${HELM_RELEASE}-worker"
  delete_resource deployment "${HELM_RELEASE}-reconciler"
  delete_resource service "${HELM_RELEASE}"
  delete_resource ingress "${HELM_RELEASE}"
  delete_resource configmap "${HELM_RELEASE}-config"
  delete_resource persistentvolumeclaim "${HELM_RELEASE}-data"
fi

if [ "${DELETE_REMOTE_IMAGE}" = "true" ] || [ "${DELETE_REMOTE_TAR}" = "true" ]; then
  require_cmd ssh

  if [ "${DELETE_REMOTE_IMAGE}" = "true" ]; then
    log "Removing imported image ${IMAGE} from containerd on ${REMOTE_HOST}"
    ssh "${REMOTE_USER}@${REMOTE_HOST}" "sudo ctr -n k8s.io images rm '${IMAGE}' || true"
  fi

  if [ "${DELETE_REMOTE_TAR}" = "true" ]; then
    log "Removing remote image tar ${REMOTE_IMAGE_TAR} from ${REMOTE_HOST}"
    ssh "${REMOTE_USER}@${REMOTE_HOST}" "rm -f '${REMOTE_IMAGE_TAR}'"
  fi
fi

if [ "${DELETE_NAMESPACE}" = "true" ]; then
  log "Deleting namespace ${NAMESPACE}"
  kubectl delete namespace "${NAMESPACE}" --ignore-not-found=true
else
  log "Skipping namespace deletion. Set DELETE_NAMESPACE=true only for a dedicated namespace."
  log "Uninstall verification"
  kubectl get deployment,svc,ingress,configmap,pvc \
    -n "${NAMESPACE}" \
    -l "app.kubernetes.io/name=${APP_NAME}" \
    --ignore-not-found
fi

log "Done"
