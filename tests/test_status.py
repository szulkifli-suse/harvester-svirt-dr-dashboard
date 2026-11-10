from urllib3.connection import HTTPSConnection
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, NewConnectionError

from app.status import friendly_error, summarize


def ok(data):
    return {"ok": True, "data": data}


def host(state):
    return ok({"state": state})


def test_friendly_timeout_names_host_and_port():
    conn = HTTPSConnection("18.136.130.87", 6443)
    msg = friendly_error(ConnectTimeoutError(conn, "timed out"))
    assert msg.startswith("cannot connect to 18.136.130.87:6443 (timed out")


def test_friendly_refused_inside_max_retry():
    conn = HTTPSConnection("10.0.0.1", 6443)
    err = MaxRetryError(None, "/api", NewConnectionError(conn, "refused"))
    assert "connection refused" in friendly_error(err)


def test_summary_levels():
    nodes_ok = ok([{"ready": True}, {"ready": True}])
    doms_ok = ok([{"name": "harvester-node-1", "state": "running"}])
    assert summarize(host("stopped"), None, None, None)["level"] == "down"
    assert summarize(host("pending"), None, None, None)["text"] == "EC2 pending"
    assert summarize(host("running"), ok(True), nodes_ok, doms_ok) == {"level": "ok", "text": "Healthy"}
    assert summarize(host("running"), ok(False), nodes_ok, doms_ok)["text"] == "Harvester not answering"
    down = ok([{"name": "harvester-node-2", "state": "shut off"}])
    assert summarize(host("running"), ok(True), nodes_ok, down)["text"] == "KVM node down: harvester-node-2"
    one_ready = ok([{"ready": True}, {"ready": False}])
    assert summarize(host("running"), ok(True), one_ready, doms_ok)["text"] == "1/2 nodes Ready"
