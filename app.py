#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ёлочка Плюс — расширенный экранный переводчик (монолитный файл app.py).

Возможности:
  * Горячая клавиша "~" (тильда) -> затемнённый оверлей выделения области экрана.
  * OCR выбранной области (pytesseract / EasyOCR, языки подставляются динамически из настроек).
  * Асинхронный перевод через deep_translator (GoogleTranslator) в QThread,
    попап с переводом появляется у курсора мыши.
  * Regex-очистка текста после OCR (игровой/системный мусор: | _ ~ @ и т.п.).
  * Вкладка "История": все переводы; оригинал разбит на кликабельные слова-кнопки,
    клик по слову -> машинный перевод слова -> автоматическое добавление в "Словарь".
  * Вкладка "Словарь": таблица пар "Оригинал - Перевод", экспорт для Quizlet (.txt, Tab-разделитель).
  * Системный трей, темы Luxury Dark / Clean Light (переключатель прямо в настройках),
    SQLite-хранение истории/словаря/настроек, подробное логирование в app.log.

Зависимости: PyQt6, deep-translator, Pillow; опционально pytesseract (+ tesseract binary), easyocr.
Запуск:  python app.py
"""

import os
import re
import sys
import csv
import json
import time
import sqlite3
import logging
import threading
from pathlib import Path

# -----------------------------------------------------------------------------
# 0. Пути, логирование, ранние проверки зависимостей
# -----------------------------------------------------------------------------

def app_dir() -> Path:
    """Каталог программы (учитывает PyInstaller --onefile)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

DATA_DIR = app_dir() / "yolochka_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "yolochka.db"
LOG_PATH = DATA_DIR / "app.log"
LOCALES_DIR = DATA_DIR / "locales"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("yolochka")

MISSING_DEPS = []
try:
    from PyQt6.QtCore import Qt, QThread, QTimer, QPoint, QRect, pyqtSignal, QObject
    from PyQt6.QtGui import (
        QAction, QColor, QCursor, QFont, QIcon, QImage, QPainter, QPen,
        QPixmap, QKeySequence, QGuiApplication,
    )
    from PyQt6.QtWidgets import (
        QApplication, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
        QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSplitter,
        QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget,
        QTextEdit, QVBoxLayout, QWidget, QFileDialog, QFormLayout, QGroupBox,
    )
except ImportError as e:
    MISSING_DEPS.append(f"PyQt6 ({e})")

try:
    from deep_translator import GoogleTranslator
except ImportError as e:
    MISSING_DEPS.append(f"deep-translator ({e})")

try:
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter
except ImportError as e:
    MISSING_DEPS.append(f"Pillow ({e})")

HAS_PYTESSERACT = False
try:
    import pytesseract
    pytesseract.get_tesseract_version()          # проверяем, что бинарь tesseract установлен
    HAS_PYTESSERACT = True
except Exception:
    pass

HAS_EASYOCR = False
try:
    import easyocr                              # сам движок грузится лениво (он тяжёлый)
    HAS_EASYOCR = True
except Exception:
    pass

OCR_AVAILABLE = HAS_PYTESSERACT or HAS_EASYOCR

# -----------------------------------------------------------------------------
# 1. Языковые конфигурации (никакого хардкода в логике — только эти таблицы)
#    Чтобы добавить новый язык: допишите одну строку в UI_LANGS / OCR_LANGS /
#    TRANSLATE_LANGS и, при желании, ключи locales/<код>.json.
# -----------------------------------------------------------------------------

UI_LANGS = {                      # локализация интерфейса
    "ru": "Русский",
    "en": "English",
}

OCR_LANGS = {                     # "Язык экрана" : ISO-код -> коды движков OCR
    "auto": {   "label": "Автоопределение",      "tess": "eng+rus",       "easy": ["en", "ru"] },
    "en":   {   "label": "Английский",           "tess": "eng",           "easy": ["en"] },
    "ru":   {   "label": "Русский",              "tess": "rus",           "easy": ["ru"] },
    "zh":   {   "label": "Китайский",            "tess": "chi_sim",       "easy": ["ch_sim"] },
    "ja":   {   "label": "Японский",             "tess": "jpn",           "easy": ["ja"] },
}

TRANSLATE_LANGS = {               # "Язык перевода" : ISO-код deep_translator
    "ru": "Русский",
    "en": "Английский",
    "es": "Испанский",
    "de": "Немецкий",
}

DEFAULT_SETTINGS = {
    "ui_lang": "ru",
    "theme": "dark",
    "source_lang": "auto",
    "target_lang": "ru",
    "hotkey": "`",
    "ocr_engine": "auto",         # auto | tesseract | easyocr
}

# -----------------------------------------------------------------------------
# 2. Локализация UI (i18n): встроенные словари + внешние JSON (можно добавлять свои)
# -----------------------------------------------------------------------------

BUILTIN_LOCALES = {
    "ru": {
        "app.title": "Ёлочка Плюс — экранный переводчик",
        "tab.history": "История",
        "tab.dictionary": "Словарь",
        "tab.settings": "Настройки",
        "history.empty": "История пуста. Нажмите «~» и выделите текст на экране.",
        "history.original": "Оригинал (кликните слово, чтобы добавить в словарь):",
        "history.translation": "Перевод:",
        "history.btn.clear": "Очистить историю",
        "dict.empty": "Словарь пуст.",
        "dict.col.word": "Оригинал",
        "dict.col.translate": "Перевод",
        "dict.col.langs": "Языки",
        "dict.col.date": "Дата",
        "dict.btn.export": "Экспорт для Quizlet",
        "dict.btn.export_csv": "Экспорт CSV",
        "dict.btn.delete": "Удалить выбранное",
        "dict.export.ok": "Файл для Quizlet сохранён:\n{path}\nВсего пар: {n}",
        "dict.export.empty": "Словарь пуст — экспортировать нечего.",
        "settings.ui_lang": "Язык интерфейса приложения",
        "settings.theme": "Тема оформления",
        "settings.theme.dark": "Luxury Dark",
        "settings.theme.light": "Clean Light",
        "settings.source": "Язык экрана (OCR)",
        "settings.target": "Язык перевода",
        "settings.hotkey": "Горячая клавиша",
        "settings.engine": "OCR-движок",
        "settings.hint": "Все изменения применяются сразу. Новые языки добавляются одной строкой в таблицах языков.",
        "tray.translate": "Перевести экран (~)",
        "tray.open": "Открыть окно",
        "tray.quit": "Выход",
        "popup.translating": "Перевод…",
        "popup.empty": "Текст не распознан",
        "popup.dblclick": "(двойной клик — открыть главное окно)",
        "err.no_ocr": "OCR недоступен: установите Tesseract\n(https://github.com/UB-Mannheim/tesseract/wiki)\nили выполните: pip install pytesseract easyocr",
        "err.translate": "Ошибка перевода",
        "word.added": "Слово «{w}» → «{t}» добавлено в словарь",
        "msg.time": "Время выполнения",
    },
    "en": {
        "app.title": "Yolochka Plus — screen translator",
        "tab.history": "History",
        "tab.dictionary": "Dictionary",
        "tab.settings": "Settings",
        "history.empty": "History is empty. Press “~” and select text on the screen.",
        "history.original": "Original (click a word to add it to the dictionary):",
        "history.translation": "Translation:",
        "history.btn.clear": "Clear history",
        "dict.empty": "Dictionary is empty.",
        "dict.col.word": "Word",
        "dict.col.translate": "Translation",
        "dict.col.langs": "Languages",
        "dict.col.date": "Date",
        "dict.btn.export": "Export for Quizlet",
        "dict.btn.export_csv": "Export CSV",
        "dict.btn.delete": "Delete selected",
        "dict.export.ok": "Quizlet file saved:\n{path}\nTotal pairs: {n}",
        "dict.export.empty": "Dictionary is empty — nothing to export.",
        "settings.ui_lang": "App interface language",
        "settings.theme": "Color theme",
        "settings.theme.dark": "Luxury Dark",
        "settings.theme.light": "Clean Light",
        "settings.source": "Screen language (OCR)",
        "settings.target": "Translation language",
        "settings.hotkey": "Hotkey",
        "settings.engine": "OCR engine",
        "settings.hint": "Changes apply instantly. New languages require just one table row.",
        "tray.translate": "Translate screen (~)",
        "tray.open": "Open window",
        "tray.quit": "Quit",
        "popup.translating": "Translating…",
        "popup.empty": "No text recognized",
        "popup.dblclick": "(double-click opens the main window)",
        "err.no_ocr": "OCR unavailable: install Tesseract\n(https://github.com/UB-Mannheim/tesseract/wiki)\nor run: pip install pytesseract easyocr",
        "err.translate": "Translation error",
        "word.added": "Word “{w}” → “{t}” added to dictionary",
        "msg.time": "Elapsed time",
    },
}


