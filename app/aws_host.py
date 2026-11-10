import datetime as dt

import boto3
from botocore.config import Config

LIVE_STATES = ["pending", "running", "stopping", "stopped"]


class HostError(RuntimeError):
    pass


def _summarize(i: dict) -> dict:
    tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
    return {
        "id": i["InstanceId"],
        "name": tags.get("Name", ""),
        "state": i["State"]["Name"],
        "type": i["InstanceType"],
        "az": i.get("Placement", {}).get("AvailabilityZone"),
        "public_ip": i.get("PublicIpAddress"),
        "private_ip": i.get("PrivateIpAddress"),
        "launch_time": i["LaunchTime"].isoformat() if i.get("LaunchTime") else None,
    }


class Ec2Host:
    def __init__(self, region: str, instance_id: str | None, tags: dict[str, str]):
        cfg = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=15)
        self.ec2 = boto3.client("ec2", region_name=region, config=cfg)
        self.cw = boto3.client("cloudwatch", region_name=region, config=cfg)
        self.instance_id = instance_id
        self.tags = tags

    def instance(self) -> dict:
        if self.instance_id:
            r = self.ec2.describe_instances(InstanceIds=[self.instance_id])
        else:
            filters = [{"Name": f"tag:{k}", "Values": [v]} for k, v in self.tags.items()]
            filters.append({"Name": "instance-state-name", "Values": LIVE_STATES})
            r = self.ec2.describe_instances(Filters=filters)
        found = [i for res in r["Reservations"] for i in res["Instances"]]
        if not found:
            raise HostError(f"no EC2 instance matches {self.instance_id or self.tags}")
        if len(found) > 1:
            ids = ", ".join(i["InstanceId"] for i in found)
            raise HostError(f"several EC2 instances match the tags ({ids}); set EC2_INSTANCE_ID")
        return _summarize(found[0])

    def start(self, instance_id: str) -> None:
        self.ec2.start_instances(InstanceIds=[instance_id])

    def stop(self, instance_id: str) -> None:
        self.ec2.stop_instances(InstanceIds=[instance_id])

    def cpu_history(self, instance_id: str, minutes: int = 180) -> list[dict]:
        end = dt.datetime.now(dt.timezone.utc)
        r = self.cw.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=end - dt.timedelta(minutes=minutes),
            EndTime=end,
            Period=300,
            Statistics=["Average"],
        )
        points = sorted(r["Datapoints"], key=lambda p: p["Timestamp"])
        return [{"t": p["Timestamp"].isoformat(), "v": round(p["Average"], 1)} for p in points]
