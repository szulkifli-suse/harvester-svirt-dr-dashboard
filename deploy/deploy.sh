#!/usr/bin/env bash
# Install or upgrade the DR dashboard Helm release on the dark-ai cluster. Safe to re-run.
#
# First install needs:
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  keys of the dedicated IAM user (deploy/iam-policy.json)
#   HARVESTER_KUBECONFIG                       default: projects/aws/suse-virt-migration-dr_kube_config.yml
# On upgrade these are optional: anything not given keeps the value already in the cluster.
#
# Optional: DASHBOARD_PASSWORD (generated on first install), SSH_KEY_FILE, RECOVER_SCRIPT_FILE,
#           KUBECONFIG (default projects/aws/dark-ai-kubectl.yaml), KUBE_CONTEXT (default dark-ai),
#           and any extra helm arguments, e.g. ./deploy.sh --set image.tag=0.1.1
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
app=$(cd "$here/.." && pwd)
# Defaults below assume this repo sits at harvester-cloud/dashboard, next to projects/aws.
root=$(cd "$app/.." && pwd)
ns=harvester-dr-dashboard
export KUBECONFIG=${KUBECONFIG:-$root/projects/aws/dark-ai-kubectl.yaml}
ctx=${KUBE_CONTEXT:-dark-ai}
ssh_key=${SSH_KEY_FILE:-$root/projects/aws/suse-virt-migration-dr-ssh_private_key.pem}
recover=${RECOVER_SCRIPT_FILE:-$root/projects/aws/recover-harvester-vms-dr.sh}
harvester_kubeconfig=${HARVESTER_KUBECONFIG:-$root/projects/aws/suse-virt-migration-dr_kube_config.yml}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
args=(--set-file "recoverScript=$recover" --set-file "secrets.sshPrivateKey=$ssh_key")

# Secrets go through 0600 temp files so they never appear in the process list.
secret_arg() {
  local value=$1 key=$2
  [ -n "$value" ] || return 0
  (umask 077; printf '%s' "$value" > "$tmp/$key")
  args+=(--set-file "secrets.$key=$tmp/$key")
}
secret_arg "${AWS_ACCESS_KEY_ID:-}" awsAccessKeyId
secret_arg "${AWS_SECRET_ACCESS_KEY:-}" awsSecretAccessKey
secret_arg "${DASHBOARD_PASSWORD:-}" dashboardPassword
if [ -r "$harvester_kubeconfig" ]; then
  args+=(--set-file "secrets.harvesterKubeconfig=$harvester_kubeconfig")
fi

helm --kube-context "$ctx" upgrade --install harvester-dashboard "$app/chart/harvester-dashboard" \
  --namespace "$ns" --create-namespace --wait --timeout 5m "${args[@]}" "$@"
