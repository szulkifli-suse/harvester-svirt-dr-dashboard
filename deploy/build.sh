#!/usr/bin/env bash
# Build the dashboard image on the dark-ai cluster with rootless BuildKit and push it.
#
# Pushing needs a docker-registry secret named "registry-push" in the namespace (see README).
# PUSH=false only checks that the image builds.
#
# Optional: IMAGE (default docker.io/darkwunan/harvester-dashboard), TAG (default: chart appVersion),
#           KUBECONFIG (default projects/aws/dark-ai-kubectl.yaml), KUBE_CONTEXT (default dark-ai)
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
app=$(cd "$here/.." && pwd)
# Defaults below assume this repo sits at harvester-cloud/dashboard, next to projects/aws.
root=$(cd "$app/.." && pwd)
ns=harvester-dashboard
job=harvester-dashboard-build
export KUBECONFIG=${KUBECONFIG:-$root/projects/aws/dark-ai-kubectl.yaml}
ctx=${KUBE_CONTEXT:-dark-ai}
version=$(sed -n 's/^appVersion: *"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$app/chart/harvester-dashboard/Chart.yaml")
tag=${TAG:-$version}
image=${IMAGE:-docker.io/darkwunan/harvester-dashboard}:$tag
push=${PUSH:-true}

k() { kubectl --context "$ctx" "$@"; }

k create namespace "$ns" --dry-run=client -o yaml | k apply -f - >/dev/null
if [ "$push" = true ] && ! k -n "$ns" get secret registry-push >/dev/null 2>&1; then
  echo "Secret $ns/registry-push is missing; create it first (see dashboard/README.md)" >&2
  exit 1
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
COPYFILE_DISABLE=1 tar -czf "$tmp/context.tgz" -C "$app" \
  --exclude '__pycache__' --exclude '.DS_Store' Dockerfile .dockerignore requirements.txt app
k -n "$ns" create configmap harvester-dashboard-build-context --from-file=context.tgz="$tmp/context.tgz" \
  --dry-run=client -o yaml | k apply -f - >/dev/null

k -n "$ns" delete job "$job" --ignore-not-found --wait >/dev/null
sed -e "s|__IMAGE__|$image|" -e "s|__VERSION__|$tag|" -e "s|__PUSH__|$push|" "$here/build-job.yaml" | k -n "$ns" apply -f - >/dev/null
echo "Building $image (push=$push) on $ctx"

for _ in $(seq 1 90); do
  phase=$(k -n "$ns" get pods -l job-name="$job" -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)
  case "$phase" in Running|Succeeded|Failed) break ;; esac
  sleep 2
done
k -n "$ns" logs -f "job/$job" || true

for _ in $(seq 1 30); do
  succeeded=$(k -n "$ns" get job "$job" -o jsonpath='{.status.succeeded}')
  failed=$(k -n "$ns" get job "$job" -o jsonpath='{.status.failed}')
  [ "$succeeded" = 1 ] && { echo "Built $image"; exit 0; }
  [ -n "$failed" ] && break
  sleep 2
done
echo "Build failed; see: kubectl -n $ns logs job/$job" >&2
exit 1
