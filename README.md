# Harvester dashboard

A small web app, deployed on the `dark-ai` cluster, to watch and power-manage the nested SUSE Virtualization (Harvester) lab on AWS. The lab itself is built with [harvester-cloud](https://github.com/rancher/harvester-cloud) (`projects/aws`).

The scripts expect this repo to be cloned as `harvester-cloud/dashboard`. Their defaults read the dark-ai kubeconfig, the host SSH key and `recover-harvester-vms.sh` from `../projects/aws`. To use files elsewhere, set `KUBECONFIG`, `SSH_KEY_FILE` and `RECOVER_SCRIPT_FILE`. In the commands below, paths starting with `dashboard/` and `projects/` are relative to the `harvester-cloud` folder.

It shows:

- **EC2 host**: state, type, public IP, uptime, and CPU over the last 3 hours (CloudWatch)
- **Harvester**: API health, version, nodes Ready, VMs running, Longhorn volume health, and a link to the UI
- **KVM node instances**: the `harvester-node-N` libvirt domains on the host (`virsh list --all` over SSH)
- **Harvester nodes**: Ready, maintenance, CPU and memory use
- **Virtual machines**: status, node, IP and size
- **Activity**: a live log of the last Start, Stop or Recover

It has three actions, each behind a confirmation dialog. Only one action can run at a time.

| Action | What it does |
| --- | --- |
| **Start** | Starts the EC2 host. Boot-time recovery (EC2 user data) re-creates the KVM nodes. If Harvester still isn't answering after 5 minutes, it runs `recover-harvester-vms.sh` over SSH. It then waits for all nodes to be Ready and restarts the VMs that Stop halted. If the host is already up but unhealthy, the button reads **Bring up** and skips the EC2 step. |
| **Stop** | Halts every running VM (it saves each VM's `runStrategy` in the `harvester-dashboard/restore-run-strategy` annotation), runs `virsh shutdown` on the KVM nodes, then stops the EC2 host. If SSH or the Harvester API is unreachable, it refuses before changing anything unless **Force** is ticked. |
| **Recover KVM nodes** | Uploads and runs `projects/aws/recover-harvester-vms.sh` on the host. It mounts the data disks, re-creates `vlan1`, re-defines the nodes from their existing disks and restarts the socat proxy. Nodes that are already running are left alone. It then waits for all nodes to be Ready. |

## One-time setup

**1. AWS credentials.** Create an IAM user that can only describe instances, read CPU metrics, and start or stop instances tagged `Workload=harvester` and `Managed_by=terraform`:

```bash
aws iam create-user --user-name harvester-dashboard
```

```bash
aws iam put-user-policy --user-name harvester-dashboard --policy-name harvester-dashboard --policy-document file://dashboard/deploy/iam-policy.json
```

```bash
aws iam create-access-key --user-name harvester-dashboard
```

**2. SSH from dark-ai to the Harvester host.** Recover, clean Stop, and the KVM panel all need port 22. The host security group only allows `public_ip_source_addresses`. Add dark-ai's public IP, `54.251.5.234/32`, to that list in `projects/aws/terraform.tfvars`, then run `terraform plan`. **Before applying, check that the plan only updates the security group.** In this project an unrelated drift once turned a small change into a full host rebuild. If SSH is still blocked, the KVM panel shows which IP to allow.

**3. Harvester kubeconfig.** Port 6443 on the host doesn't answer from outside, so go through Rancher instead. In the Rancher UI, open the `suse-virt-migration` cluster and choose **Download KubeConfig**. The server will be `https://darkrancherprime.ddns.net/k8s/clusters/c-sks87`. The file carries a Rancher token with that user's permissions. Use a dedicated Rancher user or API key that can manage this cluster's VMs, and check its expiry. When the token expires, the panels show `401` errors until you re-run the deploy with a fresh file.

**4. Docker Hub push access.** The image goes to `docker.io/darkwunan/harvester-dashboard`, next to `darksunscreen`. Create a Docker Hub access token with Read & Write scope, then store it on dark-ai as the push secret:

```bash
read -rsp 'Docker Hub token: ' DH_TOKEN; echo
```

```bash
kubectl --kubeconfig projects/aws/dark-ai-kubectl.yaml --context dark-ai create namespace harvester-dashboard --dry-run=client -o yaml | kubectl --kubeconfig projects/aws/dark-ai-kubectl.yaml --context dark-ai apply -f -
```

```bash
kubectl --kubeconfig projects/aws/dark-ai-kubectl.yaml --context dark-ai -n harvester-dashboard create secret docker-registry registry-push --docker-server=https://index.docker.io/v1/ --docker-username=darkwunan --docker-password="$DH_TOKEN"
```

## Build the image

There's no Docker on the Mac, so [deploy/build.sh](deploy/build.sh) builds on dark-ai itself. It runs a rootless BuildKit job ([deploy/build-job.yaml](deploy/build-job.yaml)) that builds the [Dockerfile](Dockerfile) (SUSE BCI Python 3.13) and pushes it, tagged with the chart's `appVersion`:

```bash
./dashboard/deploy/build.sh
```

`PUSH=false ./dashboard/deploy/build.sh` only checks that the image builds. To ship a code change, bump `appVersion` in [Chart.yaml](chart/harvester-dashboard/Chart.yaml), then build and deploy.

## Deploy with Helm

```bash
export AWS_ACCESS_KEY_ID=<from step 1> AWS_SECRET_ACCESS_KEY=<from step 1>
export HARVESTER_KUBECONFIG=~/Downloads/suse-virt-migration.yaml
./dashboard/deploy/deploy.sh
```

[deploy/deploy.sh](deploy/deploy.sh) runs `helm upgrade --install harvester-dashboard` from [chart/harvester-dashboard](chart/harvester-dashboard) into the `harvester-dashboard` namespace. It passes the host SSH key and the recovery script from `projects/aws`, and talks to dark-ai through `projects/aws/dark-ai-kubectl.yaml`. Any extra arguments go straight to helm, for example `--set image.tag=0.1.1`.

The first install needs every credential. On later runs, anything you leave out keeps the value already in the cluster. A plain `./dashboard/deploy/deploy.sh` re-applies the chart and refreshes the recovery script.

The chart generates a login password on first install. To read it:

```bash
kubectl --kubeconfig projects/aws/dark-ai-kubectl.yaml --context dark-ai -n harvester-dashboard get secret harvester-dashboard -o jsonpath='{.data.dashboard-password}' | base64 -d; echo
```

Then open `https://svirt.lepak-awan.com/`. Plain `http://` redirects there. See [values.yaml](chart/harvester-dashboard/values.yaml) for the host, region, instance tags and resources. To change the host, edit `ingress.host` there or pass `--set ingress.host=...` to deploy.sh. If you edit the Ingress in Rancher instead, the next deploy puts it back.

## Develop

```bash
cd dashboard && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
```

```bash
.venv/bin/python -m pytest -q
```

To run it locally, set `AWS_REGION`, `DASHBOARD_PASSWORD`, `SSH_KEY_PATH`, `HARVESTER_KUBECONFIG` and `RECOVER_SCRIPT`, then start `.venv/bin/uvicorn --factory app.main:create_app --port 8088`. See [app/config.py](app/config.py) for every setting.

## Security notes

- A single password protects every API call. Sessions are signed, `HttpOnly`, `SameSite=Strict` cookies that last 12 hours, and failed logins are throttled. Mutating calls need an `X-Requested-With` header, which blocks cross-site requests.
- The ingress only serves HTTPS, using Traefik's self-signed default certificate. Add a real certificate if the URL is shared.
- The pod runs as non-root with a read-only root filesystem, all capabilities dropped and no service account token.
- Credentials are stored in the `harvester-dashboard` Secret, and Helm also keeps them in its release record in the same namespace. Anyone who can read secrets in that namespace can read them.
- The SSH host key isn't pinned, because the host boots from a live ISO and gets a new key on every boot.
- Jobs and sessions live in memory with one replica. If the pod restarts during an action, that action stops part-way. Start and Stop check the real state first, so running them again is safe.