class I18n(QObject):
    """Менеджер локализации: встроенные словари + перезагрузка внешних locales/*.json."""
    changed = pyqtSignal(str)

    def __init__(self, lang: str = "ru"):
        super().__init__()
        self.lang = lang
        self._cache = {}
        self.reload()

    def reload(self):
        data = dict(BUILTIN_LOCALES.get(self.lang, {}))
        f = LOCALES_DIR / f"{self.lang}.json"          # внешний файл может переопределять/дополнять
        if f.exists():
            try:
                data.update(json.loads(f.read_text(encoding="utf-8")))
                log.info("i18n: подключён внешний файл %s", f.name)
            except Exception as e:
                log.error("i18n: битый JSON %s: %s", f, e)
        self._cache = data

    def set_lang(self, lang: str):
        if lang in UI_LANGS and lang != self.lang:
            self.lang = lang
            self.reload()
            log.info("i18n: язык интерфейса переключён на '%s'", lang)
            self.changed.emit(lang)

    def t(self, key: str, **kwargs) -> str:
        s = self._cache.get(key) or BUILTIN_LOCALES["en"].get(key) or BUILTIN_LOCALES["ru"].get(key) or key
        try:
            return s.format(**kwargs) if kwargs else s
        except Exception:
            return s


# -----------------------------------------------------------------------------
# 3. Темы оформления (плоский современный дизайн)
# -----------------------------------------------------------------------------

THEMES = {
    "dark": {
        "name": "Luxury Dark",
        "qss": """
* { font-family: 'Segoe UI', 'Arial'; font-size: 14px; }
QMainWindow, QDialog, QWidget { background: #12121c; color: #eaeaea; }
QTabWidget::pane { border: 1px solid #2a2a3d; border-radius: 8px; }
QTabBar::tab { background: #1b1b28; color: #9a9ab0; padding: 10px 22px;
               border-top-left-radius: 8px; border-top-right-radius: 8px; margin-right: 4px; }
QTabBar::tab:selected { background: #2a2a3d; color: #e8c877; font-weight: 600; }
QPushButton { background: #2a2a3d; color: #eaeaea; border: 1px solid #3a3a52;
              border-radius: 8px; padding: 8px 16px; }
QPushButton:hover { background: #3a3a52; border-color: #e8c877; }
QPushButton:pressed { background: #e8c877; color: #12121c; }
QPushButton#primary { background: #e8c877; color: #12121c; font-weight: 700; border: none; }
QPushButton#primary:hover { background: #f2d98f; }
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QTableWidget {
    background: #1b1b28; color: #eaeaea; border: 1px solid #2f2f45; border-radius: 8px; padding: 6px; }
QComboBox::drop-down { border: none; width: 26px; }
QComboBox QAbstractItemView { background: #1b1b28; color: #eaeaea;
    selection-background-color: #e8c877; selection-color: #12121c; }
QHeaderView::section { background: #2a2a3d; color: #e8c877; border: none; padding: 8px; font-weight: 600; }
QTableWidget { gridline-color: #2f2f45; }
QTableWidget::item:selected { background: #3a3a52; color: #e8c877; }
QScrollBar:vertical { background: #12121c; width: 10px; } QScrollBar::handle:vertical {
    background: #3a3a52; border-radius: 5px; min-height: 30px; }
QMenu { background: #1b1b28; color: #eaeaea; border: 1px solid #2f2f45; }
QMenu::item:selected { background: #e8c877; color: #12121c; }
QGroupBox { border: 1px solid #2f2f45; border-radius: 8px; margin-top: 14px; padding-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #e8c877; }
QLabel#accent { color: #e8c877; font-weight: 600; }
""",
        "popup_bg": "#1b1b28", "popup_fg": "#eaeaea", "popup_accent": "#e8c877",
        "popup_border": "#e8c877",
    },
    "light": {
        "name": "Clean Light",
        "qss": """
* { font-family: 'Segoe UI', 'Arial'; font-size: 14px; }
QMainWindow, QDialog, QWidget { background: #f5f6fa; color: #1e2430; }
QTabWidget::pane { border: 1px solid #d9dce6; border-radius: 8px; background: white; }
QTabBar::tab { background: #e9ebf3; color: #5a6172; padding: 10px 22px;
               border-top-left-radius: 8px; border-top-right-radius: 8px; margin-right: 4px; }
QTabBar::tab:selected { background: #ffffff; color: #2f6fed; font-weight: 600; }
QPushButton { background: #ffffff; color: #1e2430; border: 1px solid #d9dce6;
              border-radius: 8px; padding: 8px 16px; }
QPushButton:hover { border-color: #2f6fed; color: #2f6fed; }
QPushButton:pressed { background: #2f6fed; color: white; }
QPushButton#primary { background: #2f6fed; color: white; font-weight: 700; border: none; }
QPushButton#primary:hover { background: #4b83f2; }
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QTableWidget {
    background: white; color: #1e2430; border: 1px solid #d9dce6; border-radius: 8px; padding: 6px; }
QComboBox QAbstractItemView { background: white; color: #1e2430;
    selection-background-color: #2f6fed; selection-color: white; }
QHeaderView::section { background: #eef0f6; color: #2f6fed; border: none; padding: 8px; font-weight: 600; }
QTableWidget { gridline-color: #e6e8f0; background: white; }
QMenu { background: white; color: #1e2430; border: 1px solid #d9dce6; }
QMenu::item:selected { background: #2f6fed; color: white; }
QGroupBox { border: 1px solid #d9dce6; border-radius: 8px; margin-top: 14px; padding-top: 8px; background: white; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #2f6fed; }
QLabel#accent { color: #2f6fed; font-weight: 600; }
""",
        "popup_bg": "#ffffff", "popup_fg": "#1e2430", "popup_accent": "#2f6fed",
        "popup_border": "#2f6fed",
    },
}

