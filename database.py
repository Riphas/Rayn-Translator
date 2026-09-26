# -*- coding: utf-8 -*-
"""
database.py — SQLite-хранилище для «Ёлочки Плюс».

Хранит:
    * history   — все пары «оригинал → перевод» с метаданными языков и временем;
    * dictionary— сохранённые словари (пара слов для заучивания);
    * settings  — ключ-значение (язык UI, source_lang, target_lang, тема, хоткей...).

Проектирование под динамические языки:
    Ни одна таблица не «зашивает» конкретный язык — хранятся только ISO-коды
    ('en', 'ru', 'de', 'auto' ...). Добавление нового языка = добавление записи,
    а не миграция схемы.

Потокобезопасность:
    Каждому потоку (QThread перевода, GUI-поток) выдаётся собственное соединение
    через threading.local() — SQLite не любит общие соединения между потоками.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("yolochka.database")

# --------------------------------------------------------------------------- #
#  Значения настроек по умолчанию. Языки — просто ISO-коды, легко расширяемо. #
# --------------------------------------------------------------------------- #
DEFAULT_SETTINGS: dict[str, str] = {
    "ui_language":   "ru",      # локаль интерфейса (locales/<code>.json)
    "source_lang":   "auto",    # язык экрана: 'auto' или ISO-код ('en','de','fr'...)
    "target_lang":   "ru",      # язык перевода
    "hotkey":        "grave",   # «Тильда ~» по умолчанию
    "ocr_engine":    "easyocr", # easyocr | tesseract
    "theme":         "dark",    # dark (Luxury Dark) | light (Clean Light)
    "popup_delay_ms": "15000",
    "db_path":       "",        # пусто => рядом с exe (portable-режим)
}


def _app_dir() -> Path:
    """Каталог программы. В собранном EXE — папка рядом с exe (portable)."""
    if getattr(sys, "frozen", False):                     # PyInstaller
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def default_db_path() -> Path:
    return _app_dir() / "yolochka.db"


class Database:
    """Тонкий потокобезопасный слой над SQLite."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else default_db_path()
        self._local = threading.local()
        self._write_lock = threading.Lock()   # один писатель одновременно
        self._init_schema()
        self._seed_defaults()
        log.info("SQLite initialized at %s", self._path)

    # ------------------------------------------------------------------ #
    #  Соединения (по одному на поток)                                    #
    # ------------------------------------------------------------------ #
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")     # быстрый конкурентный доступ
            conn.execute("PRAGMA foreign_keys=ON;")
            self._local.conn = conn
            log.debug("New SQLite connection for thread %s", threading.current_thread().name)
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------ #
    #  Схема                                                              #
    # ------------------------------------------------------------------ #
    def _init_schema(self) -> None:
        with self._write_lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS history (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    original     TEXT    NOT NULL,          -- распознанный (очищенный) текст
                    translated   TEXT,                      -- перевод (NULL если ошибка API)
                    source_lang  TEXT    NOT NULL,          -- ISO-код или 'auto'
                    target_lang  TEXT    NOT NULL,          -- ISO-код
                    detected_lang TEXT,                     -- что определил авто-детектор
                    ocr_engine   TEXT,                      -- easyocr / tesseract
                    elapsed_ms   INTEGER DEFAULT 0,         -- время пайплайна OCR+перевод
                    created_at   TEXT    NOT NULL           -- ISO-8601
                );
                CREATE INDEX IF NOT EXISTS idx_history_created ON history(created_at DESC);

                CREATE TABLE IF NOT EXISTS dictionary (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    word        TEXT    NOT NULL COLLATE NOCASE,  -- кликнутое слово из истории
                    translation TEXT    NOT NULL,
                    source_lang TEXT    NOT NULL,
                    target_lang TEXT    NOT NULL,
                    context     TEXT,                            -- фрагмент истории-источника
                    added_at    TEXT    NOT NULL,
                    UNIQUE(word, source_lang, target_lang)       -- без дублей в паре языков
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def _seed_defaults(self) -> None:
        cur = self.conn.cursor()
        inserted = 0
        with self._write_lock, self.conn:
            for k, v in DEFAULT_SETTINGS.items():
                cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
                inserted += cur.rowcount
        if inserted:
            log.info("Seeded %d default settings", inserted)

    # ------------------------------------------------------------------ #
    #  Настройки (ключ-значение)                                          #
    # ------------------------------------------------------------------ #
    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        old = self.get_setting(key)
        with self._write_lock, self.conn:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
        if key in ("ui_language", "source_lang", "target_lang"):
            # Логируем переключение языков — требование к логированию.
            log.info("Language setting changed: %s: %r -> %r", key, old, value)
        else:
            log.debug("Setting changed: %s: %r -> %r", key, old, value)

    def get_all_settings(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        merged = dict(DEFAULT_SETTINGS)
        merged.update({r["key"]: r["value"] for r in rows})
        return merged

    # ------------------------------------------------------------------ #
    #  История                                                            #
    # ------------------------------------------------------------------ #
    def add_history(self, original: str, translated: Optional[str],
                    source_lang: str, target_lang: str,
                    detected_lang: str = "", ocr_engine: str = "",
                    elapsed_ms: int = 0) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO history(original, translated, source_lang, target_lang,"
                " detected_lang, ocr_engine, elapsed_ms, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (original, translated, source_lang, target_lang,
                 detected_lang, ocr_engine, elapsed_ms, now),
            )
        log.info("History #%s stored (%d→%d chars, %d ms)",
                 cur.lastrowid, len(original), len(translated or ""), elapsed_ms)
        return int(cur.lastrowid)

    def history(self, limit: int = 500, offset: int = 0,
                search: str = "", langs: Iterable[str] = ()) -> list[sqlite3.Row]:
        sql = "SELECT * FROM history"
        where, params = [], []
        if search:
            where.append("(original LIKE ? OR translated LIKE ?)")
            like = f"%{search}%"
            params += [like, like]
        lang_list = [l for l in langs if l]
        if lang_list:
            ph = ",".join("?" * len(lang_list))
            where.append(f"(source_lang IN ({ph}) OR target_lang IN ({ph}))")
            params += lang_list * 2
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return self.conn.execute(sql, params).fetchall()

    def history_count(self, search: str = "") -> int:
        if search:
            like = f"%{search}%"
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM history WHERE original LIKE ? OR translated LIKE ?",
                (like, like)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) c FROM history").fetchone()
        return int(row["c"])

    def delete_history(self, ids: Iterable[int]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        with self._write_lock, self.conn:
            cur = self.conn.execute(f"DELETE FROM history WHERE id IN ({ph})", ids)
        log.info("Deleted %d history rows", cur.rowcount)
        return cur.rowcount

    def clear_history(self) -> None:
        with self._write_lock, self.conn:
            self.conn.execute("DELETE FROM history")
        log.warning("History cleared by user")

    # ------------------------------------------------------------------ #
    #  Словарь                                                            #
    # ------------------------------------------------------------------ #
    def add_word(self, word: str, translation: str,
                 source_lang: str, target_lang: str, context: str = "") -> bool:
        """INSERT OR IGNORE — повтор в той же паре языков игнорируется.
        Возвращает True, если слово реально добавлено."""
        word = word.strip()
        if not word or not translation:
            return False
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO dictionary"
                "(word, translation, source_lang, target_lang, context, added_at)"
                " VALUES (?,?,?,?,?,?)",
                (word, translation, source_lang, target_lang, context[:280], now),
            )
        added = cur.rowcount > 0
        log.info("Dictionary add %r [%s→%s]: %s", word, source_lang, target_lang,
                 "ok" if added else "duplicate ignored")
        return added

    def words(self, search: str = "", limit: int = 2000) -> list[sqlite3.Row]:
        if search:
            like = f"%{search}%"
            return self.conn.execute(
                "SELECT * FROM dictionary WHERE word LIKE ? OR translation LIKE ?"
                " ORDER BY added_at DESC LIMIT ?", (like, like, limit)).fetchall()
        return self.conn.execute(
            "SELECT * FROM dictionary ORDER BY added_at DESC LIMIT ?", (limit,)).fetchall()

    def delete_words(self, ids: Iterable[int]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        with self._write_lock, self.conn:
            cur = self.conn.execute(f"DELETE FROM dictionary WHERE id IN ({ph})", ids)
        return cur.rowcount

    def export_pairs(self) -> list[tuple[str, str]]:
        """Все пары (word, translation) для экспорта в Quizlet/CSV."""
        rows = self.conn.execute(
            "SELECT word, translation FROM dictionary ORDER BY word COLLATE NOCASE"
        ).fetchall()
        return [(r["word"], r["translation"]) for r in rows]

    # ------------------------------------------------------------------ #
    #  Экспорт                                                            #
    # ------------------------------------------------------------------ #
    def export_quizlet_txt(self, dest: str | Path) -> int:
        """Quizlet импортирует plain-text вида «term<TAB>definition»."""
        pairs = self.export_pairs()
        dest = Path(dest)
        dest.write_text("\n".join(f"{w}\t{t}" for w, t in pairs), encoding="utf-8")
        log.info("Exported %d pairs (Quizlet TSV) -> %s", len(pairs), dest)
        return len(pairs)

    def export_csv(self, dest: str | Path) -> int:
        import csv
        rows = self.conn.execute(
            "SELECT word, translation, source_lang, target_lang, added_at"
            " FROM dictionary ORDER BY word COLLATE NOCASE").fetchall()
        dest = Path(dest)
        with dest.open("w", newline="", encoding="utf-8-sig") as fh:   # BOM для Excel
            w = csv.writer(fh, delimiter=",")
            w.writerow(["word", "translation", "source_lang", "target_lang", "added_at"])
            w.writerows([tuple(r) for r in rows])
        log.info("Exported %d pairs (CSV) -> %s", len(rows), dest)
        return len(rows)


if __name__ == "__main__":                                   # быстрая самопроверка
    logging.basicConfig(level=logging.DEBUG)
    db = Database(":memory:")
    db.set_setting("source_lang", "de")
    hid = db.add_history("Hallo Welt", "Привет мир", "de", "ru", "de", "easyocr", 940)
    assert db.get_setting("source_lang") == "de"
    assert db.history()[0]["original"] == "Hallo Welt"
    assert db.add_word("Hallo", "Привет", "de", "ru") is True
    assert db.add_word("hallo", "Привет", "de", "ru") is False   # NOCASE-уникальность
    n = db.export_quizlet_txt(Path("out_test.txt"))
    assert n == 1 and Path("out_test.txt").read_text(encoding="utf-8") == "Hallo\tПривет"
    print("database.py self-test OK, history id =", hid)
