import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.harvester import RESTORE_ANNOTATION, Harvester
from app.ssh_host import parse_domains

VMS = {
    "items": [
        {"metadata": {"namespace": "default", "name": "web"}, "spec": {"runStrategy": "RerunOnFailure"}},
        {"metadata": {"namespace": "default", "name": "off"}, "spec": {"runStrategy": "Halted"}},
        {"metadata": {"namespace": "default", "name": "back", "annotations": {RESTORE_ANNOTATION: "Always"}},
         "spec": {"runStrategy": "Halted"}},
    ]
}


@pytest.fixture
def server(tmp_path):
    patches = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(VMS)

        def do_PATCH(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            patches.append((self.path, self.headers["Content-Type"], body))
            self._send({})

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(f"""
apiVersion: v1
kind: Config
clusters: [{{name: t, cluster: {{server: "http://127.0.0.1:{httpd.server_port}"}}}}]
users: [{{name: t, user: {{token: x}}}}]
contexts: [{{name: t, context: {{cluster: t, user: t}}}}]
current-context: t
""")
    yield Harvester(str(kubeconfig)), patches
    httpd.shutdown()


def test_halt_uses_merge_patch_and_remembers_strategy(server):
    harvester, patches = server
    halted, skipped = harvester.halt_running_vms()
    assert halted == ["default/web"] and skipped == []
    path, ctype, body = patches[0]
    assert path == "/apis/kubevirt.io/v1/namespaces/default/virtualmachines/web"
    assert ctype == "application/merge-patch+json"
    assert body == {"metadata": {"annotations": {RESTORE_ANNOTATION: "RerunOnFailure"}}, "spec": {"runStrategy": "Halted"}}


def test_restore_puts_strategy_back_and_clears_annotation(server):
    harvester, patches = server
    assert harvester.restore_vms() == ["default/back -> Always"]
    path, ctype, body = patches[0]
    assert path.endswith("/virtualmachines/back")
    assert ctype == "application/merge-patch+json"
    assert body == {"metadata": {"annotations": {RESTORE_ANNOTATION: None}}, "spec": {"runStrategy": "Always"}}


def test_parse_virsh_list():
    out = """ Id   Name               State
-----------------------------------
 1    harvester-node-1   running
 -    harvester-node-2   shut off
"""
    assert parse_domains(out) == [
        {"name": "harvester-node-1", "state": "running"},
        {"name": "harvester-node-2", "state": "shut off"},
    ]
