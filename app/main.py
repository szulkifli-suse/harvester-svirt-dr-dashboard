import hashlib
import hmac
import secrets
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .ops import Busy, JobRunner, Operations

STATIC = Path(__file__).parent / "static"
COOKIE = "hd_session"
SESSION_SECONDS = 12 * 3600
CSRF_HEADER = "x-requested-with"


class Login(BaseModel):
    password: str


class ActionBody(BaseModel):
    force: bool = False


class Sessions:
    """Stateless signed session tokens; the key is per process, so a restart logs everyone out."""

    def __init__(self):
        self._key = secrets.token_bytes(32)

    def _sign(self, expires: int) -> str:
        return hmac.new(self._key, str(expires).encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expires = int(time.time()) + SESSION_SECONDS
        return f"{expires}.{self._sign(expires)}"

    def valid(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        expires, sig = token.split(".", 1)
        if not expires.isdigit() or int(expires) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(int(expires)))


def create_app(settings=None, ops=None, collector=None) -> FastAPI:
    if ops is None or collector is None:
        from .aws_host import Ec2Host
        from .harvester import Harvester
        from .ssh_host import SshHost
        from .status import StatusCollector

        settings = settings or config.load()
        host = Ec2Host(settings.region, settings.instance_id, settings.tags)
        ssh = SshHost(settings.ssh_user, settings.ssh_key_path)
        harvester = Harvester(settings.kubeconfig)
        ops = ops or Operations(host, ssh, harvester, settings.recover_script_path)
        collector = collector or StatusCollector(host, ssh, harvester)

    password = settings.password
    sessions = Sessions()
    runner = JobRunner()
    login_lock = threading.Lock()
    app = FastAPI(title="Harvester Dashboard", docs_url=None, redoc_url=None, openapi_url=None)

    def authed(request: Request) -> None:
        if not sessions.valid(request.cookies.get(COOKIE)):
            raise HTTPException(401, "login required")

    def mutating(request: Request) -> None:
        # Browsers cannot add custom headers cross-site without a CORS preflight, which this app never allows.
        if request.headers.get(CSRF_HEADER) != "dashboard":
            raise HTTPException(403, "missing X-Requested-With header")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/api/login", dependencies=[Depends(mutating)])
    def login(body: Login, request: Request, response: Response):
        with login_lock:
            if not hmac.compare_digest(body.password.encode(), password.encode()):
                time.sleep(1)
                raise HTTPException(401, "wrong password")
        secure = request.url.scheme == "https"
        response.set_cookie(COOKIE, sessions.issue(), max_age=SESSION_SECONDS, httponly=True, samesite="strict", secure=secure)
        return {"ok": True}

    @app.post("/api/logout", dependencies=[Depends(mutating)])
    def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/status", dependencies=[Depends(authed)])
    def status():
        return collector.snapshot()

    @app.get("/api/jobs", dependencies=[Depends(authed)])
    def jobs():
        return [j.to_dict(since=len(j.lines)) for j in reversed(runner.jobs)]

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(authed)])
    def job(job_id: str, since: int = 0):
        j = runner.get(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        return j.to_dict(since=since)

    @app.post("/api/actions/{action}", dependencies=[Depends(authed), Depends(mutating)])
    def act(action: str, body: ActionBody | None = None):
        force = bool(body and body.force)
        fns = {
            "start": ops.start,
            "stop": lambda job: ops.stop(job, force=force),
            "recover": ops.recover,
        }
        if action not in fns:
            raise HTTPException(404, "unknown action")

        def run(job):
            try:
                fns[action](job)
            finally:
                collector.invalidate()

        try:
            j = runner.submit(action, run)
        except Busy as e:
            raise HTTPException(409, str(e)) from None
        return j.to_dict()

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    return app
