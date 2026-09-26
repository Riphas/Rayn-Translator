#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ёлочка Плюс — расширенный экранный переводчик (монолитный файл app.py).

Возможности:
  * Горячая клавиша "~" (тильда) -> затемнённый оверлей выделения области экрана.
    Оверлей и захват корректно работают на мониторах Windows с High-DPI
    масштабированием 125% / 150% (логические/физические координаты, PassThrough).
  * OCR выбранной области (pytesseract / EasyOCR). Языки НЕ зашиты в логику:
    QComboBox "Язык экрана (OCR)" (auto, en, zh, ja) и "Язык перевода" (ru, en, es)
    из вкладки "Настройки" подставляют ISO-коды в движки на лету, без перезапуска.
  * Асинхронный перевод через deep_translator (GoogleTranslator) в QThread;
    попап с переводом появляется у курсора мыши (целевое время < 2 секунд).
  * Regex-очистка текста после OCR (игровой/системный мусор: | _ ~ @ ° и т.п.,
    лишние пробелы, оборванные знаки препинания, переносы слов).
  * Вкладка "История": все переводы; при выборе записи оригинал разбивается на
    кликабельные кнопки-слова. Клик по слову -> мгновенный фоновый перевод
    ТОЛЬКО этого слова -> карточка с кнопкой "Добавить в Словарь".
  * Вкладка "Словарь": таблица пар "Оригинал - Перевод", кнопка "Экспорт для
    Quizlet" сохраняет .txt, где слово и перевод разделены символом табуляции \\t.
  * Системный трей, темы Luxury Dark / Clean Light (переключатель в настройках),
    SQLite (история/словарь/настройки), подробное логирование в app.log.

Зависимости: PyQt6, deep-translator, Pillow; опционально pytesseract (+ бинарь
Tesseract) и easyocr. Установка:  pip install PyQt6 deep-translator Pillow
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
import subprocess
import contextlib
import importlib.util
from pathlib import Path

# =============================================================================
# -1. АВТОМАТИЧЕСКАЯ НАСТРОЙКА ПЕРВОГО ЗАПУСКА (полностью автономно).
#     Этап А: проверка/установка pip-пакетов (find_spec + скрытый pip install).
#     Этап Б: ИНИЦИАЛИЗАЦИЯ EasyOCR СТРОГО ДО ОТКРЫТИЯ ГЛАВНОГО ОКНА — heavy
#             `easyocr.Reader(['en','ru'], gpu=False)` больше НИКОГДА не
#             вызывается из пайплайна захвата и не «замораживает» его.
#     Всё это время на экране живёт заставка (Tkinter-окно с status_label),
#     которая показывает понятные стадии: Шаг 1/3 (пакеты), Шаг 2/3 (веса
#     CRAFT), Шаг 3/3 (языковые модули), а также прогресс скачивания из
#     urllib/tqdm (EasyOCR грузит модели именно так в ~/.EasyOCR/model/).
#     При сетевой ошибке заставка мгновенно показывает текст ошибки и
#     останавливает запуск — приложение не падает молча.
#     Если Tkinter недоступен — стадии пишутся в консоль и app.log.
# =============================================================================

# модуль -> пакет для pip
REQUIRED_PACKAGES = {
    "PyQt6":           "PyQt6",
    "deep_translator": "deep-translator",
    "PIL":             "Pillow",
    "easyocr":         "easyocr",
    "torch":           "torch",
}

SPLASH_TITLE = "Ёлочка Плюс"
SPLASH_INSTALL_TEXT = ("Первый запуск: Ёлочка Плюс настраивает компоненты\n"
                       "распознавания экрана. Пожалуйста, подождите...")
SPLASH_NETWORK_ERROR = ("Ошибка сети: Не удалось загрузить модули распознавания.\n"
                        "Проверьте подключение и перезапустите программу")

# Языковые веса EasyOCR, которые нужны приложению (en обязателен + ru как базовый;
# остальные языки экранов догружаются Reader'ом лениво при первом захвате).
EASYOCR_BASE_LANGS = ["en", "ru"]

# Справочник весов EasyOCR: имя .pth-файла в ~/.EasyOCR/model/ -> URL ZIP-архива
# релизов JaidedAI/EasyOCR (в точности те же адреса, что использует сам easyocr
# в easyocr/config.py: download_urls['craft'], recognition_models['gen2'], ...).
# Используется для предпроверки наличия весов и понятных стадий на заставке;
# скачивание выполняет _fetch_with_progress (с прогрессом), затем распаковка zip.
EASYOCR_MODEL_URL = "https://github.com/JaidedAI/EasyOCR/releases/download/"
EASYOCR_WEIGHTS = {
    # детектор текста CRAFT — общий для всех языков, КРИТИЧЕН
    "craft_mlt_25k.pth":  EASYOCR_MODEL_URL + "pre-v1.1.6/craft_mlt_25k.zip",
    # recognizer'ы базовых языков (en, ru): с ними Reader(['en','ru']) уже не качает
    "english_g2.pth":     EASYOCR_MODEL_URL + "v1.3/english_g2.zip",
    "russian_v1.1.pth":   EASYOCR_MODEL_URL + "v1.1/Russian_v1.1.zip",
    # дополнительные языки экранов (zh, ja) — догружаются лениво при первом захвате
    "zh_sim_g2.pth":      EASYOCR_MODEL_URL + "v1.3/zh_sim_g2.zip",
    "japanese_g2.pth":    EASYOCR_MODEL_URL + "v1.3/japanese_g2.zip",
}


def _extract_pth_from_zip(zip_path: Path, dest: Path) -> None:
    """Достаёт из скачанного zip-архива EasyOCR нужный .pth-файл в model-каталог."""
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".pth")]
        if not members:
            raise RuntimeError(f"В архиве {zip_path.name} нет .pth-весов")
        target = dest.name                      # 'craft_mlt_25k.pth' и т.п.
        pick = next((m for m in members if Path(m).name == target), members[0])
        with zf.open(pick) as src, open(dest, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
    try:
        zip_path.unlink(missing_ok=True)
    except OSError:
        pass


def _missing_packages() -> list:
    """Возвращает список pip-пакетов, которых нет в системе."""
    missing = []
    for mod, pkg in REQUIRED_PACKAGES.items():
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append(pkg)
        except (ImportError, ValueError):
            missing.append(pkg)
    return sorted(set(missing))


def easyocr_model_dir() -> Path:
    """~/.EasyOCR/model/ — туда EasyOCR складывает веса (учитывает USERPROFILE)."""
    base = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~")) / ".EasyOCR"
    return base / "model"


def easyocr_missing_weights() -> list:
    """Список ожидаемых весов, которых ещё нет в ~/.EasyOCR/model/
    (или файл весов повреждён — меньше 1 МБ)."""
    mdir = easyocr_model_dir()
    missing = []
    for fname in EASYOCR_WEIGHTS:
        f = mdir / fname
        try:
            if not f.exists() or f.stat().st_size < 1_000_000:
                missing.append(fname)
        except OSError:
            missing.append(fname)
    return missing


class SplashUI:
    """Заставка первого запуска в отдельном потоке Tkinter.

    Наружу отдаётся только set_status()/finish() — они потокобезопасны:
    текст применяется через root.after(), поэтому обновлять статус можно
    из любого потока (pip, загрузка весов, инициализация Reader)."""

    def __init__(self):
        self._root = None
        self._status_label = None
        self._detail_label = None
        self._bar_canvas = None
        self._rect_id = None
        self._ready = threading.Event()
        self._failed = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._last_detail = 0.0                  # троттлинг детальных строк

    # ---------- жизненный цикл ----------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="SplashScreen")
        self._thread.start()
        self._ready.wait(timeout=5)              # ждём готовности Tk-цикла

    def _run(self):
        try:
            import tkinter as tk
        except Exception:
            self._failed.set()
            self._ready.set()
            return
        root = None
        try:
            root = tk.Tk()
            self._root = root
            root.overrideredirect(True)          # без рамки окна
            root.attributes("-topmost", True)
            root.configure(bg="#14161d")
            w, h = 480, 190
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2 - 60}")

            frame = tk.Frame(root, bg="#14161d", highlightbackground="#c9a227",
                             highlightthickness=2)
            frame.pack(fill="both", expand=True, padx=2, pady=2)

            tk.Label(frame, text="🎄 Ёлочка Плюс", font=("Segoe UI", 15, "bold"),
                     bg="#14161d", fg="#c9a227").pack(pady=(14, 4))
            # главное текстовое поле статуса — сюда транслируются все стадии
            self._status_label = tk.Label(frame, text=SPLASH_INSTALL_TEXT,
                                          justify="center", wraplength=440,
                                          font=("Segoe UI", 10),
                                          bg="#14161d", fg="#e8e8ea")
            self._status_label.pack(pady=(0, 4))
            # детальная строка: прогресс скачивания весов / вывод easyocr
            self._detail_label = tk.Label(frame, text="", justify="center",
                                          wraplength=440, font=("Segoe UI", 8),
                                          bg="#14161d", fg="#8f93a0")
            self._detail_label.pack(pady=(0, 4))
            self._bar_canvas = tk.Canvas(frame, width=420, height=8, bg="#23262f",
                                         highlightthickness=0)
            self._bar_canvas.pack(pady=(0, 12))
            self._rect_id = self._bar_canvas.create_rectangle(0, 0, 4, 8,
                                                              fill="#c9a227", width=0)

            state = {"x": 4.0, "dir": 1.0}

            def animate():
                if self._stop.is_set():
                    return
                state["x"] += state["dir"] * 9
                if state["x"] >= 420:
                    state["x"], state["dir"] = 420.0, -1.0
                elif state["x"] <= 0:
                    state["x"], state["dir"] = 0.0, 1.0
                x = state["x"]
                left = max(0.0, x - 70) if state["dir"] > 0 else min(x, 420.0)
                right = x if state["dir"] > 0 else min(x + 70, 420.0)
                self._bar_canvas.coords(self._rect_id, left, 0, right, 8)
                root.after(40, animate)

            root.after(40, animate)
            self._ready.set()
            root.mainloop()
        except Exception:
            self._failed.set()
            self._ready.set()
        finally:
            try:
                if root is not None:
                    root.destroy()
            except Exception:
                pass

    # ---------- API для рабочих потоков ----------
    def set_status(self, text: str, detail: str = "", throttle_detail: bool = False):
        """Мгновенно обновляет надписи заставки (вызывать из любого потока)."""
        if throttle_detail:
            now = time.time()
            if now - self._last_detail < 0.25:
                return
            self._last_detail = now
        r = self._root
        if r is None or self._failed.is_set():
            print(f"[{SPLASH_TITLE}] {text}" + (f" | {detail}" if detail else ""),
                  flush=True)
            return
        try:
            if detail:
                r.after(0, lambda t=text, d=detail: self._apply(t, d))
            else:
                r.after(0, lambda t=text: self._apply(t, None))
        except Exception:
            pass

    def _apply(self, text, detail):
        try:
            if self._status_label is not None:
                self._status_label.config(text=text)
            if self._detail_label is not None and detail is not None:
                self._detail_label.config(text=detail[:160])
        except Exception:
            pass

    def finish(self, hold_seconds: float = 0.6):
        time.sleep(hold_seconds)                 # чтобы финальный статус был виден
        self._stop.set()
        try:
            if self._root is not None:
                self._root.after(0, self._root.quit)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)