# -----------------------------------------------------------------------------
# 4. SQLite: история, словарь, настройки
# -----------------------------------------------------------------------------

class Database:
    """Потокобезопасная обёртка над SQLite (по соединению на поток, WAL)."""

    def __init__(self, path: Path = DB_PATH):
        self._path = str(path)
        self._local = threading.local()
        self._lock = threading.Lock()
        # executescript сам делает commit; контекстный менеджер с ним конфликтует
        self._conn().executescript("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                source_text TEXT, translated_text TEXT,
                source_lang TEXT, target_lang TEXT,
                engine TEXT, elapsed_ms INTEGER);
            CREATE TABLE IF NOT EXISTS dictionary(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                word TEXT NOT NULL, translation TEXT NOT NULL,
                source_lang TEXT, target_lang TEXT,
                UNIQUE(word, source_lang, target_lang)
            );""")
        self._conn().commit()
        log.info("DB: соединение готово, схема проверена (%s)", self._path)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL;")
            self._local.conn = conn
        return conn

    # --- настройки ---
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str):
        with self._lock:
            self._conn().execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._conn().commit()

    def load_settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        for k, v in self._conn().execute("SELECT key,value FROM settings"):
            out[k] = v
        return out

    # --- история ---
    def add_history(self, src: str, dst: str, sl: str, tl: str, engine: str, ms: int) -> int:
        with self._lock:
            cur = self._conn().execute(
                "INSERT INTO history(source_text,translated_text,source_lang,target_lang,engine,elapsed_ms)"
                " VALUES(?,?,?,?,?,?)", (src, dst, sl, tl, engine, ms))
            self._conn().commit()
        log.info("DB: запись #%d в историю (%d мс, %s->%s, %s)", cur.lastrowid, ms, sl, tl, engine)
        return cur.lastrowid

    def list_history(self, limit: int = 500):
        return self._conn().execute(
            "SELECT id,ts,source_text,translated_text,source_lang,target_lang,elapsed_ms"
            " FROM history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def clear_history(self):
        with self._lock:
            self._conn().execute("DELETE FROM history")
            self._conn().commit()
        log.info("DB: история очищена")

    # --- словарь ---
    def add_word(self, word: str, translation: str, sl: str, tl: str) -> bool:
        """True — добавлено, False — уже было."""
        with self._lock:
            cur = self._conn().execute(
                "INSERT OR IGNORE INTO dictionary(word,translation,source_lang,target_lang)"
                " VALUES(?,?,?,?)", (word.strip(), translation.strip(), sl, tl))
            self._conn().commit()
        ok = cur.rowcount > 0
        log.info("DB: слово '%s'->'%s' [%s->%s] %s", word, translation, sl, tl,
                 "добавлено" if ok else "уже существует")
        return ok

    def list_words(self):
        return self._conn().execute(
            "SELECT word,translation,source_lang,target_lang,ts FROM dictionary"
            " ORDER BY id DESC").fetchall()

    def delete_word(self, word: str, sl: str, tl: str):
        with self._lock:
            self._conn().execute("DELETE FROM dictionary WHERE word=? AND source_lang=? AND target_lang=?",
                                 (word, sl, tl))
            self._conn().commit()

    def export_quizlet_txt(self, path: str) -> int:
        """Формат Quizlet: 'слово<TAB>перевод' на каждой строке."""
        rows = self.list_words()
        with open(path, "w", encoding="utf-8") as f:
            for w, t, *_ in rows:
                f.write(f"{w}\t{t}\n")
        log.info("DB: Quizlet-экспорт %d пар -> %s", len(rows), path)
        return len(rows)

    def export_csv(self, path: str) -> int:
        rows = self.list_words()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            cw = csv.writer(f)
            cw.writerow(["word", "translation", "source_lang", "target_lang", "date"])
            cw.writerows(rows)
        return len(rows)


# -----------------------------------------------------------------------------
# 5. OCR: предобработка (Оцу), распознавание с ДИНАМИЧЕСКИМИ ISO-кодами, regex-очистка
# -----------------------------------------------------------------------------

JUNK_CHARS = "~°|_/\\@#$%^&*+=<>{}[]«»…•·—–¬¦`"
_JUNK_SET = set(JUNK_CHARS)

LINE_SPLIT_RE = re.compile(r"\r?\n")                     # строки OCR-вывода
HYPHEN_JOIN_RE = re.compile(r"(\w)-\s*\n\s*(\w)")      # перенос: transla-\ntion -> translation
WS_RE = re.compile(r"[ \t]{2,}")                          # лишние пробелы
LONE_JUNK_RE = re.compile(                                 # одиночные мусорные символы
    r"(?<!\w)[" + re.escape(JUNK_CHARS) + r"](?!(?:\w|" + re.escape(JUNK_CHARS) + r"))")
TRAIL_LEAD_PUNCT_RE = re.compile(r"^\s*[^\w]+|(?![.!?…,;:])\s*[,](?=\s*$)")
WORD_SPLIT_RE = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)   # слова для клика-кнопок


def _strip_junk(s: str) -> str:
    """Удаляет из строки мусорные символы, не «съедая» нормальную пунктуацию . , ! ? ' \" : -"""
    out, n = [], len(s)
    for i, ch in enumerate(s):
        if ch in _JUNK_SET:
            prev = s[i - 1] if i else " "
            nxt = s[i + 1] if i + 1 < n else " "
            if prev.isspace() and nxt.isspace():           # одинокий символ-обрубок
                out.append(" ")
                continue
            if not prev.isalnum() and not prev.isspace():  # сломанная пунктуация вида "|." -> "."
                continue
            if not nxt.isalnum() and not nxt.isspace():
                continue
            out.append(" ")                                # приклеен к слову — вырезаем
            continue
        out.append(ch)
    return "".join(out)


