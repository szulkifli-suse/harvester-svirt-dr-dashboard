import ssl
import urllib.request
from collections import Counter

from kubernetes import client, config
from kubernetes.utils import parse_quantity

# Set on a VM when the dashboard halts it; holds the runStrategy to restore on start.
RESTORE_ANNOTATION = "harvester-dashboard/restore-run-strategy"
TIMEOUT = (3, 15)
MERGE_PATCH = "application/merge-patch+json"


def ping(ip: str, timeout: float = 5) -> bool:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(f"https://{ip}/ping", timeout=timeout, context=ctx) as r:
            return r.read(16).strip() == b"pong"
    except Exception:  # noqa: BLE001
        return False


def _vcpus(domain: dict) -> int:
    cpu = domain.get("cpu", {})
    return cpu.get("cores", 1) * cpu.get("sockets", 1) * cpu.get("threads", 1)


class Harvester:
    def __init__(self, kubeconfig: str):
        self.kubeconfig = kubeconfig
        self._clients = None

    def _api(self):
        if self._clients is None:
            cfg = client.Configuration()
            config.load_kube_config(config_file=self.kubeconfig, client_configuration=cfg)
            # Fail fast: the status page polls, so a dead API must not stall it with urllib3 retries.
            cfg.retries = False
            api_client = client.ApiClient(cfg)
            self._clients = (client.CoreV1Api(api_client), client.CustomObjectsApi(api_client))
        return self._clients

    def nodes(self) -> list[dict]:
        core, custom = self._api()
        items = core.list_node(_request_timeout=TIMEOUT).items
        usage = {}
        try:
            metrics = custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes", _request_timeout=TIMEOUT)
            usage = {m["metadata"]["name"]: m["usage"] for m in metrics["items"]}
        except Exception:  # noqa: BLE001 - metrics are optional
            pass
        out = []
        for n in items:
            labels = n.metadata.labels or {}
            alloc = n.status.allocatable or {}
            used = usage.get(n.metadata.name)
            out.append({
                "name": n.metadata.name,
                "ready": any(c.type == "Ready" and c.status == "True" for c in n.status.conditions or []),
                "roles": sorted(k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")),
                "maintenance": (n.metadata.annotations or {}).get("harvesterhci.io/maintain-status"),
                "unschedulable": bool(n.spec.unschedulable),
                "cpu_alloc": float(parse_quantity(alloc.get("cpu", "0"))),
                "mem_alloc": int(parse_quantity(alloc.get("memory", "0"))),
                "cpu_used": float(parse_quantity(used["cpu"])) if used else None,
                "mem_used": int(parse_quantity(used["memory"])) if used else None,
            })
        return out

    def _list_vms(self) -> list[dict]:
        _, custom = self._api()
        return custom.list_cluster_custom_object("kubevirt.io", "v1", "virtualmachines", _request_timeout=TIMEOUT)["items"]

    def vmi_names(self) -> list[str]:
        _, custom = self._api()
        items = custom.list_cluster_custom_object("kubevirt.io", "v1", "virtualmachineinstances", _request_timeout=TIMEOUT)["items"]
        return [f'{i["metadata"]["namespace"]}/{i["metadata"]["name"]}' for i in items]

    def vms(self) -> list[dict]:
        _, custom = self._api()
        vmis = {
            (i["metadata"]["namespace"], i["metadata"]["name"]): i
            for i in custom.list_cluster_custom_object("kubevirt.io", "v1", "virtualmachineinstances", _request_timeout=TIMEOUT)["items"]
        }
        out = []
        for vm in self._list_vms():
            meta, spec = vm["metadata"], vm.get("spec", {})
            vmi = vmis.get((meta["namespace"], meta["name"]), {})
            domain = spec.get("template", {}).get("spec", {}).get("domain", {})
            iface = (vmi.get("status", {}).get("interfaces") or [{}])[0]
            out.append({
                "namespace": meta["namespace"],
                "name": meta["name"],
                "status": vm.get("status", {}).get("printableStatus", "Unknown"),
                "run_strategy": spec.get("runStrategy"),
                "node": vmi.get("status", {}).get("nodeName"),
                "ip": iface.get("ipAddress"),
                "vcpus": _vcpus(domain),
                "memory": domain.get("memory", {}).get("guest") or domain.get("resources", {}).get("limits", {}).get("memory"),
                "restore_pending": RESTORE_ANNOTATION in (meta.get("annotations") or {}),
            })
        return out

    def _patch_vm(self, namespace: str, name: str, body: dict) -> None:
        _, custom = self._api()
        custom.patch_namespaced_custom_object(
            "kubevirt.io", "v1", namespace, "virtualmachines", name, body,
            _content_type=MERGE_PATCH, _request_timeout=TIMEOUT,
        )

    def halt_running_vms(self) -> tuple[list[str], list[str]]:
        """Halt every VM that should be running, remembering its runStrategy. Returns (halted, skipped)."""
        halted, skipped = [], []
        for vm in self._list_vms():
            meta, spec = vm["metadata"], vm.get("spec", {})
            ref = f'{meta["namespace"]}/{meta["name"]}'
            strategy = spec.get("runStrategy")
            if strategy is None:
                skipped.append(ref)
                continue
            if strategy == "Halted":
                continue
            self._patch_vm(meta["namespace"], meta["name"], {
                "metadata": {"annotations": {RESTORE_ANNOTATION: strategy}},
                "spec": {"runStrategy": "Halted"},
            })
            halted.append(ref)
        return halted, skipped

    def restore_vms(self) -> list[str]:
        restored = []
        for vm in self._list_vms():
            meta = vm["metadata"]
            strategy = (meta.get("annotations") or {}).get(RESTORE_ANNOTATION)
            if not strategy:
                continue
            self._patch_vm(meta["namespace"], meta["name"], {
                "metadata": {"annotations": {RESTORE_ANNOTATION: None}},
                "spec": {"runStrategy": strategy},
            })
            restored.append(f'{meta["namespace"]}/{meta["name"]} -> {strategy}')
        return restored

    def volumes(self) -> dict:
        _, custom = self._api()
        items = custom.list_namespaced_custom_object("longhorn.io", "v1beta2", "longhorn-system", "volumes", _request_timeout=TIMEOUT)["items"]
        robustness = Counter(v.get("status", {}).get("robustness", "unknown") for v in items)
        return {"total": len(items), **robustness}

    def version(self) -> str | None:
        _, custom = self._api()
        s = custom.get_cluster_custom_object("harvesterhci.io", "v1beta1", "settings", "server-version", _request_timeout=TIMEOUT)
        return s.get("value") or s.get("default")
