# -*- coding: utf-8 -*-
"""
capture.py — оверлей выделения области экрана + попап с переводом у курсора.

* SelectionOverlay: полноэкранное полупрозрачное затемнение, рамка выделения,
  подсказка из i18n («Выделите область мышью · Esc — отмена»).
* TranslationPopup: маленькое окно рядом с курсором: оригинал, перевод,
  кликабельные слова (в словарь), кнопки «Копировать» / «Добавить слово».
* grab_region(): снимок прямоугольника экрана через QScreen.grabWindow.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QPainter, QPen, QPixmap)
from PyQt6.QtWidgets import (QApplication, QLabel, QPushButton, QVBoxLayout,
                             QWidget)

log = logging.getLogger("yolochka.capture")


def grab_region(rect: QRect) -> "QPixmap":
    """Снимок произвольной области виртуального экрана (учитывает мультимонитор)."""
    screen = QApplication.primaryScreen()
    pm = screen.grabWindow(0, rect.x(), rect.y(), rect.width(), rect.height())
    pm.setDevicePixelRatio(1.0)
    log.debug("grab_region %dx%d at (%d,%d)",
              rect.width(), rect.height(), rect.x(), rect.y())
    return pm


class SelectionOverlay(QWidget):
    """Затемнённый экран выделения. Сигнал selected(QRect) в глобальных координатах."""

    selected = pyqtSignal(QRect)
    cancelled = pyqtSignal()

    def __init__(self, i18n=None) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._origin = QPoint()
        self._current = QPoint()
        self._dragging = False
        self._i18n = i18n
        # закрыть на весь виртуальный стол (несколько мониторов)
        geo = QApplication.virtualGeometry() if hasattr(QApplication, "virtualGeometry") \
            else QApplication.primaryScreen().geometry()
        self.setGeometry(geo)

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._dragging = False
        self._current = self._origin = QPoint()
        self.showFullScreen()
        self.activateWindow()
        log.info("Selection overlay shown")

    def keyPressEvent(self, e) -> None:                    # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            log.debug("Selection cancelled by Esc")
            self.hide()
            self.cancelled.emit()

    def mousePressEvent(self, e) -> None:                  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._origin = e.globalPosition().toPoint()
            self._current = self._origin
            self._dragging = True

    def mouseMoveEvent(self, e) -> None:                   # noqa: N802
        if self._dragging:
            self._current = e.globalPosition().toPoint()
            self.update()

    def mouseReleaseEvent(self, e) -> None:                # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton or not self._dragging:
            return
        self._dragging = False
        self.hide()
        rect = QRect(self._origin, self._current).normalized()
        if rect.width() < 5 or rect.height() < 5:          # случайный клик
            log.debug("Selection too small, ignored")
            self.cancelled.emit()
            return
        # e.globalPosition() уже даёт глобальные координаты мыши — rect и есть итог
        log.info("Region selected: %dx%d", rect.width(), rect.height())
        self.selected.emit(rect)


    # ------------------------------------------------------------------ #
    def paintEvent(self, e) -> None:                       # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 130))      # затемнение
        if self._dragging or not self._current.isNull():
            r = QRect(self._origin, self._current).normalized()
            local = r.translated(-self.geometry().topLeft())
            p.setPen(QPen(QColor("#c9a959"), 2, Qt.PenStyle.DashLine))
            p.drawRect(local)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(local, Qt.GlobalColor.transparent)  # «прореваем» затемнение
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        hint = self._i18n.tr("overlay.hint") if self._i18n else "Select area / Esc"
        p.setPen(QColor(255, 255, 255, 220))
        p.setFont(QFont("Segoe UI", 12))
        p.drawText(self.rect().adjusted(0, 60, 0, 0), Qt.AlignmentFlag.AlignHCenter, hint)


class TranslationPopup(QWidget):
    """Попап с переводом у курсора; слова кликабельны (сигнал word_clicked)."""

    word_clicked = pyqtSignal(str, str)     # (word, whole_translation_context)

    def __init__(self, i18n, delay_ms: int = 15000) -> None:
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self._i18n, self._delay = i18n, delay_ms
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        lay = QVBoxLayout(self)
        self.lbl_original = QLabel(); self.lbl_original.setWordWrap(True)
        self.lbl_translated = QLabel(); self.lbl_translated.setWordWrap(True)
        f = QFont("Segoe UI", 11, QFont.Weight.Bold); self.lbl_translated.setFont(f)
        self.btn_copy = QPushButton(i18n.tr("popup.copy"))
        self.btn_close = QPushButton(i18n.tr("popup.close"))
        self.btn_close.clicked.connect(self.hide)
        lay.addWidget(self.lbl_original)
        lay.addWidget(self.lbl_translated)
        lay.addWidget(self.btn_copy)
        lay.addWidget(self.btn_close)
        self.setStyleSheet(
            "background:#262a33;color:#e8e6e1;border:1px solid #c9a959;"
            "padding:10px; max-width:420px;")
        self.btn_copy.clicked.connect(self._copy)
        self._timer = QTimer(self, singleShot=True, timeout=self.hide)
        self.setMaximumWidth(460)

    # -- API ------------------------------------------------------------- #
    def show_busy(self, pos: QPoint) -> None:
        self.lbl_original.setText("")
        self.lbl_translated.setText(self._i18n.tr("popup.translating"))
        self._show_at(pos)

    def show_result(self, original: str, translated: str,
                    elapsed_ms: int, pos: QPoint) -> None:
        self.lbl_original.setText(original)
        # кликабельные слова — через rich text: <a href="word">word</a>
        from translator import extract_words
        words = extract_words(original)
        links = " ".join(f'<a href="{w}" style="color:#c9a959">{w}</a>' for w in words[:40])
        self.lbl_translated.setText(
            f"{translated}<br><small>{self._i18n.tr('popup.time_ms', ms=elapsed_ms)}</small>"
            f"<hr>{links}")
        self.lbl_translated.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_translated.setOpenExternalLinks(False)
        self.lbl_translated.linkActivated.connect(
            lambda w: self.word_clicked.emit(w, translated))
        self._last_pair = (original, translated)
        self._show_at(pos)

    def show_error(self, message: str, pos: QPoint) -> None:
        self.lbl_translated.setText(
            self._i18n.tr("popup.error_translate", error=message))
        self._show_at(pos)

    # -- internals --------------------------------------------------------- #
    def _show_at(self, pos: QPoint) -> None:
        self.adjustSize()
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        g = screen.availableGeometry()
        x = min(max(pos.x() + 16, g.left()), g.right() - self.width() - 8)
        y = min(max(pos.y() + 16, g.top()), g.bottom() - self.height() - 8)
        self.move(x, y)
        self.show(); self.raise_()
        self._timer.start(self._delay)
        log.debug("Popup shown at (%d,%d)", x, y)

    def _copy(self) -> None:
        QApplication.clipboard().setText(getattr(self, "_last_pair", ("", ""))[1])