_SPLASH: "SplashUI | None" = None


def _splash_start():
    global _SPLASH
    _SPLASH = SplashUI()
    _SPLASH.start()


def _splash_status(text: str, detail: str = "", throttle: bool = False):
    if _SPLASH is not None:
        _SPLASH.set_status(text, detail, throttle_detail=throttle)
    else:
        print(f"[{SPLASH_TITLE}] {text}" + (f" | {detail}" if detail else ""), flush=True)


def _splash_finish(hold_seconds: float = 0.6):
    global _SPLASH
    if _SPLASH is not None:
        _SPLASH.finish(hold_seconds)
        _SPLASH = None


# ---------- трансляция системного вывода и логов easyocr в заставку ----------

class _TeeToSplash:
    """Пишет в оригинальный поток + транслирует осмысленные строки (прогресс
    скачивания urllib/tqdm, сообщения easyocr) в детальную строку заставки."""

    def __init__(self, orig):
        self._orig = orig

    def write(self, s):
        try:
            self._orig.write(s)
        except Exception:
            pass
        if not s:
            return 0
        line = s.replace("\r", "\n").strip()
        if line:
            low = line.lower()
            keys = ("progress", "%|", "downloading", "download", "extracting",
                    "reader", "initializing", "pretrained", "weights", "model",
                    "craft", "using cpu", "gpu")
            if any(k in low for k in keys):
                _splash_status(_SPLASH_STAGE[0], line[:120], throttle=True)
        return len(s)

    def flush(self):
        try:
            self._orig.flush()
        except Exception:
            pass

    def isatty(self):                            # tqdm должен считать поток терминалом
        try:
            return self._orig.isatty()
        except Exception:
            return False


class _SplashLogHandler(logging.Handler):
    """Хватает логгер easyocr (и его наследников) и печатает записи в заставку."""

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return
        if msg:
            _splash_status(_SPLASH_STAGE[0], msg[:120], throttle=True)

    def handleError(self, record):                # тихий режим — не шуметь в stderr
        pass


@contextlib.contextmanager
def _splash_stage(stage_text: str):
    """Делает stage_text текущей стадией заставки на время блока."""
    prev = _SPLASH_STAGE[0]
    _SPLASH_STAGE[0] = stage_text
    try:
        yield
    finally:
        _SPLASH_STAGE[0] = prev


@contextlib.contextmanager
def _easyocr_logging_to_splash():
    """Временно вешает хендлер-переводчик логов easyocr на заставку."""
    lg = logging.getLogger("easyocr")
    saved_level, saved_prop = lg.level, lg.propagate
    handler = _SplashLogHandler()
    handler.setLevel(logging.DEBUG)
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    lg.propagate = True
    try:
        yield
    finally:
        lg.removeHandler(handler)
        lg.setLevel(saved_level)
        lg.propagate = saved_prop


# Текущая стадия заставки (список из одного элемента, читают все хелперы).
_SPLASH_STAGE = ["Подготовка..."]


def _install_pip_packages(missing: list) -> bool:
    """Скрытая установка недостающих pip-пакетов. True = успех."""
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--upgrade", "pip"],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, creationflags=flags)
    except Exception:
        pass                                     # обновление pip не критично
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *missing],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, creationflags=flags)
        return True
    except Exception as e:
        print(f"[{SPLASH_TITLE}] ОШИБКА авто-установки: {e}", flush=True)
        return False


def _fetch_with_progress(url: str, dest: Path, what: str) -> bool:
    """Скачивает файл с честным прогрессом в заставке (urllib, stream)."""
    import urllib.request
    import urllib.error
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (YolochkaPlus)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last_emit = 0.0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last_emit >= 0.3:
                        last_emit = now
                        if total:
                            mb_done = done / 1048576.0
                            mb_total = total / 1048576.0
                            pct = int(done * 100 / total)
                            _splash_status(
                                _SPLASH_STAGE[0],
                                f"{what}: {mb_done:.1f} / {mb_total:.1f} МБ ({pct}%)",
                                throttle=True)
                        else:
                            _splash_status(_SPLASH_STAGE[0],
                                           f"{what}: {done / 1048576.0:.1f} МБ",
                                           throttle=True)
        tmp.replace(dest)
        return True
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise                                    # пусть выше решит — фатально или нет


def _prefetch_easyocr_weights(splash: bool) -> None:
    """Предварительная проверка весов в ~/.EasyOCR/model/: чего нет — докачиваем
    сами (с прогрессом), чтобы easyocr.Reader() прошёл мгновенно и без скрытых
    многоминутных загрузок. Критичен только CRAFT — без него OCR невозможен."""
    mdir = easyocr_model_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    missing = easyocr_missing_weights()
    if not missing:
        if splash:
            _splash_status("Шаг 2/3: Модели OCR уже на диске — повторная загрузка не требуется")
        return
    names = ", ".join(missing)
    print(f"[{SPLASH_TITLE}] Нет весов OCR: {names}", flush=True)
    for fname in missing:
        url = EASYOCR_WEIGHTS[fname]
        dest = mdir / fname
        if fname == "craft_mlt_25k.pth":
            stage = "Шаг 2/3: Скачивание базовой модели детектора текста (CRAFT)..."
        else:
            stage = "Шаг 3/3: Загрузка языковых модулей OCR..."
        _SPLASH_STAGE[0] = stage
        if splash:
            _splash_status(stage, f"Файл: {fname}")
        try:
            _fetch_with_progress(url, dest, fname)
            print(f"[{SPLASH_TITLE}] Вес загружен: {fname}", flush=True)
        except Exception as e:
            print(f"[{SPLASH_TITLE}] Не удалось скачать {fname}: {e}", flush=True)
            if fname == "craft_mlt_25k.pth":
                raise RuntimeError(f"Не удалось скачать модель CRAFT: {e}") from e
            # языковой вес не критичен: Reader подтянет его позже сам


