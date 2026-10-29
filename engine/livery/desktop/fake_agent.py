"""Deterministic test agent. Runs inside the agent sandbox like the real agents and writes
Claude Code's stream-json format to stdout. Standard library only.

Behaviour, from the prompt (stdin):
  contains "SLEEP"       waits 60 s (cancellation tests)
  contains "FAIL_AUTH"   fails like an agent that is not signed in
  contains "PROBE"       tries to escape the sandbox; reports what it could reach as JSON
  contains "MAPPING"     writes everything after the word "MAPPING" to design/mapping.toml,
                         runs render-preview and reports its result as JSON
  names references/…     checks each named reference image is a PNG it can read and says so
  otherwise              edits design/design.json: a "Fake table" treatment with the first
                         #RRGGBB colour of the prompt as header fill, assigned to every attached
                         table, and an override with that colour for attached headings; then
                         runs render-preview and reports its result
Every prompt it receives is appended to /work/.fake-log so tests can inspect it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def say(text):
    out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}})
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def probe(prompt):
    report = {}
    for path in re.findall(r"(/[^\s\"']+)", prompt.split("PROBE", 1)[1]):
        try:
            if os.path.isdir(path):
                report[path] = "listed:" + ",".join(sorted(os.listdir(path))[:5])
            else:
                with open(path, "rb") as f:
                    report[path] = "read:" + f.read(40).decode("utf-8", "replace")
        except OSError as e:
            report[path] = f"blocked:{e.__class__.__name__}"
    for path in ("/home", "/run", "/proc/1/environ", "/root", "/etc/shadow"):
        try:
            report[path] = ("listed:" + ",".join(sorted(os.listdir(path))[:5])) if os.path.isdir(path) else \
                "read:" + open(path, "rb").read(40).decode("utf-8", "replace")
        except OSError as e:
            report[path] = f"blocked:{e.__class__.__name__}"
    report["env"] = sorted(os.environ)
    for port in re.findall(r"PORT=(\d+)", prompt):
        s = socket.socket()
        s.settimeout(2)
        try:
            s.connect(("127.0.0.1", int(port)))
            report[f"tcp:{port}"] = "connected"
        except OSError as e:
            report[f"tcp:{port}"] = f"blocked:{e.__class__.__name__}"
        finally:
            s.close()
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        report["internet"] = "connected"
    except OSError as e:
        report["internet"] = f"blocked:{e.__class__.__name__}"
    # Plant a symlink to a private path where the app copies design files from.
    targets = re.findall(r"LINK=(/\S+)", prompt)
    if targets:
        try:
            os.replace("/work/design/custom.typ", "/work/design/custom.typ.bak")
            os.symlink(targets[0], "/work/design/custom.typ")
            report["symlink"] = "planted"
        except OSError as e:
            report["symlink"] = f"blocked:{e.__class__.__name__}"
    try:
        with open("/tmp/../../outside-test", "w") as f:
            f.write("x")
        report["write_root"] = "written-in-sandbox-tmpfs-only" if os.path.exists("/outside-test") else "no"
    except OSError as e:
        report["write_root"] = f"blocked:{e.__class__.__name__}"
    say("PROBE-RESULT " + json.dumps(report))


def edit(prompt):
    colors = re.findall(r"#[0-9A-Fa-f]{6}", prompt)
    color = colors[0] if colors else "#1F3A5F"
    with open("/work/selection.json") as f:
        selection = json.load(f)
    with open("/work/design/design.json") as f:
        design = json.load(f)
    tables = [s["ref"] for s in selection if s["kind"] == "table"]
    headings = [s["ref"] for s in selection if s["kind"] == "heading"]
    if tables:
        design.setdefault("treatments", {})["Fake table"] = {
            "kind": "table", "style": {"header_fill": color, "header_color": "#FFFFFF", "rules": "horizontal"}}
        for ref in tables:
            design.setdefault("assignments", {})[ref] = "Fake table"
    for ref in headings:
        design.setdefault("overrides", {})[ref] = {"color": color}
    with open("/work/design/design.json", "w") as f:
        json.dump(design, f, indent=2)
    out({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/work/design/design.json"}}]}})
    r = subprocess.run(["/work/bin/render-preview"], capture_output=True, text=True, timeout=200)
    out({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "render-preview"}}]}})
    try:
        result = json.loads(r.stdout)
        ok = result["ok"]
    except (json.JSONDecodeError, KeyError):
        ok = False
    refs = []
    for name in re.findall(r"references/ref-\d+\.png", prompt):
        with open(f"/work/{name}", "rb") as f:
            refs.append(f"{name}:{'png' if f.read(8) == b'\x89PNG\r\n\x1a\n' else 'other'}")
    say(f"Styled {len(tables)} table(s) and {len(headings)} heading(s) with {color}; preview ok={ok}."
        + (f" Looked at {', '.join(refs)}." if refs else ""))


def mapping(prompt):
    text = prompt.split("MAPPING", 1)[1].split("\n\nAttached elements", 1)[0].strip() + "\n"
    with open("/work/design/mapping.toml", "w") as f:
        f.write(text)
    out({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/work/design/mapping.toml"}}]}})
    r = subprocess.run(["/work/bin/render-preview"], capture_output=True, text=True, timeout=200)
    say("PREVIEW-RESULT " + r.stdout.strip().replace("\n", " "))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session")
    ap.parse_args()
    prompt = sys.stdin.read()
    with open("/work/.fake-log", "a") as f:
        f.write(prompt + "\n=====\n")
    out({"type": "system", "subtype": "init", "model": "fake"})
    if "SLEEP" in prompt:
        time.sleep(60)
    if "FAIL_AUTH" in prompt:
        out({"type": "result", "subtype": "success", "is_error": True, "result": "Invalid API key · Please run /login"})
        return
    if "PROBE" in prompt:
        probe(prompt)
    elif "MAPPING" in prompt:
        mapping(prompt)
    else:
        edit(prompt)
    out({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})


if __name__ == "__main__":
    main()
