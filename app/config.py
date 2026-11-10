import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    region: str
    instance_id: str | None
    tags: dict[str, str]
    ssh_user: str
    ssh_key_path: str
    kubeconfig: str
    recover_script_path: str
    password: str


def _parse_tags(raw: str) -> dict[str, str]:
    return dict(p.strip().split("=", 1) for p in raw.split(",") if "=" in p)


def load() -> Settings:
    env = os.environ
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    if not region:
        raise RuntimeError("AWS_REGION is required")
    password = env.get("DASHBOARD_PASSWORD", "")
    if len(password) < 12:
        raise RuntimeError("DASHBOARD_PASSWORD is required (at least 12 characters)")
    return Settings(
        region=region,
        instance_id=env.get("EC2_INSTANCE_ID") or None,
        tags=_parse_tags(env.get("EC2_TAGS", "Workload=harvester,Managed_by=terraform")),
        ssh_user=env.get("SSH_USER", "opensuse"),
        ssh_key_path=env.get("SSH_KEY_PATH", "/secrets/ssh/id"),
        kubeconfig=env.get("HARVESTER_KUBECONFIG", "/secrets/harvester/kubeconfig"),
        recover_script_path=env.get("RECOVER_SCRIPT", "/config/recover-harvester-vms.sh"),
        password=password,
    )