def _warmup_easyocr_reader(splash: bool) -> None:
    """Инкрементальный прогрев Reader по одному языку (en, затем ru):
    после каждого шага видно реальный прогресс, а не мёрзлый экран.

    Все стадии выполняются под перехватом stdout/stderr и логов easyocr —
    текстовые шаги загрузки транслируются прямо в детальную строку заставки."""
    stage = "Шаг 3/3: Загрузка языковых модулей OCR..."
    reader = None
    langs_tried = []
    for lang in EASYOCR_BASE_LANGS:
        probe_langs = EASYOCR_BASE_LANGS[:len(langs_tried) + 1]
        with _splash_stage(stage):
            if splash:
                _splash_status(stage, f"Инициализация EasyOCR: язык '{lang}'…")
            try:
                import easyocr
                # tee системного вывода + логгер easyocr -> заставка;
                # torch показывает "Downloading model ... to ..." прямо на экране
                with contextlib.ExitStack() as stack:
                    stack.enter_context(contextlib.redirect_stdout(_TeeToSplash(sys.stdout)))
                    stack.enter_context(contextlib.redirect_stderr(_TeeToSplash(sys.stderr)))
                    stack.enter_context(_easyocr_logging_to_splash())
                    reader = easyocr.Reader(probe_langs, gpu=False, verbose=True)
                langs_tried = probe_langs
            except Exception as e:
                print(f"[{SPLASH_TITLE}] Reader({probe_langs}) не поднялся: {e}", flush=True)
                if langs_tried:                  # хотя бы один язык работает — ок
                    break
                raise
    # Единый глобальный экземпляр: OcrEngine переиспользует его, новых
    # многосекундных инициализаций во время захвата больше НЕ БУДЕТ.
    globals()["WARM_EASYOCR_READER"] = reader
    if splash:
        _splash_status("Шаг 3/3: Языковые модули готовы",
                       f"EasyOCR инициализирован: {', '.join(langs_tried)}")


def bootstrap_first_run() -> bool:
    """Полная автономная настройка ПЕРВОГО ЗАПУСКА до открытия MainWindow:
      Шаг 1/3 — установка pip-пакетов;
      Шаг 2/3 — проверка/скачивание весов CRAFT (~/.EasyOCR/model/);
      Шаг 3/3 — языковые модули + инициализация easyocr.Reader(['en','ru']).
    Возвращает False, если произошла фатальная ошибка (сеть/диск) — в этом
    случае заставка показывает текст ошибки, а запуск главного окна
    останавливается (приложение не падает молча)."""
    missing = _missing_packages()
    easyocr_present = _easyocr_importable()
    weights_complete = easyocr_present and not easyocr_missing_weights()
    if not missing and (not easyocr_present or weights_complete):
        # Нечего устанавливать; easyocr недоступен (работаем через Tesseract)
        # либо все веса уже на диске — мгновенный старт без заставки.
        return True

    print(f"[{SPLASH_TITLE}] Первый запуск: автономная настройка компонентов", flush=True)
    _splash_start()
    fatal = None
    try:
        # ---------------- Шаг 1/3: pip-пакеты ----------------
        if missing:
            with _splash_stage("Шаг 1/3: Установка компонентов распознавания экрана..."):
                _splash_status(SPLASH_INSTALL_TEXT,
                               "Устанавливаю: " + ", ".join(missing))
                ok = _install_pip_packages(missing)
                if ok and _missing_packages():
                    ok = False
                if not ok:
                    fatal = SPLASH_NETWORK_ERROR
        easyocr_present = _easyocr_importable()
        if fatal is None and not easyocr_present:
            # pip поставил всё, кроме easyocr/torch (например, нет места на диске) —
            # это не фатально: OCR сможет работать через Tesseract, если он есть.
            print(f"[{SPLASH_TITLE}] EasyOCR недоступен — приложении будет использовать "
                  f"Tesseract (или покажет подсказку при первом захвате)", flush=True)

        # ---------------- Шаги 2/3 и 3/3: веса и Reader ----------------
        if fatal is None and easyocr_present:
            try:
                _prefetch_easyocr_weights(splash=True)
                _warmup_easyocr_reader(splash=True)
            except Exception as e:
                logging.getLogger("yolochka.bootstrap").exception(
                    "Bootstrap: ошибка загрузки модулей OCR")
                fatal = SPLASH_NETWORK_ERROR
    finally:
        if fatal:
            # МГНОВЕННЫЙ перевод заставки в режим ошибки + остановка запуска.
            _splash_status(fatal, "", throttle=False)
            _splash_finish(hold_seconds=2.5)
            print(f"[{SPLASH_TITLE}] ФАТАЛЬНО: {fatal}", flush=True)
            return False
        _splash_status("Готово! Открываю главное окно...", "")
        _splash_finish(hold_seconds=1.0)
    return True


def _easyocr_importable() -> bool:
    try:
        return importlib.util.find_spec("easyocr") is not None
    except Exception:
        return False


# ВАЖНО: bootstrap_first_run() выполняется ДО открытия MainWindow — именно здесь
# (на заставке) происходит единственная тяжёлая инициализация easyocr.Reader.
BOOTSTRAP_OK = bootstrap_first_run()
if not BOOTSTRAP_OK:
    # Заставка уже показала ошибку сети; корректно выходим без падения.
    sys.exit(2)

# -----------------------------------------------------------------------------
# 0. High-DPI: объявляем приложение DPI-aware ДО создания QApplication.
#    На Windows 11 с масштабированием 125% / 150% это гарантирует, что Qt и
#    Win32-хук работают в согласованной системе координат (физические пиксели
#    экрана <-> логические координаты виджетов пересчитываются через devicePixelRatio).
# -----------------------------------------------------------------------------

