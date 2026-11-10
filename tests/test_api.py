import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PASSWORD = "correct-horse-battery"
CSRF = {"X-Requested-With": "dashboard"}


class FakeOps:
    def __init__(self):
        self.ran = []

    def start(self, job):
        self.ran.append("start")
        time.sleep(0.2)

    def stop(self, job, force=False):
        self.ran.append(f"stop force={force}")

    def recover(self, job):
        self.ran.append("recover")


class FakeCollector:
    def snapshot(self):
        return {"summary": {"level": "ok", "text": "Healthy"}}

    def invalidate(self):
        pass


def client():
    settings = Settings("ap-southeast-1", None, {}, "u", "k", "kc", "s", PASSWORD)
    ops = FakeOps()
    return TestClient(create_app(settings, ops=ops, collector=FakeCollector())), ops


def login(c):
    r = c.post("/api/login", json={"password": PASSWORD}, headers=CSRF)
    assert r.status_code == 200


def test_status_requires_login():
    c, _ = client()
    assert c.get("/api/status").status_code == 401
    assert c.get("/healthz").status_code == 200


def test_wrong_password_rejected():
    c, _ = client()
    assert c.post("/api/login", json={"password": "nope"}, headers=CSRF).status_code == 401


def test_login_cookie_is_httponly_strict():
    c, _ = client()
    r = c.post("/api/login", json={"password": PASSWORD}, headers=CSRF)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_actions_need_csrf_header():
    c, ops = client()
    login(c)
    assert c.post("/api/actions/stop", json={}).status_code == 403
    assert ops.ran == []


def test_action_runs_and_rejects_concurrent():
    c, ops = client()
    login(c)
    r = c.post("/api/actions/start", json={}, headers=CSRF)
    assert r.status_code == 200
    assert c.post("/api/actions/stop", json={}, headers=CSRF).status_code == 409
    job_id = r.json()["id"]
    for _ in range(50):
        if c.get(f"/api/jobs/{job_id}").json()["status"] != "running":
            break
        time.sleep(0.05)
    assert c.get(f"/api/jobs/{job_id}").json()["status"] == "succeeded"
    assert c.post("/api/actions/stop", json={"force": True}, headers=CSRF).status_code == 200
    time.sleep(0.1)
    assert ops.ran == ["start", "stop force=True"]


def test_unknown_action():
    c, _ = client()
    login(c)
    assert c.post("/api/actions/reboot", json={}, headers=CSRF).status_code == 404


def test_forged_session_rejected():
    c, _ = client()
    c.cookies.set("hd_session", f"{int(time.time()) + 999}.deadbeef")
    assert c.get("/api/status").status_code == 401
