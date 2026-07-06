#!/usr/bin/env python3
"""
Cardboard — desktop-запуск (pywebview).

Локальный инструмент вёрстки карточек товара. Фотографии остаются на
своих местах, приложение хранит только пути к ним. Проект устроен как
сессия Capture One: папка проекта, внутри `имя.cardboard` (JSON: пути
к фото + структура карточек) и `previews/` — локальные JPEG-превью
до 1200px. Превью пишутся один раз при импорте, дальше проект
открывается мгновенно с диска и не зависит от доступа к исходникам.

Запуск: python3 main.py
Зависимости: pywebview, Pillow (доустановятся сами при первом запуске).
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


def _ensure_deps() -> None:
    """Доустановить зависимости при первом запуске, чтобы не лезть в терминал."""
    required = {"webview": "pywebview", "PIL": "Pillow", "certifi": "certifi"}
    missing = [pip for imp, pip in required.items() if not _importable(imp)]
    if missing:
        print(f"[Cardboard] Устанавливаю зависимости: {', '.join(missing)} ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + missing)
        print("[Cardboard] Готово.")


def _importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


if not getattr(sys, "frozen", False) and __name__ == "__main__":
    _ensure_deps()

from PIL import Image, ImageOps  # noqa: E402

import hashlib  # noqa: E402

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp"}
THUMB_SIDE = 1200   # макс. сторона превью (просьба Маши: локальные превью 1200px)
JPEG_Q = 85
FILE_TYPES = ("Изображения (*.jpg;*.jpeg;*.png;*.gif;*.webp;*.tif;*.tiff;*.bmp)",)
PROJ_TYPES = ("Проект Cardboard (*.cardboard)",)


def make_thumb(path: str, save_to: Optional[Path] = None) -> Optional[dict]:
    """Прочитать изображение с диска и вернуть превью как base64 dataURL.

    save_to — записать JPEG-превью на диск (папка previews проекта).
    При ошибке возвращает {"error": kind}:
    - "denied"  — macOS TCC запретил доступ (Загрузки/Документы и т.п.);
    - "missing" — файла нет по этому пути;
    - "unreadable" — файл есть, но не читается как изображение.
    """
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((THUMB_SIDE, THUMB_SIDE))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=JPEG_Q)
            if save_to is not None:
                try:
                    save_to.write_bytes(buf.getvalue())
                except Exception:
                    pass   # кэш — best effort, превью в памяти всё равно есть
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return {"dataUrl": "data:image/jpeg;base64," + b64, "w": im.width, "h": im.height}
    except PermissionError:
        return {"error": "denied"}
    except FileNotFoundError:
        return {"error": "missing"}
    except Exception:
        return {"error": "unreadable"}


def from_dataurl(du: str) -> Optional[Image.Image]:
    """data:image/...;base64 -> PIL Image (RGB)."""
    try:
        b64 = du.split(",", 1)[1]
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    except Exception:
        return None


def cover_fit(im: Image.Image, w: int, h: int) -> Image.Image:
    """Заполнить бокс с обрезкой по центру (как CSS object-fit: cover)."""
    return ImageOps.fit(im.convert("RGB"), (w, h), Image.LANCZOS)


def load_preview(fp: Path) -> Optional[dict]:
    """Готовое JPEG-превью с диска -> dataUrl + размеры (быстро, без исходника)."""
    try:
        data = fp.read_bytes()
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        b64 = base64.b64encode(data).decode("ascii")
        return {"dataUrl": "data:image/jpeg;base64," + b64, "w": w, "h": h}
    except Exception:
        return None


class CardboardAPI:
    """Мост JS <-> Python. Все методы вызываются из frontend через pywebview."""

    def __init__(self) -> None:
        self._window = None                 # webview.Window, задаётся при старте
        self._thumb_cache: dict = {}        # path -> {dataUrl, w, h} (только RAM)
        self._watch_dir: Optional[str] = None
        self._watch_seen: set = set()
        self._project_path: Optional[str] = None
        self._dirty: bool = False           # есть несохранённые изменения
        self._native_menu: bool = False     # удалось ли собрать нативное меню

    # ---------- служебное ----------

    def ping(self) -> str:
        """Проверка живости моста (frontend определяет desktop-режим)."""
        return "cardboard"

    def set_dirty(self, dirty: bool) -> None:
        """Frontend сообщает о несохранённых изменениях (для диалога при выходе)."""
        self._dirty = bool(dirty)

    def set_title(self, title: str) -> None:
        """Имя проекта в заголовке окна (топбар лаконичный, имени там нет)."""
        if self._window:
            self._window.set_title(str(title)[:120])

    def has_native_menu(self) -> bool:
        """Frontend спрашивает: есть ли нативное меню (тогда HTML-меню прячется)."""
        return self._native_menu

    def _previews_dir(self) -> Optional[Path]:
        """Папка previews рядом с файлом проекта (как сессия Capture One)."""
        if not self._project_path:
            return None
        d = Path(self._project_path).parent / "previews"
        try:
            d.mkdir(exist_ok=True)
        except Exception:
            return None
        return d

    def _thumb_cached(self, path: str) -> Optional[dict]:
        """Превью: RAM-кэш -> дисковый кэш previews/ -> генерация из исходника.

        Дисковый кэш: previews/<sha1(путь)>_<mtime>.jpg. Если исходник
        недоступен (файл переехал, TCC) — используем последнее превью
        с диска: проект открывается всегда.
        """
        if path in self._thumb_cache:
            return self._thumb_cache[path]
        pdir = self._previews_dir()
        key = hashlib.sha1(path.encode("utf-8")).hexdigest()[:24]
        try:
            mtime = int(Path(path).stat().st_mtime)
        except Exception:
            mtime = None   # исходник недоступен — сгодится любое превью
        hits = sorted(pdir.glob(key + "_*.jpg")) if pdir else []
        for f in hits:
            try:
                fm = int(f.stem.split("_", 1)[1])
            except Exception:
                fm = None
            if mtime is None or fm == mtime:
                t = load_preview(f)
                if t:
                    self._thumb_cache[path] = t
                    return t
        save_to = (pdir / f"{key}_{mtime}.jpg") if (pdir and mtime is not None) else None
        if save_to is not None:
            for old in hits:   # устаревшие превью этого файла
                try:
                    old.unlink()
                except Exception:
                    pass
        t = make_thumb(path, save_to)
        if t is None or "error" in t:
            return t
        self._thumb_cache[path] = t
        return t

    def _import_paths(self, paths: list) -> list:
        """Пути -> список фото для frontend: name, path, размеры, миниатюра."""
        out = []
        for p in paths:
            if Path(p).suffix.lower() not in IMG_EXT:
                continue
            t = self._thumb_cached(p)
            if t is None or "error" in t:
                continue
            out.append({
                "name": Path(p).name,
                "path": str(p),
                "w": t["w"], "h": t["h"],
                "dataUrl": t["dataUrl"],
            })
        return out

    # ---------- импорт фото ----------

    def pick_photos(self) -> list:
        """Диалог выбора фото (мультивыбор). Возвращает список фото."""
        import webview
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True, file_types=FILE_TYPES)
        return self._import_paths(list(result)) if result else []

    def pick_folder(self) -> list:
        """Диалог выбора папки — импорт всех изображений из неё (без подпапок)."""
        import webview
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return []
        folder = Path(result[0])
        paths = sorted(str(f) for f in folder.iterdir()
                       if f.is_file() and f.suffix.lower() in IMG_EXT)
        return self._import_paths(paths)

    def pick_folder_paths(self) -> list:
        """Диалог выбора папки — только СПИСОК путей, без миниатюр.

        Миниатюры frontend запрашивает батчами (import_dropped) и
        показывает прогресс: на больших съёмках (сотни кадров) один
        синхронный вызов замораживал интерфейс без обратной связи.
        """
        import webview
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return []
        folder = Path(result[0])
        return sorted(str(f) for f in folder.iterdir()
                      if f.is_file() and f.suffix.lower() in IMG_EXT)

    def import_dropped(self, paths: list) -> list:
        """Импорт файлов, брошенных drag-and-drop (пути передаёт frontend)."""
        return self._import_paths([p for p in paths if p])

    def thumbs_for(self, paths: list) -> dict:
        """Миниатюры для списка путей (при открытии проекта).

        Возвращает {path: {dataUrl,w,h} | {"error": kind}} —
        kind = denied (нет доступа, TCC) | missing (файла нет) | unreadable.
        """
        return {p: self._thumb_cached(p) for p in paths}

    def write_pdf(self, spec: dict, out: str) -> Optional[str]:
        """Собрать многостраничный PDF A4 (300 dpi) из раскладки карточек.

        Страница = карточка: боксы из движка (юниты W), фото — оригинал
        (макс. качество), фолбэк превью-кэш/dataUrl; пустой слот — плашка.
        """
        from PIL import ImageDraw
        pages = []
        for pg in spec.get("pages", []):
            size = (3508, 2480) if pg.get("canvas") == "h" else (2480, 3508)
            try:
                bg = pg.get("bg") or "#ffffff"
                img = Image.new("RGB", size, bg)
            except Exception:
                img = Image.new("RGB", size, (255, 255, 255))
            draw = ImageDraw.Draw(img)
            sc = size[0] / float(pg.get("W") or 1000)
            for b in pg.get("boxes", []):
                bx, by = int(b["x"] * sc), int(b["y"] * sc)
                bw, bh = max(1, int(b["w"] * sc)), max(1, int(b["h"] * sc))
                src = self._pdf_source(b)
                if src is not None:
                    img.paste(cover_fit(src, bw, bh), (bx, by))
                    src.close()
                else:
                    draw.rectangle([bx, by, bx + bw, by + bh],
                                   fill=(245, 245, 244), outline=(221, 221, 219), width=2)
            title = (pg.get("title") or "").strip()
            if title:
                try:
                    from PIL import ImageFont
                    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 42)
                    draw.text((int(size[0] * 0.032), 20), title, fill=(128, 128, 128), font=font)
                except Exception:
                    pass   # нет шрифта — страница без заголовка
            pages.append(img)
        if not pages:
            return None
        pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=300.0)
        return out

    def _pdf_source(self, b: dict) -> Optional[Image.Image]:
        """Источник фото для бокса: оригинал -> превью-кэш -> dataUrl."""
        path = b.get("path")
        if path:
            try:
                im = Image.open(path)
                im.load()
                return ImageOps.exif_transpose(im).convert("RGB")
            except Exception:
                t = self._thumb_cached(path)
                if t and t.get("dataUrl"):
                    return from_dataurl(t["dataUrl"])
        if b.get("dataUrl"):
            return from_dataurl(b["dataUrl"])
        return None

    def export_pdf(self, spec_json: str) -> Optional[str]:
        """Экспорт PDF в файл (печать — отдельно, через системный диалог)."""
        import webview
        try:
            spec = json.loads(spec_json)
        except Exception:
            return None
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=f"{spec.get('name') or 'Карточки'}.pdf",
            file_types=("PDF (*.pdf)",))
        if not result:
            return None
        out = result if isinstance(result, str) else result[0]
        if not out.endswith(".pdf"):
            out += ".pdf"
        return self.write_pdf(spec, out)

    # ---------- автообновление с GitHub Releases ----------

    UPDATE_REPO = "barinskim-cmyk/content-pulse-cardboard"

    @staticmethod
    def _https_ctx():
        """SSL-контекст с сертификатами certifi (python.org-Python без него
        не доверяет ни одному сайту: CERTIFICATE_VERIFY_FAILED)."""
        import ssl
        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()

    def check_update(self) -> Optional[str]:
        """Версия последнего релиза на GitHub (version.txt из релиза) или None."""
        import urllib.request
        url = f"https://github.com/{self.UPDATE_REPO}/releases/latest/download/version.txt"
        try:
            with urllib.request.urlopen(url, timeout=10, context=self._https_ctx()) as r:
                v = r.read().decode("utf-8").strip()
            return v if v and len(v) < 20 else None
        except Exception:
            return None

    def install_update(self, relaunch: bool = True) -> dict:
        """Скачать свежий Cardboard-macOS.zip и подменить текущий .app.

        Скачивает Python (без карантина Gatekeeper), распаковывает во
        временную папку, меняет местами с текущим .app (с откатом при
        ошибке) и перезапускает. Возвращает {"ok": bool, "error": str}.
        """
        import shutil
        import tempfile
        import urllib.request
        if not getattr(sys, "frozen", False):
            return {"ok": False, "error": "запущено из исходников — обновляйтесь через git"}
        if sys.platform != "darwin":
            # Windows: запущенный .exe нельзя подменить на лету —
            # frontend показывает ссылку на страницу релиза
            return {"ok": False, "error": "скачайте новую версию со страницы релиза"}
        app = Path(sys.executable).resolve().parents[2]
        if app.suffix != ".app":
            return {"ok": False, "error": "не удалось определить .app"}
        parent = app.parent
        if not os.access(parent, os.W_OK):
            return {"ok": False, "error": f"нет прав на запись в {parent}"}
        url = f"https://github.com/{self.UPDATE_REPO}/releases/latest/download/Cardboard-macOS.zip"
        try:
            tmpd = Path(tempfile.mkdtemp(prefix="cardboard_upd_"))
            zpath = tmpd / "Cardboard-macOS.zip"
            with urllib.request.urlopen(url, timeout=180, context=self._https_ctx()) as r:
                zpath.write_bytes(r.read())
            subprocess.run(["ditto", "-x", "-k", str(zpath), str(tmpd)], check=True)
            new_app = tmpd / "Cardboard.app"
            if not (new_app / "Contents" / "MacOS" / "Cardboard").exists():
                return {"ok": False, "error": "архив обновления неполный"}
            old = parent / (app.name + ".old")
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
            os.rename(app, old)
            try:
                shutil.move(str(new_app), str(app))
            except Exception as exc:
                os.rename(old, app)   # откат: старая версия на месте
                return {"ok": False, "error": f"не удалось заменить: {exc}"}
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(tmpd, ignore_errors=True)
            if relaunch:
                subprocess.Popen(["open", str(app)])
            return {"ok": True, "error": ""}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def quit_app(self) -> None:
        """Закрыть окно после установки обновления (без диалогов закрытия)."""
        import webview
        self._silent_quit = True
        if webview.windows:
            webview.windows[0].destroy()

    def locate_scan(self) -> Optional[dict]:
        """Locate как в Capture One: выбрать папку, собрать {имя файла: путь}.

        Сканирует папку с подпапками; при дублях имён берётся первый
        (сортировка по пути). Frontend сопоставляет фото проекта по имени.
        """
        import webview
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        folder = result[0]
        names: dict = {}
        for root, _dirs, files in os.walk(folder):
            for fn in sorted(files):
                if Path(fn).suffix.lower() in IMG_EXT and fn not in names:
                    names[fn] = str(Path(root) / fn)
        return {"folder": folder, "names": names}

    def grant_folder_access(self, directory: Optional[str] = None) -> bool:
        """Вернуть доступ к папке с фото после запрета macOS (TCC).

        Открывает системный диалог выбора папки (сразу на нужной) —
        выбор пользователем в диалоге даёт приложению право чтения.
        """
        import webview
        kwargs = {}
        if directory and Path(directory).is_dir():
            kwargs["directory"] = directory
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG, **kwargs)
        return bool(result)

    # ---------- слежение за папкой (Capture One hot key) ----------

    def watch_pick(self) -> Optional[str]:
        """Выбрать папку для слежения. Уже лежащие файлы попадут в первый скан."""
        import webview
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        self._watch_dir = result[0]
        self._watch_seen = set()
        return self._watch_dir

    def watch_stop(self) -> None:
        self._watch_dir = None
        self._watch_seen = set()

    def watch_scan(self) -> list:
        """Один проход по папке слежения: вернуть новые фото (frontend зовёт по таймеру)."""
        if not self._watch_dir or not os.path.isdir(self._watch_dir):
            return []
        fresh = []
        for f in sorted(Path(self._watch_dir).iterdir()):
            if f.is_file() and f.suffix.lower() in IMG_EXT and f.name not in self._watch_seen:
                self._watch_seen.add(f.name)
                fresh.append(str(f))
        return self._import_paths(fresh)

    # ---------- проект ----------

    def save_project(self, name: str, json_str: str, force_dialog: bool = False) -> Optional[str]:
        """Сохранить проект. Первый раз — диалог, дальше перезапись того же файла.

        Возвращает путь к файлу или None (пользователь отменил).
        """
        import webview
        path = self._project_path
        if path is None or force_dialog:
            result = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f"{name or 'Проект'}.cardboard",
                file_types=PROJ_TYPES)
            if not result:
                return None
            path = result if isinstance(result, str) else result[0]
            if not path.endswith(".cardboard"):
                path += ".cardboard"
            # Проект как сессия Capture One: папка проекта, внутри файл
            # и previews/. Если пользователь уже в одноимённой папке —
            # не вкладывать вторую.
            p = Path(path)
            if p.parent.name != p.stem:
                try:
                    folder = p.parent / p.stem
                    folder.mkdir(exist_ok=True)
                    path = str(folder / p.name)
                except Exception:
                    pass   # не смогли создать папку — сохраняем как выбрано
        # Атомарная запись: сначала соседний tmp-файл, потом мгновенная
        # подмена (os.replace). Даже если приложение убьют посреди
        # автосейва — старый файл проекта останется целым.
        p = Path(path)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json_str, encoding="utf-8")
        os.replace(tmp, p)
        self._project_path = path
        self._previews_dir()   # создать previews/ сразу
        return path

    def open_project(self) -> Optional[dict]:
        """Диалог открытия проекта. Возвращает {path, data(str)} или None."""
        import webview
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=PROJ_TYPES)
        if not result:
            return None
        path = result[0]
        try:
            data = Path(path).read_text(encoding="utf-8")
            json.loads(data)  # валидация
        except Exception:
            return {"path": path, "data": None}
        self._project_path = path
        return {"path": path, "data": data}

    def project_path(self) -> Optional[str]:
        return self._project_path

    def open_project_at(self, path: str) -> Optional[dict]:
        """Открыть проект по известному пути (лаунчер недавних проектов)."""
        try:
            data = Path(path).read_text(encoding="utf-8")
            json.loads(data)  # валидация
        except Exception:
            return None
        self._project_path = path
        return {"path": path, "data": data}

    def reveal(self, path: str) -> None:
        """Показать файл в Finder/Explorer."""
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])


def find_frontend() -> Path:
    """Путь к index.html: рядом с main.py или внутри frozen-бандла (PyInstaller).

    ВАЖНО: .resolve() обязателен — в .app-бандле PyInstaller кладёт data-файлы
    в Resources и делает симлинк из Frameworks; WKWebView отказывается грузить
    file:// по симлинку, ведущему за пределы разрешённой папки (белое окно).
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = base / "index.html"
        if candidate.exists():
            return candidate.resolve()
    return (Path(__file__).parent / "index.html").resolve()