def set_process_dpi_aware() -> None:
    """SetProcessDpiAwarenessContext(PER_MONITOR_V2); безопасный no-op вне Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # Per-Monitor v2 контекст (Win10 1703+ / Win11)
        ctx = ctypes.c_void_p(-4)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctx):
            return
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # fallback: PROCESS_PER_MONITOR_DPI_AWARE
    except Exception as e:
        logging.getLogger("yolochka").warning("DPI: не удалось включить DPI-awareness: %s", e)


set_process_dpi_aware()

# -----------------------------------------------------------------------------
# 0b. Пути, логирование, ранние проверки зависимостей
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
        QPixmap, QGuiApplication, QScreen,
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
    from PIL import Image, ImageOps, ImageFilter
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
    import easyocr                              # пакет уже импортирован bootstrap'ом,
    HAS_EASYOCR = True                          # torch веса в памяти — это дешёвый импорт
except Exception:
    pass

# Разогретый easyocr.Reader из заставки (None, если OCR-этап не запускался).
WARM_EASYOCR_READER: "easyocr.Reader | None" = globals().get("WARM_EASYOCR_READER")
if WARM_EASYOCR_READER is not None:
    log.info("Bootstrap: переиспользую готовый easyocr.Reader — захват экрана не будет "
             "замораживаться на инициализацию моделей")

OCR_AVAILABLE = HAS_PYTESSERACT or HAS_EASYOCR
log.info("Зависимости: PyQt6=%s deep_translator=%s Pillow=%s | OCR: tesseract=%s easyocr=%s "
         "(bootstrap первого запуска: %s)",
         not any("PyQt6" in d for d in MISSING_DEPS),
         not any("deep-translator" in d for d in MISSING_DEPS),
         not any("Pillow" in d for d in MISSING_DEPS),
         HAS_PYTESSERACT, HAS_EASYOCR, "OK" if BOOTSTRAP_OK else "FAIL")

# Если bootstrap не смог поставить нужные пакеты (нет интернета / pip),
# помечаем их как недостающие — пользователь получит понятное окно с инструкцией.
if not BOOTSTRAP_OK:
    still = _missing_packages()
    if any(p == "PyQt6" for p in still):
        MISSING_DEPS.append("PyQt6 (не удалось установить автоматически)")

# -----------------------------------------------------------------------------
# 1. Языковые конфигурации. НИКАКОГО хардкода языков в логике — только таблицы.
#    Новый язык = одна строка в OCR_LANGS / TRANSLATE_LANGS. Код распознавания
#    и перевода от этого не меняется: функции принимают ISO-код как параметр.
# -----------------------------------------------------------------------------

UI_LANGS = {                      # локализация интерфейса
    "ru": "Русский",
    "en": "English",
}

# QComboBox «Язык экрана (OCR)»: auto, en, zh, ja — ПОРЯДОК элементов списка.
# Новый язык = одна строка здесь; логика OCR/перевода не меняется.
OCR_LANG_ORDER = ["auto", "en", "zh", "ja"]
OCR_LANGS = {                     # ISO-код -> подписи в UI + коды движков OCR
    "auto": {"label": "Автоопределение", "tess": "eng+chi_sim+jpn", "easy": ["en", "ch_sim", "ja"]},
    "en":   {"label": "Английский",      "tess": "eng",             "easy": ["en"]},
    "zh":   {"label": "Китайский",       "tess": "chi_sim",         "easy": ["ch_sim"]},
    "ja":   {"label": "Японский",        "tess": "jpn",             "easy": ["ja"]},
}

# QComboBox «Язык перевода»: ru, en, es — значения = ISO-коды deep_translator
TRANSLATE_LANG_ORDER = ["ru", "en", "es"]
TRANSLATE_LANGS = {
    "ru": "Русский",
    "en": "Английский",
    "es": "Испанский",
}

DEFAULT_SETTINGS = {
    "ui_lang": "ru",
    "theme": "dark",
    "source_lang": "auto",        # значение QComboBox «Язык экрана»
    "target_lang": "ru",          # значение QComboBox «Язык перевода»
    "hotkey": "`",
    "ocr_engine": "auto",         # auto | tesseract | easyocr
}

# -----------------------------------------------------------------------------
# 2. Локализация UI (i18n): встроенные словари + внешние JSON (можно дополнять)
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
        "settings.source": "Язык экрана (OCR)",
        "settings.target": "Язык перевода",
        "settings.hotkey": "Горячая клавиша",
        "settings.engine": "OCR-движок",
        "settings.hint": "Все изменения применяются сразу, без перезапуска. Новые языки добавляются одной строкой в языковых таблицах.",
        "tray.translate": "Перевести экран (~)",
        "tray.open": "Открыть окно",
        "tray.quit": "Выход",
        "popup.translating": "Перевод…",
        "popup.busy": "Переводим слово…",
        "popup.empty": "Текст не распознан",
        "popup.dblclick": "(двойной клик — открыть главное окно)",
        "err.no_ocr": "OCR недоступен: установите Tesseract\n(https://github.com/UB-Mannheim/tesseract/wiki)\nили выполните: pip install pytesseract easyocr",
        "err.translate": "Ошибка перевода",
        "word.added": "Слово «{w}» → «{t}» добавлено в словарь",
        "word.translated": "Перевод слова «{w}»:",
        "word.btn.add": "Добавить в Словарь",
        "word.btn.again": "Выбрать другое слово",
        "word.in_dict": "Уже в словаре ✓",
        "word.err": "Не удалось перевести слово «{w}»: {e}",
        "word.empty_ocr": "OCR вернул пустой текст — выделите область с текстом плотнее.",
        "settings.apply_ok": "Языки обновлены на лету: OCR={sl}, перевод={tl}",
        "btn.capture": "🎯 Перевести экран (~)",
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
        "settings.source": "Screen language (OCR)",
        "settings.target": "Translation language",
        "settings.hotkey": "Hotkey",
        "settings.engine": "OCR engine",
        "settings.hint": "Changes apply instantly, no restart. New languages require just one table row.",
        "tray.translate": "Translate screen (~)",
        "tray.open": "Open window",
        "tray.quit": "Quit",
        "popup.translating": "Translating…",
        "popup.busy": "Translating the word…",
        "popup.empty": "No text recognized",
        "popup.dblclick": "(double-click opens the main window)",
        "err.no_ocr": "OCR unavailable: install Tesseract\n(https://github.com/UB-Mannheim/tesseract/wiki)\nor run: pip install pytesseract easyocr",
        "err.translate": "Translation error",
        "word.added": "Word “{w}” → “{t}” added to dictionary",
        "word.translated": "Translation of “{w}”:",
        "word.btn.add": "Add to Dictionary",
        "word.btn.again": "Pick another word",
        "word.in_dict": "Already in dictionary ✓",
        "word.err": "Failed to translate “{w}”: {e}",
        "word.empty_ocr": "OCR returned empty text — select a tighter region with text.",
        "settings.apply_ok": "Languages applied live: OCR={sl}, target={tl}",
        "btn.capture": "🎯 Translate screen (~)",
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
QPushButton:disabled { color: #6a6a80; border-color: #2a2a3d; }
QPushButton#primary { background: #e8c877; color: #12121c; font-weight: 700; border: none; }
QPushButton#primary:hover { background: #f2d98f; }
QPushButton#wordBtn { background: transparent; border: 1px dashed #4a4a66;
                      border-radius: 6px; padding: 4px 10px; color: #cfd3ea; }
QPushButton#wordBtn:hover { border-style: solid; border-color: #e8c877; color: #e8c877; }
QPushButton#captureBtn { background: #e8c877; color: #12121c; font-weight: 700;
                         border: none; border-radius: 8px; padding: 8px 12px; }
QPushButton#captureBtn:hover { background: #f2d98f; }
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
QWidget#wordCard { background: #1b1b28; border: 1px solid #e8c877; border-radius: 10px; }
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
QPushButton:disabled { color: #a6adbd; border-color: #e6e8f0; }
QPushButton#primary { background: #2f6fed; color: white; font-weight: 700; border: none; }
QPushButton#primary:hover { background: #4b83f2; }
QPushButton#wordBtn { background: transparent; border: 1px dashed #b9c0d4;
                      border-radius: 6px; padding: 4px 10px; color: #38415a; }
QPushButton#wordBtn:hover { border-style: solid; border-color: #2f6fed; color: #2f6fed; }
QPushButton#captureBtn { background: #2f6fed; color: white; font-weight: 700;
                         border: none; border-radius: 8px; padding: 8px 12px; }
QPushButton#captureBtn:hover { background: #4b83f2; }
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
QWidget#wordCard { background: #ffffff; border: 1px solid #2f6fed; border-radius: 10px; }
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
        """True — добавлено, False — уже было (UNIQUE по слову и языковой паре)."""
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

    def has_word(self, word: str, sl: str, tl: str) -> bool:
        """Есть ли слово в словаре для данной языковой пары (без учёта регистра)."""
        row = self._conn().execute(
            "SELECT 1 FROM dictionary WHERE word=? COLLATE NOCASE"
            " AND source_lang=? AND target_lang=? LIMIT 1", (word, sl, tl)).fetchone()
        return row is not None

    def delete_word(self, word: str, sl: str, tl: str):
        with self._lock:
            self._conn().execute("DELETE FROM dictionary WHERE word=? AND source_lang=? AND target_lang=?",
                                 (word, sl, tl))
            self._conn().commit()

    def export_quizlet_txt(self, path: str) -> int:
        """Формат Quizlet: каждая строка — 'слово<TAB>перевод' (строго \\t)."""
        rows = self.list_words()
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for w, tr, *_ in rows:
                f.write(f"{w}\t{tr}\n")
        log.info("DB: Quizlet-экспорт %d пар -> %s (разделитель '\\t')", len(rows), path)
        return len(rows)

    def export_csv(self, path: str) -> int:
        rows = self.list_words()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            cw = csv.writer(f)
            cw.writerow(["word", "translation", "source_lang", "target_lang", "date"])
            cw.writerows(rows)
        log.info("DB: CSV-экспорт %d пар -> %s", len(rows), path)
        return len(rows)


# -----------------------------------------------------------------------------
# 5. OCR: предобработка (порог Оцу), распознавание с ДИНАМИЧЕСКИМИ ISO-кодами,
#    regex-очистка мусора
# -----------------------------------------------------------------------------

JUNK_CHARS = "~°|_/\\@#$%^&*+=<>{}[]«»…•·—–¬¦`"
_JUNK_SET = set(JUNK_CHARS)

LINE_SPLIT_RE = re.compile(r"\r?\n")                       # строки OCR-вывода
HYPHEN_JOIN_RE = re.compile(r"(\w)-\s*\n\s*(\w)")          # перенос: transla-\ntion -> translation
WS_RE = re.compile(r"[ \t]{2,}")                           # двойные/лишние пробелы
LONE_JUNK_RE = re.compile(                                 # одиночные мусорные символы
    r"(?<!\w)[" + re.escape(JUNK_CHARS) + r"](?!(?:\w|" + re.escape(JUNK_CHARS) + r"))")
WORD_SPLIT_RE = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)   # слова для кнопок


def _strip_junk(s: str) -> str:
    """Удаляет мусорные символы, не «съедая» нормальную пунктуацию . , ! ? ' : -"""
    out, n = [], len(s)
    for i, ch in enumerate(s):
        if ch in _JUNK_SET:
            prev = s[i - 1] if i else " "
            nxt = s[i + 1] if i + 1 < n else " "
            if prev.isspace() and nxt.isspace():           # одинокий символ-обрубок
                out.append(" ")
                continue
            if not prev.isalnum() and not prev.isspace():  # сломанная пунктуация "|." -> "."
                continue
            if not nxt.isalnum() and not nxt.isspace():
                continue
            out.append(" ")                                # приклеен к слову — вырезаем
            continue
        out.append(ch)
    return "".join(out)


