"""Тесты yadisk.py на локальной заглушке REST API Яндекс Диска.

Запуск: python3 tests/test_yadisk.py
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yadisk  # noqa: E402

FILES = {}       # path -> bytes
DIRS = set()     # пути папок
FAIL_ONCE = set()  # пути, первая выдача которых отвечает 429
CALLS = {"429": 0}


def reset():
    FILES.clear(); DIRS.clear(); FAIL_ONCE.clear(); CALLS["429"] = 0
    DIRS.update({"disk:/Архив", "disk:/Архив/LM-1", "disk:/Архив/LM-1/part2",
                 "disk:/Архив/LM-2"})
    FILES["disk:/Архив/LM-1/a.jpg"] = b"A" * 10
    FILES["disk:/Архив/LM-1/b.JPEG"] = b"B" * 20
    FILES["disk:/Архив/LM-1/notes.txt"] = b"x"
    FILES["disk:/Архив/LM-1/part2/c.jpg"] = b"C" * 30


def meta(path):
    name = path.rsplit("/", 1)[-1]
    if path in DIRS:
        return {"path": path, "name": name, "type": "dir", "modified": path}
    b = FILES[path]
    return {"path": path, "name": name, "type": "file", "size": len(b),
            "md5": hashlib.md5(b).hexdigest(), "preview": f"{BASE}/preview?p={urllib.parse.quote(path)}"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None, raw=None):
        body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route(self, method):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        p = q.get("path") or q.get("p", "")
        if u.path == "/v1/disk/resources" and method == "GET":
            if p in DIRS:
                kids = sorted(x for x in list(DIRS) + list(FILES)
                              if x.rsplit("/", 1)[0] == p and x != p)
                off, lim = int(q.get("offset", 0)), int(q.get("limit", 20))
                m = meta(p)
                m["_embedded"] = {"items": [meta(k) for k in kids[off:off + lim]], "total": len(kids)}
                return self.send(200, m)
            if p in FILES:
                return self.send(200, meta(p))
            return self.send(404, {"message": "not found"})
        if u.path == "/v1/disk/resources" and method == "PUT":
            if p in DIRS:
                return self.send(409, {"message": "exists"})
            DIRS.add(p); return self.send(201, {})
        if u.path == "/v1/disk/resources/download":
            return self.send(200, {"href": f"{BASE}/dl?p={urllib.parse.quote(p)}"})
        if u.path == "/dl":
            if p in FAIL_ONCE:
                FAIL_ONCE.discard(p); CALLS["429"] += 1
                return self.send(429, {"message": "too many"})
            return self.send(200, raw=FILES[p])
        if u.path == "/v1/disk/resources/upload":
            return self.send(200, {"href": f"{BASE}/up?p={urllib.parse.quote(p)}"})
        if u.path == "/up" and method == "PUT":
            n = int(self.headers.get("Content-Length", 0))
            FILES[p] = self.rfile.read(n); return self.send(201, {})
        if u.path == "/v1/disk/resources/copy" and method == "POST":
            if q["path"] in FILES and q.get("overwrite") != "true":
                return self.send(409, {"message": "exists"})
            FILES[q["path"]] = FILES[q["from"]]; return self.send(201, {})
        return self.send(400, {"message": "bad route " + u.path})

    def do_GET(self): self.route("GET")
    def do_PUT(self): self.route("PUT")
    def do_POST(self): self.route("POST")


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()
C = yadisk.YaDiskClient("t", base=BASE + "/v1/disk", backoff=0, sleep=lambda s: None)

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


reset()
shoots = C.list_shoots("disk:/Архив")
check("витрина: две съёмки, только папки", sorted(s.name for s in shoots) == ["LM-1", "LM-2"])

C2 = yadisk.YaDiskClient("t", base=BASE + "/v1/disk", backoff=0, sleep=lambda s: None)
small = C2.list_dir("disk:/Архив/LM-1", page=1)
check("пагинация: все элементы при page=1", len(small) == 4)

photos = C.walk_photos("disk:/Архив/LM-1")
check("обход: только JPEG, включая подпапку части", [p.name for p in photos] == ["a.jpg", "b.JPEG", "c.jpg"])

with tempfile.TemporaryDirectory() as d:
    FAIL_ONCE.add("disk:/Архив/LM-1/part2/c.jpg")
    jobs = yadisk.plan_downloads(photos, "disk:/Архив/LM-1", d)
    rep = C.download_many(jobs, workers=2)
    check("скачивание: 3 файла, повтор после 429", len(rep.done) == 3 and CALLS["429"] == 1 and not rep.failed)
    check("структура подпапок сохранена", os.path.exists(os.path.join(d, "part2", "c.jpg")))
    check("нет хвостов .part", not any(f.endswith(".part") for _, _, fs in os.walk(d) for f in fs))
    FILES["disk:/Архив/LM-1/part2/d.jpg"] = b"D" * 5
    rep2 = C.download_many(yadisk.plan_downloads(C.walk_photos("disk:/Архив/LM-1"), "disk:/Архив/LM-1", d))
    check("докачка части: скачан только новый файл", len(rep2.done) == 1 and len(rep2.skipped) == 3)

r = yadisk.send_to_retouch(C, "disk:/Архив/LM-1", ["disk:/Архив/LM-1/a.jpg", "disk:/Архив/LM-1/b.JPEG"], "партия 1")
check("ретушь: копии в Ретушь/партия 1, оригиналы на месте",
      len(r["copied"]) == 2 and "disk:/Архив/LM-1/Ретушь/партия 1/a.jpg" in FILES and "disk:/Архив/LM-1/a.jpg" in FILES)
r2 = yadisk.send_to_retouch(C, "disk:/Архив/LM-1", ["disk:/Архив/LM-1/a.jpg"], "партия 1")
check("ретушь: повторная отправка не перезаписывает", r2["existed"] == ["disk:/Архив/LM-1/a.jpg"] and not r2["copied"])
check("служебная папка Ретушь не попадает в галерею съёмки",
      all("/Ретушь/" not in p.path for p in C.walk_photos("disk:/Архив/LM-1")))

data, rev = C.read_state("disk:/Архив/LM-1")
check("общий файл: нет файла -> пустая ревизия", data is None and rev == "")
ev = yadisk.make_event("to_retouch", "Маша", ["a.jpg"])
rev1 = C.write_state("disk:/Архив/LM-1", {"events": [ev]}, rev)
data, rev_read = C.read_state("disk:/Архив/LM-1")
check("общий файл: записан и прочитан", data["events"][0]["action"] == "to_retouch" and rev_read == rev1)
C.write_state("disk:/Архив/LM-1", {"events": [ev, ev]}, rev_read)  # другой пользователь
try:
    C.write_state("disk:/Архив/LM-1", {"events": []}, rev_read)
    check("конфликт: устаревшая ревизия отклонена", False)
except yadisk.StateConflict as e:
    check("конфликт: устаревшая ревизия отклонена, отдан свежий отбор", len(e.remote_data["events"]) == 2)

srv.shutdown()
failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} PASS")
sys.exit(1 if failed else 0)