def main() -> None:
    import webview

    api = CardboardAPI()
    html = find_frontend()
    window = webview.create_window(
        "Cardboard — вёрстка карточек",
        html.as_uri(),
        js_api=api,
        width=1440, height=900,
        min_size=(1000, 640),
    )
    api._window = window

    def on_closing() -> bool:
        """Диалоги при закрытии окна.

        1. Несохранённые изменения — предложить не терять их.
        2. Спросить, удалить ли файл проекта (по просьбе Маши: локальная
           версия — гарантия сохранности, но после работы можно подчистить).
        Возврат False отменяет закрытие.
        """
        if getattr(api, "_silent_quit", False):
            return True   # перезапуск после обновления — без вопросов
        if api._dirty:
            keep = window.create_confirmation_dialog(
                "Cardboard",
                "Есть несохранённые изменения. Закрыть без сохранения?")
            if not keep:
                return False
        if api._project_path and os.path.isfile(api._project_path):
            delete = window.create_confirmation_dialog(
                "Cardboard",
                "Удалить файл проекта?\n\n" + api._project_path +
                "\n\nOK — удалить файл, Cancel — оставить.")
            if delete:
                try:
                    os.remove(api._project_path)
                except OSError:
                    pass
        return True

    window.events.closing += on_closing

    def _diag() -> None:
        """CB_DIAG=1 — самопроверка после запуска: загрузился ли frontend."""
        import time
        time.sleep(5)
        try:
            print("DIAG READY:", window.evaluate_js("document.readyState"))
            print("DIAG TOPBAR:", window.evaluate_js(
                "document.querySelector('.cb-topbar') ? 'yes' : 'no'"))
            print("DIAG BRIDGE:", window.evaluate_js(
                "!!(window.pywebview && window.pywebview.api)"))
        except Exception as exc:   # noqa: BLE001
            print("DIAG FAIL:", exc)
        window.destroy()

    # Нативное меню (macOS: системный бар сверху — «где обычно Файл»).
    # Пункты дёргают JS-обработчики через evaluate_js; HTML-меню при этом
    # прячется (frontend спрашивает has_native_menu).
    menu = None
    try:
        import webview.menu as wm

        def _js(action: str):
            def _cb():
                window.evaluate_js(f'cbMenuNative("{action}")')
            return _cb

        menu = [
            wm.Menu("Файл", [
                wm.MenuAction("Новый проект", _js("new")),
                wm.MenuAction("Открыть проект", _js("open")),
                wm.MenuAction("Сохранить", _js("save")),
                wm.MenuAction("Переименовать проект", _js("rename")),
                wm.MenuSeparator(),
                wm.MenuAction("Импорт фото", _js("pick")),
                wm.MenuAction("Импорт референсов", _js("pickref")),
                wm.MenuAction("Импорт папки целиком", _js("folder")),
                wm.MenuAction("Следить за папкой (вкл/выкл)", _js("watch")),
                wm.MenuAction("Подставить папку с фото (Locate)", _js("locate")),
                wm.MenuSeparator(),
                wm.MenuAction("Экспорт PDF", _js("pdf")),
                wm.MenuAction("Печать", _js("print")),
                wm.MenuAction("Экспорт списка (CSV + TXT)", _js("list")),
            ]),
            wm.Menu("Шаблон", [
                wm.MenuAction("Создать шаблон", _js("tplnew")),
                wm.MenuAction("Галерея шаблонов", _js("tplgal")),
            ]),
            wm.Menu("Справка Cardboard", [
                wm.MenuAction("Инструкция", _js("helpguide")),
                wm.MenuAction("Синхронизация с Capture One", _js("helpsync")),
                wm.MenuAction("Другие продукты", _js("helpprod")),
                wm.MenuSeparator(),
                wm.MenuAction("Проверить обновления", _js("update")),
                wm.MenuAction("Отчёты об ошибках (вкл/выкл)", _js("telemetry")),
            ]),
        ]
        api._native_menu = True
    except Exception:
        menu = None
        api._native_menu = False

    # Persistent storage: иначе pywebview (private_mode по умолчанию) стирает
    # localStorage при каждом запуске — пропадала бы библиотека шаблонов.
    home = Path.home()
    if sys.platform == "darwin":
        storage_dir = home / "Library" / "Application Support" / "Cardboard"
    elif sys.platform.startswith("win"):
        storage_dir = Path(os.environ.get("APPDATA", str(home))) / "Cardboard"
    else:
        storage_dir = home / ".config" / "Cardboard"
    start_kwargs = {"private_mode": False, "debug": "--debug" in sys.argv}
    if menu:
        start_kwargs["menu"] = menu
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
        start_kwargs["storage_path"] = str(storage_dir)
    except OSError:
        pass

    if os.environ.get("CB_DIAG"):
        print("DIAG HTML:", find_frontend())
        webview.start(_diag, **start_kwargs)
    else:
        webview.start(**start_kwargs)


if __name__ == "__main__":
    main()