def clean_text(text: str) -> str:
    """Regex-зачистка OCR-мусора (| _ ~ ° @), склейка разорванных слов, чистка
    пробелов и оборванной пунктуации. Возвращает «чистую» одну строку."""
    if not text:
        return ""
    t = HYPHEN_JOIN_RE.sub(r"\1\2", text)                  # склейка слов на стыке строк
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
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)                 # "привет ." -> "привет."
    t = re.sub(r"([,.!?;:])(?=\w)", r"\1 ", t)             # "привет,мир" -> "привет, мир"
    t = re.sub(r"([.!?])[.!?;:,]+", r"\1", t)              # "Quest! ." -> "Quest!"
    t = re.sub(r"^[\s,.!?;:]+|[\s,.!?;:]+$", "", t)        # оборванные знаки по краям
    t = WS_RE.sub(" ", t).strip()
    # если после всей зачистки не осталось ни одного «печатного» символа языка — мусор
    if not re.search(r"[A-Za-zА-Яа-яЁё\u4e00-\u9fff\u3040-\u30ff\d]", t):
        return ""
    return t


def extract_words(text: str):
    """Список «значимых» слов для кликабельных кнопок (без цифр и мусора)."""
    words = WORD_SPLIT_RE.findall(text or "")
    return [w for w in words if len(w) >= 2][:80]


class OcrEngine:
    """Единая точка входа для любого OCR: принимает ISO-код языка из UI и сам
    маппит его на коды движка (EasyOCR/Tesseract). Логика НЕ зависит от того,
    какой язык выбран — только от таблиц OCR_LANGS."""

    def __init__(self):
        # Переиспользуем Reader, прогретый на заставке первого запуска:
        # тяжёлая инициализация easyocr больше НЕ замораживает захват экрана.
        self._easy_reader = WARM_EASYOCR_READER
        self._easy_langs = (tuple(EASYOCR_BASE_LANGS)
                            if WARM_EASYOCR_READER is not None else None)
        if self._easy_reader is not None:
            log.info("OcrEngine: подхвачен разогретый EasyOCR Reader %s из bootstrap",
                     self._easy_langs)

    def preferred_engine(self, configured: str) -> str:
        if configured == "tesseract" and HAS_PYTESSERACT:
            return "tesseract"
        if configured == "easyocr" and HAS_EASYOCR:
            return "easyocr"
        return "tesseract" if HAS_PYTESSERACT else ("easyocr" if HAS_EASYOCR else "none")

    # ---- предобработка: gray -> контраст -> бинаризация по порогу Оцу -> шумоподавление
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
        """iso_lang — код из QComboBox «Язык экрана (OCR)» ('auto','en','zh','ja')."""
        engine = self.preferred_engine(engine_cfg if engine_cfg in ("auto", "tesseract", "easyocr") else "auto")
        if engine == "none":
            raise RuntimeError("OCR недоступен: не найден ни tesseract, ни easyocr")
        cfg = OCR_LANGS.get(iso_lang, OCR_LANGS["auto"])   # динамическая подстановка кодов
        pre = self.preprocess(img)
        t0 = time.perf_counter()
        if engine == "tesseract":
            txt = pytesseract.image_to_string(pre, lang=cfg["tess"])
        else:
            langs = tuple(cfg["easy"])
            if self._easy_reader is None or self._easy_langs != langs:
                # EasyOCR не поддерживает смешение кириллицы с CJK в одном Reader —
                # при выборе zh/ja пересоздаём Reader БЕЗ 'ru'. Если веса уже на
                # диске (bootstrap их докачал), это секунды, а не минуты.
                want = list(langs)
                if any(l in ("ch_sim", "ja", "ko") for l in want) and "ru" in want:
                    want.remove("ru")
                log.info("OcrEngine: инициализация EasyOCR для %s "
                         "(веса уже на диске — быстро)…", want)
                try:
                    self._easy_reader = easyocr.Reader(want, gpu=False, verbose=False)
                    self._easy_langs = tuple(want)
                except Exception as e:
                    log.exception("OcrEngine: EasyOCR Reader(%s) не поднялся", want)
                    # fallback: пробуем базовый разогретый Reader (en+ru) —
                    # распознавание всё равно состоится, качество может быть ниже
                    if self._easy_reader is not None:
                        log.warning("OcrEngine: остаюсь на прогретом Reader %s",
                                    self._easy_langs)
                    else:
                        raise RuntimeError(
                            f"EasyOCR не удалось инициализировать: {e}") from e
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
# 6. Перевод (deep_translator, динамические ISO-коды) + асинхронные QThread-воркеры
# -----------------------------------------------------------------------------

_TRANSLATE_CACHE: dict = {}


def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    """source_lang — ISO из QComboBox ('auto' тоже валиден) или None.
    Повторы берутся из кэша мгновенно (гарантия <2s на вторичных запросах)."""
    if not text or not text.strip():
        return ""
    key = (text.strip().lower(), source_lang, target_lang)
    if key in _TRANSLATE_CACHE:
        log.debug("translate: кэш-хит '%.40s'", text)
        return _TRANSLATE_CACHE[key]
    t0 = time.perf_counter()
    src = None if source_lang in ("auto", "", None) else source_lang
    try:
        # request_params добавляет timeout: при обрыве сети получаем исключение
        # сразу, а не зависший поток; вызывающий код ловит его через try/except.
        res = GoogleTranslator(source=src or "auto", target=target_lang,
                               request_params={"timeout": 5}).translate(text)
    except Exception as e:
        log.error("translate: СБОЙ [%s->%s] '%.40s': %s", source_lang, target_lang, text, e)
        raise                                   # воркеры перехватят и покажут ошибку в UI
    ms = int((time.perf_counter() - t0) * 1000)
    flag = " [SLOW >2s!]" if ms > 2000 else ""
    log.info("translate: '%.40s…' [%s->%s] %d мс%s", text, source_lang, target_lang, ms, flag)
    _TRANSLATE_CACHE[key] = res or ""
    return res or ""


class TranslateWorker(QThread):
    """Асинхронный пайплайн: скриншот -> предобработка -> OCR -> очистка -> перевод.

    Языковые коды передаются ЯВНО (снимок настроек из GUI-потока), поэтому смена
    QComboBox в «Настройках» применяется к следующему же захвату БЕЗ перезапуска.
    Каждый этап обёрнут в try/except с записью в app.log — обрыв сети или ошибка
    API не роняют приложение."""
    finished_ok = pyqtSignal(str, str, str, int)     # original, translated, engine, ms
    failed = pyqtSignal(str)

    def __init__(self, image: "Image.Image", settings: dict, parent=None):
        super().__init__(parent)
        self.image = image
        self.settings = dict(settings)               # снимок настроек из GUI-потока (без гонок)

    def run(self):
        t0 = time.perf_counter()
        try:
            if not OCR_AVAILABLE:
                self.failed.emit("no_ocr")
                return
            s = self.settings                         # ДИНАМИЧЕСКИЕ ISO-коды из UI на момент старта
            log.info("Worker: старт пайплайна (OCR=%s -> TARGET=%s)",
                     s.get("source_lang"), s.get("target_lang"))
            try:
                original, engine = OCR.recognize(
                    self.image, s["source_lang"], s.get("ocr_engine", "auto"))
            except Exception:
                log.exception("Worker: ошибка OCR-этапа")
                raise
            log.info("Worker: OCR дал %.60s", original or "<пусто>")
            if not original:
                self.finished_ok.emit("", "", engine, int((time.perf_counter() - t0) * 1000))
                return
            try:
                translated = translate_text(original, s["source_lang"], s["target_lang"])
            except Exception:
                log.exception("Worker: ошибка перевода (обрыв сети / API?)")
                raise
            total_ms = int((time.perf_counter() - t0) * 1000)
            if total_ms > 2000:
                log.warning("Worker: полный цикл %d мс превысил целевые 2000 мс", total_ms)
            else:
                log.info("Worker: цикл уложился в цель — %d мс", total_ms)
            self.finished_ok.emit(original, translated, engine, total_ms)
        except Exception as e:
            log.exception("Worker: необработанная ошибка пайплайна")
            try:
                self.failed.emit(str(e))              # сигнал вместо вылета фонового потока
            except Exception:
                log.exception("Worker: не удалось отправить failed")


