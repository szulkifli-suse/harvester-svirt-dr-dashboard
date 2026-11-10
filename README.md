# Harvester DR dashboard

The DR copy of [harvester-svirt-dashboard](https://github.com/szulkifli-suse/harvester-svirt-dashboard): a small web app, deployed on the `dark-ai` cluster, to watch and power-manage the nested SUSE Virtualization (Harvester) **DR** lab on AWS, `suse-virt-migration-dr`. The lab is built with [harvester-cloud](https://github.com/rancher/harvester-cloud) (`projects/aws`, Terraform workspace `dr`).

The app code, chart and container image (`docker.io/darkwunan/harvester-dashboard`) are the same as the primary dashboard. Only the defaults differ:

| | Primary | DR (this repo) |
| --- | --- | --- |
| URL | `https://svirt.lepak-awan.com/` | `https://svirt-dr.lepak-awan.com/` |
| Namespace on dark-ai | `harvester-dashboard` | `harvester-dr-dashboard` |
| EC2 host (tag `Name`) | `suse-virt-migration` | `suse-virt-migration-dr` (`i-08ffa222ec290c59f`) |
| SSH key | `suse-virt-migration-ssh_private_key.pem` | `suse-virt-migration-dr-ssh_private_key.pem` |
| Harvester kubeconfig | downloaded from Rancher | `suse-virt-migration-dr_kube_config.yml` (direct, port 6443) |
| Recovery script | `recover-harvester-vms.sh` | `recover-harvester-vms-dr.sh` |

The scripts expect this repo to be cloned as `harvester-cloud/dashboard-dr`. Their defaults read the dark-ai kubeconfig, the DR SSH key, the DR Harvester kubeconfig and `recover-harvester-vms-dr.sh` from `../projects/aws`. To use files elsewhere, set `KUBECONFIG`, `SSH_KEY_FILE`, `HARVESTER_KUBECONFIG` and `RECOVER_SCRIPT_FILE`. In the commands below, paths starting with `dashboard-dr/` and `projects/` are relative to the `harvester-cloud` folder.

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
| **Start** | Starts the EC2 host. The DR host has no EC2 user data, so its KVM nodes don't come back on their own: after 5 minutes without an answer from Harvester, the dashboard runs `recover-harvester-vms-dr.sh` over SSH. It then waits for all nodes to be Ready and restarts the VMs that Stop halted. If the host is already up but unhealthy, the button reads **Bring up** and skips the EC2 step. |
| **Stop** | Halts every running VM (it saves each VM's `runStrategy` in the `harvester-dashboard/restore-run-strategy` annotation), runs `virsh shutdown` on the KVM nodes, then stops the EC2 host. If SSH or the Harvester API is unreachable, it refuses before changing anything unless **Force** is ticked. |
| **Recover KVM nodes** | Uploads and runs `projects/aws/recover-harvester-vms-dr.sh` on the host. It mounts the data disks, re-creates `vlan1` with fixed DHCP leases for the DR nodes, re-defines the nodes from their existing disks and restarts the socat proxy. Nodes that are already running are left alone. It then waits for all nodes to be Ready. |

## DR recovery script

`projects/aws/recover-harvester-vms-dr.sh` is the primary script with the DR node MACs and IPs, which were read from the running DR host. DR has no Coriolis VM, so there is no port 8443 proxy.

| Node | MAC | IP |
| --- | --- | --- |
| harvester-node-1 | `52:54:00:06:7a:29` | `192.168.122.254` |
| harvester-node-2 | `52:54:00:1f:d4:96` | `192.168.122.75` |
| harvester-node-3 | `52:54:00:ba:53:4d` | `192.168.122.162` |

If the DR cluster is rebuilt, the MACs change. Read the new ones with `sudo virsh domiflist harvester-node-N` and `sudo virsh net-dhcp-leases vlan1` on the host, then update the script.

## One-time setup

**1. DNS.** Add an A record for `svirt-dr.lepak-awan.com` pointing to dark-ai, `54.251.5.234`, the same address as `svirt.lepak-awan.com`.

**2. Security group.** The dashboard reaches the DR host on port 22 (SSH) and 6443 (Harvester API). Both are open to `0.0.0.0/0` on `sg-067d6257d878f03a9`, matching the primary host. Those rules were added directly in AWS, so the next `terraform apply` in the `dr` workspace removes them. Re-add them afterwards, or add the addresses to `public_ip_source_addresses` in `terraform.tfvars` instead.

**3. AWS credentials.** These are the same as the primary dashboard: either the limited IAM user from [deploy/iam-policy.json](deploy/iam-policy.json), or your existing keys. The policy already covers the DR host, because it allows any instance tagged `Workload=harvester` and `Managed_by=terraform`.

There is nothing to build. The DR release uses the image the primary repo already publishes.

## Deploy with Helm

From the `harvester-cloud` folder:

```bash
AWS_ACCESS_KEY_ID="$(aws configure get aws_access_key_id)" AWS_SECRET_ACCESS_KEY="$(aws configure get aws_secret_access_key)" ./dashboard-dr/deploy/deploy.sh
```

[deploy/deploy.sh](deploy/deploy.sh) runs `helm upgrade --install harvester-dashboard` from [chart/harvester-dashboard](chart/harvester-dashboard) into the `harvester-dr-dashboard` namespace. It passes the DR SSH key, the DR Harvester kubeconfig and the DR recovery script from `projects/aws`, and talks to dark-ai through `projects/aws/dark-ai-kubectl.yaml`. Any extra arguments go straight to helm, for example `--set image.tag=0.1.1`.

The first install needs every credential. On later runs, anything you leave out keeps the value already in the cluster, so a plain `./dashboard-dr/deploy/deploy.sh` re-applies the chart and refreshes the kubeconfig and recovery script.

The chart generates a login password on first install, separate from the primary dashboard's. To read it:

```bash
kubectl --kubeconfig projects/aws/dark-ai-kubectl.yaml --context dark-ai -n harvester-dr-dashboard get secret harvester-dashboard -o jsonpath='{.data.dashboard-password}' | base64 -d; echo
```

Then open `https://svirt-dr.lepak-awan.com/`. Plain `http://` redirects there. See [values.yaml](chart/harvester-dashboard/values.yaml) for the host, region, instance tags and resources.

## Taking changes from the primary dashboard

This repo shares history with harvester-svirt-dashboard, which is set as the `upstream` remote. To pull in app or chart changes:

```bash
git -C dashboard-dr pull upstream main
```

To ship a code change, make it in the primary repo, build and push the image there (its `deploy/build.sh`), then pull it here and re-run the deploy with the new `image.tag`.

## Security notes

- A single password protects every API call. Sessions are signed, `HttpOnly`, `SameSite=Strict` cookies that last 12 hours, and failed logins are throttled. Mutating calls need an `X-Requested-With` header, which blocks cross-site requests.
- The ingress only serves HTTPS, using Traefik's self-signed default certificate. Add a real certificate if the URL is shared.
- The pod runs as non-root with a read-only root filesystem, all capabilities dropped and no service account token.
- Credentials are stored in the `harvester-dashboard` Secret in the `harvester-dr-dashboard` namespace, and Helm also keeps them in its release record there. Anyone who can read secrets in that namespace can read them.
- The SSH host key isn't pinned, because the host boots from a live ISO and gets a new key on every boot.
- Jobs and sessions live in memory with one replica. If the pod restarts during an action, that action stops part-way. Start and Stop check the real state first, so running them again is safe.
