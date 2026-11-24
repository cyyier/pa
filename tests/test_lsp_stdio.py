"""End-to-end: speak real LSP JSON-RPC to `pac lsp` over stdio, exactly like the VS Code client does."""
import json
import queue
import subprocess
import sys
import threading

import pytest

SRC = """import np_ops

def main():
    arr = [0.0] * 16
    x = np_ops.sum_array(arr)
    res = []
    for i in range(8):
        res.append(i)
    return 0
"""


class Client:
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, "-m", "pa_lang.cli", "lsp"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.q = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self.reader, daemon=True).start()

    def reader(self):
        out = self.p.stdout
        while True:
            length = None
            while True:
                line = out.readline()
                if not line:
                    return
                if line in (b"\r\n", b"\n"):
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            self.q.put(json.loads(out.read(length)))

    def send(self, msg):
        body = json.dumps(msg).encode()
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.p.stdin.flush()

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params, timeout=20):
        self.next_id += 1
        rid = self.next_id
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return self.wait(lambda m: m.get("id") == rid and "method" not in m, timeout)

    def wait(self, pred, timeout=20):
        stash = []
        try:
            while True:
                m = self.q.get(timeout=timeout)
                if pred(m):
                    return m
                stash.append(m)
        finally:
            for m in stash:
                self.q.put(m)


def test_lsp_roundtrip(tmp_path):
    (tmp_path / "np_ops.paw").write_text("def sum_array(arr: span[f64]) -> f64:\n    return 0.0\n")
    main = tmp_path / "main.pa"
    main.write_text(SRC)
    uri = main.as_uri()
    c = Client()
    try:
        init = c.request("initialize", {"processId": None, "rootUri": tmp_path.as_uri(), "capabilities": {}})
        caps = init["result"]["capabilities"]
        assert caps.get("inlayHintProvider") and caps.get("hoverProvider")
        c.notify("initialized", {})
        c.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "pa", "version": 1, "text": SRC}})

        diag = c.wait(lambda m: m.get("method") == "textDocument/publishDiagnostics")
        assert diag["params"]["uri"] == uri and diag["params"]["diagnostics"] == []

        hints = c.request("textDocument/inlayHint", {
            "textDocument": {"uri": uri},
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 20, "character": 0}}})["result"]
        labels = [h["label"] if isinstance(h["label"], str) else "".join(p["value"] for p in h["label"]) for h in hints]
        assert ": list[float]" in labels and ": list[int]" in labels
        assert any("[🌉 .paw → float]" in l for l in labels)
        assert not any("flat" in l or "Zero-Heap" in l for l in labels)

        hover = c.request("textDocument/hover", {"textDocument": {"uri": uri},
                                                 "position": {"line": 3, "character": 5}})["result"]
        assert "128 B" in hover["contents"]["value"] and "[16 x double]" in hover["contents"]["value"]

        bad = "import requests\n\ndef main():\n    return 0\n"
        c.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": 2},
                                            "contentChanges": [{"text": bad}]})
        diag = c.wait(lambda m: m.get("method") == "textDocument/publishDiagnostics"
                      and m["params"]["diagnostics"])
        assert diag["params"]["diagnostics"][0]["code"] == "PA_E001"

        c.request("shutdown", None)
        c.notify("exit", None)
        assert c.p.wait(timeout=10) == 0
    finally:
        c.p.kill()


def test_detailed_hints_via_initialization_options(tmp_path):
    (tmp_path / "np_ops.paw").write_text("def sum_array(arr: span[f64]) -> f64:\n    return 0.0\n")
    main = tmp_path / "main.pa"
    main.write_text(SRC)
    c = Client()
    try:
        c.request("initialize", {"processId": None, "rootUri": tmp_path.as_uri(), "capabilities": {},
                                 "initializationOptions": {"hintDetail": "detailed"}})
        c.notify("initialized", {})
        c.notify("textDocument/didOpen", {"textDocument": {"uri": main.as_uri(), "languageId": "pa", "version": 1, "text": SRC}})
        hints = c.request("textDocument/inlayHint", {
            "textDocument": {"uri": main.as_uri()},
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 20, "character": 0}}})["result"]
        labels = [h["label"] for h in hints]
        assert ": list[float, 16] (128B flat)" in labels and any("FFI -> float" in l for l in labels)
        c.request("shutdown", None)
        c.notify("exit", None)
    finally:
        c.p.kill()


def test_inlay_hints_off_still_serves_hover(tmp_path):
    (tmp_path / "np_ops.paw").write_text("def sum_array(arr: span[f64]) -> f64:\n    return 0.0\n")
    main = tmp_path / "main.pa"
    main.write_text(SRC)
    uri = main.as_uri()
    c = Client()
    try:
        c.request("initialize", {"processId": None, "rootUri": tmp_path.as_uri(), "capabilities": {},
                                 "initializationOptions": {"hintDetail": "off"}})
        c.notify("initialized", {})
        c.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "pa", "version": 1, "text": SRC}})
        hints = c.request("textDocument/inlayHint", {
            "textDocument": {"uri": uri},
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 20, "character": 0}}})["result"]
        assert hints == []
        hover = c.request("textDocument/hover", {"textDocument": {"uri": uri},
                                                 "position": {"line": 3, "character": 5}})["result"]
        assert "128 B" in hover["contents"]["value"]
        c.request("shutdown", None)
        c.notify("exit", None)
    finally:
        c.p.kill()


def test_source_watcher_detects_edits(tmp_path):
    from pa_lang.lsp_server import SourceWatcher
    (tmp_path / "a.py").write_text("x = 1\n")
    w = SourceWatcher(str(tmp_path))
    assert not w.changed()
    (tmp_path / "a.py").write_text("x = 22\n")
    assert w.changed()
    w2 = SourceWatcher(str(tmp_path))
    (tmp_path / "new.c").write_text("//\n")
    assert w2.changed()


def test_server_exits_when_sources_change_and_can_be_disabled(tmp_path):
    import os
    import time
    watch = tmp_path / "src"
    watch.mkdir()
    (watch / "m.py").write_text("a = 1\n")
    env = dict(os.environ, PA_LSP_WATCH_DIR=str(watch))
    p = subprocess.Popen([sys.executable, "-m", "pa_lang.cli", "lsp"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    q = subprocess.Popen([sys.executable, "-m", "pa_lang.cli", "lsp"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(env, PA_LSP_AUTORELOAD="0"))
    try:
        time.sleep(1.5)
        assert p.poll() is None
        (watch / "m.py").write_text("a = 2\n")
        assert p.wait(timeout=10) == 0
        assert b"restarts a fresh server" in p.stderr.read()
        time.sleep(2.0)
        assert q.poll() is None                      # opt-out keeps running
    finally:
        p.kill()
        q.kill()
