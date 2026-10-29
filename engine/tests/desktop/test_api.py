"""The local API: authentication, host check, private endpoints, and the egress proxy."""
from __future__ import annotations

import socket
import threading

import pytest
from fastapi.testclient import TestClient

from livery.desktop.api import create_app
from livery.desktop.netbridge import EgressProxy, host_allowed

TOKEN = "t" * 48


@pytest.fixture()
def client(project):
    return TestClient(create_app(TOKEN))


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_token_is_required(client):
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/health", headers=auth()).json() == {"ok": True}


def test_dns_rebinding_host_is_refused(client):
    assert client.get("/api/health", headers={**auth(), "Host": "evil.example:1234"}).status_code == 421


def test_documents_previews_and_selection(client, project):
    docs = client.get(f"/api/projects/{project.id}/documents", headers=auth()).json()
    assert {d["id"] for d in docs} >= set(project.ids.values())
    doc = project.ids["report"]
    r = client.get(f"/api/projects/{project.id}/documents/{doc}/pdf", headers=auth())
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    smap = client.get(f"/api/projects/{project.id}/documents/{doc}/selection", headers=auth()).json()
    assert any(e["kind"] == "table" for e in smap["elements"])
    assert client.get(f"/api/projects/{project.id}/documents/{doc}/pdf?kind=original", headers=auth()).status_code in (200, 404)


def test_unmapped_or_unsupported_elements_cannot_be_attached(client, project):
    doc = project.ids["report"]
    r = client.post(f"/api/projects/{project.id}/chat/fake/messages", headers=auth(),
                    json={"text": "style this list", "selections": [{"doc": doc, "element": "l0012"}]})
    assert r.status_code == 409 and "not a selectable element" in r.json()["detail"]
    r = client.post(f"/api/projects/{project.id}/chat/fake/messages", headers=auth(),
                    json={"text": "style", "selections": [{"doc": doc, "element": "t9999"}]})
    assert r.status_code == 409


def test_unknown_project_is_404(client):
    assert client.get("/api/projects/pdeadbeef00", headers=auth()).status_code == 404
    assert client.get("/api/projects/..%2F..%2Fetc", headers=auth()).status_code == 404


def test_proxy_allows_only_provider_hosts():
    allow = ("api.anthropic.com", "*.claude.ai")
    assert host_allowed("api.anthropic.com", allow)
    assert host_allowed("x.claude.ai", allow)
    for bad in ("evil.com", "127.0.0.1", "localhost", "[::1]", "api.anthropic.com.evil.com", "10.0.0.1"):
        assert not host_allowed(bad, allow), bad


def test_proxy_tunnels_allowed_and_refuses_others(tmp_path):
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen(1)
    up_port = upstream.getsockname()[1]

    def echo():
        c, _ = upstream.accept()
        c.sendall(c.recv(100).upper())
        c.close()
    threading.Thread(target=echo, daemon=True).start()
    seen = []
    proxy = EgressProxy(tmp_path / "p.sock", ("api.example.com",), log=lambda h, p, ok: seen.append((h, ok)),
                        connect=lambda h, p: socket.create_connection(("127.0.0.1", up_port)))
    proxy.start()
    try:
        def ask(target):
            s = socket.socket(socket.AF_UNIX)
            s.connect(str(tmp_path / "p.sock"))
            s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            return s, s.recv(200)
        s, head = ask("api.example.com:443")
        assert head.startswith(b"HTTP/1.1 200")
        s.sendall(b"hello")
        assert s.recv(100) == b"HELLO"
        s.close()
        for target in ("evil.example.com:443", "api.example.com:80", "127.0.0.1:443"):
            s, head = ask(target)
            assert head.startswith(b"HTTP/1.1 403"), target
            s.close()
        assert ("evil.example.com", False) in seen
    finally:
        proxy.stop()
        upstream.close()


def test_delete_project_removes_its_folder(client):
    pid = client.post("/api/projects", headers=auth(), json={"name": "Throwaway"}).json()["id"]
    assert any(p["id"] == pid for p in client.get("/api/projects", headers=auth()).json())
    assert client.delete(f"/api/projects/{pid}", headers=auth()).json() == {"ok": True}
    assert not any(p["id"] == pid for p in client.get("/api/projects", headers=auth()).json())
    assert client.get(f"/api/projects/{pid}", headers=auth()).status_code == 404
    assert client.delete(f"/api/projects/{pid}", headers=auth()).status_code == 404


def test_mark_done_and_stats(client, project):
    base = f"/api/projects/{project.id}"
    doc = project.ids["plain"]
    r = client.post(f"{base}/documents/{doc}/done", headers=auth(), json={"done": True})
    assert r.status_code == 200 and r.json()["state"] in ("done", "recheck", "checking")
    try:
        row = next(d for d in client.get(f"{base}/documents", headers=auth()).json() if d["id"] == doc)
        assert row["review"]["approved"]["version"] == row["current"]
        stats = client.get(f"{base}/stats", headers=auth()).json()
        assert stats["documents"] == len(project.documents()) and sum(stats["review"].values()) == stats["documents"]
    finally:
        assert client.post(f"{base}/documents/{doc}/done", headers=auth(), json={"done": False}).json() == {"state": "open"}
    assert client.post(f"{base}/documents/dnothere/done", headers=auth(), json={"done": True}).status_code == 404