class WordTranslateWorker(QThread):
    """Мгновенный перевод ОДНОГО слова по клику из Истории (в фоне, UI не блокируется).

    Язык перевода передаётся ЯВНО (snapshot из QComboBox на момент клика) — смена
    «Языка перевода» в настройках действует сразу, без перезапуска; ошибки сети
    пишутся в app.log и показываются карточкой, а не вылетом."""
    done = pyqtSignal(str, str, str)                 # word, translation, target_lang
    err = pyqtSignal(str, str)                       # word, error

    def __init__(self, word: str, target_lang: str, parent=None):
        super().__init__(parent)
        self.word = word
        self.target_lang = target_lang               # ISO из QComboBox на момент клика

    def run(self):
        target = self.target_lang                     # ДИНАМИЧЕСКИЙ код из UI, без перезапуска
        try:
            # для одиночного слова источник всегда 'auto': исключаем ошибку
            # направления, если язык экрана (OCR) не совпадает с языком записи
            tr = translate_text(self.word, "auto", target)
            self.done.emit(self.word, tr or "", target)
        except Exception as e:
            log.exception("WordTranslateWorker: ошибка перевода '%s'", self.word)
            self.err.emit(self.word, str(e))


# -----------------------------------------------------------------------------
# 7. Захват экрана: оверлей выделения + попап перевода у курсора
# -----------------------------------------------------------------------------

def grab_region(rect: QRect) -> "Image.Image":
    """Захват области экрана с корректным пересчётом DPI (125% / 150%).

    Координаты оверлея — ЛОГИЧЕСКИЕ (системные единицы Qt). grabWindow() в PyQt6
    принимает координаты в логических единицах, а возвращает pixmap в ФИЗИЧЕСКИХ
    пикселях; devicePixelRatio pixmap'а сообщает это отношение. Дальнейшая
    обработка идёт в физических пикселях — именно так OCR получает резкий текст
    на масштабируемых мониторах Windows.
    """
    scr = screen_at(rect)
    pm = scr.grabWindow(0, rect.x(), rect.y(), rect.width(), rect.height())
    dpr = pm.devicePixelRatio() or scr.devicePixelRatio() or 1.0
    qimg = pm.toImage().convertToFormat(QImage.Format.Format_RGB888)
    w, h, bpl = qimg.width(), qimg.height(), qimg.bytesPerLine()
    ptr = qimg.constBits()
    buf = bytes(ptr[: bpl * h]) if hasattr(ptr, "__getitem__") else bytes(ptr)
    log.info("Capture: лог. область %s -> физич. %dx%d (DPR=%.2f)", rect, w, h, dpr)
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
        # Оверлей покрывает ВСЕ экраны виртуального стола (корректно для
        # мультимониторных конфигураций Windows с разным DPI 125%/150%).
        virtual = QRect()
        for s in QGuiApplication.screens():
            virtual = virtual.united(s.geometry())
        screen = screen_at(QCursor.pos())
        self._bg = screen.grabWindow(0)          # чистый экран захватываем ДО затемнения
        self._bg.setDevicePixelRatio(1.0)        # рисуем pixmap на всю логическую ширину
        self.setGeometry(virtual)
        log.info("Overlay: геометрия %s, DPR экрана курсора=%.2f", virtual, screen.devicePixelRatio())

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


def screen_at(rect_or_point) -> "QScreen":
    """Экран, которому принадлежит прямоугольник/точка (учёт мультимониторности)."""
    screens = QGuiApplication.screens()
    if isinstance(rect_or_point, QRect):
        target = rect_or_point.center()
    else:
        target = rect_or_point
    for s in screens:
        if s.geometry().contains(target):
            return s
    return QGuiApplication.primaryScreen()


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
        self._lbl_hint.setStyleSheet("color:#9a9ab0; background:transparent;")

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
        # попап держим в границах того экрана, где находится курсор (High-DPI/мультимонитор)
        scr = screen_at(pos).availableGeometry()
        x = max(scr.left(), min(pos.x() + 16, scr.right() - self.width() - 4))
        y = max(scr.top(), min(pos.y() + 16, scr.bottom() - self.height() - 4))
        self._apply_label_styles()
        self.move(x, y)
        self.show()
        self.raise_()
        QTimer.singleShot(12000, self.hide)

    def mouseDoubleClickEvent(self, _):
        self.hide()
        if app_main_window is not None:
            app_main_window.showNormal()
            app_main_window.activateWindow()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        th = THEMES.get(DB.get_setting("theme", "dark"), THEMES["dark"])
        bg = QColor(th["popup_bg"])
        bg.setAlphaF(0.97)
        p.setPen(QPen(QColor(th["popup_border"]), 1.5))
        p.setBrush(bg)
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 12, 12)


# -----------------------------------------------------------------------------
# 8. Глобальный хоткей "~"
# -----------------------------------------------------------------------------

