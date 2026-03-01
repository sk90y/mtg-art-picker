from __future__ import annotations

__version__ = "2.0.1"

import json
import re
import threading
import time
import hashlib
import logging
import sys
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

import requests
from shiboken6 import isValid

from PySide6.QtCore import Qt, QSize, Signal, QObject, QSettings, QTimer
from PySide6.QtGui import QPixmap, QKeySequence, QAction, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QSplitter,
    QFileDialog, QDialog, QLineEdit, QTextEdit, QMessageBox,
    QScrollArea, QFrame, QProgressDialog, QInputDialog,
    QCheckBox, QComboBox, QFormLayout, QGroupBox
)

# ---------------------------- Config ----------------------------

SCRYFALL_SEARCH = "https://api.scryfall.com/cards/search"
UNDO_STACK_LIMIT = 50

# Image fetch pool (bounded)
# If you want the ultimate “diagnostic safe mode”, set this to 1.
IMAGE_WORKERS = 4
_image_executor = ThreadPoolExecutor(max_workers=IMAGE_WORKERS, thread_name_prefix="img")

# Metadata fetch pool (bounded)
META_WORKERS = 2
_meta_executor = ThreadPoolExecutor(max_workers=META_WORKERS, thread_name_prefix="meta")

# Preload pool (bounded) – prevents “one new thread per navigation”
PRELOAD_WORKERS = 2
_preload_executor = ThreadPoolExecutor(max_workers=PRELOAD_WORKERS, thread_name_prefix="preload")

# Download pool (kept separate from preload so long downloads do not starve preloading)
DOWNLOAD_WORKERS = 1
_download_executor = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS, thread_name_prefix="download")

# Lazy thumb loading tuning (important for ALL PRINTS)
THUMB_LOAD_BUFFER = 10          # thumbs beyond viewport to prefetch
PRELOAD_NEXT_THUMBS = 30        # next-card thumb prefetch
PRELOAD_NEXT_BIG = 2            # next-card big prefetch

# ---------------------------- Thread-local requests.Session ----------------------------

_thread_local = threading.local()

def get_session() -> requests.Session:
    """One Session per worker thread: avoids shared-session concurrency weirdness."""
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "MTGArtPicker/1.0 (+https://scryfall.com/docs/api)"
        })
        _thread_local.session = s
    return s

# ---------------------------- Logging (project-based) ----------------------------

def setup_project_logging(project_folder: Path) -> Path:
    logs_dir = project_folder / "SOFTWARE LOGS USE TO CHECK FOR ERRORS"
    logs_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = logs_dir / f"mtg_art_picker_{ts}.log"

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s - %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    logging.info("========== MTG ART PICKER START ==========")
    logging.info(f"Version: {__version__}")
    logging.info(f"Project folder: {project_folder}")
    logging.info(f"Log file: {log_path}")

    def handle_exception(exc_type, exc, tb):
        logging.critical("UNHANDLED EXCEPTION", exc_info=(exc_type, exc, tb))
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = handle_exception

    if hasattr(threading, "excepthook"):
        def thread_excepthook(args):
            logging.critical(
                f"UNHANDLED THREAD EXCEPTION in {args.thread.name}",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        threading.excepthook = thread_excepthook

    return log_path

# ---------------------------- Rate Limiter (API only) ----------------------------

class RateLimiter:
    def __init__(self, min_interval_sec: float = 0.12):
        self.min_interval = min_interval_sec
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self):
        with self._lock:
            now = time.time()
            if now < self._next_allowed:
                time.sleep(self._next_allowed - now)
            self._next_allowed = time.time() + self.min_interval

API_LIMITER = RateLimiter(min_interval_sec=0.12)

# ---------------------------- Data Models ----------------------------

@dataclass
class Printing:
    set_code: str
    set_name: str
    collector_number: str
    released_at: str
    scryfall_uri: str
    image_small: str
    image_normal: str
    image_png: Optional[str]
    image_large: Optional[str]

# ---------------------------- Utilities ----------------------------

def safe_filename(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:180]

def cache_key(card: str) -> str:
    safe = safe_filename(card)
    h = hashlib.sha256(card.encode("utf-8")).hexdigest()[:10]
    return f"{safe}_{h}"

def tr(language: str, en: str, zh: str) -> str:
    return zh if language == "zh" else en

def parse_deck_quantities(text: str) -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    qty_re = re.compile(r"^\s*(\d+)\s*x?\s+(.+?)\s*$", re.IGNORECASE)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        m = qty_re.match(line)
        qty = int(m.group(1)) if m else 1
        name = (m.group(2) if m else line).strip()
        name = re.sub(r"\s*\([A-Z0-9]{2,6}\)\s*$", "", name).strip()
        if name:
            out.append((max(1, qty), name))
    return out

def parse_token_queries(text: str) -> List[str]:
    tokens: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if "type:token" not in line.lower():
            line = f"type:token {line}"
        tokens.append(line)
    return tokens

def http_get_bytes(url: str, timeout: int = 30) -> bytes:
    r = get_session().get(url, timeout=timeout)
    r.raise_for_status()
    return r.content

def filters_signature(filters: Dict[str, Any]) -> str:
    blob = json.dumps(filters or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:10]

# ---------------------------- Filters / Query Building ----------------------------

DEFAULT_FILTERS: Dict[str, Any] = {
    "prefer_borderless": False,
    "border": "any",
    "frame_edition": "any",
    "frame_effect": "any",
    "is_full": False,
    "is_hires": False,
    "is_default": False,
    "is_atypical": False,
    "exclude_ub": False,
    "stamp": "any",
}

def build_filter_terms(f: Dict[str, Any]) -> List[str]:
    terms: List[str] = []

    border = (f.get("border") or "any").lower()
    if border != "any":
        terms.append(f"border:{border}")

    fe = (f.get("frame_edition") or "any").lower()
    if fe != "any":
        terms.append(f"frame:{fe}")

    fx = (f.get("frame_effect") or "any").lower()
    if fx != "any":
        terms.append(f"frame:{fx}")

    if bool(f.get("is_full")):
        terms.append("is:full")
    if bool(f.get("is_hires")):
        terms.append("is:hires")
    if bool(f.get("is_default")):
        terms.append("is:default")
    if bool(f.get("is_atypical")):
        terms.append("is:atypical")

    if bool(f.get("exclude_ub")):
        terms.append("not:universesbeyond")

    stamp = (f.get("stamp") or "any").lower()
    if stamp != "any":
        terms.append(f"stamp:{stamp}")

    return terms

# ---------------------------- Scryfall Fetch ----------------------------

def extract_images(card_obj: Dict[str, Any]) -> Tuple[Optional[Dict[str, str]], str]:
    scryfall_uri = str(card_obj.get("scryfall_uri", ""))

    if "image_uris" in card_obj and isinstance(card_obj["image_uris"], dict):
        return card_obj["image_uris"], scryfall_uri

    faces = card_obj.get("card_faces")
    if isinstance(faces, list) and faces:
        iu = faces[0].get("image_uris")
        if isinstance(iu, dict):
            return iu, scryfall_uri

    return None, scryfall_uri

def fetch_all_printings_with_query(q: str) -> List[Printing]:
    params = {"q": q, "unique": "prints", "order": "released", "dir": "desc"}
    url = SCRYFALL_SEARCH
    out: List[Printing] = []

    while True:
        API_LIMITER.wait()
        r = get_session().get(url, params=params, timeout=30)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()

        for c in data.get("data", []):
            iu, scryfall_uri = extract_images(c)
            if not iu:
                continue

            image_small = iu.get("small") or iu.get("normal")
            image_normal = iu.get("normal") or iu.get("large") or iu.get("small")
            image_png = iu.get("png")
            image_large = iu.get("large")

            if not image_small or not image_normal:
                continue

            out.append(
                Printing(
                    set_code=str(c.get("set", "")).upper(),
                    set_name=str(c.get("set_name", "")),
                    collector_number=str(c.get("collector_number", "")),
                    released_at=str(c.get("released_at", "")),
                    scryfall_uri=scryfall_uri,
                    image_small=image_small,
                    image_normal=image_normal,
                    image_png=image_png,
                    image_large=image_large,
                )
            )

        if data.get("has_more"):
            url = data.get("next_page")
            params = None
        else:
            break

    return out

