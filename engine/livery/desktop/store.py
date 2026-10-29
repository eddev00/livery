"""Projects on disk.

    <projects>/<project id>/
        project.json                  name, documents, revisions, accepted revision and its history
        sources/<doc>/v<n>/source.docx   immutable copy of the imported Word file (read-only)
        imports/<doc>/v<n>/           the conversion of that version: content.typ, element inventory,
                                      media, original.pdf (LibreOffice) and the lorem twin
        imports/<doc>/v<n>/mapped/<key>/   the same version converted with another mapping
        design/<revision>/            theme.toml, design.json, custom.typ, mapping.toml of one design revision
        builds/<doc>/v<n>/<revision>/ template/ + doc/: the document rendered with one revision
        proposals/<proposal>/         meta.json, design/, synthetic/ and real/ previews
        chats/<provider>.jsonl        sanitized chat records
        exports/<stamp>/              exported PDFs and the batch report

Identifiers are opaque: projects "p…", documents "d…", revisions "r0001"…, proposals "q…".
Every JSON file is replaced atomically; a project's state is guarded by one lock.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .design import Design, neutral_design
from .paths import projects_dir

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(5)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    pass


class Project:
    def __init__(self, root: Path):
        self.root = root
        self.id = root.name
        with _locks_guard:
            self.lock = _locks.setdefault(str(root), threading.RLock())

    # -------------------------------------------------------------- state
    @property
    def file(self) -> Path:
        return self.root / "project.json"

    def state(self) -> dict[str, Any]:
        st = read_json(self.file)
        if st is None:
            raise NotFound(self.id)
        return st

    def save(self, st: dict[str, Any]) -> None:
        st["updated"] = now()
        write_json(self.file, st)

    # -------------------------------------------------------------- creation
    @classmethod
    def create(cls, name: str, base: Path | None = None) -> "Project":
        root = (base or projects_dir()) / new_id("p")
        root.mkdir(parents=True)
        p = cls(root)
        rev = "r0001"
        neutral_design().write(p.design_dir(rev))
        p.save({"id": p.id, "name": name.strip() or "Untitled project", "created": now(),
                "documents": {}, "order": [],
                "revisions": [{"id": rev, "parent": None, "created": now(), "kind": "initial",
                               "note": "Neutral design"}],
                "accepted": rev, "history": [rev], "next_revision": 2})
        return p

    @classmethod
    def open(cls, project_id: str, base: Path | None = None) -> "Project":
        if not project_id.startswith("p") or not project_id[1:].isalnum():
            raise NotFound(project_id)
        root = (base or projects_dir()) / project_id
        if not (root / "project.json").exists():
            raise NotFound(project_id)
        return cls(root)

    @classmethod
    def all(cls, base: Path | None = None) -> Iterator["Project"]:
        d = base or projects_dir()
        if not d.is_dir():
            return
        for child in sorted(d.iterdir()):
            if (child / "project.json").exists():
                yield cls(child)

    def delete(self) -> None:
        """Remove the project folder and everything in it."""
        shutil.rmtree(self.root)

    # -------------------------------------------------------------- paths
    def design_dir(self, rev: str) -> Path:
        return self.root / "design" / rev

    def source_dir(self, doc: str, version: int) -> Path:
        return self.root / "sources" / doc / f"v{version}"

    def import_dir(self, doc: str, version: int) -> Path:
        return self.root / "imports" / doc / f"v{version}"

    def build_dir(self, doc: str, version: int, rev: str) -> Path:
        return self.root / "builds" / doc / f"v{version}" / rev

    def proposal_dir(self, pid: str) -> Path:
        return self.root / "proposals" / pid

    # -------------------------------------------------------------- documents
    def add_source(self, src: Path, *, replace: str | None = None) -> tuple[str, int]:
        """Copy a Word file in as a new document, or as a new version of `replace`.
        The copy is made read-only: source documents are never modified."""
        src = Path(src)
        if src.suffix.lower() != ".docx":
            raise ValueError(f"{src.name}: only .docx files can be imported")
        if not src.is_file():
            raise FileNotFoundError(str(src))
        with self.lock:
            st = self.state()
            if replace is None:
                doc = new_id("d")
                version = 1
                st["documents"][doc] = {"id": doc, "name": src.name, "versions": [], "current": 1}
                st["order"].append(doc)
            else:
                if replace not in st["documents"]:
                    raise NotFound(replace)
                doc = replace
                version = max(v["v"] for v in st["documents"][doc]["versions"]) + 1 \
                    if st["documents"][doc]["versions"] else 1
                st["documents"][doc]["current"] = version
                st["documents"][doc]["name"] = src.name
            dest = self.source_dir(doc, version) / "source.docx"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            os.chmod(dest, stat.S_IRUSR | stat.S_IRGRP)
            st["documents"][doc]["versions"].append({"v": version, "sha256": sha256_of(dest),
                                                     "imported_at": now(), "file_name": src.name,
                                                     "status": "pending"})
            self.save(st)
        return doc, version

    def document(self, doc: str) -> dict[str, Any]:
        st = self.state()
        if doc not in st["documents"]:
            raise NotFound(doc)
        return st["documents"][doc]

    def set_version_status(self, doc: str, version: int, **fields: Any) -> None:
        with self.lock:
            st = self.state()
            for v in st["documents"][doc]["versions"]:
                if v["v"] == version:
                    v.update(fields)
            self.save(st)

    def documents(self) -> list[dict[str, Any]]:
        st = self.state()
        return [st["documents"][d] for d in st["order"] if d in st["documents"]]

    def remove_document(self, doc: str) -> None:
        with self.lock:
            st = self.state()
            if doc not in st["documents"]:
                raise NotFound(doc)
            del st["documents"][doc]
            st["order"] = [d for d in st["order"] if d != doc]
            self.save(st)

    # -------------------------------------------------------------- design revisions
    def accepted(self) -> str:
        return self.state()["accepted"]

    def design(self, rev: str) -> Design:
        d = self.design_dir(rev)
        if not d.is_dir():
            raise NotFound(rev)
        return Design.load(d)

    def add_revision(self, design: Design, *, parent: str, kind: str, note: str,
                     expect_accepted: str | None = None, accept: bool = True,
                     reverts: str | None = None) -> str:
        """Store a new revision; with `accept`, make it the accepted one atomically. When
        `expect_accepted` is given and the accepted revision has moved on, raise Conflict.
        `reverts` names the accepted change this revision removes."""
        with self.lock:
            st = self.state()
            if expect_accepted is not None and st["accepted"] != expect_accepted:
                raise Conflict(f"the accepted design changed ({expect_accepted} → {st['accepted']}); "
                               "this proposal is stale")
            rev = f"r{st['next_revision']:04d}"
            st["next_revision"] += 1
            tmp = self.root / "design" / f".{rev}.tmp"
            if tmp.exists():
                shutil.rmtree(tmp)
            design.write(tmp)
            os.replace(tmp, self.design_dir(rev))
            record = {"id": rev, "parent": parent, "created": now(), "kind": kind, "note": note}
            if reverts:
                record["reverts"] = reverts
            st["revisions"].append(record)
            if accept:
                st["accepted"] = rev
                st["history"].append(rev)
            self.save(st)
            return rev

    def undo(self) -> str:
        """Go back to the previously accepted revision. Sources and revisions are untouched."""
        with self.lock:
            st = self.state()
            if len(st["history"]) < 2:
                raise Conflict("nothing to undo")
            st["history"].pop()
            st["accepted"] = st["history"][-1]
            self.save(st)
            return st["accepted"]

    def changes(self) -> list[dict[str, Any]]:
        """The accepted changes, oldest first: every revision in the history after the first,
        with `reverted_by` set when a later accepted change removed it."""
        st = self.state()
        records = {r["id"]: r for r in st["revisions"]}
        hist = [records[r] for r in st["history"][1:]]
        removed = {r["reverts"]: r["id"] for r in hist if r.get("reverts")}
        return [{**r, "reverted_by": removed.get(r["id"])} for r in hist]


def _health(p: Project, st: dict[str, Any]) -> dict[str, int]:
    """Document counts for the project list: ready (PASS/WARN with the accepted design),
    failing (import failed or FAIL), working (importing or not rendered yet)."""
    out = {"ready": 0, "failing": 0, "working": 0}
    for doc in st["order"]:
        meta = st["documents"].get(doc)
        if not meta:
            continue
        ver = next((v for v in meta["versions"] if v["v"] == meta["current"]), {})
        if ver.get("status") == "failed":
            out["failing"] += 1
            continue
        build = read_json(p.build_dir(doc, meta["current"], st["accepted"]) / "status.json") or {}
        if ver.get("status") != "imported" or build.get("state") != "done":
            out["failing" if build.get("state") == "failed" else "working"] += 1
        elif build.get("report_status") == "FAIL":
            out["failing"] += 1
        else:
            out["ready"] += 1
    return out


def list_projects(base: Path | None = None) -> list[dict[str, Any]]:
    out = []
    for p in Project.all(base):
        st = p.state()
        out.append({"id": st["id"], "name": st["name"], "created": st["created"], "updated": st.get("updated"),
                    "documents": len(st["order"]), "accepted": st["accepted"], "health": _health(p, st)})
    return sorted(out, key=lambda x: x.get("updated") or "", reverse=True)
