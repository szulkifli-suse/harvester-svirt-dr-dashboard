import datetime as dt
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .harvester import ping as https_ping

REMOTE_SCRIPT = "/tmp/recover-harvester-vms.sh"
MAX_LINES = 5000
MAX_JOBS = 20


class Busy(RuntimeError):
    pass


class OpError(RuntimeError):
    pass


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    action: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "running"
    started: str = field(default_factory=_now_iso)
    finished: str | None = None
    error: str | None = None
    lines: list[dict] = field(default_factory=list)

    def log(self, msg: str) -> None:
        self.lines.append({"t": _now_iso(), "msg": msg})
        del self.lines[:-MAX_LINES]

    def to_dict(self, since: int = 0) -> dict:
        return {
            "id": self.id, "action": self.action, "status": self.status,
            "started": self.started, "finished": self.finished, "error": self.error,
            "line_count": len(self.lines), "lines": self.lines[since:],
        }


class JobRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self.jobs: list[Job] = []

    def current(self) -> Job | None:
        return next((j for j in reversed(self.jobs) if j.status == "running"), None)

    def get(self, job_id: str) -> Job | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def submit(self, action: str, fn: Callable[[Job], None]) -> Job:
        with self._lock:
            running = self.current()
            if running:
                raise Busy(f"'{running.action}' is already running")
            job = Job(action)
            self.jobs.append(job)
            del self.jobs[:-MAX_JOBS]
        threading.Thread(target=self._run, args=(job, fn), daemon=True).start()
        return job

    @staticmethod
    def _run(job: Job, fn: Callable[[Job], None]) -> None:
        try:
            fn(job)
            job.status = "succeeded"
            job.log("Done.")
        except Exception as e:  # noqa: BLE001 - every failure must end up in the job log
            job.status = "failed"
            job.error = str(e)
            job.log(f"FAILED: {e}")
        finally:
            job.finished = _now_iso()


@dataclass
class Timeouts:
    ec2_running: float = 600
    ec2_stopped: float = 900
    ssh_ready: float = 300
    boot_recovery_grace: float = 300
    api_ready: float = 1500
    nodes_ready: float = 1500
    vms_stopped: float = 600
    domains_stopped: float = 600
    recovery_script: float = 1800
    poll: float = 10


