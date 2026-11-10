import re
import shlex
import time
from typing import Callable

import paramiko

_DOMAIN_LINE = re.compile(r"^\s*(\S+)\s+(\S+)\s+(.+?)\s*$")


class SshError(RuntimeError):
    pass


def _load_key(path: str) -> paramiko.PKey:
    last: Exception | None = None
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key_file(path)
        except Exception as e:  # noqa: BLE001 - try the next key type
            last = e
    raise SshError(f"cannot load SSH key {path}: {last}")


def parse_domains(output: str) -> list[dict]:
    domains = []
    for line in output.splitlines()[2:]:
        m = _DOMAIN_LINE.match(line)
        if m:
            domains.append({"name": m.group(2), "state": m.group(3)})
    return domains


class SshHost:
    def __init__(self, user: str, key_path: str, port: int = 22):
        self.user = user
        self.key_path = key_path
        self.port = port

    def _connect(self, ip: str, timeout: float) -> paramiko.SSHClient:
        c = paramiko.SSHClient()
        # The host runs from a live ISO and regenerates its host key on every boot,
        # so there is no stable key to pin.
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            ip,
            port=self.port,
            username=self.user,
            pkey=_load_key(self.key_path),
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        return c

    def reachable(self, ip: str, timeout: float = 8) -> bool:
        try:
            self._connect(ip, timeout).close()
            return True
        except Exception:  # noqa: BLE001
            return False

    def run(
        self,
        ip: str,
        command: str,
        stdin: str | None = None,
        on_line: Callable[[str], None] | None = None,
        timeout: float = 120,
    ) -> tuple[int, str]:
        client = self._connect(ip, 15)
        try:
            chan = client.get_transport().open_session()
            chan.set_combine_stderr(True)
            chan.exec_command(command)
            if stdin is not None:
                chan.sendall(stdin.encode())
                chan.shutdown_write()
            deadline = time.monotonic() + timeout
            buf, out = b"", []

            def emit(final: bool = False) -> None:
                nonlocal buf
                *lines, buf = buf.split(b"\n")
                if final and buf:
                    lines, buf = lines + [buf], b""
                for raw in lines:
                    line = raw.decode(errors="replace").rstrip("\r")
                    out.append(line)
                    if on_line:
                        on_line(line)

            while True:
                got = False
                while chan.recv_ready():
                    buf += chan.recv(65536)
                    got = True
                emit()
                if chan.exit_status_ready() and not chan.recv_ready():
                    break
                if time.monotonic() > deadline:
                    chan.close()
                    raise SshError(f"command timed out after {timeout:.0f}s: {command}")
                if not got:
                    time.sleep(0.2)
            emit(final=True)
            return chan.recv_exit_status(), "\n".join(out)
        finally:
            client.close()

    def put_file(self, ip: str, path: str, content: str) -> None:
        rc, out = self.run(ip, f"cat > {shlex.quote(path)}", stdin=content)
        if rc != 0:
            raise SshError(f"writing {path} failed: {out}")

    def domains(self, ip: str) -> list[dict]:
        rc, out = self.run(ip, "sudo -n virsh list --all", timeout=30)
        if rc != 0:
            raise SshError(f"virsh list failed: {out}")
        return parse_domains(out)

    def virsh(self, ip: str, action: str, domain: str) -> None:
        rc, out = self.run(ip, f"sudo -n virsh {action} {shlex.quote(domain)}", timeout=60)
        if rc != 0:
            raise SshError(f"virsh {action} {domain} failed: {out}")