def clean_text(text: str) -> str:
    """Regex-зачистка OCR-мусора (| _ ~ ° @ и т.п.), склейка строк, чистка пробелов/пунктуации."""
    if not text:
        return ""
    t = HYPHEN_JOIN_RE.sub(r"\1\2", text)
    lines = []
    for ln in LINE_SPLIT_RE.split(t):
        stripped = ln.strip()
        alnum = sum(c.isalnum() for c in stripped)
        if len(stripped) <= 3 and alnum == 0:              # мусор-строка: "|", "_-", "@@"
            continue
        ln = _strip_junk(ln)
        ln = WS_RE.sub(" ", ln).strip()
        if ln:
            lines.append(ln)
    t = " ".join(lines)
    t = LONE_JUNK_RE.sub(" ", t)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)                # "привет ." -> "привет."
    t = re.sub(r"([,.!?;:])(?=\w)", r"\1 ", t)             # "привет,мир" -> "привет, мир"
    t = re.sub(r"([.!?])[.!?;:,]+", r"\1", t)                            # "Quest! ." -> "Quest!"
    t = TRAIL_LEAD_PUNCT_RE.sub("", t)
    t = WS_RE.sub(" ", t).strip()
    return re.sub(r"^[^A-Za-zА-Яа-яЁё\u4e00-\u9fff\u3040-\u30ff\d]+", "", t).strip()


def extract_words(text: str):
    """Список «значимых» слов для кнопки-кликов (без цифр и мусора)."""
    words = WORD_SPLIT_RE.findall(text or "")
    return [w for w in words if len(w) >= 2][:80]


class OcrEngine:
    """Единый вход для любого OCR: принимает ISO-код языка и сам маппит его на код движка."""

    def __init__(self):
        self._easy_reader = None
        self._easy_langs = None

    def preferred_engine(self, configured: str) -> str:
        if configured == "tesseract" and HAS_PYTESSERACT:
            return "tesseract"
        if configured == "easyocr" and HAS_EASYOCR:
            return "easyocr"
        return "tesseract" if HAS_PYTESSERACT else ("easyocr" if HAS_EASYOCR else "none")

    # ---- предобработка: gray -> контраст -> бинаризация по порогу Оцу -> лёгкое шумоподавление
    @staticmethod
    def preprocess(img: "Image.Image") -> "Image.Image":
        g = ImageOps.grayscale(img)
        g = ImageOps.autocontrast(g, cutoff=1)
        hist = g.histogram()
        total, sum_all = g.width * g.height, sum(i * h for i, h in enumerate(hist))
        sum_b, w_b, best_t, best_var = 0.0, 0.0, 0, 0.0
        for i in range(256):
            w_fg = hist[i]
            if w_fg == 0:
                continue
            w_bg = total - w_fg
            if w_bg == 0:
                break
            sum_b += i * w_fg
            w_b += w_fg
            m_b, m_w = sum_b / w_b, (sum_all - sum_b) / w_bg
            var = w_b * w_bg * (m_b - m_w) ** 2
            if var > best_var:
                best_var, best_t = var, i
        bw = g.point(lambda p: 255 if p > best_t else 0)
        bw = bw.filter(ImageFilter.MedianFilter(3))
        log.debug("OcrEngine: порог Оцу=%d", best_t)
        return bw

    def recognize(self, img: "Image.Image", iso_lang: str, engine_cfg: str = "auto") -> tuple[str, str]:
        engine = self.preferred_engine(engine_cfg if engine_cfg in ("auto", "tesseract", "easyocr") else "auto")
        if engine == "none":
            raise RuntimeError("OCR недоступен: не найден ни tesseract, ни easyocr")
        cfg = OCR_LANGS.get(iso_lang, OCR_LANGS["auto"])
        pre = self.preprocess(img)
        t0 = time.perf_counter()
        if engine == "tesseract":
            txt = pytesseract.image_to_string(pre, lang=cfg["tess"])
        else:
            langs = tuple(cfg["easy"])
            if self._easy_reader is None or self._easy_langs != langs:
                log.info("OcrEngine: инициализация EasyOCR для %s (первый запуск дольше)…", langs)
                self._easy_reader = easyocr.Reader(list(langs), gpu=False)
                self._easy_langs = langs
            txt = " ".join(r[1] for r in self._easy_reader.readtext(np_array(pre)))
        ms = int((time.perf_counter() - t0) * 1000)
        log.info("OcrEngine: распознавание [%s/%s] заняло %d мс", engine, iso_lang, ms)
        return clean_text(txt), engine


def np_array(img: "Image.Image"):
    try:
        import numpy as np
        return np.array(img.convert("L"))
    except Exception:
        return img


OCR = OcrEngine()

# -----------------------------------------------------------------------------
# 6. Перевод (deep_translator, динамические ISO-коды) + QThread-воркеры
# -----------------------------------------------------------------------------

_TRANSLATE_CACHE: dict = {}

def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    """source_lang — ISO ('auto' тоже валиден) или None. Кэширует повторы (ускорение >2s нет)."""
    if not text or not text.strip():
        return ""
    key = (text.strip().lower(), source_lang, target_lang)
    if key in _TRANSLATE_CACHE:
        log.debug("translate: кэш-хит '%.40s'", text)
        return _TRANSLATE_CACHE[key]
    t0 = time.perf_counter()
    src = None if source_lang in ("auto", "", None) else source_lang
    res = GoogleTranslator(source=src or "auto", target=target_lang).translate(text)
    ms = int((time.perf_counter() - t0) * 1000)
    flag = " [SLOW >2s!]" if ms > 2000 else ""
    log.info("translate: '%.40s…' [%s->%s] %d мс%s", text, source_lang, target_lang, ms, flag)
    _TRANSLATE_CACHE[key] = res or ""
    return res or ""


class TranslateWorker(QThread):
    """Асинхронный пайплайн: скриншот -> предобработка -> OCR -> очистка -> перевод."""
    finished_ok = pyqtSignal(str, str, str, int)     # original, translated, engine, ms
    failed = pyqtSignal(str)

    def __init__(self, image: "Image.Image", parent=None):
        super().__init__(parent)
        self.image = image
        self.settings = DB.load_settings()

    def run(self):
        t0 = time.perf_counter()
        try:
            if not OCR_AVAILABLE:
                self.failed.emit("no_ocr")
                return
            original, engine = OCR.recognize(
                self.image, self.settings["source_lang"], self.settings.get("ocr_engine", "auto"))
            log.info("Worker: OCR дал %.60s", original or "<пусто>")
            if not original:
                self.finished_ok.emit("", "", engine, int((time.perf_counter() - t0) * 1000))
                return
            translated = translate_text(original, self.settings["source_lang"],
                                        self.settings["target_lang"])
            total_ms = int((time.perf_counter() - t0) * 1000)
            if total_ms > 2000:
                log.warning("Worker: полный цикл %d мс превысил целевые 2000 мс", total_ms)
            self.finished_ok.emit(original, translated, engine, total_ms)
        except Exception as e:
            log.exception("Worker: ошибка пайплайна")
            self.failed.emit(str(e))


