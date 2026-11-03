"""
yadisk.py — клиент REST API Яндекс Диска для Content Pulse.

Склад (хранилище клиента) остаётся единственным источником правды:
приложение только читает папки, тянет превью, скачивает файлы и хранит
служебный файл отбора `_contentpulse.json` прямо в папке съёмки.

Только стандартная библиотека (urllib), без внешних зависимостей —
чтобы сборка .app не тянула requests.

Типовые ошибки API и что с ними делаем:
    409  нет папки назначения / файл уже есть  -> ensure_dir, overwrite=true
    429  слишком много запросов                -> повтор с растущей паузой
    5xx  сбой на стороне Диска                 -> повтор с растущей паузой
    202  асинхронная операция (перенос папки)  -> wait_operation()
    423  ресурс заблокирован                   -> повтор
    507  нет места                             -> YaDiskError, показать пользователю
    Ссылки на скачивание/загрузку живут недолго -> запрашиваем прямо перед
    каждым файлом, не кэшируем.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

API_BASE = "https://cloud-api.yandex.net/v1/disk"

#: Расширения, которые считаем фотографиями съёмки (RAW в архиве нет).
PHOTO_EXTS = (".jpg", ".jpeg")

#: Имя служебного файла отбора в папке съёмки.
STATE_FILE = "_contentpulse.json"

#: Папка подстраховки для ретуши внутри съёмки (копии, в галерею не попадают).
RETOUCH_DIR = "Ретушь"

#: Статусы, при которых имеет смысл повторить запрос.
RETRY_STATUSES = {423, 429, 500, 502, 503, 504}


class YaDiskError(Exception):
    """Ошибка API. Всегда несёт статус и полное тело ответа — для диагностики."""

    def __init__(self, status: int, body: str, url: str = "") -> None:
        self.status = status
        self.body = body
        self.url = url
        msg = body
        try:
            data = json.loads(body)
            msg = data.get("message") or data.get("description") or body
        except (ValueError, AttributeError):
            pass
        super().__init__(f"Яндекс Диск {status}: {msg}")


class StateConflict(Exception):
    """Служебный файл изменил кто-то другой с момента нашего чтения."""

    def __init__(self, remote_data: Any, remote_rev: str) -> None:
        self.remote_data = remote_data
        self.remote_rev = remote_rev
        super().__init__("Отбор изменил другой пользователь")


@dataclass
class Resource:
    """Файл или папка на Диске (подмножество полей API)."""

    path: str
    name: str
    type: str  # "dir" | "file"
    size: int = 0
    md5: str = ""
    modified: str = ""
    preview: str = ""
    mime_type: str = ""

    @property
    def is_dir(self) -> bool:
        return self.type == "dir"

    @property
    def is_photo(self) -> bool:
        return self.type == "file" and self.name.lower().endswith(PHOTO_EXTS)

    @classmethod
    def from_api(cls, d: Dict[str, Any]) -> "Resource":
        return cls(
            path=d.get("path", ""),
            name=d.get("name", ""),
            type=d.get("type", ""),
            size=int(d.get("size") or 0),
            md5=d.get("md5", "") or "",
            modified=d.get("modified", "") or "",
            preview=d.get("preview", "") or "",
            mime_type=d.get("mime_type", "") or "",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path, "name": self.name, "type": self.type,
            "size": self.size, "md5": self.md5, "modified": self.modified,
        }


@dataclass
class DownloadJob:
    """Один файл к скачиванию: откуда (путь на Диске) и куда (локальный путь)."""

    remote: str
    local: str
    size: int = 0
    md5: str = ""


@dataclass
class DownloadReport:
    done: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)  # (remote, ошибка)

    def to_dict(self) -> Dict[str, Any]:
        return {"done": len(self.done), "skipped": len(self.skipped),
                "failed": [{"path": p, "error": e} for p, e in self.failed]}


class YaDiskClient:
    """Тонкий клиент над REST API с повторами и понятными ошибками."""

    def __init__(self, token: str, base: str = API_BASE, max_retries: int = 6,
                 backoff: float = 1.0, timeout: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.token = token
        self.base = base.rstrip("/")
        self.max_retries = max_retries
        self.backoff = backoff
        self.timeout = timeout
        self._sleep = sleep

    # ── транспорт ──────────────────────────────────────────

    def _headers(self, auth: bool = True) -> Dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": "ContentPulse/2.0"}
        if auth and self.token:
            h["Authorization"] = f"OAuth {self.token}"
        return h

    def _url(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = self.base + endpoint
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(clean)
        return url

    def _delay(self, attempt: int, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return self.backoff * (2 ** attempt) + random.uniform(0, self.backoff / 2)

    def _open(self, url: str, method: str = "GET", data: Optional[bytes] = None,
              auth: bool = True, extra_headers: Optional[Dict[str, str]] = None):
        """Открыть URL с повторами. Возвращает (status, headers, body_bytes)."""
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            headers = self._headers(auth)
            if extra_headers:
                headers.update(extra_headers)
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code in RETRY_STATUSES and attempt < self.max_retries:
                    self._sleep(self._delay(attempt, e.headers.get("Retry-After")))
                    last_err = YaDiskError(e.code, body, url)
                    continue
                raise YaDiskError(e.code, body, url) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt < self.max_retries:
                    self._sleep(self._delay(attempt, None))
                    last_err = e
                    continue
                raise YaDiskError(0, f"Нет связи с Яндекс Диском: {e}", url) from None
        raise last_err or YaDiskError(0, "неизвестная ошибка", url)

    def _json(self, endpoint: str, params: Optional[Dict[str, Any]] = None,
              method: str = "GET") -> Dict[str, Any]:
        status, _h, body = self._open(self._url(endpoint, params), method=method)
        return json.loads(body.decode("utf-8")) if body else {"_status": status}

    # ── чтение ─────────────────────────────────────────────

    def info(self) -> Dict[str, Any]:
        """Сведения о Диске (место, пользователь). Удобно как проверка токена."""
        return self._json("")

    def list_dir(self, path: str, page: int = 500,
                 preview_size: Optional[str] = None) -> List[Resource]:
        """Содержимое одной папки, все страницы."""
        out: List[Resource] = []
        offset = 0
        while True:
            data = self._json("/resources", {
                "path": path, "limit": page, "offset": offset,
                "preview_size": preview_size, "sort": "name",
            })
            emb = data.get("_embedded") or {}
            items = emb.get("items") or []
            out.extend(Resource.from_api(i) for i in items)
            total = int(emb.get("total") or 0)
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    def list_shoots(self, archive: str) -> List[Resource]:
        """Витрина: папки съёмок в архиве, свежие сверху."""
        dirs = [r for r in self.list_dir(archive) if r.is_dir]
        dirs.sort(key=lambda r: r.modified, reverse=True)
        return dirs

    def walk_photos(self, root: str, preview_size: Optional[str] = "L",
                    skip_service: bool = True) -> List[Resource]:
        """Все JPEG съёмки рекурсивно (части съёмки в подпапках тоже)."""
        out: List[Resource] = []
        stack = [root]
        while stack:
            cur = stack.pop()
            for r in self.list_dir(cur, preview_size=preview_size):
                if r.is_dir:
                    if skip_service and (r.name.startswith("_") or r.name == RETOUCH_DIR):
                        continue
                    stack.append(r.path)
                elif r.is_photo:
                    out.append(r)
        out.sort(key=lambda r: r.path)
        return out

    def get_preview(self, preview_url: str) -> bytes:
        """Байты превью. Ссылки превью приватных файлов требуют OAuth-заголовок."""
        _s, _h, body = self._open(preview_url)
        return body

    def download_href(self, path: str) -> str:
        """Свежая ссылка на скачивание (живёт недолго — не кэшировать)."""
        return self._json("/resources/download", {"path": path})["href"]

    # ── запись ─────────────────────────────────────────────

    def ensure_dir(self, path: str) -> None:
        """Создать папку и всех родителей; 409 «уже есть» — не ошибка."""
        parts = [p for p in path.replace("disk:", "").split("/") if p]
        cur = "disk:"
        for p in parts:
            cur = f"{cur}/{p}"
            try:
                self._json("/resources", {"path": cur}, method="PUT")
            except YaDiskError as e:
                if e.status != 409:
                    raise

    def upload_bytes(self, path: str, data: bytes, overwrite: bool = True) -> None:
        href = self._json("/resources/upload", {
            "path": path, "overwrite": "true" if overwrite else "false"})["href"]
        self._open(href, method="PUT", data=data, auth=False,
                   extra_headers={"Content-Type": "application/octet-stream"})

    def copy(self, src: str, dst: str, overwrite: bool = False) -> str:
        """Скопировать файл/папку на Диске. Возвращает "done" или "exists".

        Для папок API отвечает 202 (асинхронно) — ждём операцию.
        Без overwrite существующий файл не трогаем (409 -> "exists").
        """
        try:
            status, _h, body = self._open(self._url("/resources/copy", {
                "from": src, "path": dst,
                "overwrite": "true" if overwrite else "false"}), method="POST")
        except YaDiskError as e:
            if e.status == 409 and not overwrite:
                return "exists"
            raise
        if status == 202 and body:
            href = json.loads(body.decode("utf-8")).get("href", "")
            if href and self.wait_operation(href) != "success":
                raise YaDiskError(0, f"Копирование не удалось: {src}")
        return "done"

    def wait_operation(self, href: str, poll: float = 1.0, limit: float = 600) -> str:
        """Дождаться асинхронной операции (ответ 202). Возвращает статус."""
        t0 = time.monotonic()
        while True:
            _s, _h, body = self._open(href)
            status = json.loads(body.decode("utf-8")).get("status", "")
            if status in ("success", "failed"):
                return status
            if time.monotonic() - t0 > limit:
                raise YaDiskError(0, "Операция на Диске не завершилась вовремя", href)
            self._sleep(poll)

    # ── скачивание ─────────────────────────────────────────

    def download_file(self, job: DownloadJob) -> bool:
        """Скачать один файл атомарно (.part -> rename).

        Возвращает False, если файл уже есть и совпадает по размеру
        (докачка частями: качаем только новое).
        """
        if os.path.exists(job.local) and job.size and os.path.getsize(job.local) == job.size:
            return False
        os.makedirs(os.path.dirname(job.local) or ".", exist_ok=True)
        tmp = job.local + ".part"
        for attempt in range(self.max_retries + 1):
            href = self.download_href(job.remote)
            try:
                _s, _h, body = self._open(href, auth=False)
            except YaDiskError:
                if attempt < self.max_retries:
                    continue
                raise
            if job.size and len(body) != job.size:
                if attempt < self.max_retries:
                    self._sleep(self._delay(attempt, None))
                    continue
                raise YaDiskError(0, f"Файл скачался не полностью: {job.remote}")
            if job.md5 and hashlib.md5(body).hexdigest() != job.md5:
                if attempt < self.max_retries:
                    continue
                raise YaDiskError(0, f"Контрольная сумма не совпала: {job.remote}")
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, job.local)
            return True
        return False

    def download_many(self, jobs: Iterable[DownloadJob], workers: int = 4,
                      progress: Optional[Callable[[int, int, str], None]] = None,
                      cancel: Optional[threading.Event] = None) -> DownloadReport:
        """Скачать пачку файлов параллельно. Ошибка одного файла не роняет остальные."""
        jobs = list(jobs)
        report = DownloadReport()
        total = len(jobs)
        n = 0
        lock = threading.Lock()

        def run(j: DownloadJob) -> Tuple[DownloadJob, Optional[bool], str]:
            if cancel is not None and cancel.is_set():
                return j, None, "отменено"
            try:
                return j, self.download_file(j), ""
            except YaDiskError as e:
                return j, None, str(e)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for fut in as_completed([ex.submit(run, j) for j in jobs]):
                j, ok, err = fut.result()
                with lock:
                    n += 1
                    if ok is True:
                        report.done.append(j.remote)
                    elif ok is False:
                        report.skipped.append(j.remote)
                    else:
                        report.failed.append((j.remote, err))
                    if progress:
                        progress(n, total, j.remote)
        return report

    # ── общий файл отбора ──────────────────────────────────

    def read_state(self, shoot: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """Прочитать `_contentpulse.json` съёмки. Возвращает (данные, ревизия).

        Ревизия = md5 файла на Диске; пустая строка, если файла ещё нет.
        """
        path = f"{shoot.rstrip('/')}/{STATE_FILE}"
        try:
            meta = self._json("/resources", {"path": path})
        except YaDiskError as e:
            if e.status == 404:
                return None, ""
            raise
        _s, _h, body = self._open(self.download_href(path), auth=False)
        rev = meta.get("md5") or hashlib.md5(body).hexdigest()
        return json.loads(body.decode("utf-8")), rev

    def write_state(self, shoot: str, data: Dict[str, Any], base_rev: str) -> str:
        """Записать отбор, если с момента чтения его никто не менял.

        base_rev — ревизия, которую мы читали. Если на Диске уже другая —
        StateConflict с актуальными данными: вызывающий сливает и повторяет.
        Возвращает новую ревизию.
        """
        current, rev = self.read_state(shoot)
        if rev != base_rev:
            raise StateConflict(current, rev)
        body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
        self.upload_bytes(f"{shoot.rstrip('/')}/{STATE_FILE}", body, overwrite=True)
        return hashlib.md5(body).hexdigest()


def plan_downloads(photos: Iterable[Resource], shoot_root: str, dest: str) -> List[DownloadJob]:
    """Вся съёмка: сохранить структуру подпапок относительно корня съёмки."""
    root = shoot_root.rstrip("/") + "/"
    jobs = []
    for r in photos:
        rel = r.path[len(root):] if r.path.startswith(root) else r.name
        jobs.append(DownloadJob(r.path, os.path.join(dest, *rel.split("/")), r.size, r.md5))
    return jobs


def send_to_retouch(client: YaDiskClient, shoot: str, files: Iterable[str],
                    batch: str) -> Dict[str, Any]:
    """Подстраховка: продублировать файлы в `<съёмка>/Ретушь/<партия>/`.

    Оригиналы не трогаем — только копия. Уже скопированное не перезаписываем,
    поэтому повторная отправка безопасна. Возвращает сводку для журнала.
    """
    target = f"{shoot.rstrip('/')}/{RETOUCH_DIR}/{safe_name(batch)}"
    client.ensure_dir(target)
    copied, existed, failed = [], [], []
    for src in files:
        dst = f"{target}/{src.rsplit('/', 1)[-1]}"
        try:
            (copied if client.copy(src, dst) == "done" else existed).append(src)
        except YaDiskError as e:
            failed.append({"path": src, "error": str(e)})
    return {"folder": target, "copied": copied, "existed": existed, "failed": failed}


def make_event(action: str, actor: str, items: Iterable[str],
               note: str = "", **extra: Any) -> Dict[str, Any]:
    """Запись журнала: кто, что, когда, с какими кадрами/карточками.

    Журнал — список таких записей в `_contentpulse.json` (поле "events").
    Тот же формат потом переносится в базу без изменений.
    action: "open" | "download_shoot" | "download_selection" | "to_retouch" |
            "to_reshoot" | "to_approval" | "edit_cards" | ...
    """
    ev = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": action,
        "actor": actor,
        "items": list(items),
    }
    if note:
        ev["note"] = note
    ev.update(extra)
    return ev


def safe_name(name: str) -> str:
    """Имя папки/файла без символов, которые не любит файловая система."""
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad or ord(c) < 32 else c for c in name).strip(" .")
    return out or "_"
