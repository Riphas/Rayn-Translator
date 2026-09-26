# -*- coding: utf-8 -*-
"""
gui.py — окна, темы (Luxury Dark / Clean Light) и i18n-менеджер.

Все строки интерфейса берутся из locales/*.json — хардкод текста запрещён.
Смена языка UI и темы происходит НА ЛЕТУ: I18n и ThemeManager рассылают
сигнал changed, главное окно пересобирает подписи и применяет новый QSS.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QHeaderView,
                             QLabel, QLineEdit, QMainWindow, QPushButton,
                             QStatusBar, QTabWidget, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

log = logging.getLogger("yolochka.gui")

LOCALES_DIR = Path(__file__).resolve().parent / "locales"

# --------------------------------------------------------------------------- #
#  Темы                                                                       #
# --------------------------------------------------------------------------- #
THEMES: dict[str, dict[str, str]] = {
    "dark": {   # Luxury Dark — графит + золото
        "bg": "#1e2128", "panel": "#262a33", "text": "#e8e6e1",
        "accent": "#c9a959", "border": "#3a3f4b", "sel": "#39404e",
    },
    "light": {  # Clean Light — белый + стальной синий
        "bg": "#f5f6f8", "panel": "#ffffff", "text": "#20242b",
        "accent": "#2f6fb2", "border": "#d7dbe0", "sel": "#e3edf7",
    },
}


def build_qss(theme: str) -> str:
    c = THEMES.get(theme, THEMES["dark"])
    return f"""
    QWidget {{ background:{c['bg']}; color:{c['text']};
               font-family:'Segoe UI'; font-size:10pt; }}
    QTabWidget::pane {{ border:1px solid {c['border']}; }}
    QTabBar::tab {{ padding:6px 16px; border:1px solid {c['border']};
                    border-bottom:none; background:{c['panel']}; }}
    QTabBar::tab:selected {{ color:{c['accent']}; font-weight:600; }}
    QTableWidget {{ gridline-color:{c['border']}; alternate-background-color:{c['panel']}; }}
    QTableWidget::item:selected {{ background:{c['sel']}; }}
    QPushButton {{ background:{c['accent']}; color:{c['bg']};
                   border-radius:4px; padding:6px 14px; font-weight:600; }}
    QPushButton:hover {{ opacity:0.9; }}
    QLineEdit, QComboBox {{ background:{c['panel']}; border:1px solid {c['border']};
                            border-radius:4px; padding:4px 8px; }}
    QHeaderView::section {{ background:{c['panel']}; color:{c['accent']};
                            padding:6px; border:none; }}
    QStatusBar {{ background:{c['panel']}; color:{c['text']}; }}
    """


# --------------------------------------------------------------------------- #
#  I18N-менеджер                                                              #
# --------------------------------------------------------------------------- #
class I18n(QObject):
    """tr('history.title') -> 'История'/'History'. Смена языка на лету."""

    changed = pyqtSignal(str)            # новый код локали

    def __init__(self, fallback: str = "en") -> None:
        super().__init__()
        self.fallback = fallback
        self.lang = fallback
        self._data: dict[str, dict] = {}
        self.available: list[str] = []
        self.reload_catalogs()

    def reload_catalogs(self) -> None:
        self._data.clear()
        for p in sorted(LOCALES_DIR.glob("*.json")):
            try:
                self._data[p.stem] = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                log.exception("Bad locale file %s", p)
        self.available = list(self._data)
        log.info("Loaded locales: %s", ", ".join(self.available))

    def set_lang(self, code: str) -> None:
        if code not in self._data:
            log.warning("Locale %r missing, keeping %r", code, self.lang)
            return
        self.lang = code
        log.info("UI language switched -> %s", code)
        self.changed.emit(code)

    def tr(self, key: str, **fmt) -> str:
        node = self._data.get(self.lang) or self._data.get(self.fallback, {})
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                log.debug("i18n miss: %s.%s", self.lang, key)
                return key                       # ключ лучше, чем пустая строка
            node = node[part]
        return node.format(**fmt) if fmt and isinstance(node, str) else node


# --------------------------------------------------------------------------- #
#  Главное окно с вкладками                                                   #
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, db, i18n: I18n, theme: str = "dark") -> None:
        super().__init__()
        self.db, self.i18n = db, i18n
        i18n.changed.connect(lambda _c: self._retranslate())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_history_tab(), "")
        self.tabs.addTab(self._build_dictionary_tab(), "")
        self.tabs.addTab(self._build_settings_tab(), "")
        self.setCentralWidget(self.tabs)
        self.setStatusBar(QStatusBar())
        self.resize(900, 600)
        self.apply_theme(theme)
        self._retranslate()

    # -- темы --------------------------------------------------------------- #
    def apply_theme(self, name: str) -> None:
        QApplication.instance().setStyleSheet(build_qss(name))
        log.info("Theme applied: %s", name)

    # -- конструкторы вкладок ---------------------------------------------- #
    def _build_history_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        self.search_edit = QLineEdit(); self.search_edit.textChanged.connect(self.refresh_history)
        self.hist_table = QTableWidget(0, 5); self.hist_table.setAlternatingRowColors(True)
        self.hist_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.hist_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        btn_add = QPushButton(""); btn_add.setObjectName("btnAddWord")
        btn_add.clicked.connect(self._word_from_history)
        btn_del = QPushButton(""); btn_del.setObjectName("btnDelHist")
        btn_del.clicked.connect(lambda: (self.db.delete_history(
            [s.row() for s in self.hist_table.selectedItems()]), self.refresh_history()))
        lay.addWidget(self.search_edit); lay.addWidget(self.hist_table)
        row = QHBoxLayout(); row.addWidget(btn_add); row.addWidget(btn_del)
        lay.addLayout(row)
        return w

    def _build_dictionary_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        self.dict_table = QTableWidget(0, 4); self.dict_table.setAlternatingRowColors(True)
        self.dict_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        b_quizlet = QPushButton(""); b_quizlet.setObjectName("btnQuizlet")
        b_csv = QPushButton(""); b_csv.setObjectName("btnCsv")
        b_del = QPushButton(""); b_del.setObjectName("btnDelDict")
        b_quizlet.clicked.connect(lambda: self._export("txt"))
        b_csv.clicked.connect(lambda: self._export("csv"))
        b_del.clicked.connect(lambda: (self.db.delete_words(
            [s.row() + 1 for s in self.dict_table.selectedItems()]), self.refresh_dictionary()))
        lay.addWidget(self.dict_table)
        row = QHBoxLayout(); row.addWidget(b_quizlet); row.addWidget(b_csv); row.addWidget(b_del)
        lay.addLayout(row)
        return w

    def _build_settings_tab(self) -> QWidget:
        from translator import SUPPORTED_TRANSLATE_LANGS   # матрица языков
        w = QWidget(); lay = QVBoxLayout(w); s = self.db.get_all_settings()

        self.setting_labels: dict[str, QLabel] = {}

        def combo(key: str, items: list[tuple[str, str]], current: str):
            lbl = QLabel("")                             # текст проставит _retranslate
            lbl.setObjectName(f"lbl_{key}")
            lay.addWidget(lbl)
            self.setting_labels[key] = lbl
            cb = QComboBox()
            cb.setObjectName(f"set_{key}")
            for code, label in items:
                cb.addItem(label, code)
            idx = next((i for i, (c, _) in enumerate(items) if c == current), 0)
            cb.setCurrentIndex(idx)
            cb.currentIndexChanged.connect(
                lambda _i, k=key, box=cb: self.db.set_setting(k, box.currentData()))
            lay.addWidget(cb)
            return cb

        iso_items = [("auto", "Auto")] + [(c, c) for c in SUPPORTED_TRANSLATE_LANGS]
        combo("ui_language", [(c, c) for c in self.i18n.available], s["ui_language"])
        combo("source_lang", iso_items, s["source_lang"])
        combo("target_lang", [(c, c) for c in SUPPORTED_TRANSLATE_LANGS], s["target_lang"])
        combo("theme", [("dark", ""), ("light", "")], s["theme"])
        save = QPushButton(""); save.setObjectName("btnSave")
        save.clicked.connect(self._save_settings)
        lay.addWidget(save); lay.addStretch()
        return w

    # -- данные -------------------------------------------------------------- #
    def refresh_history(self) -> None:
        rows = self.db.history(search=self.search_edit.text())
        self.hist_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate((row["original"], row["translated"] or "",
                                     row["source_lang"], row["target_lang"],
                                     row["created_at"])):
                it = QTableWidgetItem(val)
                if c >= 2:
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.hist_table.setItem(r, c, it)
        self.statusBar().showMessage(self.i18n.tr("history.status_bar", count=len(rows)))

    def refresh_dictionary(self) -> None:
        rows = self.db.words()
        self.dict_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate((row["word"], row["translation"],
                                     f"{row['source_lang']}→{row['target_lang']}",
                                     row["added_at"])):
                self.dict_table.setItem(r, c, QTableWidgetItem(val))

    def _word_from_history(self) -> None:
        items = self.hist_table.selectedItems()
        if len(items) < 2:
            return
        src = items[0].text().split()[:1]
        tgt = items[1].text().split()[:1]
        if src and tgt:
            self.db.add_word(src[0], tgt[0],
                             items[2].text(), items[3].text(), items[0].text()[:280])
            self.refresh_dictionary()

    def _export(self, kind: str) -> None:
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "", f"dictionary.{'txt' if kind == 'txt' else 'csv'}")
        if not path:
            return
        n = self.db.export_quizlet_txt(path) if kind == "txt" else self.db.export_csv(path)
        self.statusBar().showMessage(f"{self.i18n.tr('dictionary.export_done', count=n)} {path}")

    def _save_settings(self) -> None:
        self.statusBar().showMessage(self.i18n.tr("settings.saved"), 5000)

    # -- локализация на лету -------------------------------------------------- #
    def _retranslate(self) -> None:
        t = self.i18n.tr
        self.setWindowTitle(t("app_name"))
        self.tabs.setTabText(0, t("tabs.history"))
        self.tabs.setTabText(1, t("tabs.dictionary"))
        self.tabs.setTabText(2, t("tabs.settings"))
        self.search_edit.setPlaceholderText(t("history.search_placeholder"))
        self.hist_table.setHorizontalHeaderLabels(
            [t("history.original"), t("history.translated"), t("history.source_lang"),
             t("history.target_lang"), t("history.datetime")])
        self.dict_table.setHorizontalHeaderLabels(
            [t("dictionary.word"), t("dictionary.translation"),
             t("dictionary.lang_pair"), t("dictionary.added")])
        names = {"btnAddWord": t("history.add_to_dict"), "btnDelHist": t("history.delete"),
                 "btnQuizlet": t("dictionary.export_quizlet"), "btnCsv": t("dictionary.export_csv"),
                 "btnDelDict": t("dictionary.delete"), "btnSave": t("settings.save")}
        for obj, text in names.items():
            w = self.findChild(QPushButton, obj)
            if w:
                w.setText(text)
        # подписи настроек — так же из JSON, без хардкода
        label_keys = {"ui_language": "settings.ui_language", "source_lang": "settings.source_lang",
                      "target_lang": "settings.target_lang", "theme": "settings.theme"}
        for key, lbl in getattr(self, "setting_labels", {}).items():
            lbl.setText(t(label_keys.get(key, key)))
        theme_cb = self.findChild(QComboBox, "set_theme")
        if theme_cb:
            theme_cb.setItemText(0, t("settings.theme_dark"))
            theme_cb.setItemText(1, t("settings.theme_light"))
            theme_cb.currentIndexChanged.connect(
                lambda i, box=theme_cb: self.apply_theme(box.currentData()))
        self.refresh_history()
        self.refresh_dictionary()