def fetch_all_printings(card_name: str, filters: Dict[str, Any], exact_name: bool = True) -> List[Printing]:
    base = f'!"{card_name}"' if exact_name else card_name
    if not filters:
        return fetch_all_printings_with_query(base)

    terms = build_filter_terms(filters)
    prefer_borderless = bool(filters.get("prefer_borderless"))
    border = (filters.get("border") or "any").lower()

    if border == "any" and prefer_borderless:
        q1 = " ".join([base, "border:borderless"] + [t for t in terms if not t.startswith("border:")])
        res = fetch_all_printings_with_query(q1)
        if res:
            return res
        q2 = " ".join([base] + terms)
        return fetch_all_printings_with_query(q2)

    q = " ".join([base] + terms)
    return fetch_all_printings_with_query(q)

# ---------------------------- Worker Signals ----------------------------

class ImageLoaded(QObject):
    bytes_ready = Signal(str, bytes)
    error = Signal(str, str)

class MetaLoaded(QObject):
    meta_ready = Signal(str, list, str, bool)   # card, prints, sig, auto_relaxed
    error = Signal(str, str, str)               # card, msg, sig

class DownloadSignals(QObject):
    progress = Signal(int)
    done = Signal(str)
    error = Signal(str)

# ---------------------------- Project Manager ----------------------------

