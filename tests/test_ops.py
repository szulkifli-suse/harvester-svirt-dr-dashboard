import pytest

from app.ops import Job, Operations, OpError, Timeouts


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class FakeHost:
    def __init__(self, state="stopped"):
        self.state = state
        self.calls = []

    def instance(self):
        ip = "1.2.3.4" if self.state == "running" else None
        return {"id": "i-1", "state": self.state, "public_ip": ip}

    def start(self, iid):
        self.calls.append("ec2.start")
        self.state = "running"

    def stop(self, iid):
        self.calls.append("ec2.stop")
        self.state = "stopped"


class FakeSsh:
    def __init__(self, reachable=True, domains=("harvester-node-1", "harvester-node-2")):
        self.ok = reachable
        self.domain_state = {d: "running" for d in domains}
        self.calls = []
        self.shutdown_works = True

    def reachable(self, ip, timeout=8):
        return self.ok

    def domains(self, ip):
        return [{"name": n, "state": s} for n, s in self.domain_state.items()]

    def virsh(self, ip, action, name):
        self.calls.append(f"virsh {action} {name}")
        if action == "destroy" or self.shutdown_works:
            self.domain_state[name] = "shut off"

    def put_file(self, ip, path, content):
        self.calls.append(f"put {path}")

    def run(self, ip, command, stdin=None, on_line=None, timeout=0):
        self.calls.append(f"run {command}")
        for name in self.domain_state:
            self.domain_state[name] = "running"
        on_line("Harvester is up")
        return 0, ""


class FakeHarvester:
    def __init__(self, reachable=True):
        self.ok = reachable
        self.vm = {"default/a": "RerunOnFailure", "default/b": "Halted"}
        self.restore = {}
        self.calls = []

    def nodes(self):
        if not self.ok:
            raise RuntimeError("api down")
        return [{"name": "n1", "ready": True}]

    def vmi_names(self):
        return [k for k, v in self.vm.items() if v != "Halted"]

    def halt_running_vms(self):
        halted = [k for k, v in self.vm.items() if v != "Halted"]
        for k in halted:
            self.restore[k] = self.vm[k]
            self.vm[k] = "Halted"
        self.calls.append("halt")
        return halted, []

    def restore_vms(self):
        out = []
        for k, v in self.restore.items():
            self.vm[k] = v
            out.append(f"{k} -> {v}")
        self.restore = {}
        self.calls.append("restore")
        return out


def make(tmp_path, host=None, ssh=None, harv=None, ping=lambda ip: True):
    script = tmp_path / "recover.sh"
    script.write_text("echo hi\n")
    clock = Clock()
    ops = Operations(host or FakeHost(), ssh or FakeSsh(), harv or FakeHarvester(), str(script),
                     timeouts=Timeouts(), ping=ping, sleep=clock.sleep, clock=clock)
    return ops


def test_start_relies_on_boot_recovery_when_harvester_answers(tmp_path):
    ops = make(tmp_path)
    ops.harvester.restore = {"default/a": "RerunOnFailure"}
    job = Job("start")
    ops.start(job)
    assert ops.host.calls == ["ec2.start"]
    assert not any(c.startswith("run") for c in ops.ssh.calls)
    assert ops.harvester.vm["default/a"] == "RerunOnFailure"
    assert any("Restored VM default/a" in line["msg"] for line in job.lines)


def test_start_runs_recovery_when_harvester_stays_silent(tmp_path):
    answers = {"n": 0}

    def ping(ip):
        answers["n"] += 1
        return answers["n"] > 40  # silent through the boot grace period

    ops = make(tmp_path, ping=ping)
    ops.start(Job("start"))
    assert "put /tmp/recover-harvester-vms.sh" in ops.ssh.calls
    assert any(c.startswith("run sudo -n bash /tmp/recover-harvester-vms.sh") for c in ops.ssh.calls)


def test_start_fails_clearly_without_ssh_when_harvester_silent(tmp_path):
    ops = make(tmp_path, ssh=FakeSsh(reachable=False), ping=lambda ip: False)
    with pytest.raises(OpError, match="SSH is unreachable"):
        ops.start(Job("start"))


def test_stop_order_vms_then_nodes_then_ec2(tmp_path):
    host = FakeHost("running")
    ops = make(tmp_path, host=host)
    ops.stop(Job("stop"))
    assert ops.harvester.vm == {"default/a": "Halted", "default/b": "Halted"}
    assert ops.harvester.restore == {"default/a": "RerunOnFailure"}
    assert ops.ssh.calls == ["virsh shutdown harvester-node-1", "virsh shutdown harvester-node-2"]
    assert host.calls == ["ec2.stop"] and host.state == "stopped"


def test_stop_refuses_without_ssh_and_changes_nothing(tmp_path):
    host = FakeHost("running")
    ops = make(tmp_path, host=host, ssh=FakeSsh(reachable=False))
    with pytest.raises(OpError, match="Nothing was changed"):
        ops.stop(Job("stop"))
    assert ops.harvester.calls == [] and host.calls == []


def test_stop_refuses_without_harvester_api(tmp_path):
    host = FakeHost("running")
    ops = make(tmp_path, host=host, harv=FakeHarvester(reachable=False))
    with pytest.raises(OpError, match="Harvester API is unreachable"):
        ops.stop(Job("stop"))
    assert host.calls == []


def test_stop_keeps_host_up_when_nodes_hang(tmp_path):
    host = FakeHost("running")
    ssh = FakeSsh()
    ssh.shutdown_works = False
    ops = make(tmp_path, host=host, ssh=ssh)
    with pytest.raises(OpError, match="did not shut down in time"):
        ops.stop(Job("stop"))
    assert host.calls == []


def test_forced_stop_destroys_hung_nodes(tmp_path):
    host = FakeHost("running")
    ssh = FakeSsh()
    ssh.shutdown_works = False
    ops = make(tmp_path, host=host, ssh=ssh)
    ops.stop(Job("stop"), force=True)
    assert "virsh destroy harvester-node-1" in ssh.calls
    assert host.state == "stopped"


def test_recover_requires_running_host(tmp_path):
    ops = make(tmp_path)
    with pytest.raises(OpError, match="use Start"):
        ops.recover(Job("recover"))


def test_recover_runs_script_and_waits(tmp_path):
    ops = make(tmp_path, host=FakeHost("running"))
    ops.ssh.domain_state = {"harvester-node-1": "shut off"}
    job = Job("recover")
    ops.recover(job)
    assert any("| Harvester is up" in line["msg"] for line in job.lines)
    assert ops.ssh.domain_state == {"harvester-node-1": "running"}