class HotkeyManager(QObject):
    """Глобальная горячая клавиша '~'. Windows: RegisterHotKey; X11: XGrabKey.
    Колбэк всегда маршализуется в GUI-поток через postEvent (Qt-виджеты не
    тредобезопасны). Наследует QObject, чтобы принимать события Qt в event()."""

    VK_TILDE_GRV = 0xC0          # виртуальный код ` / ~ на US-клавиатуре Windows

    def __init__(self, callback):
        super().__init__()       # приёмник постится в поток, где создан менеджер (GUI)
        self._user_cb = callback
        self._alive = True
        self.callback = self._marshal      # вызывается из хук-потока
        self.ok = False
        plat = sys.platform
        try:
            # На Windows RegisterHotKey ОБЯЗАТЕЛЬНО должен вызываться из того же
            # потока, который затем крутит цикл GetMessageW (message queue
            # привязана к потоку). Поэтому весь хук живёт в daemon-потоке.
            if plat.startswith("win"):
                t = threading.Thread(target=self._hook_windows, daemon=True,
                                     name="HotkeyWin32")
                t.start()
                # ждём результата регистрации максимум 2 секунды
                for _ in range(40):
                    if self.ok is not None:
                        break
                    time.sleep(0.05)
                self.ok = bool(self.ok)
            elif plat.startswith("linux"):
                self.ok = self._hook_x11()
            else:
                log.warning("Hotkey: глобальные хоткеи на %s не поддерживаются, используйте меню трея", plat)
        except Exception as e:
            self.ok = False
            log.exception("Hotkey: не удалось установить хук: %s", e)
        if not self.ok:
            log.warning("Hotkey: '~' не перехвачена глобально — выделяйте текст через меню трея "
                        "(или кнопку «Перевести экран» в главном окне)")

    def _marshal(self):
        """Потокобезопасно исполняет колбэк в GUI-потоке.

        Из хук-потока Windows/X11 нельзя создавать Qt-виджеты. Единственный
        надёжный способ попасть в поток QApplication — QCoreApplication.postEvent:
        он документирован как потокобезопасный из ЛЮБОГО потока и кладёт событие
        в очередь GUI-потока. Тип события — произвольный QEvent.Type.User+N,
        приёмник — экземпляр HotkeyManager (он QObject), обработчик event()
        выполняется уже в GUI-потоке и вызывает пользовательский колбэк."""
        try:
            if not self._alive:
                return
            from PyQt6.QtCore import QEvent, QCoreApplication
            ev = QEvent(QEvent.Type(QEvent.Type.User + 7))   # наш маркер WM_HOTKEY
            QCoreApplication.postEvent(self, ev)             # безопасно из чужого потока
        except Exception:
            log.exception("Hotkey: не удалось поставить колбэк в очередь GUI-потока")

    def event(self, e):
        """Вызывается уже в GUI-потоке: доставляем сигнал hotkey_fired."""
        from PyQt6.QtCore import QEvent
        if e.type() == QEvent.Type(QEvent.Type.User + 7):
            try:
                self._user_cb()
            except Exception:
                log.exception("Hotkey: ошибка выполнения колбэка в GUI-потоке")
            return True
        return super().event(e)

    def _hook_windows(self) -> None:
        """Полностью выполняется В СВОЁМ daemon-потоке:
        RegisterHotKey -> цикл GetMessageW. При WM_HOTKEY с нашим wParam
        вызывается self.callback(), который через QTimer.start(0) маршализует
        открытие оверлея в GUI-поток (Qt-виджеты из чужого потока не трогаем).
        Результат записи в self.ok: None = ещё не готово, True/False = итог."""
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        HK_ID = 0xB0B5
        MOD_NOREPEAT = 0x4000
        WM_HOTKEY = 0x0312
        PM_REMOVE = 1

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                        ("time", wintypes.DWORD), ("pt", POINT)]

        # Регистрация hotkey ОБЯЗАТЕЛЬНО в том же потоке, что крутит GetMessageW
        ok = bool(user32.RegisterHotKey(None, HK_ID, MOD_NOREPEAT, self.VK_TILDE_GRV))
        self.ok = ok
        if not ok:
            err = ctypes.get_last_error()
            log.warning("RegisterHotKey('~') вернула ошибку %s — "
                        "клавиша, возможно, занята другим приложением", err)
            return

        log.info("Hotkey: глобальная клавиша '~' зарегистрирована (Win32)")

        msg = MSG()
        try:
            while True:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r == 0 or r == -1:          # PostQuitMessage / ошибка
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HK_ID:
                    log.info("Hotkey: перехвачен WM_HOTKEY (~) -> открываю оверлей")
                    try:
                        self.callback()
                    except Exception:
                        log.exception("Hotkey: ошибка колбэка")
                else:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("Hotkey: цикл сообщений прервался исключением")
        finally:
            try:
                user32.UnregisterHotKey(None, HK_ID)
            except Exception:
                pass
            log.info("Hotkey: цикл сообщений завершён, клавиша отвязана")

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
    """Кнопка-слово в деталях Истории: клик -> перевод только этого слова."""

    def __init__(self, word: str, i18n: I18n, on_click):
        super().__init__(word)
        self.word = word
        self.setObjectName("wordBtn")
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFixedHeight(30)
        self.clicked.connect(lambda _=False, w=word: on_click(w))


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
        self._hist_rows: list = []         # актуальные строки истории (id, ts, src, dst, ...)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_history_tab(), "")
        self.tabs.addTab(self._build_dict_tab(), "")
        self.tabs.addTab(self._build_settings_tab(), "")
        self.setCentralWidget(self.tabs)
        self.resize(980, 640)
        i18n.changed.connect(self.retranslate)
        self.apply_theme(self.settings.get("theme", "dark"))
        self.retranslate()                # порядок важен: сначала тема, потом подписи
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
        self.btn_capture = QPushButton()
        self.btn_capture.setObjectName("captureBtn")
        self.btn_capture.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_capture.clicked.connect(self.start_capture)
        self.btn_clear_hist = QPushButton()
        self.btn_clear_hist.clicked.connect(self._clear_history)
        top.addWidget(self.lst_history, 1)
        side = QVBoxLayout()
        side.addWidget(self.btn_capture)
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
        self.btn_export = QPushButton()          # «Экспорт для Quizlet» (.txt, \t)
        self.btn_export.setObjectName("primary")
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
        # «Язык экрана (OCR)»: auto, ru, en, zh, ja — itemData хранит ISO-код
        for code in OCR_LANG_ORDER:
            self.cb_source.addItem(OCR_LANGS[code]["label"], code)
        # «Язык перевода»: ru, en, es — itemData хранит ISO-код deep_translator
        for code in TRANSLATE_LANG_ORDER:
            self.cb_target.addItem(TRANSLATE_LANGS[code], code)
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
            self.i18n.set_lang(val)                     # retranslate() вызовется по сигналу changed
        elif key == "theme":
            self.apply_theme(val)
        elif key in ("source_lang", "target_lang"):
            # Языки применяются МГНОВЕННО: каждый TranslateWorker/WordTranslateWorker
            # получает снимок настроек при старте — никакого перезапуска не нужно.
            tess = OCR_LANGS.get(self.settings.get("source_lang", ""), {}).get("tess", "?")
            easy = "+".join(OCR_LANGS.get(self.settings.get("source_lang", ""), {}).get("easy", []))
            log.info("Settings: языки обновлены на лету: OCR=%s (tesseract:'%s', easyocr:'%s'), "
                     "TARGET=%s (deep_translator)",
                     self.settings.get("source_lang"), tess, easy,
                     self.settings.get("target_lang"))
            self.statusBar().showMessage(
                self.i18n.t("settings.apply_ok",
                            sl=self.settings.get("source_lang", ""),
                            tl=self.settings.get("target_lang", "")), 4000)
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
        self.lst_history.setHorizontalHeaderLabels(
            ["ID", t("dict.col.date"), t("history.original"), t("history.translation")])
        self.tbl_dict.setHorizontalHeaderLabels(
            [t("dict.col.word"), t("dict.col.translate"), t("dict.col.langs"), t("dict.col.date")])
        self.btn_capture.setText(t("btn.capture"))
        self.btn_clear_hist.setText(t("history.btn.clear"))
        self.btn_export.setText(t("dict.btn.export"))
        self.btn_export_csv.setText(t("dict.btn.export_csv"))
        self.btn_del_word.setText(t("dict.btn.delete"))
        self.lbl_hist_src_title.setText(t("history.original")); self.lbl_hist_src_title.setObjectName("accent")
        self.lbl_hist_tr_title.setText(t("history.translation")); self.lbl_hist_tr_title.setObjectName("accent")
        # подписи настроек (никакого хардкода — всё из i18n)
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
        keep_id = None
        sel = self.lst_history.selectedItems()
        if sel:
            keep_id = sel[0].data(Qt.ItemDataRole.UserRole)
        self._hist_rows = DB.list_history()
        self.lst_history.setRowCount(len(self._hist_rows))
        restore = -1
        for i, (hid, ts, src, dst, sl, tl, ms) in enumerate(self._hist_rows):
            for j, val in enumerate((str(hid), ts, src or "", dst or "")):
                it = QTableWidgetItem(val)
                it.setData(Qt.ItemDataRole.UserRole, hid)
                self.lst_history.setItem(i, j, it)
            if hid == keep_id:
                restore = i
        hdr = self.lst_history.horizontalHeader()
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.lst_history.setColumnHidden(0, True)
        if restore >= 0:
            self.lst_history.selectRow(restore)
        elif self._hist_rows:
            self.lst_history.selectRow(0)

    def _current_history_row(self):
        items = self.lst_history.selectedItems()
        if not items:
            return None
        hid = items[0].data(Qt.ItemDataRole.UserRole)
        for row in self._hist_rows:
            if row[0] == hid:
                return row                       # (id, ts, src, dst, sl, tl, ms)
        return None

    def _on_history_selected(self):
        """Выбор записи -> оригинал разбивается на кликабельные кнопки-слова."""
        self._clear_words_layout()
        row = self._current_history_row()
        if not row:
            self.txt_hist_tr.clear()
            hint = QLabel(self.i18n.t("history.empty"))
            hint.setWordWrap(True)
            self.words_layout.addWidget(hint)
            self.words_layout.addStretch()
            return
        src, dst = row[2] or "", row[3] or ""
        self.txt_hist_tr.setPlainText(dst)
        words = extract_words(src)
        if not words:                            # оригинал пуст/без слов
            hint = QLabel(self.i18n.t("word.empty_ocr"))
            hint.setWordWrap(True)
            self.words_layout.addWidget(hint)
            self.words_layout.addStretch()
            return
        chunk = QWidget()
        cl = QVBoxLayout(chunk)
        cl.setContentsMargins(4, 4, 4, 4)
        cl.setSpacing(6)
        line = None
        for wd in words:
            if line is None or line.count() >= 12:
                line = QHBoxLayout()
                line.setSpacing(6)
                line.setAlignment(Qt.AlignmentFlag.AlignLeft)
                cl.addLayout(line)
            line.addWidget(ClickableWordButton(wd, self.i18n, self._on_word_clicked))
        self.words_layout.addWidget(chunk)
        self.words_layout.addStretch()

    def _clear_words_layout(self):
        """Полностью чистим блок деталей: и виджеты, и вложенные layout'ы строк слов.
        takeAt() возвращает QLayoutItem; у stretch-элементов widget() == None."""
        def _drain(layout):
            while layout.count():
                item = layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)            # немедленное изъятие из дерева виджетов
                    w.deleteLater()
                    continue
                sub = item.layout()
                if sub is not None:              # вложенный QHBoxLayout со словами
                    _drain(sub)
                    while sub.count():           # страховка: оставшиеся пустые элементы (stretch)
                        sub.takeAt(0)
                    sub.deleteLater()
        _drain(self.words_layout)

    def _on_word_clicked(self, word: str):
        """Клик по слову в деталях Истории: мгновенный фоновый перевод ТОЛЬКО этого слова."""
        log.info("History: клик по слову '%s' -> запрос перевода слова", word)
        # дебаунс: пока идёт запрос, игнорируем клики по остальным словам
        if any(w.isRunning() for w in self.word_workers):
            return
        self._clear_words_layout()
        card = QWidget(); card.setObjectName("wordCard")
        cl = QVBoxLayout(card); cl.setContentsMargins(12, 10, 12, 10); cl.setSpacing(8)
        lbl_busy = QLabel(f"<b>{word}</b> — {self.i18n.t('popup.busy')}…")
        lbl_busy.setTextFormat(Qt.TextFormat.RichText)
        cl.addWidget(lbl_busy)
        self.words_layout.addWidget(card)
        self.words_layout.addStretch()
        target_now = DB.get_setting("target_lang", "ru")     # актуальный ISO из QComboBox
        wb = WordTranslateWorker(word, target_now, self)
        wb.done.connect(self._word_done)
        wb.err.connect(self._word_error)
        wb.finished.connect(lambda w=wb: self.word_workers.remove(w) if w in self.word_workers else None)
        wb.start()
        self.word_workers.append(wb)

    def _word_done(self, word: str, translation: str, target_lang: str):
        """Перевод слова готов -> карточка «Оригинал → Перевод» с кнопкой «Добавить в Словарь»."""
        log.info("WordFlow: слово '%s' -> '%s' (target=%s)", word, translation, target_lang)
        self._clear_words_layout()
        s = DB.load_settings(); self.settings.update(s)
        src_lang = s.get("source_lang", "auto")
        already = bool(translation) and DB.has_word(word, src_lang, target_lang)
        card = QWidget(); card.setObjectName("wordCard")
        cl = QVBoxLayout(card); cl.setContentsMargins(12, 10, 12, 10); cl.setSpacing(8)
        head = QLabel(self.i18n.t("word.translated", w=word)); head.setObjectName("accent")
        body = QLabel(f"<b>{word}</b> &nbsp;→&nbsp; <b>{translation or '—'}</b>")
        body.setTextFormat(Qt.TextFormat.RichText); body.setWordWrap(True)
        cl.addWidget(head); cl.addWidget(body)
        row = QHBoxLayout()
        btn_add = QPushButton(self.i18n.t("word.in_dict") if already
                              else self.i18n.t("word.btn.add"))
        btn_add.setObjectName("primary")
        btn_add.setEnabled(not already)
        btn_add.clicked.connect(lambda _=False, w=word, tr=translation:
                                self._add_word_to_dict(w, tr))
        btn_again = QPushButton(self.i18n.t("word.btn.again"))
        btn_again.clicked.connect(self._on_history_selected)   # вернуться к списку слов
        row.addWidget(btn_add); row.addWidget(btn_again); row.addStretch()
        cl.addLayout(row)
        self.words_layout.addWidget(card)
        self.words_layout.addStretch()

    def _word_error(self, word: str, err: str):
        log.error("WordFlow: ошибка перевода '%s': %s", word, err)
        self._on_history_selected()                            # восстановить список слов
        QMessageBox.warning(self, self.i18n.t("err.translate"),
                            self.i18n.t("word.err", w=word, e=err))

    def _add_word_to_dict(self, word: str, translation: str):
        s = DB.load_settings(); self.settings.update(s)
        src_lang = s.get("source_lang", "auto")
        added = DB.add_word(word, translation, src_lang, s.get("target_lang", "ru"))
        self.refresh_dict()
        self.statusBar().showMessage(self.i18n.t("word.added", w=word, t=translation), 5000)
        log.info("WordFlow: '%s'->'%s' добавлено в словарь=%s", word, translation, added)
        # обновим карточку: кнопка станет неактивной («Уже в словаре ✓»)
        self._word_done(word, translation, s.get("target_lang", "ru"))

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
        """Сохраняет .txt: каждая строка 'слово\\tперевод' — формат импорта Quizlet."""
        if not DB.list_words():
            QMessageBox.information(self, self.i18n.t("dict.btn.export"), self.i18n.t("dict.export.empty"))
            return
        path, _ = QFileDialog.getSaveFileName(self, self.i18n.t("dict.btn.export"),
                                              str(Path.home() / "quizlet.txt"), "Text (*.txt)")
        if path:
            if not path.lower().endswith(".txt"):
                path += ".txt"
            try:
                n = DB.export_quizlet_txt(path)
                QMessageBox.information(self, self.i18n.t("dict.btn.export"),
                                        self.i18n.t("dict.export.ok", path=path, n=n))
            except Exception as e:
                log.exception("Export: ошибка записи Quizlet-файла")
                QMessageBox.critical(self, self.i18n.t("dict.btn.export"), str(e))

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
        if len(langs) == 2:
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
        s = DB.load_settings()                               # снимок языков из UI (GUI-поток)
        self.settings.update(s)
        log.info("Capture: язык экрана=%s -> язык перевода=%s (из QComboBox)",
                 s.get("source_lang"), s.get("target_lang"))
        self.worker = TranslateWorker(img, s, self)
        self.worker.finished_ok.connect(lambda o, d, eng, ms: self._on_done(o, d, pos, ms))
        self.worker.failed.connect(self._on_fail)
        self.worker.start()

    def _on_done(self, original: str, translated: str, pos: QPoint, ms: int):
        if not original and not translated:
            self.statusBar().showMessage(self.i18n.t("word.empty_ocr"), 5000)
            log.warning("Capture: OCR вернул пустой текст")
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