class Project:
    def __init__(self, folder: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)

        self.cache_meta = self.folder / "cache" / "meta"
        self.cache_small = self.folder / "cache" / "small"
        self.cache_normal = self.folder / "cache" / "normal"
        self.cache_meta.mkdir(parents=True, exist_ok=True)
        self.cache_small.mkdir(parents=True, exist_ok=True)
        self.cache_normal.mkdir(parents=True, exist_ok=True)

        self.project_path = self.folder / "project.json"
        self.selections_path = self.folder / "selections.json"

        self.deck: List[str] = []
        self.current_index: int = 0
        self.active_printing_index: Dict[str, int] = {}
        self.selections: Dict[str, Dict[str, Any]] = {}
        self.filters: Dict[str, Any] = dict(DEFAULT_FILTERS)
        self.card_query: Dict[str, str] = {}
        self.card_qty: Dict[str, int] = {}
        self.ui_language: str = "en"

    def save(self):
        try:
            self.project_path.write_text(json.dumps({
                "deck": self.deck,
                "current_index": self.current_index,
                "active_printing_index": self.active_printing_index,
                "filters": self.filters,
                "card_query": self.card_query,
                "card_qty": self.card_qty,
                "ui_language": self.ui_language,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            self.selections_path.write_text(
                json.dumps(self.selections, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logging.warning(f"Failed to save project files in {self.folder}: {e}", exc_info=True)

    def load(self):
        try:
            if self.project_path.exists():
                obj = json.loads(self.project_path.read_text(encoding="utf-8"))
                self.deck = obj.get("deck", []) or []
                self.current_index = int(obj.get("current_index", 0))
                self.active_printing_index = obj.get("active_printing_index", {}) or {}
                loaded_filters = obj.get("filters", None)
                if isinstance(loaded_filters, dict):
                    merged = dict(DEFAULT_FILTERS)
                    merged.update(loaded_filters)
                    self.filters = merged
                else:
                    self.filters = dict(DEFAULT_FILTERS)
                self.card_query = obj.get("card_query", {}) or {}
                self.card_qty = obj.get("card_qty", {}) or {}
                self.ui_language = str(obj.get("ui_language", "en"))
                if not self.card_query:
                    self.card_query = {c: c for c in self.deck}
                if not self.card_qty:
                    self.card_qty = {c: 1 for c in self.deck}

            if self.selections_path.exists():
                self.selections = json.loads(self.selections_path.read_text(encoding="utf-8")) or {}

        except Exception as e:
            logging.warning(f"Failed to load project files in {self.folder}: {e}", exc_info=True)
            self.current_index = 0
            self.active_printing_index = {}
            self.selections = {}
            self.filters = dict(DEFAULT_FILTERS)
            self.card_query = {}
            self.card_qty = {}
            self.ui_language = "en"

    def meta_cache_path(self, card: str, sig: str) -> Path:
        return self.cache_meta / f"{cache_key(card)}__{sig}.json"

    def get_cached_meta(self, card: str, sig: str) -> Optional[List[Printing]]:
        p = self.meta_cache_path(card, sig)
        if not p.exists():
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [Printing(**x) for x in raw]

    def set_cached_meta(self, card: str, sig: str, printings: List[Printing]):
        p = self.meta_cache_path(card, sig)
        try:
            p.write_text(json.dumps([pp.__dict__ for pp in printings], ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logging.warning(f"Failed to write meta cache for {card} ({sig}): {e}", exc_info=True)

    def image_cache_path(self, kind: str, card: str, sig: str, idx: int) -> Path:
        base = self.cache_small if kind == "small" else self.cache_normal
        return base / f"{cache_key(card)}__{sig}__{idx}.img"

    def get_cached_image_bytes(self, kind: str, card: str, sig: str, idx: int) -> Optional[bytes]:
        p = self.image_cache_path(kind, card, sig, idx)
        return p.read_bytes() if p.exists() else None

    def set_cached_image_bytes(self, kind: str, card: str, sig: str, idx: int, data: bytes):
        p = self.image_cache_path(kind, card, sig, idx)
        try:
            p.write_bytes(data)
        except Exception as e:
            logging.warning(f"Failed to write {kind} image cache for {card} ({sig}) [{idx}]: {e}", exc_info=True)

# ---------------------------- UI Dialogs ----------------------------

class StartDialog(QDialog):
    def __init__(self, parent=None, recent: List[str] = None, ui_language: str = "en"):
        super().__init__(parent)
        self.lang = ui_language if ui_language in ("en", "zh") else "en"
        self.setWindowTitle(tr(self.lang, "MTG Art Picker — Start", "万智牌选图器 — 开始"))
        self.setModal(True)
        self.resize(520, 320)

        self.choice: Optional[Tuple[str, Optional[str]]] = None

        layout = QVBoxLayout(self)
        title = QLabel(tr(self.lang, "Choose an option:", "请选择一个选项："))
        title.setFont(QFont("Arial", 12, QFont.Bold))
        layout.addWidget(title)

        row = QHBoxLayout()
        self.btn_new = QPushButton(tr(self.lang, "New Project", "新建项目"))
        self.btn_continue = QPushButton(tr(self.lang, "Continue Project", "继续项目"))
        self.btn_browse = QPushButton(tr(self.lang, "Browse…", "浏览…"))
        row.addWidget(self.btn_new)
        row.addWidget(self.btn_continue)
        row.addWidget(self.btn_browse)
        layout.addLayout(row)

        layout.addWidget(QLabel(tr(self.lang, "Recent projects:", "最近项目：")))
        self.recent_list = QListWidget()
        layout.addWidget(self.recent_list)

        if recent:
            for p in recent[:12]:
                self.recent_list.addItem(QListWidgetItem(p))

        hint = QLabel(tr(self.lang, "Tip: Project folder stores selections + cache so you can resume anytime.", "提示：项目文件夹会保存选择结果和缓存，可随时继续。"))
        hint.setStyleSheet("color:#666;")
        layout.addWidget(hint)

        self.btn_new.clicked.connect(self.on_new)
        self.btn_continue.clicked.connect(self.on_continue)
        self.btn_browse.clicked.connect(self.on_browse)
        self.recent_list.itemDoubleClicked.connect(self.on_recent_double)

    def on_new(self):
        self.choice = ("new", None)
        self.accept()

    def on_continue(self):
        item = self.recent_list.currentItem()
        if item:
            self.choice = ("continue", item.text())
            self.accept()
        else:
            QMessageBox.information(self, tr(self.lang, "Continue", "继续"), tr(self.lang, "Select a recent project or use Browse…", "请选择一个最近项目，或点击“浏览…”。"))

    def on_recent_double(self, item: QListWidgetItem):
        self.choice = ("continue", item.text())
        self.accept()

    def on_browse(self):
        folder = QFileDialog.getExistingDirectory(self, tr(self.lang, "Select project folder", "选择项目文件夹"))
        if folder:
            self.choice = ("browse", folder)
            self.accept()

class NewProjectDialog(QDialog):
    def __init__(self, parent=None, ui_language: str = "en"):
        super().__init__(parent)
        self.lang = ui_language if ui_language in ("en", "zh") else "en"
        self.setWindowTitle(tr(self.lang, "New Project", "新建项目"))
        self.setModal(True)
        self.resize(760, 650)

        self.project_folder: Optional[str] = None
        self.deck_text: str = ""
        self.token_text: str = ""
        self.duplicate_mode: str = "same"

        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText(tr(self.lang, "Choose a project folder…", "选择项目文件夹…"))
        btn_folder = QPushButton(tr(self.lang, "Choose Folder…", "选择文件夹…"))
        row.addWidget(QLabel(tr(self.lang, "Project folder:", "项目文件夹：")))
        row.addWidget(self.folder_edit, 1)
        row.addWidget(btn_folder)
        layout.addLayout(row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel(tr(self.lang, "Duplicate cards:", "重复卡牌：")))
        self.dd_dupes = QComboBox()
        self.dd_dupes.addItems([
            tr(self.lang, "Use same selected printing for all copies", "所有张数使用同一印刷版本"),
            tr(self.lang, "Select each copy separately", "每一张单独选择印刷版本"),
        ])
        mode_row.addWidget(self.dd_dupes, 1)
        layout.addLayout(mode_row)

        layout.addWidget(QLabel(tr(self.lang, "Decklist (paste) OR import from file:", "牌表（粘贴）或从文件导入：")))

        import_row = QHBoxLayout()
        self.btn_import = QPushButton(tr(self.lang, "Import Decklist File…", "导入牌表文件…"))
        import_row.addWidget(self.btn_import)
        import_row.addStretch(1)
        layout.addLayout(import_row)

        self.text = QTextEdit()
        self.text.setPlaceholderText(tr(self.lang, "Paste decklist here (one card per line; quantities ok)…", "在此粘贴牌表（每行一张，支持数量）…"))
        layout.addWidget(self.text, 1)

        layout.addWidget(QLabel(tr(self.lang, "Token queries (optional, one query per line):", "衍生物查询（可选，每行一个查询）：")))
        self.token_box = QTextEdit()
        self.token_box.setPlaceholderText(tr(self.lang, "Examples: cat pow=2 tou=2 OR type:token treasure", "示例：cat pow=2 tou=2 或 type:token treasure"))
        self.token_box.setFixedHeight(120)
        layout.addWidget(self.token_box)

        btns = QHBoxLayout()
        btn_ok = QPushButton(tr(self.lang, "Create Project", "创建项目"))
        btn_cancel = QPushButton(tr(self.lang, "Cancel", "取消"))
        btns.addStretch(1)
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_ok)
        layout.addLayout(btns)

        btn_folder.clicked.connect(self.choose_folder)
        self.btn_import.clicked.connect(self.import_file)
        btn_cancel.clicked.connect(self.reject)
        btn_ok.clicked.connect(self.on_ok)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, tr(self.lang, "Choose project folder", "选择项目文件夹"))
        if folder:
            self.folder_edit.setText(folder)

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr(self.lang, "Import decklist file", "导入牌表文件"), "",
            "Text files (*.txt *.csv);;All files (*.*)"
        )
        if not path:
            return
        txt = Path(path).read_text(encoding="utf-8", errors="ignore")
        self.text.setPlainText(txt)

    def on_ok(self):
        folder = self.folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, tr(self.lang, "Missing folder", "缺少文件夹"), tr(self.lang, "Please choose a project folder.", "请选择项目文件夹。"))
            return

        deck_text = self.text.toPlainText().strip()
        token_text = self.token_box.toPlainText().strip()
        if not deck_text and not token_text:
            QMessageBox.warning(self, tr(self.lang, "Missing input", "缺少输入"), tr(self.lang, "Please paste a decklist and/or token queries.", "请粘贴牌表和/或衍生物查询。"))
            return

        self.project_folder = folder
        self.deck_text = deck_text
        self.token_text = token_text
        self.duplicate_mode = "different" if self.dd_dupes.currentIndex() == 1 else "same"
        self.accept()


class FilterSetupDialog(QDialog):
    def __init__(self, filters: Dict[str, Any], ui_language: str = "en", allow_skip: bool = False, parent=None):
        super().__init__(parent)
        self.ui_language = ui_language if ui_language in ("en", "zh") else "en"
        self.setWindowTitle(tr(self.ui_language, "Project Settings", "项目设置"))
        self.setModal(True)
        self.resize(560, 420)

        self.filters = dict(DEFAULT_FILTERS)
        self.filters.update(filters or {})
        self.use_current = False

        layout = QVBoxLayout(self)

        lang_box = QGroupBox(tr(self.ui_language, "Language", "语言"))
        lang_form = QFormLayout(lang_box)
        self.dd_lang = QComboBox()
        self.dd_lang.addItems(["English", "中文"])
        self.dd_lang.setCurrentIndex(1 if self.ui_language == "zh" else 0)
        lang_form.addRow(tr(self.ui_language, "UI Language:", "界面语言："), self.dd_lang)
        layout.addWidget(lang_box)

        filt_box = QGroupBox(tr(self.ui_language, "Default Filters", "默认筛选"))
        form = QFormLayout(filt_box)

        self.cb_prefer_borderless = QCheckBox(tr(self.ui_language, "Prefer borderless when border=Any", "当边框=任意时优先无边框"))

        self.dd_border = QComboBox()
        self.dd_border.addItems(["any", "borderless", "black", "white", "silver"])
        self.dd_border.setCurrentText(str(self.filters.get("border", "any")).lower())

        self.dd_frame = QComboBox()
        self.dd_frame.addItems(["any", "1993", "1997", "2003", "2015", "future"])
        self.dd_frame.setCurrentText(str(self.filters.get("frame_edition", "any")).lower())

        self.cb_full = QCheckBox(tr(self.ui_language, "Full art", "全图"))
        self.cb_full.setChecked(bool(self.filters.get("is_full", False)))
        self.cb_hires = QCheckBox(tr(self.ui_language, "Hi-res", "高清"))
        self.cb_hires.setChecked(bool(self.filters.get("is_hires", False)))

        form.addRow(tr(self.ui_language, "Border", "边框"), self.dd_border)
        form.addRow(tr(self.ui_language, "Frame", "画框"), self.dd_frame)
        form.addRow("", self.cb_prefer_borderless)
        form.addRow("", self.cb_full)
        form.addRow("", self.cb_hires)
        layout.addWidget(filt_box)

        advanced_hint = QLabel(tr(self.ui_language, "Advanced filters (frame effect/default/atypical/stamp/UB) can be changed later in the main window.", "高级筛选（画框效果/默认/非典型/印章/排除UB）可在主窗口继续调整。"))
        advanced_hint.setWordWrap(True)
        advanced_hint.setStyleSheet("color:#666;")
        layout.addWidget(advanced_hint)

        btns = QHBoxLayout()
        ok = QPushButton(tr(self.ui_language, "Continue", "继续"))
        cancel = QPushButton(tr(self.ui_language, "Cancel", "取消"))
        btns.addStretch(1)
        btns.addWidget(cancel)
        if allow_skip:
            use_current = QPushButton(tr(self.ui_language, "Use Current", "使用当前设置"))
            use_current.clicked.connect(self.on_use_current)
            btns.addWidget(use_current)
        btns.addWidget(ok)
        layout.addLayout(btns)

        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.on_ok)
        self.cb_prefer_borderless.setChecked(bool(self.filters.get("prefer_borderless", False)) and str(self.filters.get("border", "any")).lower() == "any")
        self.dd_border.currentTextChanged.connect(self._update_borderless_enabled)
        self._update_borderless_enabled()

    def _update_borderless_enabled(self):
        enabled = self.dd_border.currentText().lower() == "any"
        self.cb_prefer_borderless.setEnabled(enabled)
        if not enabled:
            self.cb_prefer_borderless.setChecked(False)

    def on_use_current(self):
        self.use_current = True
        self.accept()

    def on_ok(self):
        self.ui_language = "zh" if self.dd_lang.currentIndex() == 1 else "en"
        merged = dict(self.filters)
        merged.update({
            "prefer_borderless": bool(self.cb_prefer_borderless.isChecked()) and self.dd_border.currentText().lower() == "any",
            "border": self.dd_border.currentText().lower(),
            "frame_edition": self.dd_frame.currentText().lower(),
            "is_full": bool(self.cb_full.isChecked()),
            "is_hires": bool(self.cb_hires.isChecked()),
        })
        self.filters = merged
        self.accept()

# ---------------------------- Thumbnail Widget ----------------------------

class ThumbLabel(QLabel):
    clicked = Signal(int)

    def __init__(self, idx: int):
        super().__init__()
        self.idx = idx
        self.loaded = False
        self.token = 0
        self.url: Optional[str] = None

        self.setFixedSize(QSize(92, 128))
        self.setFrameShape(QFrame.Box)
        self.setLineWidth(1)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#111; color:#aaa;")
        self.setText("…")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.idx)

# ---------------------------- Main Window ----------------------------

class MainWindow(QMainWindow):
    def __init__(self, project: Project, settings: QSettings):
        super().__init__()
        self.setWindowTitle(self.t("MTG Art Picker", "万智牌选图器"))
        self.resize(1280, 820)

        self.settings = settings
        self.project = project

        self.meta_by_key: Dict[Tuple[str, str], List[Printing]] = {}
        self._fetching_meta: set[Tuple[str, str]] = set()
        self._all_prints_override: set[str] = set()
        self._auto_relaxed_cards: set[str] = set()

        self.undo_stack: List[Tuple[int, Dict[str, Any], Dict[str, int]]] = []

        self.img_signals = ImageLoaded()
        self.meta_signals = MetaLoaded()
        self.dl_signals = DownloadSignals()

        self.img_signals.bytes_ready.connect(self.on_image_bytes_ready)
        self.img_signals.error.connect(self.on_image_error)
        self.meta_signals.meta_ready.connect(self.on_meta_ready)
        self.meta_signals.error.connect(self.on_meta_error)

        self.dl_signals.progress.connect(self.on_download_progress)
        self.dl_signals.done.connect(self.on_download_done)
        self.dl_signals.error.connect(self.on_download_error)

        self._progress_dialog: Optional[QProgressDialog] = None
        self._download_cancel = threading.Event()
        self._downloading = False

        # Thumb rebuild context + tokens (stale signal protection)
        self.thumb_widgets: List[ThumbLabel] = []
        self._thumb_context: Optional[Tuple[str, str, Tuple[Any, ...]]] = None
        self._thumb_token: int = 0

        self._big_token: int = 0

        # Scroll throttle for lazy thumb loading
        self._thumb_scroll_timer = QTimer(self)
        self._thumb_scroll_timer.setSingleShot(True)
        self._thumb_scroll_timer.timeout.connect(self._load_visible_thumbs)

        # UI
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self.hotkey_bar = QLabel(
            self.t(
                "↑ Prev card   ↓ Select+Next   ←/→ Printings   0 All prints (toggle)   U Undo   ⌫ Clear   D Download   ? Help   (Newest printings first)",
                "↑ 上一张  ↓ 选择并下一张  ←/→ 印刷版本  0 切换全部版本  U 撤销  ⌫ 清除  D 下载  ? 帮助（最新版本优先）"
            )
        )
        self.hotkey_bar.setStyleSheet("background:#222; color:#ddd; padding:6px;")
        root.addWidget(self.hotkey_bar)

        # Filters bar
        self.filters_bar = QWidget()
        fb = QHBoxLayout(self.filters_bar)
        fb.setContentsMargins(6, 6, 6, 6)
        fb.setSpacing(10)

        fb.addWidget(QLabel(self.t("Filters:", "筛选：")))

        self.cb_prefer_borderless = QCheckBox(self.t("Prefer borderless (fallback)", "优先无边框（回退）"))
        self.cb_prefer_borderless.setChecked(bool(self.project.filters.get("prefer_borderless", False)))
        fb.addWidget(self.cb_prefer_borderless)

        fb.addWidget(QLabel(self.t("Border:", "边框：")))
        self.dd_border = QComboBox()
        self.dd_border.addItems(["Any", "Borderless", "Black", "White", "Silver"])
        fb.addWidget(self.dd_border)

        fb.addWidget(QLabel(self.t("Frame:", "画框：")))
        self.dd_frame = QComboBox()
        self.dd_frame.addItems(["Any", "1993", "1997", "2003", "2015", "Future"])
        fb.addWidget(self.dd_frame)

        fb.addWidget(QLabel(self.t("Frame effect:", "画框效果：")))
        self.dd_frame_fx = QComboBox()
        self.dd_frame_fx.addItems(["Any", "legendary", "colorshifted", "tombstone", "enchantment"])
        fb.addWidget(self.dd_frame_fx)

        self.cb_full = QCheckBox(self.t("Full art (is:full)", "全图 (is:full)"))
        self.cb_full.setChecked(bool(self.project.filters.get("is_full", False)))
        fb.addWidget(self.cb_full)

        self.cb_hires = QCheckBox(self.t("Hi-res (is:hires)", "高清 (is:hires)"))
        self.cb_hires.setChecked(bool(self.project.filters.get("is_hires", False)))
        fb.addWidget(self.cb_hires)

        self.cb_default = QCheckBox(self.t("Default (is:default)", "默认 (is:default)"))
        self.cb_default.setChecked(bool(self.project.filters.get("is_default", False)))
        fb.addWidget(self.cb_default)

        self.cb_atypical = QCheckBox(self.t("Atypical (is:atypical)", "非典型 (is:atypical)"))
        self.cb_atypical.setChecked(bool(self.project.filters.get("is_atypical", False)))
        fb.addWidget(self.cb_atypical)

        self.cb_ex_ub = QCheckBox(self.t("Exclude UB (not:universesbeyond)", "排除UB (not:universesbeyond)"))
        self.cb_ex_ub.setChecked(bool(self.project.filters.get("exclude_ub", False)))
        fb.addWidget(self.cb_ex_ub)

        fb.addWidget(QLabel(self.t("Stamp:", "印章：")))
        self.dd_stamp = QComboBox()
        self.dd_stamp.addItems(["Any", "oval", "acorn", "triangle", "arena"])
        fb.addWidget(self.dd_stamp)

        fb.addStretch(1)

        self.filters_bar.setStyleSheet("background:#f3f6fb; color:#222; border:1px solid #ccd5e1; border-radius:8px;")
        root.addWidget(self.filters_bar)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("font-weight:600;")
        left_layout.addWidget(self.progress_label)
        self.deck_list = QListWidget()
        left_layout.addWidget(self.deck_list, 1)

        btn_row = QHBoxLayout()
        self.btn_download = QPushButton(self.t("Download (D)", "下载 (D)"))
        self.btn_help = QPushButton(self.t("Help (?)", "帮助 (?)"))
        btn_row.addWidget(self.btn_download)
        btn_row.addWidget(self.btn_help)
        left_layout.addLayout(btn_row)

        splitter.addWidget(left)
        splitter.setStretchFactor(0, 1)

        main = QWidget()
        main_layout = QVBoxLayout(main)

        self.card_title = QLabel("")
        self.card_title.setFont(QFont("Arial", 16, QFont.Bold))
        main_layout.addWidget(self.card_title)

        self.card_info = QLabel("")
        self.card_info.setStyleSheet("color:#666;")
        main_layout.addWidget(self.card_info)

        self.big_image = QLabel(self.t("Loading…", "加载中…"))
        self.big_image.setAlignment(Qt.AlignCenter)
        self.big_image.setMinimumHeight(440)
        self.big_image.setStyleSheet("background:#0d0d0d; border:1px solid #333; color:#888;")
        main_layout.addWidget(self.big_image, 1)

        self.thumb_area = QScrollArea()
        self.thumb_area.setWidgetResizable(True)
        self.thumb_area.setFixedHeight(178)
        self.thumb_container = QWidget()
        self.thumb_layout = QHBoxLayout(self.thumb_container)
        self.thumb_layout.setContentsMargins(8, 8, 8, 8)
        self.thumb_layout.setSpacing(6)
        self.thumb_area.setWidget(self.thumb_container)
        main_layout.addWidget(self.thumb_area)

        splitter.addWidget(main)
        splitter.setStretchFactor(1, 3)

        self.thumb_area.horizontalScrollBar().valueChanged.connect(lambda _: self._thumb_scroll_timer.start(25))

        self._wire_events()
        self._load_filters_into_ui()

        self.refresh_deck_list()
        if self.project.deck:
            self.goto_index(self.project.current_index, force=True)
        else:
            self.update_progress()
            self.card_title.setText(self.t("No deck loaded", "未加载牌表"))
            self.card_info.setText(self.t("Create a new project or continue an existing one.", "请创建新项目或继续已有项目。"))
            self.big_image.setText("")

    # ---------------- i18n / query helpers ----------------

    def t(self, en: str, zh: str) -> str:
        return zh if getattr(self.project, "ui_language", "en") == "zh" else en

    def query_for_card(self, card: str) -> str:
        return self.project.card_query.get(card, card)

    def is_exact_name(self, card: str) -> bool:
        q = self.query_for_card(card)
        return not q.lower().startswith("type:token")

    # ---------------- Filters helpers ----------------

    def current_sig(self) -> str:
        return filters_signature(self.project.filters or {})

    def effective_sig_for_card(self, card: str) -> str:
        return "ALL" if card in self._all_prints_override else self.current_sig()

    def effective_filters_for_card(self, card: str) -> Dict[str, Any]:
        if card in self._all_prints_override:
            return {}
        return dict(self.project.filters or dict(DEFAULT_FILTERS))

    def _load_filters_into_ui(self):
        f = self.project.filters or dict(DEFAULT_FILTERS)

        border_map = {"any": "Any", "borderless": "Borderless", "black": "Black", "white": "White", "silver": "Silver"}
        b = (f.get("border") or "any").lower()
        self.dd_border.setCurrentText(border_map.get(b, "Any"))

        fe = (f.get("frame_edition") or "any").lower()
        if fe == "any":
            self.dd_frame.setCurrentText("Any")
        elif fe == "future":
            self.dd_frame.setCurrentText("Future")
        else:
            self.dd_frame.setCurrentText(fe)

        fx = (f.get("frame_effect") or "any").lower()
        self.dd_frame_fx.setCurrentText("Any" if fx == "any" else fx)

        st = (f.get("stamp") or "any").lower()
        self.dd_stamp.setCurrentText("Any" if st == "any" else st)

        self.cb_prefer_borderless.setChecked(bool(f.get("prefer_borderless", False)))
        self.cb_full.setChecked(bool(f.get("is_full", False)))
        self.cb_hires.setChecked(bool(f.get("is_hires", False)))
        self.cb_default.setChecked(bool(f.get("is_default", False)))
        self.cb_atypical.setChecked(bool(f.get("is_atypical", False)))
        self.cb_ex_ub.setChecked(bool(f.get("exclude_ub", False)))

        self._update_prefer_borderless_enabled()

    def _update_prefer_borderless_enabled(self):
        self.cb_prefer_borderless.setEnabled(self.dd_border.currentText().lower() == "any")

    def _read_filters_from_ui(self) -> Dict[str, Any]:
        border_rev = {"Any": "any", "Borderless": "borderless", "Black": "black", "White": "white", "Silver": "silver"}
        frame_text = self.dd_frame.currentText()
        frame_edition = "any" if frame_text == "Any" else frame_text.lower()

        fx_text = self.dd_frame_fx.currentText()
        frame_effect = "any" if fx_text == "Any" else fx_text

        stamp_text = self.dd_stamp.currentText()
        stamp = "any" if stamp_text == "Any" else stamp_text

        border = border_rev.get(self.dd_border.currentText(), "any")
        prefer_borderless = bool(self.cb_prefer_borderless.isChecked()) and (border == "any")

        return {
            "prefer_borderless": prefer_borderless,
            "border": border,
            "frame_edition": frame_edition,
            "frame_effect": frame_effect,
            "is_full": bool(self.cb_full.isChecked()),
            "is_hires": bool(self.cb_hires.isChecked()),
            "is_default": bool(self.cb_default.isChecked()),
            "is_atypical": bool(self.cb_atypical.isChecked()),
            "exclude_ub": bool(self.cb_ex_ub.isChecked()),
            "stamp": stamp,
        }

    def _apply_filters_if_changed(self):
        if not self.project.deck:
            self._update_prefer_borderless_enabled()
            return

        self._update_prefer_borderless_enabled()
        new_filters = self._read_filters_from_ui()
        if new_filters == (self.project.filters or {}):
            return

        self.project.filters = new_filters
        self.project.save()
        logging.info(f"Filters changed -> sig={self.current_sig()} filters={new_filters}")

        self._thumb_context = None

        card = self.current_card()
        self.ensure_meta(card)
        self.refresh_card_ui(card)

    # ---------------- Events / Hotkeys ----------------

    def _wire_events(self):
        self.deck_list.currentRowChanged.connect(self.on_list_row_changed)
        self.btn_help.clicked.connect(self.show_help)
        self.btn_download.clicked.connect(self.download_prompt)

        self.cb_prefer_borderless.stateChanged.connect(lambda _: self._apply_filters_if_changed())
        self.dd_border.currentIndexChanged.connect(lambda _: self._apply_filters_if_changed())
        self.dd_frame.currentIndexChanged.connect(lambda _: self._apply_filters_if_changed())
        self.dd_frame_fx.currentIndexChanged.connect(lambda _: self._apply_filters_if_changed())
        self.cb_full.stateChanged.connect(lambda _: self._apply_filters_if_changed())
        self.cb_hires.stateChanged.connect(lambda _: self._apply_filters_if_changed())
        self.cb_default.stateChanged.connect(lambda _: self._apply_filters_if_changed())
        self.cb_atypical.stateChanged.connect(lambda _: self._apply_filters_if_changed())
        self.cb_ex_ub.stateChanged.connect(lambda _: self._apply_filters_if_changed())
        self.dd_stamp.currentIndexChanged.connect(lambda _: self._apply_filters_if_changed())

        self.addAction(self._mk_action("Left", lambda: self.shift_printing(-1)))
        self.addAction(self._mk_action("Right", lambda: self.shift_printing(+1)))
        self.addAction(self._mk_action("Up", lambda: self.goto_index(self.project.current_index - 1)))
        self.addAction(self._mk_action("Down", self.select_and_next))
        self.addAction(self._mk_action("Backspace", self.clear_selection))
        self.addAction(self._mk_action("U", self.undo))
        self.addAction(self._mk_action("D", self.download_prompt))
        self.addAction(self._mk_action("G", self.go_to_card_number))
        self.addAction(self._mk_action("O", self.open_scryfall))
        self.addAction(self._mk_action("0", self.toggle_all_prints_for_current))
        self.addAction(self._mk_action("?", self.show_help))

    def _mk_action(self, key: str, fn):
        act = QAction(self)
        act.setShortcut(QKeySequence(key))
        act.triggered.connect(fn)
        return act

    # ---------------- Deck UI ----------------

    def refresh_deck_list(self):
        self.deck_list.clear()
        for card in self.project.deck:
            self.deck_list.addItem(QListWidgetItem(self.format_card_row(card)))
        if self.project.deck:
            self.deck_list.setCurrentRow(max(0, min(self.project.current_index, len(self.project.deck) - 1)))

    def format_card_row(self, card: str) -> str:
        qty = int(self.project.card_qty.get(card, 1))
        has_copy_suffix = bool(re.search(r"\[\d+/\d+\]$", card))
        qty_txt = f" x{qty}" if qty > 1 and not has_copy_suffix else ""
        sel = self.project.selections.get(card)
        if sel:
            return f"✅ {card}{qty_txt} [{sel.get('set','')} {sel.get('collector','')}]"
        return f"⬜ {card}{qty_txt}"

    def update_row_text(self, idx: int):
        if 0 <= idx < self.deck_list.count():
            card = self.project.deck[idx]
            self.deck_list.item(idx).setText(self.format_card_row(card))

    def update_progress(self):
        total = len(self.project.deck)
        selected = len(self.project.selections)
        self.progress_label.setText(
            self.t(f"Card {self.project.current_index + 1} / {total}    Selected {selected} / {total}", f"卡牌 {self.project.current_index + 1} / {total}    已选择 {selected} / {total}")
            if total else ""
        )

    def on_list_row_changed(self, row: int):
        if row >= 0 and row != self.project.current_index:
            self.goto_index(row)

    # ---------------- Navigation ----------------

    def goto_index(self, idx: int, force: bool = False):
        if not self.project.deck:
            return

        idx = max(0, min(idx, len(self.project.deck) - 1))
        if not force and idx == self.project.current_index:
            return

        self.project.current_index = idx
        self.project.save()

        self.update_progress()
        self.deck_list.blockSignals(True)
        self.deck_list.setCurrentRow(idx)
        self.deck_list.blockSignals(False)

        card = self.current_card()
        title = card + (self.t("  (ALL PRINTS)", "  （全部印刷）") if card in self._all_prints_override else "")
        self.card_title.setText(title)

        self._thumb_context = None

        self.ensure_meta(card)
        self.preload_next(idx)

    def current_card(self) -> str:
        return self.project.deck[self.project.current_index]

    def active_printing_idx(self, card: str) -> int:
        return int(self.project.active_printing_index.get(card, 0))

    def set_active_printing_idx(self, card: str, idx: int):
        self.project.active_printing_index[card] = int(idx)
        self.project.save()

    # ---------------- Meta ----------------

    def ensure_meta(self, card: str):
        sig = self.effective_sig_for_card(card)
        key = (card, sig)

        if key in self.meta_by_key:
            self.refresh_card_ui(card)
            return

        cached = self.project.get_cached_meta(card, sig)
        if cached is not None:
            self.meta_by_key[key] = cached
            self.refresh_card_ui(card)
            return

        if key in self._fetching_meta:
            return
        self._fetching_meta.add(key)

        filters_copy = self.effective_filters_for_card(card)
        query = self.query_for_card(card)
        exact_name = self.is_exact_name(card)
        logging.debug(f"Fetching printings for card={card} query={query} sig={sig}")

        def worker():
            try:
                prints = fetch_all_printings(query, filters_copy, exact_name=exact_name)
                auto_relaxed = False
                emit_sig = sig
                if filters_copy and 0 < len(prints) < 5 and card not in self._all_prints_override:
                    fallback = fetch_all_printings(query, {}, exact_name=exact_name)
                    if fallback:
                        prints = fallback
                        emit_sig = "ALL"
                        auto_relaxed = True
                self.project.set_cached_meta(card, emit_sig, prints)
                self.meta_signals.meta_ready.emit(card, prints, emit_sig, auto_relaxed)
            except Exception as e:
                self.meta_signals.error.emit(card, str(e), sig)

        _meta_executor.submit(worker)

    def on_meta_ready(self, card: str, prints: list, sig: str, auto_relaxed: bool):
        logging.debug(f"Meta ready for card={card} sig={sig} count={len(prints)}")
        if auto_relaxed:
            self._all_prints_override.add(card)
            self._auto_relaxed_cards.add(card)
        if sig != self.effective_sig_for_card(card):
            self._fetching_meta.discard((card, sig))
            return
        self._fetching_meta.discard((card, sig))
        self.meta_by_key[(card, sig)] = prints
        if self.project.deck and card == self.current_card():
            self._thumb_context = None
            self.refresh_card_ui(card)

    def on_meta_error(self, card: str, msg: str, sig: str):
        logging.error(f"Meta error for card={card} sig={sig}: {msg}")
        self._fetching_meta.discard((card, sig))
        if sig != self.effective_sig_for_card(card):
            return
        if self.project.deck and card == self.current_card():
            self.big_image.setText(self.t("Metadata load failed:\n", "元数据加载失败：\n") + msg)

    # ---------------- Images (SAFE) ----------------

    def load_image_bytes_cached(self, kind: str, card: str, sig: str, idx: int, url: str, key: str):
        if not url:
            return

        cached = self.project.get_cached_image_bytes(kind, card, sig, idx)
        if cached:
            self.img_signals.bytes_ready.emit(key, cached)
            return

        def job():
            try:
                data = http_get_bytes(url, timeout=60)
                self.project.set_cached_image_bytes(kind, card, sig, idx, data)
                self.img_signals.bytes_ready.emit(key, data)
            except Exception as e:
                logging.error(f"Image fetch failed key={key}: {e}", exc_info=True)
                self.img_signals.error.emit(key, str(e))

        _image_executor.submit(job)

    def prefetch_to_cache(self, kind: str, card: str, sig: str, idx: int, url: str, timeout: int):
        """Preload helper: fills disk cache only, no signals/UI work."""
        if not url:
            return
        if self.project.get_cached_image_bytes(kind, card, sig, idx) is not None:
            return

        def job():
            try:
                data = http_get_bytes(url, timeout=timeout)
                self.project.set_cached_image_bytes(kind, card, sig, idx, data)
            except Exception:
                pass

        _image_executor.submit(job)

    def on_image_bytes_ready(self, key: str, data: bytes):
        # Key formats:
        #  big::card::sig::bigtoken::aidx
        #  thumb::card::sig::thumbtoken::idx
        if key.startswith("big::"):
            parts = key.split("::")
            if len(parts) != 5:
                return
            _, card, sig, tok_s, _ = parts
            try:
                tok = int(tok_s)
            except Exception:
                return

            if (not self.project.deck) or (card != self.current_card()) or (sig != self.effective_sig_for_card(card)):
                return
            if tok != self._big_token:
                return
            if not isValid(self.big_image):
                return

            pm = QPixmap()
            if not pm.loadFromData(data) or pm.isNull():
                self.on_image_error(key, "Failed to decode image data.")
                return
            self.big_image.setPixmap(pm.scaled(self.big_image.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            return

        if key.startswith("thumb::"):
            parts = key.split("::")
            if len(parts) != 5:
                return
            _, card, sig, tok_s, idx_s = parts
            try:
                tok = int(tok_s)
                idx = int(idx_s)
            except Exception:
                return

            if (not self.project.deck) or (card != self.current_card()) or (sig != self.effective_sig_for_card(card)):
                return
            if tok != self._thumb_token:
                return
            if not (0 <= idx < len(self.thumb_widgets)):
                return

            w = self.thumb_widgets[idx]
            if (w is None) or (not isValid(w)) or (getattr(w, "token", None) != tok):
                return

            pm = QPixmap()
            if not pm.loadFromData(data) or pm.isNull():
                self.on_image_error(key, "Failed to decode image data.")
                return

            w.setPixmap(pm.scaled(w.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            w.loaded = True
            return

    def on_image_error(self, key: str, msg: str):
        if key.startswith("big::"):
            parts = key.split("::")
            if len(parts) != 5:
                return
            _, card, sig, tok_s, _ = parts
            try:
                tok = int(tok_s)
            except Exception:
                return
            if not self.project.deck or card != self.current_card() or sig != self.effective_sig_for_card(card):
                return
            if tok != self._big_token:
                return
            if not isValid(self.big_image):
                return
            self.big_image.setText(self.t("Image load failed:\n", "图像加载失败：\n") + msg)
            return

        if key.startswith("thumb::"):
            parts = key.split("::")
            if len(parts) != 5:
                return
            _, card, sig, tok_s, idx_s = parts
            try:
                tok = int(tok_s)
                idx = int(idx_s)
            except Exception:
                return
            if not self.project.deck or card != self.current_card() or sig != self.effective_sig_for_card(card):
                return
            if tok != self._thumb_token:
                return
            if 0 <= idx < len(self.thumb_widgets):
                w = self.thumb_widgets[idx]
                if w and isValid(w):
                    w.setText("ERR")
                    w.setToolTip(msg)

    # ---------------- Render ----------------

    def _prints_fingerprint(self, prints: List[Printing]) -> Tuple[Any, ...]:
        if not prints:
            return (0,)
        first = prints[0]
        last = prints[-1]
        return (len(prints), first.set_code, first.collector_number, last.set_code, last.collector_number)

    def refresh_card_ui(self, card: str):
        sig = self.effective_sig_for_card(card)
        prints = self.meta_by_key.get((card, sig), [])
        if not prints:
            self.card_info.setText(self.t("No printings found (with current filters). Try loosening filters.", "未找到符合当前筛选的印刷版本。请放宽筛选条件。"))
            self.big_image.setText(self.t("No printings found.", "未找到印刷版本。"))
            self.clear_thumbnails()
            return

        aidx = max(0, min(self.active_printing_idx(card), len(prints) - 1))
        self.set_active_printing_idx(card, aidx)
        p = prints[aidx]

        sel = self.project.selections.get(card)
        sel_txt = ""
        missing_hint = ""
        if sel:
            sel_txt = self.t(" | Selected: ", " | 已选择：") + f"{sel.get('set','')} #{sel.get('collector','')}"
            sel_key = (sel.get("set"), str(sel.get("collector")))
            visible = any((pr.set_code, pr.collector_number) == sel_key for pr in prints)
            if not visible:
                missing_hint = self.t("  [!] Selected printing hidden by filters", "  [!] 所选印刷版本被筛选隐藏")

        mode_hint = self.t(" | Mode: ALL PRINTS", " | 模式：全部印刷") if card in self._all_prints_override else ""
        relax_hint = self.t(" | Filter auto-ignored (<5 results)", " | 筛选自动忽略（结果<5）") if card in self._auto_relaxed_cards else ""
        self.card_info.setText(
            f"{p.set_name} | {p.set_code} #{p.collector_number} | {self.t('Released', '发行')} {p.released_at} | "
            f"{self.t('Printing', '印刷')} {aidx+1}/{len(prints)}{sel_txt}{missing_hint}{mode_hint}{relax_hint}"
        )

        # big preview token bump
        self._big_token += 1
        self.big_image.setText(self.t("Loading preview…", "加载预览中…"))
        self.big_image.setPixmap(QPixmap())
        self.load_image_bytes_cached("normal", card, sig, aidx, p.image_normal, f"big::{card}::{sig}::{self._big_token}::{aidx}")

        # Thumbs: rebuild only when card/sig/prints changed.
        fp = self._prints_fingerprint(prints)
        ctx = (card, sig, fp)
        if self._thumb_context != ctx:
            self.build_thumbnails(card, sig, prints)
            self._thumb_context = ctx

        self.highlight_thumbnails(card, prints)
        self.center_active_thumbnail(aidx)
        self._load_visible_thumbs()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.project.deck:
            return
        card = self.current_card()
        sig = self.effective_sig_for_card(card)
        prints = self.meta_by_key.get((card, sig), [])
        if prints:
            aidx = self.active_printing_idx(card)
            cached = self.project.get_cached_image_bytes("normal", card, sig, aidx)
            if cached:
                pm = QPixmap()
                if pm.loadFromData(cached) and not pm.isNull() and isValid(self.big_image):
                    self.big_image.setPixmap(pm.scaled(self.big_image.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def clear_thumbnails(self):
        self.thumb_widgets = []
        self._thumb_token += 1  # invalidate pending thumb signals

        while self.thumb_layout.count():
            item = self.thumb_layout.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None and isValid(w):
                w.deleteLater()

    def build_thumbnails(self, card: str, sig: str, prints: List[Printing]):
        self.clear_thumbnails()

        self._thumb_token += 1
        tok = self._thumb_token

        for i, pr in enumerate(prints):
            lbl = ThumbLabel(i)
            lbl.token = tok
            lbl.url = pr.image_small
            lbl.clicked.connect(self.on_thumb_clicked)
            self.thumb_layout.addWidget(lbl)
            self.thumb_widgets.append(lbl)

        self.thumb_layout.addStretch(1)

    def _load_visible_thumbs(self):
        if not self.project.deck:
            return
        card = self.current_card()
        sig = self.effective_sig_for_card(card)
        prints = self.meta_by_key.get((card, sig), [])
        if not prints or not self.thumb_widgets:
            return

        bar = self.thumb_area.horizontalScrollBar()
        viewport_w = self.thumb_area.viewport().width()
        start_px = bar.value()
        end_px = start_px + viewport_w

        step = self.thumb_widgets[0].width() + self.thumb_layout.spacing()
        if step <= 0:
            step = 98

        start_idx = max(0, int(start_px // step) - THUMB_LOAD_BUFFER)
        end_idx = min(len(self.thumb_widgets), int(end_px // step) + THUMB_LOAD_BUFFER + 1)

        tok = self._thumb_token

        for i in range(start_idx, end_idx):
            w = self.thumb_widgets[i]
            if (w is None) or (not isValid(w)) or (w.token != tok) or w.loaded:
                continue
            url = w.url
            if not url:
                continue
            self.load_image_bytes_cached("small", card, sig, i, url, f"thumb::{card}::{sig}::{tok}::{i}")

    def highlight_thumbnails(self, card: str, prints: List[Printing]):
        if not prints:
            return

        active = self.active_printing_idx(card)
        sel = self.project.selections.get(card)
        selected_sig = (sel.get("set"), str(sel.get("collector"))) if sel else None

        for i, w in enumerate(self.thumb_widgets):
            if not w or not isValid(w):
                continue
            base = "background:#111;"
            base += "border:3px solid #4da3ff;" if i == active else "border:1px solid #333;"
            if selected_sig:
                p = prints[i]
                if (p.set_code, p.collector_number) == selected_sig:
                    base += "border:3px solid #34c759;"
            w.setStyleSheet(base)

    def center_active_thumbnail(self, active_idx: int):
        if active_idx < 0 or active_idx >= len(self.thumb_widgets):
            return
        w = self.thumb_widgets[active_idx]
        if not w or not isValid(w):
            return
        viewport = self.thumb_area.viewport()
        bar = self.thumb_area.horizontalScrollBar()
        x = w.pos().x()
        w_center = x + w.width() // 2
        target = w_center - viewport.width() // 2
        bar.setValue(max(0, target))

    # ---------------- Actions ----------------

    def shift_printing(self, delta: int):
        card = self.current_card()
        sig = self.effective_sig_for_card(card)
        prints = self.meta_by_key.get((card, sig), [])
        if not prints:
            return

        aidx = self.active_printing_idx(card)
        nidx = max(0, min(aidx + delta, len(prints) - 1))
        if nidx == aidx:
            return

        self.set_active_printing_idx(card, nidx)
        self.refresh_card_ui(card)

    def on_thumb_clicked(self, idx: int):
        card = self.current_card()
        sig = self.effective_sig_for_card(card)
        prints = self.meta_by_key.get((card, sig), [])
        if not (0 <= idx < len(prints)):
            return
        self.set_active_printing_idx(card, idx)
        self.refresh_card_ui(card)

    def toggle_all_prints_for_current(self):
        if not self.project.deck:
            return
        card = self.current_card()

        if card in self._all_prints_override:
            self._all_prints_override.remove(card)
            self._auto_relaxed_cards.discard(card)
        else:
            self._all_prints_override.add(card)

        self._thumb_context = None

        title = card + (self.t("  (ALL PRINTS)", "  （全部印刷）") if card in self._all_prints_override else "")
        self.card_title.setText(title)

        self.ensure_meta(card)
        self.refresh_card_ui(card)

    def snapshot_selections(self) -> Dict[str, Any]:
        return dict(self.project.selections)

    def snapshot_active_printing(self) -> Dict[str, int]:
        return dict(self.project.active_printing_index)

    def _push_undo(self):
        snap = (self.project.current_index, self.snapshot_selections(), self.snapshot_active_printing())
        if self.undo_stack and self.undo_stack[-1] == snap:
            return
        self.undo_stack.append(snap)
        if len(self.undo_stack) > UNDO_STACK_LIMIT:
            self.undo_stack = self.undo_stack[-UNDO_STACK_LIMIT:]

    def select_current_printing(self, advance: bool):
        card = self.current_card()
        sig = self.effective_sig_for_card(card)
        prints = self.meta_by_key.get((card, sig), [])
        if not prints:
            return
        aidx = self.active_printing_idx(card)
        p = prints[aidx]

        self._push_undo()

        self.project.selections[card] = {
            "set": p.set_code,
            "collector": p.collector_number,
            "set_name": p.set_name,
            "released_at": p.released_at,
            "scryfall_uri": p.scryfall_uri,
            "png_url": p.image_png,
            "large_url": p.image_large,
        }
        self.project.save()
        self.update_row_text(self.project.current_index)
        self.update_progress()
        self.refresh_card_ui(card)

        if advance:
            self.goto_index(self.project.current_index + 1)

    def select_and_next(self):
        self.select_current_printing(advance=True)

    def clear_selection(self):
        card = self.current_card()
        if card in self.project.selections:
            self._push_undo()
            del self.project.selections[card]
            self.project.save()
            self.update_row_text(self.project.current_index)
            self.update_progress()
            self.refresh_card_ui(card)

    def undo(self):
        if not self.undo_stack:
            return
        prev_idx, prev_sel, prev_active = self.undo_stack.pop()
        self.project.selections = prev_sel
        self.project.active_printing_index = prev_active
        self.project.current_index = prev_idx
        self.project.save()
        self.refresh_deck_list()
        self.goto_index(prev_idx, force=True)

    def show_help(self):
        QMessageBox.information(
            self, self.t("Hotkeys", "快捷键"),
            "\n".join([
                self.t("↑ / ↓    Prev / Next card (↓ overwrites selection + advances)", "↑ / ↓    上一张 / 下一张（↓会覆盖并前进）"),
                self.t("← / →    Prev / Next printing", "← / →    上一个 / 下一个印刷版本"),
                self.t("0        Toggle ALL prints for THIS card only (does not change global filters)", "0        仅切换当前卡牌的全部印刷（不改全局筛选）"),
                self.t("U        Undo last selection change", "U        撤销上一次选择"),
                self.t("Backspace Clear selection", "Backspace 清除选择"),
                self.t("D        Download (warns if not all selected; downloads selected only)", "D        下载（未全选会提示；仅下载已选）"),
                self.t("G        Go to card number", "G        跳转到卡牌编号"),
                self.t("O        Open in browser", "O        在浏览器打开"),
                self.t("?        Help", "?        帮助"),
                "",
                self.t("Tip: Printings are displayed newest-first (released desc).", "提示：印刷版本按最新优先显示（发行日期降序）。"),
                self.t("Tip: Filters are saved per project and affect the printing list.", "提示：筛选会按项目保存，并影响印刷列表。"),
                self.t("Tip: Downloads always go to <project>/DOWNLOADED IMAGES HERE.", "提示：下载文件固定保存到 <项目>/DOWNLOADED IMAGES HERE。"),
            ])
        )

    def go_to_card_number(self):
        if not self.project.deck:
            return
        n, ok = QInputDialog.getInt(
            self, self.t("Go to card", "跳转卡牌"), self.t("Card number:", "卡牌编号："),
            self.project.current_index + 1, 1, len(self.project.deck), 1
        )
        if ok:
            self.goto_index(n - 1)

    def open_scryfall(self):
        import webbrowser
        card = self.current_card()
        sel = self.project.selections.get(card)
        if sel and sel.get("scryfall_uri"):
            webbrowser.open(sel["scryfall_uri"])
            return
        sig = self.effective_sig_for_card(card)
        prints = self.meta_by_key.get((card, sig), [])
        if prints:
            webbrowser.open(prints[self.active_printing_idx(card)].scryfall_uri)

    # ---------------- Preload (bounded + cache-only) ----------------

    def preload_next(self, idx: int):
        nxt = idx + 1
        if nxt >= len(self.project.deck):
            return

        card = self.project.deck[nxt]
        sig = self.effective_sig_for_card(card)

        # Only use disk cache here; never spawn unbounded work.
        # (If meta isn't cached yet, we simply skip preloading.)
        def worker():
            try:
                prints = self.project.get_cached_meta(card, sig)
                if not prints:
                    return

                for i, pr in enumerate(prints[:PRELOAD_NEXT_THUMBS]):
                    self.prefetch_to_cache("small", card, sig, i, pr.image_small, timeout=25)

                for i, pr in enumerate(prints[:PRELOAD_NEXT_BIG]):
                    self.prefetch_to_cache("normal", card, sig, i, pr.image_normal, timeout=40)

            except Exception:
                pass

        _preload_executor.submit(worker)

    # ---------------- Download ----------------

    def download_prompt(self):
        if self._downloading:
            QMessageBox.information(self, self.t("Download", "下载"), self.t("A download is already in progress.", "已有下载任务正在进行。"))
            return

        total = len(self.project.deck)
        selected = len(self.project.selections)
        if total == 0:
            return

        if selected != total:
            missing = total - selected
            resp = QMessageBox.question(
                self, self.t("Not fully selected", "未完全选择"),
                self.t(f"You selected {selected} / {total} cards.\n{missing} cards have no selection.\n\nDownload selected cards and ignore the rest?", f"你已选择 {selected} / {total} 张卡。\n还有 {missing} 张未选择。\n\n是否仅下载已选择的卡牌并忽略其余？"),
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel
            )
            if resp != QMessageBox.Yes:
                return

        out_dir = self.project.folder / "DOWNLOADED IMAGES HERE"
        logging.info(f"Download destination: {out_dir}")

        items = list(self.project.selections.items())
        if not items:
            QMessageBox.information(self, self.t("Download", "下载"), self.t("No selected cards to download.", "没有可下载的已选卡牌。"))
            return

        logging.info("Download started.")
        self._downloading = True
        self.btn_download.setEnabled(False)
        self._download_cancel.clear()

        prog = QProgressDialog(self.t(f"Downloading selected cards to:\n{out_dir}", f"正在下载到：\n{out_dir}"), self.t("Cancel", "取消"), 0, len(items), self)
        prog.setWindowModality(Qt.WindowModal)
        prog.canceled.connect(lambda: self._download_cancel.set())
        prog.show()
        self._progress_dialog = prog

        def worker():
            try:
                outp = Path(out_dir)
                outp.mkdir(parents=True, exist_ok=True)

                sess = get_session()

                for i, (card, sel) in enumerate(items, start=1):
                    if self._download_cancel.is_set():
                        break

                    url = sel.get("png_url") or sel.get("large_url")
                    if not url:
                        self.dl_signals.progress.emit(i)
                        continue

                    ext = ".png" if sel.get("png_url") else ".jpg"
                    qty = int(self.project.card_qty.get(card, 1))
                    for copy_idx in range(1, qty + 1):
                        suffix = f" ({copy_idx})" if qty > 1 else ""
                        fname = safe_filename(f"{card}{suffix} [{sel.get('set','')} {sel.get('collector','')}]") + ext
                        dest = outp / fname
                        if dest.exists():
                            continue

                        r = sess.get(url, stream=True, timeout=90)
                        r.raise_for_status()
                        with open(dest, "wb") as f:
                            for chunk in r.iter_content(chunk_size=1024 * 256):
                                if chunk:
                                    f.write(chunk)

                    self.dl_signals.progress.emit(i)

                self.dl_signals.done.emit(self.t(f"Download finished. Files saved to:\n{outp}", f"下载完成。文件已保存到：\n{outp}"))
            except Exception as e:
                self.dl_signals.error.emit(str(e))

        _download_executor.submit(worker)

    def on_download_progress(self, value: int):
        if self._progress_dialog:
            self._progress_dialog.setValue(value)

    def on_download_done(self, msg: str):
        if self._progress_dialog:
            self._progress_dialog.close()
            self._progress_dialog = None
        self._downloading = False
        self.btn_download.setEnabled(True)
        logging.info("Download finished.")
        if not self._download_cancel.is_set():
            QMessageBox.information(self, self.t("Download", "下载"), msg)

    def on_download_error(self, msg: str):
        if self._progress_dialog:
            self._progress_dialog.close()
            self._progress_dialog = None
        self._downloading = False
        self.btn_download.setEnabled(True)
        logging.error(f"Download failed: {msg}")
        QMessageBox.critical(self, self.t("Download failed", "下载失败"), msg)

# ---------------------------- App Bootstrap ----------------------------

def update_recent_projects(settings: QSettings, folder: str):
    rec = settings.value("recent_projects", [])
    if not isinstance(rec, list):
        rec = []
    rec = [p for p in rec if p != folder]
    rec.insert(0, folder)
    settings.setValue("recent_projects", rec[:15])

def main():
    app = QApplication([])
    settings = QSettings("LocalTools", "MTGArtPicker")
    ui_language = str(settings.value("ui_language", "en"))

    recent = settings.value("recent_projects", [])
    if not isinstance(recent, list):
        recent = []

    start = StartDialog(recent=recent, ui_language=ui_language)
    if start.exec() != QDialog.Accepted or not start.choice:
        return

    mode, path = start.choice

    if mode == "new":
        dlg = NewProjectDialog(ui_language=ui_language)
        if dlg.exec() != QDialog.Accepted or not dlg.project_folder:
            return
        pr = Project(Path(dlg.project_folder))

        deck_entries: List[str] = []
        card_query: Dict[str, str] = {}
        card_qty: Dict[str, int] = {}

        for qty, name in parse_deck_quantities(dlg.deck_text):
            if dlg.duplicate_mode == "different":
                for i in range(1, qty + 1):
                    key = f"{name} [{i}/{qty}]" if qty > 1 else name
                    deck_entries.append(key)
                    card_query[key] = name
                    card_qty[key] = 1
            else:
                deck_entries.append(name)
                card_query[name] = name
                card_qty[name] = card_qty.get(name, 0) + qty

        for i, token_q in enumerate(parse_token_queries(dlg.token_text), start=1):
            key = f"Token Query {i}: {token_q}"
            deck_entries.append(key)
            card_query[key] = token_q
            card_qty[key] = 1

        if not deck_entries:
            QMessageBox.warning(None, tr(ui_language, "Decklist", "牌表"), tr(ui_language, "Could not parse any card names or token queries.", "无法解析任何卡名或衍生物查询。"))
            return

        pr.deck = deck_entries
        pr.card_query = card_query
        pr.card_qty = card_qty
        pr.current_index = 0
        pr.active_printing_index = {}
        pr.selections = {}
        pr.filters = dict(DEFAULT_FILTERS)

    elif mode in ("continue", "browse"):
        if not path:
            return
        pr = Project(Path(path))
        pr.load()
        if not pr.deck:
            QMessageBox.warning(None, tr(ui_language, "Project", "项目"), tr(ui_language, "This project has no deck. Create a new project instead.", "该项目没有牌表。请改为创建新项目。"))
            return
    else:
        return

    pre = FilterSetupDialog(pr.filters, pr.ui_language, allow_skip=(mode in ("continue", "browse")))
    if pre.exec() != QDialog.Accepted:
        return
    if not pre.use_current:
        pr.filters = pre.filters
        pr.ui_language = pre.ui_language
    settings.setValue("ui_language", pr.ui_language)
    pr.save()

    update_recent_projects(settings, str(pr.folder))

    setup_project_logging(pr.folder)
    logging.info("Project loaded successfully. Starting UI...")

    w = MainWindow(pr, settings)
    w.show()

    try:
        app.exec()
    finally:
        # bounded pools shutdown
        for ex in (_image_executor, _meta_executor, _preload_executor, _download_executor):
            try:
                ex.shutdown(wait=False, cancel_futures=False)
            except Exception:
                pass

if __name__ == "__main__":
    main()