class WordTranslateWorker(QThread):
    """Быстрый перевод одного слова по клику из Истории."""
    done = pyqtSignal(str, str)                      # word, translation
    err = pyqtSignal(str, str)

    def __init__(self, word: str, parent=None):
        super().__init__(parent)
        self.word = word

    def run(self):
        s = DB.load_settings()
        try:
            tr = translate_text(self.word, s["source_lang"] if s["source_lang"] != "auto" else "auto",
                                s["target_lang"])
            self.done.emit(self.word, tr)
        except Exception as e:
            self.err.emit(self.word, str(e))

# -----------------------------------------------------------------------------
# 7. Захват экрана: оверлей выделения + попап перевода у курсора
# -----------------------------------------------------------------------------

def grab_region(rect: QRect) -> "Image.Image":
    """Захват области экрана с учётом DPI (координаты оверлея — логические)."""
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio()
    pm = screen.grabWindow(0, round(rect.x() * dpr), round(rect.y() * dpr),
                           round(rect.width() * dpr), round(rect.height() * dpr))
    if dpr != 1.0:
        pm.setDevicePixelRatio(1.0)                       # дальше работаем в пикселях pixmap
    qimg = pm.toImage().convertToFormat(QImage.Format.Format_RGB888)
    w, h, bpl = qimg.width(), qimg.height(), qimg.bytesPerLine()
    buf = bytes(qimg.constBits()[: bpl * h]) if hasattr(qimg.constBits(), "__getitem__") else bytes(qimg.constBits())
    if bpl == w * 3:
        return Image.frombytes("RGB", (w, h), buf[: w * h * 3])
    img = Image.new("RGB", (w, h))                        # stride != ширина — копируем построчно
    for y in range(h):
        img.paste(Image.frombytes("RGB", (w, 1), buf[y * bpl: y * bpl + w * 3]), (0, y))
    return img


class SelectionOverlay(QWidget):
    """Затемнённый полноэкранный оверлей; рисуем рамку мышью -> сигнал region_selected."""
    region_selected = pyqtSignal(QRect)
    cancelled = pyqtSignal()

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._start = self._cur = None
        screen = QGuiApplication.primaryScreen()
        self._bg = screen.grabWindow(0)          # чистый экран захватываем ДО затемнения
        self._bg.setDevicePixelRatio(1.0)
        self.setGeometry(screen.geometry())
        log.info("Overlay: открыт на %dx%d", self._bg.width(), self._bg.height())

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(QRect(0, 0, self.width(), self.height()), self._bg)
        p.fillRect(self.rect(), QColor(0, 0, 0, 130))
        if self._start and self._cur:
            r = QRect(self._start, self._cur).normalized()
            p.setPen(QPen(QColor("#e8c877"), 2))
            p.setBrush(QColor(232, 200, 119, 30))
            p.drawRect(r)
            p.setPen(QColor("#e8e8e8"))
            p.drawText(r.topLeft() + QPoint(0, -6), f"{r.width()}×{r.height()}")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._start = self._cur = e.position().toPoint()
            self.update()

    def mouseMoveEvent(self, e):
        if self._start:
            self._cur = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        if not self._start:
            return
        r = QRect(self._start, e.position().toPoint()).normalized()
        self._start = self._cur = None
        self.hide()
        if r.width() > 10 and r.height() > 10:
            log.info("Overlay: выделена область %s", r)
            self.region_selected.emit(r)
        else:
            self.cancelled.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.hide()
            self.cancelled.emit()


class PopupWidget(QWidget):
    """Компактное окно перевода у курсора. Двойной клик — открыть главное окно."""

    def __init__(self, i18n: I18n):
        super().__init__(None, Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.i18n = i18n
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._lbl_src = QLabel(self)
        self._lbl_dst = QLabel(self)
        self._lbl_hint = QLabel(i18n.t("popup.dblclick"), self)
        for l, bold, size in ((self._lbl_src, False, 11), (self._lbl_dst, True, 15), (self._lbl_hint, False, 9)):
            f = QFont("Segoe UI", size)
            f.setBold(bold)
            l.setFont(f)
            l.setWordWrap(True)
            l.setMaximumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 8)
        lay.setSpacing(4)
        lay.addWidget(self._lbl_src)
        lay.addWidget(self._lbl_dst)
        lay.addWidget(self._lbl_hint)
        self._lbl_hint.setObjectName("hintlbl")
        self.setStyleSheet("background:transparent;")
        self._apply_label_styles()

    def _apply_label_styles(self):
        th = THEMES.get(DB.get_setting("theme", "dark"), THEMES["dark"])
        self._lbl_src.setStyleSheet(f"color:{th['popup_accent']}; background:transparent;")
        self._lbl_dst.setStyleSheet(f"color:{th['popup_fg']}; background:transparent;")
        self._lbl_hint.setStyleSheet(f"color:#9a9ab0; background:transparent;")

    def show_busy(self, cursor_pos: QPoint):
        self._lbl_src.setText("")
        self._lbl_dst.setText(self.i18n.t("popup.translating"))
        self._show_at(cursor_pos)

    def show_result(self, original: str, translated: str, pos: QPoint):
        self._lbl_src.setText(original[:300] or self.i18n.t("popup.empty"))
        self._lbl_dst.setText(translated[:600] or self.i18n.t("popup.empty"))
        self._show_at(pos)

    def _show_at(self, pos: QPoint):
        self.adjustSize()
        scr = QGuiApplication.primaryScreen().availableGeometry()
        x = max(scr.left(), min(pos.x() + 16, scr.right() - self.width() - 4))
        y = max(scr.top(), min(pos.y() + 16, scr.bottom() - self.height() - 4))
        self._apply_label_styles()
        self.move(x, y)
        self.show()
        self.raise_()
        QTimer.singleShot(12000, self.hide)

    def mouseDoubleClickEvent(self, _):
        self.hide()
        app_main_window.showNormal()
        app_main_window.activateWindow()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        th = THEMES[DB.get_setting("theme", "dark")]
        bg = QColor(th["popup_bg"])
        bg.setAlphaF(0.97)
        p.setPen(QPen(QColor(th["popup_border"]), 1.5))
        p.setBrush(bg)
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 12, 12)


# -----------------------------------------------------------------------------
# 8. Глобальный хоткей "~"
# -----------------------------------------------------------------------------

