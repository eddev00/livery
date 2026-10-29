from __future__ import annotations

import os
from pathlib import Path

import pytest

from . import docs


ENV = {"LIVERY_IGNORE_SYSTEM_FONTS": "1", "LIVERY_ENABLE_FAKE_AGENT": "1", "LIVERY_TESTING": "1"}


@pytest.fixture(scope="package")
def desktop_home(tmp_path_factory):
    """Desktop settings for this package only: the engine's own tests run without them."""
    from livery import render
    from livery.desktop import builder, conversation

    home = tmp_path_factory.mktemp("we-home")
    saved = {k: os.environ.get(k) for k in [*ENV, "LIVERY_HOME"]}
    os.environ.update(ENV, LIVERY_HOME=str(home))
    wrapper = render.COMMAND_WRAPPER
    builder.install_render_sandbox()
    yield home
    conversation.close_all()
    render.COMMAND_WRAPPER = wrapper
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="package")
def doc_files(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("docs")
    return {"report": docs.report_doc(d / "Quarterly report.docx"),
            "small": docs.small_doc(d / "Supplier summary.docx"),
            "plain": docs.small_doc(d / "No groups.docx", title="Plain tables", grouped=False),
            "broken": docs.broken_doc(d / "Broken.docx")}


@pytest.fixture(scope="package")
def project(desktop_home, doc_files):
    """One project with the three good documents imported and rendered with r0001."""
    from livery.desktop import builder
    from livery.desktop.store import Project

    p = Project.create("Pilot")
    ids = {}
    for key in ("report", "small", "plain"):
        doc, version = p.add_source(doc_files[key])
        report = builder.import_document(p, doc, version)
        assert report["status"] in ("PASS", "WARN"), (key, [c for c in report["checks"] if c["status"] == "FAIL"])
        st = builder.render_document(p, doc, version, "r0001")
        assert st["state"] == "done" and st["report_status"] in ("PASS", "WARN"), st
        ids[key] = doc
    p.ids = ids  # type: ignore[attr-defined]
    return p