tray: "QSystemTrayIcon | None" = None
app_main_window: "MainWindow | None" = None


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
        # Пользователь не программист: показываем понятное окно ошибки вместо консоли
        msg = ("Не хватает библиотек: " + ", ".join(MISSING_DEPS) +
               "\n\nЗапустите run.bat — он установит всё автоматически,\n"
               "или выполните вручную:\n  pip install PyQt6 deep-translator Pillow")
        print(msg)
        try:
            _a = QApplication(sys.argv)
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(None, "Ёлочка Плюс — ошибка запуска", msg)
        except Exception:
            pass
        sys.exit(1)

    log.info("=== Ёлочка Плюс стартует === Python %s, PyQt6, OCR: %s",
             sys.version.split()[0],
             "+".join([e for e, ok in (("tesseract", HAS_PYTESSERACT), ("easyocr", HAS_EASYOCR)) if ok] or ["none"]))

    # выгружаем эталонные locales наружу (можно редактировать/дополнять без правки кода)
    LOCALES_DIR.mkdir(exist_ok=True)
    for code, data in BUILTIN_LOCALES.items():
        f = LOCALES_DIR / f"{code}.json"
        if not f.exists():
            f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("i18n: создан эталонный файл %s", f.name)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    # Окно НЕ закрывает приложение: закрытие крестиком сворачивает в трей
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("YolochkaPlus")

    settings = DB.load_settings()
    # валидация сохранённых языков: если в БД оказался неизвестный код — откат к дефолту
    if settings.get("source_lang") not in OCR_LANGS:
        settings["source_lang"] = DEFAULT_SETTINGS["source_lang"]
        DB.set_setting("source_lang", settings["source_lang"])
    if settings.get("target_lang") not in TRANSLATE_LANGS:
        settings["target_lang"] = DEFAULT_SETTINGS["target_lang"]
        DB.set_setting("target_lang", settings["target_lang"])

    i18n = I18n(settings.get("ui_lang", "ru"))

    win = MainWindow(i18n)
    app_main_window = win          # попап по двойному клику разворачивает это окно

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

    hk = HotkeyManager(win.start_capture)   # noqa: F841 (хук живёт всё время работы)
    if not hk.ok:
        log.warning("Hotkey: глобальная '~' недоступна — используйте кнопку "
                    "«Перевести экран» во вкладке «История» или меню трея")

    # КРИТИЧНО: приложение при старте сразу ОТКРЫВАЕТ главное окно перед
    # пользователем (а не прячется в трей). В трей оно уходит только когда
    # пользователь сам нажимает крестик (closeEvent -> hide()).
    win.show()                              # обычное видимое окно на старте
    win.raise_()
    win.activateWindow()
    log.info("Startup: главное окно показано пользователю; закрытие крестиком свернёт его в трей")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