class HotkeyManager:
    """Глобальная горячая клавиша '~'. Windows: RegisterHotKey; X11: XGrabKey.
    Колбэк всегда маршализуется в GUI-поток через QTimer (Qt-виджеты не тредобезопасны)."""

    VK_TILDE_GRV = 0xC0          # виртуальный код ` / ~ на US-клавиатуре Windows

    def __init__(self, callback):
        self._timer = QTimer()               # живёт в GUI-потоке
        self._timer.timeout.connect(callback)
        self.callback = lambda: self._timer.start(0)
        self.ok = False
        plat = sys.platform
        try:
            if plat.startswith("win"):
                self.ok = self._hook_windows()
            elif plat.startswith("linux"):
                self.ok = self._hook_x11()
            else:
                log.warning("Hotkey: глобальные хоткеи на %s не поддерживаются, используйте меню трея", plat)
        except Exception as e:
            log.error("Hotkey: не удалось установить хук: %s", e)
        if not self.ok:
            log.warning("Hotkey: '~' не перехвачена глобально — выделяйте текст через меню трея")

    def _hook_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        HK_ID = 0xB0B5
        MOD_NOREPEAT = 0x4000
        WM_HOTKEY = 0x0312

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                        ("time", wintypes.DWORD), ("pt", POINT)]

        def _proc():
            msg = MSG()
            while True:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r == 0 or r == -1:
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HK_ID:
                    try:
                        self.callback()
                    except Exception:
                        log.exception("Hotkey: ошибка колбэка")
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        if not user32.RegisterHotKey(None, HK_ID, MOD_NOREPEAT, self.VK_TILDE_GRV):
            log.warning("Hotkey: RegisterHotKey('~') не удалась — клавиша, возможно, занята другим приложением")
            return False
        threading.Thread(target=_proc, daemon=True, name="HotkeyLoop").start()
        log.info("Hotkey: глобальная клавиша '~' зарегистрирована (Win32)")
        return True

    def _hook_x11(self) -> bool:
        import ctypes
        display = ctypes.cdll.LoadLibrary("libX11.so.6")
        dpy = display.XOpenDisplay(None)
        if not dpy:
            return False
        keycode = display.XKeysymToKeycode(dpy, ctypes.c_ulong(0x27)).value   # XK_grave (`/~)
        root = display.XDefaultRootWindow(dpy)
        AnyModifier = 1 << 15
        display.XSelectInput(dpy, root, 0x00001000)                            # KeyPressMask
        display.XGrabKey(dpy, keycode, AnyModifier, root, 1, 1, 1)             # ~ с любым состоянием модификаторов
        display.XSync(dpy, 0)

        def _poll():
            class XEvent(ctypes.Structure):
                _fields_ = [("type", ctypes.c_int), ("pad", ctypes.c_byte * 224)]
            ev = XEvent()
            while True:
                display.XNextEvent(dpy, ctypes.byref(ev))
                if ev.type == 2:                                               # KeyPress
                    try:
                        self.callback()
                    except Exception:
                        log.exception("Hotkey: ошибка колбэка")

        threading.Thread(target=_poll, daemon=True, name="HotkeyX11").start()
        log.info("Hotkey: глобальная клавиша '~' зарегистрирована (X11)")
        return True


# -----------------------------------------------------------------------------
# 9. Главное окно: вкладки История / Словарь / Настройки
# -----------------------------------------------------------------------------

DB = Database()

class ClickableWordButton(QPushButton):
    def __init__(self, word: str, i18n: I18n, on_click):
        super().__init__(word)
        self.word = word
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFixedHeight(30)
        self.clicked.connect(lambda: on_click(word))


