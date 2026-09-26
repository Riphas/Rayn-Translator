# -*- coding: utf-8 -*-
"""
main.py — точка входа «Ёлочка Плюс»: трей, глобальный хоткей, сборка модулей.

Запуск:     python main.py        (разработка)
Portable:   pyinstaller --onefile --noconsole --add-data "locales;locales" main.py

Логирование всех этапов — в app.log рядом с exe.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# --------------------------------------------------------------------------- #
#  Логирование (файл app.log + консоль в dev-режиме)                          #
# --------------------------------------------------------------------------- #
def _app_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    fh = RotatingFileHandler(_app_dir() / "app.log", maxBytes=2_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger("yolochka")
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    if not getattr(sys, "frozen", False):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)
        root.addHandler(sh)


setup_logging()
log = logging.getLogger("yolochka.main")

from PyQt6.QtCore import QPoint, Qt, pyqtSignal                                # noqa: E402
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap  # noqa: E402
from PyQt6.QtWidgets import (QApplication, QMenu, QSystemTrayIcon, # noqa: E402
                             QWidget)

from capture import SelectionOverlay, TranslationPopup, grab_region  # noqa: E402
from database import Database                                       # noqa: E402
from gui import I18n, MainWindow                                    # noqa: E402
from translator import CaptureWorker, OcrEngine                     # noqa: E402


# --------------------------------------------------------------------------- #
#  Иконка трея рисуем кодом (не нужен внешний .ico => проще для portable EXE) #
# --------------------------------------------------------------------------- #
def make_tray_icon() -> QIcon:
    """Иконка-«ёлочка» из трёх треугольников — без внешних .ico (portable-friendly)."""
    from PyQt6.QtCore import QPointF
    pm = QPixmap(64, 64); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#2e7d32"))
    for top, h, half in ((6, 18, 12), (18, 18, 18), (30, 18, 24)):
        p.drawPolygon(QPointF(32, top), QPointF(32 - half, top + h),
                      QPointF(32 + half, top + h))
    p.fillRect(29, 48, 6, 10, QColor("#795548"))      # ствол
    p.setBrush(QColor("#ffd54f"))                     # звезда
    p.drawEllipse(28, 0, 8, 8)
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------- #
#  Приложение-координатор                                                     #
# --------------------------------------------------------------------------- #
class YolochkaApp(QWidget):
    hotkey_pressed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.hide()
        self.db = Database()
        s = self.db.get_all_settings()

        self.i18n = I18n(fallback="en")
        self.i18n.set_lang(s["ui_language"])
        self.i18n.changed.connect(self._on_ui_lang_changed)

        self.ocr = OcrEngine(engine=s["ocr_engine"])
        self.main_window = MainWindow(self.db, self.i18n, theme=s["theme"])
        self.overlay = SelectionOverlay(i18n=self.i18n)
        self.overlay.selected.connect(self._on_region_selected)
        self.popup = TranslationPopup(self.i18n, delay_ms=int(s["popup_delay_ms"]))
        self.popup.word_clicked.connect(self._on_word_clicked)

        self.hotkey_pressed.connect(self.start_capture)
        self.worker: CaptureWorker | None = None
        self._cursor_pos = QPoint()

        self._build_tray()
        self._register_hotkey(s["hotkey"])
        log.info("Application started (source=%s target=%s ui=%s)",
                 s["source_lang"], s["target_lang"], self.i18n.lang)

    # -- трей -------------------------------------------------------------- #
    def _build_tray(self) -> None:
        t = self.i18n.tr
        self.tray_menu = QMenu()
        self.act_capture = QAction(t("tray.capture"), self)
        self.act_history = QAction(t("tray.history"), self)
        self.act_dict = QAction(t("tray.dictionary"), self)
        self.act_quit = QAction(t("tray.quit"), self)
        for a in (self.act_capture, self.act_history, self.act_dict):
            self.tray_menu.addAction(a)
        self.tray_menu.addSeparator(); self.tray_menu.addAction(self.act_quit)
        self.act_capture.triggered.connect(self.start_capture)
        self.act_history.triggered.connect(lambda: self._show_tab(0))
        self.act_dict.triggered.connect(lambda: self._show_tab(1))
        self.act_quit.triggered.connect(QApplication.instance().quit)

        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip(t("tray.tooltip"))
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(
            lambda r: self._show_tab(0)
            if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def _show_tab(self, idx: int) -> None:
        self.main_window.tabs.setCurrentIndex(idx)
        self.main_window.show(); self.main_window.raise_()

    # -- глобальный хоткей (~ по умолчанию) --------------------------------- #
    def _register_hotkey(self, key_name: str) -> None:
        """Windows: RegisterHotKey через ctypes. Кнопка настраивается в БД."""
        if sys.platform != "win32":
            log.warning("Global hotkey is Windows-only; use tray menu on %s", sys.platform)
            return
        import ctypes
        from ctypes import wintypes
        VK = {"grave": 0xC0, "f1": 0x70, "f2": 0x71, "space": 0x20,
              "printscreen": 0x2C}                    # расширяемый словарь клавиш
        vk = VK.get(key_name.lower(), 0xC0)
        MOD_NONE = 0x0000
        self._user32 = ctypes.windll.user32
        if not self._user32.RegisterHotKey(None, 1, MOD_NONE, vk):
            log.error("RegisterHotKey failed (err=%s)", ctypes.get_last_error())
        else:
            log.info("Global hotkey registered: %s (VK=0x%02X)", key_name, vk)
            self._poll_hotkey()

    def _poll_hotkey(self) -> None:
        """Отдельный поток с GetMessage-циклом ловит WM_HOTKEY и гасит его PostQuitThread-безопасно."""
        import ctypes, threading
        from ctypes import wintypes

        def loop():
            msg = wintypes.MSG()
            while True:
                r = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r == 0 or r == -1:
                    break
                if msg.message == 0x0312 and msg.wParam == 1:      # WM_HOTKEY
                    self.hotkey_pressed.emit()                     # в GUI через queued signal
                self._user32.TranslateMessage(ctypes.byref(msg))
                self._user32.DispatchMessageW(ctypes.byref(msg))
        threading.Thread(target=loop, daemon=True, name="hotkey-loop").start()

    # -- пайплайн захвата ----------------------------------------------------- #
    def start_capture(self) -> None:
        if self.worker and self.worker.isRunning():
            log.info("Capture ignored: previous job still running")
            return
        self._cursor_pos = QCursor_pos()
        self.overlay.start()

    def _on_region_selected(self, rect) -> None:
        from PyQt6.QtGui import QImage
        pm = grab_region(rect)
        image = pm.toImage()
        import PIL.Image as Image
        qimg = image.convertToFormat(image.Format.Format_RGB888)
        ptr = qimg.constBits(); ptr.setsize(qimg.sizeInBytes() if hasattr(qimg, "sizeInBytes")
                                            else qimg.byteCount())
        pil_img = Image.frombytes("RGB", (qimg.width(), qimg.height()),
                                  bytes(ptr), "raw", "BGR", qimg.bytesPerLine())
        s = self.db.get_all_settings()
        self.popup.show_busy(self._cursor_pos)
        self.worker = CaptureWorker(pil_img, self.ocr,
                                    s["source_lang"], s["target_lang"])
        self.worker.finished_ok.connect(self._on_result)
        self.worker.failed.connect(self._on_fail)
        self.worker.start()

    def _on_result(self, res) -> None:
        self.db.add_history(res.original, res.translated, res.source_lang,
                            res.target_lang, res.detected_lang, res.ocr_engine,
                            res.total_ms)
        self.main_window.refresh_history()
        self.popup.show_result(res.original, res.translated, res.total_ms,
                               self._cursor_pos)

    def _on_fail(self, err: str) -> None:
        if err == "no_text":
            self.popup.show_error(self.i18n.tr("history.empty"), self._cursor_pos)
        else:
            self.popup.show_error(err, self._cursor_pos)

    def _on_word_clicked(self, word: str, translated: str) -> None:
        s = self.db.get_all_settings()
        self.db.add_word(word, translated, s["source_lang"], s["target_lang"])
        self.main_window.refresh_dictionary()
        self.tray.showMessage(self.i18n.tr("app_name"), word,
                              QSystemTrayIcon.MessageIcon.Information, 2000)

    # -- смена языка UI на лету ------------------------------------------------ #
    def _on_ui_lang_changed(self, code: str) -> None:
        t = self.i18n.tr
        self.tray.setToolTip(t("tray.tooltip"))
        self.act_capture.setText(t("tray.capture"))
        self.act_history.setText(t("tray.history"))
        self.act_dict.setText(t("tray.dictionary"))
        self.act_quit.setText(t("tray.quit"))
        log.info("Tray menu retranslated to %s", code)


def QCursor_pos() -> QPoint:
    from PyQt6.QtGui import QCursor
    return QCursor.pos()


def main() -> int:
    log.info("=" * 60)
    log.info("Yolochka Plus starting, python=%s frozen=%s",
             sys.version.split()[0], getattr(sys, "frozen", False))
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)              # живём в трее
    icon = make_tray_icon()
    app.setWindowIcon(icon)
    controller = YolochkaApp()
    rc = app.exec()
    log.info("Application exited with code %s", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