def _quietly(fn: Callable, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - used for polling while things come up
        return default


class Operations:
    def __init__(self, host, ssh, harvester, recover_script_path: str, timeouts: Timeouts | None = None,
                 ping: Callable[[str], bool] = https_ping,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.host = host
        self.ssh = ssh
        self.harvester = harvester
        self.recover_script_path = recover_script_path
        self.t = timeouts or Timeouts()
        self.ping = ping
        self.sleep = sleep
        self.clock = clock

    def _wait(self, predicate: Callable, timeout: float, what: str):
        deadline = self.clock() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if self.clock() >= deadline:
                raise OpError(f"timed out after {timeout:.0f}s waiting for {what}")
            self.sleep(self.t.poll)

    def _instance_in(self, *states: str):
        i = _quietly(self.host.instance)
        return i if i and i["state"] in states else None

    def _running_with_ip(self):
        i = self._instance_in("running")
        return i if i and i["public_ip"] else None

    def _nodes_ready(self) -> bool:
        nodes = _quietly(self.harvester.nodes, [])
        return bool(nodes) and all(n["ready"] for n in nodes)

    def _running_domains(self, ip: str) -> list[str]:
        return [d["name"] for d in self.ssh.domains(ip) if d["state"] == "running"]

    # -- start -----------------------------------------------------------------

    def start(self, job: Job) -> None:
        inst = self.host.instance()
        if inst["state"] == "stopping":
            job.log("EC2 host is still stopping; waiting for it to stop first")
            self._wait(lambda: self._instance_in("stopped"), self.t.ec2_stopped, "EC2 host to stop")
            inst["state"] = "stopped"
        if inst["state"] == "stopped":
            job.log(f"Starting EC2 host {inst['id']}")
            self.host.start(inst["id"])
        else:
            job.log(f"EC2 host {inst['id']} is already {inst['state']}")
        inst = self._wait(self._running_with_ip, self.t.ec2_running, "EC2 host to be running")
        ip = inst["public_ip"]
        job.log(f"EC2 host running at {ip}")

        job.log("Waiting for SSH")
        try:
            self._wait(lambda: self.ssh.reachable(ip), self.t.ssh_ready, "SSH")
            ssh_ok = True
        except OpError:
            ssh_ok = False
            job.log("WARNING: SSH is not reachable from the dashboard; relying on boot-time recovery (EC2 user data)")

        job.log("Waiting for boot-time recovery to bring Harvester up")
        try:
            self._wait(lambda: self.ping(ip), self.t.boot_recovery_grace, "Harvester /ping")
        except OpError:
            if not ssh_ok:
                raise OpError("Harvester is not answering and SSH is unreachable, so the dashboard cannot run recovery. "
                              "Allow the dashboard's egress IP on port 22 in the host security group.") from None
            job.log("Harvester is not answering yet; running recovery over SSH")
            self._run_recovery(job, ip)
        self._bring_up(job, ip)

    # -- recover ---------------------------------------------------------------

    def recover(self, job: Job) -> None:
        inst = self.host.instance()
        if inst["state"] != "running" or not inst["public_ip"]:
            raise OpError(f"EC2 host is {inst['state']}; use Start instead")
        ip = inst["public_ip"]
        if not self.ssh.reachable(ip):
            raise OpError(f"SSH to {ip} is unreachable; allow the dashboard's egress IP on port 22 in the host security group")
        self._run_recovery(job, ip)
        self._bring_up(job, ip)

    def _run_recovery(self, job: Job, ip: str) -> None:
        path = Path(self.recover_script_path)
        if not path.is_file():
            raise OpError(f"recovery script not found at {path}")
        job.log(f"Uploading {path.name} to the host and running it (this can take 15 minutes)")
        self.ssh.put_file(ip, REMOTE_SCRIPT, path.read_text())
        rc, _ = self.ssh.run(ip, f"sudo -n bash {REMOTE_SCRIPT} </dev/null", on_line=lambda line: job.log(f"  | {line}"),
                             timeout=self.t.recovery_script)
        if rc != 0:
            raise OpError(f"recovery script exited with status {rc}")
        stopped = [d["name"] for d in self.ssh.domains(ip) if d["state"] != "running"]
        if stopped:
            raise OpError(f"KVM nodes still not running after recovery: {', '.join(stopped)}")
        job.log("All KVM node instances are running")

    def _bring_up(self, job: Job, ip: str) -> None:
        job.log("Waiting for the Harvester API")
        self._wait(lambda: self.ping(ip), self.t.api_ready, "Harvester /ping")
        job.log("Harvester API is answering; waiting for all nodes to be Ready")
        self._wait(self._nodes_ready, self.t.nodes_ready, "all Harvester nodes Ready")
        job.log("All Harvester nodes are Ready")
        restored = self.harvester.restore_vms()
        for ref in restored:
            job.log(f"Restored VM {ref}")
        if not restored:
            job.log("No VMs were waiting to be restored")

    # -- stop ------------------------------------------------------------------

    def stop(self, job: Job, force: bool = False) -> None:
        inst = self.host.instance()
        if inst["state"] == "stopped":
            raise OpError("EC2 host is already stopped")
        if inst["state"] != "running" or not inst["public_ip"]:
            raise OpError(f"EC2 host is {inst['state']}; try again once it is running")
        ip = inst["public_ip"]

        ssh_ok = self.ssh.reachable(ip)
        api_ok = _quietly(lambda: self.harvester.nodes() is not None, False)
        if not force:
            if not ssh_ok:
                raise OpError("SSH to the host is unreachable, so the KVM nodes cannot be shut down cleanly. "
                              "Nothing was changed. Use force to stop the EC2 host anyway.")
            if not api_ok:
                raise OpError("The Harvester API is unreachable, so VMs cannot be stopped cleanly. "
                              "Nothing was changed. Use force to stop the EC2 host anyway.")

        if api_ok:
            halted, skipped = self.harvester.halt_running_vms()
            for ref in halted:
                job.log(f"Stopping VM {ref}")
            for ref in skipped:
                job.log(f"WARNING: VM {ref} has no runStrategy; left as is")
            if halted:
                try:
                    self._wait(lambda: not _quietly(self.harvester.vmi_names, ["?"]), self.t.vms_stopped, "VMs to stop")
                    job.log("All VMs stopped")
                except OpError:
                    if not force:
                        raise OpError("VMs did not stop in time. The host is still running; run Start to restore the VMs.") from None
                    job.log("WARNING: VMs did not stop in time; continuing because force is set")
        else:
            job.log("WARNING: Harvester API unreachable; skipping VM shutdown")

        if ssh_ok:
            running = self._running_domains(ip)
            for name in running:
                job.log(f"Shutting down KVM node {name}")
                self.ssh.virsh(ip, "shutdown", name)
            try:
                self._wait(lambda: not _quietly(lambda: self._running_domains(ip), ["?"]), self.t.domains_stopped,
                           "KVM nodes to shut down")
                job.log("All KVM nodes are shut off")
            except OpError:
                if not force:
                    raise OpError("KVM nodes did not shut down in time. The EC2 host is still running; "
                                  "run Recover to bring them back, or stop again with force.") from None
                for name in _quietly(lambda: self._running_domains(ip), []):
                    job.log(f"WARNING: forcing off KVM node {name}")
                    self.ssh.virsh(ip, "destroy", name)
        else:
            job.log("WARNING: SSH unreachable; the KVM nodes will be cut off by the EC2 shutdown")

        job.log(f"Stopping EC2 host {inst['id']}")
        self.host.stop(inst["id"])
        self._wait(lambda: self._instance_in("stopped"), self.t.ec2_stopped, "EC2 host to stop")
        job.log("EC2 host stopped")