class MainWindow(QMainWindow):
    def __init__(self, i18n: I18n):
        super().__init__()
        self.i18n = i18n
        self.settings = DB.load_settings()
        self.worker: "TranslateWorker | None" = None
        self.word_workers: "list[WordTranslateWorker]" = []
        self.overlay: "SelectionOverlay | None" = None
        self.popup = PopupWidget(i18n)
        self._tray_actions = None          # заполняет main() после создания трея

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_history_tab(), "")
        self.tabs.addTab(self._build_dict_tab(), "")
        self.tabs.addTab(self._build_settings_tab(), "")
        self.setCentralWidget(self.tabs)
        self.resize(980, 640)
        i18n.changed.connect(self.retranslate)
        self.apply_theme(self.settings.get("theme", "dark"))
        self.retranslate()                # порядок важен: сначала тема, потом подписи (Qt полиморфизм)
        self.refresh_history()
        self.refresh_dict()

    # ---------- трей-меню (обновляется при смене языка) ----------
    def _rebuild_tray_menu(self):
        if not self._tray_actions:
            return
        act_tr, act_open, act_quit = self._tray_actions
        act_tr.setText(self.i18n.t("tray.translate"))
        act_open.setText(self.i18n.t("tray.open"))
        act_quit.setText(self.i18n.t("tray.quit"))
        if tray is not None:
            tray.setToolTip(self.i18n.t("app.title"))

    # ---------- конструктор вкладок ----------
    def _build_history_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.lst_history = QTableWidget()
        self.lst_history.setColumnCount(4)
        self.lst_history.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.lst_history.verticalHeader().setVisible(False)
        self.lst_history.itemSelectionChanged.connect(self._on_history_selected)
        self.btn_clear_hist = QPushButton()
        self.btn_clear_hist.clicked.connect(self._clear_history)
        top.addWidget(self.lst_history, 1)
        side = QVBoxLayout()
        side.addWidget(self.btn_clear_hist)
        side.addStretch()
        top.addLayout(side)
        lay.addLayout(top)
        splitter = QSplitter()
        self.lbl_hist_src_title = QLabel()
        self.scroll_words = QScrollArea()
        self.scroll_words.setWidgetResizable(True)
        self.words_container = QWidget()
        self.words_layout = QVBoxLayout(self.words_container)
        self.words_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.scroll_words.setWidget(self.words_container)
        self.lbl_hist_tr_title = QLabel()
        self.txt_hist_tr = QTextEdit()
        self.txt_hist_tr.setReadOnly(True)
        left = QVBoxLayout(); left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(self.lbl_hist_src_title); left.addWidget(self.scroll_words)
        right = QVBoxLayout(); right.setContentsMargins(0, 0, 0, 0)
        right.addWidget(self.lbl_hist_tr_title); right.addWidget(self.txt_hist_tr)
        lw, rw = QWidget(), QWidget()
        lw.setLayout(left); rw.setLayout(right)
        splitter.addWidget(lw); splitter.addWidget(rw)
        lay.addWidget(splitter, 1)
        return w

    def _build_dict_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.tbl_dict = QTableWidget()
        self.tbl_dict.setColumnCount(4)
        self.tbl_dict.verticalHeader().setVisible(False)
        self.tbl_dict.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        lay.addWidget(self.tbl_dict, 1)
        btns = QHBoxLayout()
        self.btn_export = QPushButton(); self.btn_export.setObjectName("primary")
        self.btn_export_csv = QPushButton()
        self.btn_del_word = QPushButton()
        self.btn_export.clicked.connect(self._export_quizlet)
        self.btn_export_csv.clicked.connect(self._export_csv)
        self.btn_del_word.clicked.connect(self._delete_selected_word)
        for b in (self.btn_export, self.btn_export_csv, self.btn_del_word):
            btns.addWidget(b)
        btns.addStretch()
        lay.addLayout(btns)
        return w

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        box = QGroupBox()
        bl = QVBoxLayout(box)
        form = QFormLayout()
        bl.addLayout(form)

        self.lbl_s_ui = QLabel(); self.cb_ui = QComboBox()
        self.lbl_s_theme = QLabel(); self.cb_theme = QComboBox()
        self.lbl_s_source = QLabel(); self.cb_source = QComboBox()
        self.lbl_s_target = QLabel(); self.cb_target = QComboBox()
        self.lbl_s_engine = QLabel(); self.cb_engine = QComboBox()
        self.lbl_s_hotkey = QLabel(); self.ed_hotkey = QLineEdit(self.settings.get("hotkey", "`"))
        self.lbl_hint = QLabel()

        for code, label in UI_LANGS.items():
            self.cb_ui.addItem(label, code)
        for key in ("dark", "light"):
            self.cb_theme.addItem(THEMES[key]["name"], key)
        for code, cfg in OCR_LANGS.items():
            self.cb_source.addItem(cfg["label"], code)
        for code, label in TRANSLATE_LANGS.items():
            self.cb_target.addItem(label, code)
        self.cb_engine.addItems(["auto", "tesseract", "easyocr"])
        self.cb_engine.setCurrentText(self.settings.get("ocr_engine", "auto"))

        pairs = ((self.lbl_s_ui, self.cb_ui, "ui_lang"), (self.lbl_s_theme, self.cb_theme, "theme"),
                 (self.lbl_s_source, self.cb_source, "source_lang"), (self.lbl_s_target, self.cb_target, "target_lang"))
        for lab, combo, skey in pairs:
            self._select_by_data(combo, self.settings.get(skey, ""))
            combo.currentIndexChanged.connect(lambda _=0, k=skey, c=combo: self._setting_changed(k, c))
            form.addRow(lab, combo)
        self.cb_engine.currentIndexChanged.connect(
            lambda _=0: self._setting_changed("ocr_engine", self.cb_engine))
        form.addRow(self.lbl_s_engine, self.cb_engine)
        form.addRow(self.lbl_s_hotkey, self.ed_hotkey)

        outer.addWidget(box)
        outer.addWidget(self.lbl_hint)
        outer.addStretch()
        return w

    @staticmethod
    def _select_by_data(combo: QComboBox, value: str):
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _setting_changed(self, key: str, combo: QComboBox):
        val = combo.currentData() if combo.itemData(combo.currentIndex()) is not None \
            else combo.currentText()
        DB.set_setting(key, str(val))
        self.settings[key] = str(val)
        log.info("Settings: '%s' := '%s'", key, val)
        if key == "ui_lang":
            self.i18n.set_lang(val)
        elif key == "theme":
            self.apply_theme(val)
        elif key in ("source_lang", "target_lang"):
            if tray is not None:
                tray.setToolTip(self.i18n.t("app.title"))

    # ---------- темы ----------
    def apply_theme(self, name: str):
        th = THEMES.get(name, THEMES["dark"])
        QApplication.instance().setStyleSheet(th["qss"])
        log.info("Theme: применена тема '%s'", th["name"])

    # ---------- локализация ----------
    def retranslate(self, *_):
        t = self.i18n.t
        self.setWindowTitle(t("app.title"))
        self.tabs.setTabText(0, t("tab.history"))
        self.tabs.setTabText(1, t("tab.dictionary"))
        self.tabs.setTabText(2, t("tab.settings"))
        self.lst_history.setHorizontalHeaderLabels(["ID", t("dict.col.date"), t("history.original"), t("history.translation")])
        self.tbl_dict.setHorizontalHeaderLabels([t("dict.col.word"), t("dict.col.translate"), t("dict.col.langs"), t("dict.col.date")])
        self.btn_clear_hist.setText(t("history.btn.clear"))
        self.btn_export.setText(t("dict.btn.export"))
        self.btn_export_csv.setText(t("dict.btn.export_csv"))
        self.btn_del_word.setText(t("dict.btn.delete"))
        self.lbl_hist_src_title.setText(t("history.original")); self.lbl_hist_src_title.setObjectName("accent")
        self.lbl_hist_tr_title.setText(t("history.translation")); self.lbl_hist_tr_title.setObjectName("accent")
        # подписи настроек (i18n без хардкода)
        self.lbl_s_ui.setText(t("settings.ui_lang"))
        self.lbl_s_theme.setText(t("settings.theme"))
        self.lbl_s_source.setText(t("settings.source"))
        self.lbl_s_target.setText(t("settings.target"))
        self.lbl_s_engine.setText(t("settings.engine"))
        self.lbl_s_hotkey.setText(t("settings.hotkey"))
        self.lbl_hint.setText(t("settings.hint"))
        if tray is not None:
            self._rebuild_tray_menu()
        self.refresh_history()
        self.refresh_dict()

    # ---------- история ----------
    def refresh_history(self):
        rows = DB.list_history()
        self.lst_history.setRowCount(len(rows))
        for i, (hid, ts, src, dst, sl, tl, ms) in enumerate(rows):
            for j, val in enumerate((str(hid), ts, src or "", dst or "")):
                it = QTableWidgetItem(val)
                it.setData(Qt.ItemDataRole.UserRole, hid)
                self.lst_history.setItem(i, j, it)
        hdr = self.lst_history.horizontalHeader()
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.lst_history.setColumnHidden(0, True)

    def _current_history_row(self):
        items = self.lst_history.selectedItems()
        if not items:
            return None
        r = items[0].row()
        return (self.lst_history.item(r, 2).text(), self.lst_history.item(r, 3).text())

    def _on_history_selected(self):
        row = self._current_history_row()
        # чистим контейнер слов
        while self.words_layout.count():
            it = self.words_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not row:
            self.txt_hist_tr.clear()
            return
        src, dst = row
        self.txt_hist_tr.setPlainText(dst)
        words = extract_words(src)
        chunk = QWidget()
        cl = QVBoxLayout(chunk); cl.setContentsMargins(4, 4, 4, 4); cl.setSpacing(6)
        line = None
        for wd in words:
            if line is None or line.count() >= 12:
                line = QHBoxLayout(); line.setSpacing(6); line.setAlignment(Qt.AlignmentFlag.AlignLeft)
                cl.addLayout(line)
            line.addWidget(ClickableWordButton(wd, self.i18n, self._on_word_clicked))
        self.words_layout.addWidget(chunk)
        self.words_layout.addStretch()

    def _on_word_clicked(self, word: str):
        log.info("History: клик по слову '%s' -> запрос перевода слова", word)
        wb = WordTranslateWorker(word, self)
        wb.done.connect(lambda w, tr: self._word_done(w, tr))
        wb.err.connect(lambda w, e: QMessageBox.warning(self, self.i18n.t("err.translate"), f"{w}: {e}"))
        wb.start()
        self.word_workers.append(wb)

    def _word_done(self, word: str, translation: str):
        if not translation:
            return
        s = DB.load_settings()                                # актуальные языки на момент завершения
        self.settings.update(s)
        added = DB.add_word(word, translation, s.get("source_lang", "auto"), s.get("target_lang", "ru"))
        self.refresh_dict()
        self.statusBar().showMessage(self.i18n.t("word.added", w=word, t=translation), 5000)
        log.info("WordFlow: '%s'->'%s' added=%s", word, translation, added)

    def _clear_history(self):
        DB.clear_history()
        self.refresh_history()
        self._on_history_selected()

    # ---------- словарь ----------
    def refresh_dict(self):
        rows = DB.list_words()
        self.tbl_dict.setRowCount(len(rows))
        for i, (wd, tr, sl, tl, ts) in enumerate(rows):
            for j, val in enumerate((wd, tr, f"{sl} → {tl}", ts)):
                self.tbl_dict.setItem(i, j, QTableWidgetItem(val))
        hdr = self.tbl_dict.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

    def _export_quizlet(self):
        if not DB.list_words():
            QMessageBox.information(self, self.i18n.t("dict.btn.export"), self.i18n.t("dict.export.empty"))
            return
        path, _ = QFileDialog.getSaveFileName(self, self.i18n.t("dict.btn.export"),
                                              str(Path.home() / "quizlet.txt"), "Text (*.txt)")
        if path:
            n = DB.export_quizlet_txt(path)
            QMessageBox.information(self, self.i18n.t("dict.btn.export"),
                                    self.i18n.t("dict.export.ok", path=path, n=n))

    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, self.i18n.t("dict.btn.export_csv"),
                                              str(Path.home() / "dictionary.csv"), "CSV (*.csv)")
        if path:
            n = DB.export_csv(path)
            QMessageBox.information(self, self.i18n.t("dict.btn.export_csv"),
                                    self.i18n.t("dict.export.ok", path=path, n=n))

    def _delete_selected_word(self):
        items = self.tbl_dict.selectedItems()
        if not items:
            return
        r = items[0].row()
        wd = self.tbl_dict.item(r, 0).text()
        langs = self.tbl_dict.item(r, 2).text().split("→")
        DB.delete_word(wd, langs[0].strip(), langs[1].strip())
        self.refresh_dict()

    # ---------- основной сценарий: хоткей -> оверлей -> worker -> попап ----------
    def start_capture(self):
        if not OCR_AVAILABLE:
            self.show()
            QMessageBox.warning(self, self.i18n.t("app.title"), self.i18n.t("err.no_ocr"))
            return
        if self.overlay and self.overlay.isVisible():
            return
        self.hide()                                            # окно не должно попасть в скриншот
        QTimer.singleShot(150, self._open_overlay)             # даём оконному менеджеру дорисовать экран

    def _open_overlay(self):
        self.overlay = SelectionOverlay()
        self.overlay.region_selected.connect(self._on_region)
        self.overlay.cancelled.connect(lambda: None)
        self.overlay.showFullScreen()
        self.overlay.activateWindow()

    def _on_region(self, rect: QRect):
        pos = QCursor.pos()
        try:
            img = grab_region(rect)
        except Exception as e:
            log.exception("Capture: ошибка захвата экрана")
            QMessageBox.critical(self, "Capture", str(e))
            return
        self.popup.show_busy(pos)
        if self.worker and self.worker.isRunning():          # не допускаем параллельных OCR-циклов
            self.worker.wait(1500)
        self.worker = TranslateWorker(img, self)
        self.worker.finished_ok.connect(lambda o, d, eng, ms: self._on_done(o, d, pos, ms))
        self.worker.failed.connect(self._on_fail)
        self.worker.start()

    def _on_done(self, original: str, translated: str, pos: QPoint, ms: int):
        s = DB.load_settings()                                # актуальные языки на момент завершения
        self.settings.update(s)
        if original:
            DB.add_history(original, translated, s["source_lang"], s["target_lang"],
                           OCR.preferred_engine(s.get("ocr_engine", "auto")), ms)
            self.refresh_history()
        self.popup.show_result(original, translated, pos)
        self.statusBar().showMessage(f"{self.i18n.t('msg.time')}: {ms} мс", 6000)

    def _on_fail(self, err: str):
        self.popup.hide()
        if err == "no_ocr":
            QMessageBox.warning(self, self.i18n.t("app.title"), self.i18n.t("err.no_ocr"))
        else:
            QMessageBox.critical(self, self.i18n.t("err.translate"), err)

    def closeEvent(self, e):
        e.ignore()
        self.hide()
        if tray:
            tray.showMessage(self.i18n.t("app.title"), self.i18n.t("tray.translate"),
                             QSystemTrayIcon.MessageIcon.Information, 3000)


