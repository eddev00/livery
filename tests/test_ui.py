"""Local UI server: access control and the document/job API."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wordeasy.ui.server import COOKIE, create_app

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
TOKEN = "t0ken-for-tests"
HOST = "127.0.0.1:5555"


@pytest.fixture
def client(built_features):
    _, out_dir = built_features
    app = create_app(token=TOKEN, out_root=out_dir.parent, hosts={HOST, "localhost:5555"})
    with TestClient(app, base_url=f"http://{HOST}") as c:
        yield c


def _login(c: TestClient) -> None:
    r = c.get(f"/?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 303 and COOKIE in r.cookies


def test_requires_token_cookie(client):
    assert client.get("/").status_code == 403
    assert client.get("/api/docs").status_code == 403
    assert client.get("/?token=wrong").status_code == 403
    _login(client)
    assert client.get("/").status_code == 200
    assert client.get("/api/docs").status_code == 200


def test_rejects_foreign_host_and_cross_site_posts(client):
    _login(client)
    assert client.get("/api/docs", headers={"Host": "evil.example:5555"}).status_code == 421
    assert client.get("/api/docs", headers={"Host": "192.168.1.5:5555"}).status_code == 421
    r = client.post("/api/jobs", json={"files": []}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/jobs", json={"files": []}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.post("/api/jobs", json={"files": []}, headers={"Origin": f"http://{HOST}"})
    assert r.status_code == 400  # passed the checks, rejected for having no files
    assert "default-src 'self'" in client.get("/").headers["content-security-policy"]


def test_lists_docs_and_serves_only_registered_files(client, built_features):
    _login(client)
    _, out_dir = built_features
    from wordeasy.ui.server import Registry
    deep = client.get(f"/api/docs/{Registry.id_for(out_dir)}")  # deep link before any listing
    assert deep.status_code == 200 and deep.json()["name"] == "features"
    docs = client.get("/api/docs").json()
    assert [d["name"] for d in docs] == ["features"] and docs[0]["status"] == "PASS"
    doc_id = docs[0]["id"]
    detail = client.get(f"/api/docs/{doc_id}").json()
    assert detail["report"]["status"] == "PASS" and "report.json" in detail["files"]
    assert client.get(f"/files/{doc_id}/features.pdf").headers["content-type"] == "application/pdf"
    assert client.get(f"/files/{doc_id}/content.typ").status_code == 404  # sources are not served
    assert client.get(f"/files/{doc_id}/../../../etc/passwd").status_code == 404
    assert client.get(f"/files/{doc_id}/%2e%2e/%2e%2e/etc/passwd").status_code == 404
    assert client.get(f"/files/{doc_id}/_preflight/features.docx").status_code == 404  # extension not servable
    assert client.get("/files/000000000000/report.json").status_code == 404
    assert client.post("/api/docs/000000000000/verify").status_code == 404


def test_fs_listing_only_shows_word_documents(client):
    _login(client)
    r = client.get("/api/fs", params={"path": str(FIXTURES)}).json()
    assert r["path"] == str(FIXTURES)
    assert "features.docx" in [f["name"] for f in r["files"]]
    assert all(f["name"].rsplit(".", 1)[-1] in ("docx", "docm", "doc", "odt", "rtf") for f in r["files"])
    assert client.get("/api/fs", params={"path": "/nonexistent/dir"}).status_code == 404


def _wait(client, job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] == "done":
            return snap
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_build_job_and_reverify(client, tmp_path):
    _login(client)
    bad = client.post("/api/jobs", json={"files": [str(FIXTURES / "nope.docx")]})
    assert bad.status_code == 400 and "not found" in bad.json()["error"]
    bad = client.post("/api/jobs", json={"files": [str(FIXTURES / "make_fixtures.py")]})
    assert bad.status_code == 400

    # the theme's cover variants are offered to the setup form
    variants = [v["name"] for v in client.get("/api/state").json()["cover_variants"]]
    assert variants == ["small", "large"]

    r = client.post("/api/jobs", json={"files": [str(FIXTURES / "features.docx")],
                                       "options": {"out_root": str(tmp_path), "department_name": "Finance Ops", "cover": "large",
                                                   "theme": "/ignored.toml"}})
    assert r.status_code == 200, r.text
    job = _wait(client, r.json()["id"])
    task = job["docs"][0]
    assert task["stage"] == "done" and task["status"] == "PASS" and task["error"] is None, task
    assert (tmp_path / "features" / "features.pdf").exists()
    doc_id = task["doc_id"]
    detail = client.get(f"/api/docs/{doc_id}").json()
    st = detail["state"]
    assert st["department_name"] == "Finance Ops" and st["cover"] == "large" and "label" not in st
    main = (tmp_path / "features" / "main.typ").read_text(encoding="utf-8")
    assert 'cover: "large"' in main and 'department_name: "Finance Ops"' in main and "label:" not in main
    # one build yields the styled PDF, the LibreOffice render of the original and the nested lorem twin
    assert detail["original_pdf"] == "original.pdf" and detail["checks"]["original_pdf"] == "PASS"
    twin = detail["lorem"]
    assert twin and twin["anonymized"] and twin["pdf"] == "lorem/lorem.pdf" and twin["original_pdf"] == "lorem/original.pdf"
    assert twin["status"] in ("PASS", "WARN") and detail["checks"]["lorem_twin"] == "PASS"
    assert "Finance" not in twin["title"] and len(twin["title"]) == len("Feature Coverage Document")
    for rel in ("original.pdf", "lorem/lorem.pdf", "lorem/original.pdf"):
        assert client.get(f"/files/{doc_id}/{rel}").headers["content-type"] == "application/pdf", rel
    assert client.get(f"/files/{doc_id}/lorem/lorem.docx").status_code == 404  # the twin's Word file is not served
    from wordeasy.ui.server import Registry
    assert client.get(f"/api/docs/{Registry.id_for(tmp_path / 'features' / 'lorem')}").status_code == 404  # the twin is not a document of its own

    # an unknown variant is refused before anything is rebuilt
    bad = client.post(f"/api/docs/{doc_id}/cover", json={"cover": "nope"})
    assert bad.status_code == 400 and "small, large" in bad.json()["error"]
    r2 = client.post("/api/jobs", json={"files": [str(FIXTURES / "features.docx")],
                                        "options": {"out_root": str(tmp_path), "cover": "nope"}})
    assert _wait(client, r2.json()["id"])["docs"][0]["stage"] == "error"

    # events stream replays the stages and ends
    stream = client.get(f"/api/jobs/{job['id']}/events").text
    assert "event: snapshot" in stream and "converting" in stream and "event: end" in stream

    # cover/metadata edits regenerate main.typ and rebuild
    twin_main_before = (tmp_path / "features" / "lorem" / "main.typ").read_text(encoding="utf-8")
    c = client.post(f"/api/docs/{doc_id}/cover", json={"department_name": "Annual report", "version": "V2", "title": "", "cover": "small",
                                                       "author": "", "date": "", "department": ""})
    assert c.status_code == 200
    job_c = _wait(client, c.json()["id"])
    assert job_c["docs"][0]["status"] == "PASS"
    main = (tmp_path / "features" / "main.typ").read_text(encoding="utf-8")
    assert 'department_name: "Annual report"' in main and 'version: "V2"' in main and 'cover: "small"' in main
    detail = client.get(f"/api/docs/{doc_id}").json()
    st = detail["state"]
    assert st["department_name"] == "Annual report" and st["title"] == "Feature Coverage Document"  # empty title kept
    assert st["cover"] == "small"
    # the twin followed: same variant, lorem-ised copies of the new values, recompiled
    twin_main = (tmp_path / "features" / "lorem" / "main.typ").read_text(encoding="utf-8")
    assert twin_main != twin_main_before and 'cover: "small"' in twin_main and "Annual" not in twin_main
    assert detail["lorem"]["status"] in ("PASS", "WARN") and detail["lorem"]["generated_at"] > twin["generated_at"]
    assert client.post(f"/api/docs/{doc_id}/cover", json=["nope"]).status_code == 400

    # a hand edit of content.typ on disk is caught by rebuild + re-verify
    typ_file = tmp_path / "features" / "content.typ"
    typ = typ_file.read_text(encoding="utf-8")
    assert "Introduction to the System" in typ
    typ_file.write_text(typ.replace("Introduction to the System", "Intro to the System"), encoding="utf-8")
    v = client.post(f"/api/docs/{doc_id}/verify")
    assert v.status_code == 200
    job2 = _wait(client, v.json()["id"])
    assert job2["kind"] == "verify" and job2["docs"][0]["status"] == "FAIL"
    detail = client.get(f"/api/docs/{doc_id}").json()
    text_check = next(c for c in detail["report"]["checks"] if c["name"] == "text_after_conversion")
    assert text_check["status"] == "FAIL" and any("Introduction" in d for d in text_check["details"])


def test_anonymize_endpoint_rebuilds_the_twin(client, tmp_path):
    _login(client)
    r = client.post("/api/jobs", json={"files": [str(FIXTURES / "features.docx")], "options": {"out_root": str(tmp_path)}})
    doc_id = _wait(client, r.json()["id"])["docs"][0]["doc_id"]
    before = client.get(f"/api/docs/{doc_id}").json()["lorem"]
    assert before and before["anonymized"]
    a = client.post(f"/api/docs/{doc_id}/anonymize")
    assert a.status_code == 200 and a.json()["kind"] == "anonymize"
    job = _wait(client, a.json()["id"])
    task = job["docs"][0]
    assert task["error"] is None and task["status"] in ("PASS", "WARN") and task["doc_id"] == doc_id, task
    detail = client.get(f"/api/docs/{doc_id}").json()
    twin = detail["lorem"]
    assert twin["anonymized"] is True and twin["name"] == "lorem" and twin["generated_at"] > before["generated_at"]
    assert "Feature Coverage" not in twin["title"] and len(twin["title"]) == len("Feature Coverage Document")
    assert detail["checks"]["lorem_twin"] == "PASS"
    assert client.get(f"/files/{doc_id}/{twin['pdf']}").status_code == 200
    assert (tmp_path / "features" / "lorem" / "lorem.docx").exists()


def test_delete_removes_output_folder_only(client, tmp_path):
    _login(client)
    r = client.post("/api/jobs", json={"files": [str(FIXTURES / "features.docx")], "options": {"out_root": str(tmp_path)}})
    doc_id = _wait(client, r.json()["id"])["docs"][0]["doc_id"]
    assert (tmp_path / "features" / "features.pdf").exists()
    d = client.delete(f"/api/docs/{doc_id}")
    assert d.status_code == 200 and d.json()["ok"]
    assert not (tmp_path / "features").exists()
    assert (FIXTURES / "features.docx").exists()  # the source is untouched
    assert client.get(f"/api/docs/{doc_id}").status_code == 404
    assert client.delete(f"/api/docs/{doc_id}").status_code == 404
    assert client.delete(f"/api/docs/{doc_id}", headers={"Origin": "http://evil.example"}).status_code == 403


def test_template_editor_reads_validates_and_writes(client, tmp_path, monkeypatch):
    import shutil
    from wordeasy.ui import server as srv
    tdir = tmp_path / "template"
    shutil.copytree(ROOT / "template", tdir)
    monkeypatch.setattr(srv, "TEMPLATE_DIR", tdir)
    _login(client)
    names = [f["name"] for f in client.get("/api/template").json()["files"]]
    assert "theme.toml" in names and "template.typ" in names
    original = client.get("/api/template/theme.toml").text
    assert "[fonts]" in original
    bad = client.put("/api/template/theme.toml", content="[fonts\nbody = ")
    assert bad.status_code == 400 and "TOML" in bad.json()["error"]
    assert (tdir / "theme.toml").read_text() == original  # untouched
    ok = client.put("/api/template/theme.toml", content=original + "\n# edited\n")
    assert ok.status_code == 200 and (tdir / "theme.toml").read_text().endswith("# edited\n")
    assert client.put("/api/template/template.typ", content="// typ\n").status_code == 200
    for name in ("../pyproject.toml", "assets/x.toml", ".hidden.toml", "theme.txt", "nope.toml"):
        assert client.get(f"/api/template/{name}").status_code in (404, 400), name
        assert client.put(f"/api/template/{name}", content="x").status_code in (404, 400, 405), name
    assert client.post("/api/template/rebuild").status_code in (200, 400)


def test_quit_sets_should_exit(client):
    _login(client)

    class Fake:
        should_exit = False

    fake = Fake()
    client.app.state.server = fake
    assert client.post("/api/quit").json() == {"ok": True}
    assert fake.should_exit is True