# -----------------------------------------------------------------------------
# 10. Трей + сборка приложения
# -----------------------------------------------------------------------------

tray: QSystemTrayIcon | None = None
app_main_window: MainWindow | None = None


def make_tray_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#e8c877"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(6, 6, 52, 52)
    p.setPen(QColor("#12121c"))
    f = QFont("Arial", 26, QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "Я")
    p.end()
    return QIcon(pm)


def main():
    global tray, app_main_window
    if MISSING_DEPS:
        print("Не хватает зависимостей:", ", ".join(MISSING_DEPS))
        print("Установите их командой:  pip install PyQt6 deep-translator Pillow")
        sys.exit(1)

    log.info("=== Ёлочка Плюс стартует === Python %s, PyQt6, OCR: %s",
             sys.version.split()[0],
             "+".join([e for e, ok in (("tesseract", HAS_PYTESSERACT), ("easyocr", HAS_EASYOCR)) if ok] or ["none"]))

    # выгружаем эталонные locales наружу (пользователь может редактировать/дополнять без правки кода)
    LOCALES_DIR.mkdir(exist_ok=True)
    for code, data in BUILTIN_LOCALES.items():
        f = LOCALES_DIR / f"{code}.json"
        if not f.exists():
            f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("i18n: создан эталонный файл %s", f.name)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    settings = DB.load_settings()
    i18n = I18n(settings.get("ui_lang", "ru"))

    win = MainWindow(i18n)
    app_main_window = win

    menu = QMenu()
    act_tr = QAction(i18n.t("tray.translate"), menu)
    act_tr.triggered.connect(win.start_capture)
    act_open = QAction(i18n.t("tray.open"), menu)
    act_open.triggered.connect(lambda: (win.showNormal(), win.activateWindow()))
    act_quit = QAction(i18n.t("tray.quit"), menu)
    act_quit.triggered.connect(app.quit)
    menu.addAction(act_tr); menu.addSeparator(); menu.addAction(act_open); menu.addSeparator(); menu.addAction(act_quit)
    win._tray_actions = (act_tr, act_open, act_quit)

    tray = QSystemTrayIcon(make_tray_icon(), app)
    tray.setContextMenu(menu)
    tray.setToolTip(i18n.t("app.title"))
    tray.activated.connect(lambda reason: (win.showNormal(), win.activateWindow())
                           if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    hk = HotkeyManager(win.start_capture)
    log.info("=== Приложение готово к работе ===")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
