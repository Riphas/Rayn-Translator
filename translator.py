# -*- coding: utf-8 -*-
"""
translator.py — OCR + предобработка + очистка текста + асинхронный перевод.

Ключевой принцип: НИКАКИХ захардкоженных языков.
    Все функции принимают ISO-коды (`source_lang`, `target_lang`) как параметры.
    Новый язык = новый код в выпадающем списке настроек, логика не меняется.

Пайплайн захвата экрана:
    screenshot (PIL.Image)
      └─> preprocess_image()   grayscale → autocontrast → порог Оцу (бинаризация)
      └─> OcrEngine.recognize()            easyocr | pytesseract, динамические языки
      └─> clean_text()                     regex-выгребание мусора, склейка переносов
      └─> TranslationThread (QThread)      deep_translator.GoogleTranslator, < 2 сек
      └─> Database.add_history(...)

Легко тестируется без GUI: translate_pipeline() — чистая функция-обёртка.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("yolochka.translator")

# --------------------------------------------------------------------------- #
# 1. ЯЗЫКОВАЯ МАТРИЦА: UI-код ISO -> коды движков. Расширение = одна строка.  #
# --------------------------------------------------------------------------- #
# EasyOCR использует свои названия вместо некоторых ISO-кодов:
EASYOCR_LANG_MAP: dict[str, str] = {
    "en": "english", "ru": "russian", "de": "german", "fr": "french",
    "es": "spanish", "it": "italian", "pt": "portuguese", "pl": "polish",
    "tr": "turkish", "uk": "ukrainian", "ja": "japanese", "ko": "korean",
    "zh": "chinese_simple", "ar": "arabic", "hi": "hindi", "nl": "dutch",
    "sv": "swedish", "cs": "czech", "da": "danish", "fi": "finnish",
    "el": "greek", "hu": "hungarian", "no": "norwegian", "ro": "romanian",
}
# Tesseract и GoogleTranslate понимают стандартные ISO-639-1 коды как есть;
# для tesseract некоторые языки имеют нестандартные имена:
TESSERACT_LANG_MAP: dict[str, str] = {
    "he": "heb", "ja": "jpn", "ko": "kor", "zh": "chi_sim",
}


# Языки, доступные пользователю в настройках (расширение = одна строка списка).
SUPPORTED_UI_LANGS: list[str] = ["en", "ru"]                 # locales/<code>.json
SUPPORTED_TRANSLATE_LANGS: list[str] = [                      # Google Translate ISO-639-1
    "en", "ru", "de", "fr", "es", "it", "pt", "pl", "tr", "uk",
    "ja", "ko", "zh-CN", "ar", "hi", "nl", "sv", "cs", "da", "fi",
]


def ocr_langs_easyocr(source_lang: str) -> list[str]:
    """Список языков для Reader. При 'auto' грузим самый частый набор,
    дальше сработает lang_detector EasyOCR."""
    if source_lang == "auto":
        return ["english", "russian", "german", "french", "spanish"]
    return [EASYOCR_LANG_MAP.get(source_lang, source_lang)]


def ocr_lang_tesseract(source_lang: str) -> str:
    if source_lang == "auto":
        return "eng+rus+deu+fra+spa"
    iso = TESSERACT_LANG_MAP.get(source_lang, source_lang)
    return iso


# --------------------------------------------------------------------------- #
# 2. ПРЕДОБРАБОТКА ИЗОБРАЖЕНИЯ (шумодаун перед OCR)                           #
# --------------------------------------------------------------------------- #
def preprocess_image(img):
    """Grayscale → автоконтраст → бинаризация по порогу Оцу.

    Оцу считается вручную по гистограмме (256 корзин) — без зависимости от
    cv2, что упрощает сборку portable-EXE. Результат — L-изображение 0/255,
    на котором шрифты выглядят «гладкими» и OCR ошибается меньше.
    """
    from PIL import ImageOps

    t0 = time.perf_counter()
    g = img.convert("L")
    g = ImageOps.autocontrast(g)                 # растянуть гистограмму

    hist = g.histogram()                         # 256 значений
    total = sum(hist)
    best_thr, best_var, sum_b, w_b = 127, -1.0, 0, 0
    sum_all = sum(i * hist[i] for i in range(256))
    for thr in range(256):
        w_b += hist[thr]
        if w_b == 0 or w_b == total:
            continue
        sum_b += thr * hist[thr]
        m_b = sum_b / w_b                        # среднее переднего плана
        m_f = (sum_all - sum_b) / (total - w_b)  # среднее фона
        var = w_b * (total - w_b) * (m_b - m_f) ** 2   # межклассовая дисперсия
        if var > best_var:
            best_var, best_thr = var, thr

    g = g.point(lambda p: 255 if p > best_thr else 0)  # бинаризация
    log.debug("preprocess: otsu_threshold=%d, %.1f ms",
              best_thr, (time.perf_counter() - t0) * 1000)
    return g


# --------------------------------------------------------------------------- #
# 3. REGEX-ОЧИСТКА РАСПОЗНАННОГО ТЕКСТА                                       #
# --------------------------------------------------------------------------- #
# Мусорные одиночные символы, которые любит генерировать OCR:
_GARBAGE_CHARS = r"~`|_°@•·¦¤§≡—–\u00ad"
RE_GARBAGE_SINGLES = re.compile(rf"(?<![\w])[{re.escape(_GARBAGE_CHARS)}](?![\w])")
RE_MULTI_SPACE     = re.compile(r"[ \t]{2,}")
RE_HYPHEN_BREAK    = re.compile(r"(\w)-\s*\n\s*(\w)")          # дефисный перенос строки
RE_SOFT_BREAK      = re.compile(r"([a-zA-Zа-яА-ЯёЁ\u0400-\u04FF])\s*\n\s*([a-zA-Zа-яА-ЯёЁ\u0400-\u04FF])")
RE_SPACES_AROUND   = re.compile(r"\s*\n\s*")                   # склейка строк через \n
RE_NOISE_LINE      = re.compile(r"^[\W_]{1,3}$", re.M)         # целые «мусорные» строки
RE_DOTS_RUN        = re.compile(r"\.{4,}")                     # многоточия-заполнители ...

# Слова: буквы Unicode (любой язык), внутренние ' и -, на конце ни ' ни -
WORD_RE = re.compile(r"[^\W\d_]+(?:['\-][^\W\d_]+)*")


def clean_text(raw: str) -> str:
    """Привести сырой OCR-вывод к читаемому виду.

    1. склейка слов, разорванных переносом строки ("transla-\ntion" -> "translation");
    2. удаление одиночного мусора (~ | _ ° @ ...);
    3. схлопывание двойных пробелов и пустых строк;
    4. отбрасывание строк-«шума» целиком.
    """
    t0 = time.perf_counter()
    txt = raw.replace("\r\n", "\n").replace("\r", "\n")

    # 1) склейка разорванных на стыке строк слов (с дефисом и без)
    txt = RE_HYPHEN_BREAK.sub(r"\1\2", txt)
    txt = RE_SOFT_BREAK.sub(r"\1\2", txt)
    # остальные переводы строк считаем реальными — но схлопнем обрамляющие пробелы
    txt = RE_SPACES_AROUND.sub("\n", txt)

    # 2) мусорные одиночные символы
    txt = RE_GARBAGE_SINGLES.sub("", txt)
    txt = RE_DOTS_RUN.sub("…", txt)

    # 3) двойные пробелы, хвостовые/слипшиеся пробелы вокруг удалённого мусора
    txt = RE_MULTI_SPACE.sub(" ", txt)
    txt = re.sub(r" +\n", "\n", txt)
    txt = re.sub(r" ?~ ?", " ", txt)              # '~', прилипший к слову (OCR-артефакт)
    txt = RE_MULTI_SPACE.sub(" ", txt)

    # 4) строки из одного-двух «не-слов» (типа "||", "--")
    lines = [ln for ln in txt.split("\n")
             if not RE_NOISE_LINE.match(ln.strip())]
    txt = "\n".join(lines).strip()

    log.debug("clean_text: %d -> %d chars, %.1f ms",
              len(raw), len(txt), (time.perf_counter() - t0) * 1000)
    return txt


def extract_words(text: str) -> list[str]:
    """Слова для «кликабельности» в истории/попапе."""
    return WORD_RE.findall(text)


# --------------------------------------------------------------------------- #
# 4. OCR-ДВИЖОК (ленивая инициализация, динамические языки)                   #
# --------------------------------------------------------------------------- #
@dataclass
class OcrResult:
    text: str
    detected_lang: str = ""
    engine: str = ""
    elapsed_ms: int = 0
    raw: str = field(default="", repr=False)


class OcrEngine:
    """Единственный экземпляр на приложение: загрузка моделей дорогая.

    Держит кэш Reader'ов по языкам — переключение source_lang в настройках
    НЕ требует перезапуска: просто берётся/создаётся нужный reader.
    """

    def __init__(self, engine: str = "easyocr", gpu: bool = False) -> None:
        self.engine = engine.lower()
        self.gpu = gpu
        self._readers: dict[str, object] = {}       # iso -> easyocr.Reader
        self._current_iso: Optional[str] = None
        log.info("OcrEngine created (engine=%s, gpu=%s)", self.engine, gpu)

    # -- easyocr ---------------------------------------------------------- #
    def _get_easyocr_reader(self, source_lang: str):
        key = source_lang if source_lang != "auto" else "auto"
        reader = self._readers.get(key)
        if reader is None:
            import easyocr  # тяжёлый импорт — только при первом захвате
            langs = ocr_langs_easyocr(source_lang)
            t0 = time.perf_counter()
            reader = easyocr.Reader(langs, gpu=self.gpu,
                                    lang_recognition=(source_lang == "auto"))
            self._readers[key] = reader
            log.info("EasyOCR reader loaded for %s (%.0f ms)",
                     langs, (time.perf_counter() - t0) * 1000)
        return reader

    def _run_easyocr(self, img, source_lang: str) -> OcrResult:
        t0 = time.perf_counter()
        reader = self._get_easyocr_reader(source_lang)
        parts: list[str] = []
        detected = ""
        # detail=1 -> [(bbox, text, conf)]; берём только уверенные фрагменты
        results = reader.readtext(img, detail=1, paragraph=True)
        for item in results:
            if isinstance(item, tuple) and len(item) >= 2:
                text = str(item[1])
                conf = float(item[2]) if len(item) > 2 else 1.0
                if conf >= 0.25:
                    parts.append(text)
            else:                                   # paragraph=True отдаёт строки
                parts.append(str(item))
        raw = "\n".join(parts)
        elapsed = int((time.perf_counter() - t0) * 1000)
        log.info("EasyOCR done: %d chars, %d ms", len(raw), elapsed)
        return OcrResult(text=clean_text(raw), detected_lang=detected or source_lang,
                         engine="easyocr", elapsed_ms=elapsed, raw=raw)

    # -- tesseract --------------------------------------------------------- #
    def _run_tesseract(self, img, source_lang: str) -> OcrResult:
        import pytesseract
        t0 = time.perf_counter()
        lang = ocr_lang_tesseract(source_lang)
        cfg = f"--oem 3 --psm 6 -l {lang}"
        raw = pytesseract.image_to_string(img, config=cfg)
        elapsed = int((time.perf_counter() - t0) * 1000)
        log.info("Tesseract done: %d chars, %d ms (langs=%s)", len(raw), elapsed, lang)
        return OcrResult(text=clean_text(raw), detected_lang=source_lang,
                         engine="tesseract", elapsed_ms=elapsed, raw=raw)

    # -- публичный API ------------------------------------------------------ #
    def recognize(self, pil_image, source_lang: str = "auto") -> OcrResult:
        """Главная точка входа. source_lang — ЛЮБОЙ ISO-код из матрицы выше."""
        img = preprocess_image(pil_image)
        t0 = time.perf_counter()
        if self.engine == "tesseract":
            res = self._run_tesseract(img, source_lang)
        else:
            try:
                res = self._run_easyocr(img, source_lang)
            except ImportError:
                log.warning("easyocr not installed, falling back to tesseract")
                res = self._run_tesseract(img, source_lang)
        res.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return res


# --------------------------------------------------------------------------- #
# 5. ПЕРЕВОДЧИК (deep_translator / GoogleTranslator)                          #
# --------------------------------------------------------------------------- #
class TranslationError(RuntimeError):
    pass


def translate_text(text: str, source_lang: str, target_lang: str,
                   timeout_s: float = 2.5) -> tuple[str, str]:
    """Синхронный перевод одного куска текста. Возвращает (перевод, определённый язык).

    source_lang='auto' передаётся Google как есть — автодетекция на стороне API,
    поэтому добавление новых языков экрана не меняет этот код вообще.
    """
    from deep_translator import GoogleTranslator

    t0 = time.perf_counter()
    translator = GoogleTranslator(source=source_lang or "auto", target=target_lang)
    translated = translator.translate(text)
    elapsed = time.perf_counter() - t0
    if translated is None:
        raise TranslationError("empty response from GoogleTranslator")
    log.info("Translated %s->%s: %d -> %d chars, %.0f ms%s",
             source_lang, target_lang, len(text), len(translated), elapsed * 1000,
             " [SLOW >2s]" if elapsed > 2.0 else "")
    # deep-translator не отдаёт reliably detected source; при 'auto' уточняем эвристикой
    detected = source_lang if source_lang != "auto" else _guess_lang(text)
    return str(translated), detected


_CYR = re.compile(r"[а-яё]", re.I)
_LAT = re.compile(r"[a-z]", re.I)


def _guess_lang(text: str) -> str:
    """Мини-эвристика для лога, когда source='auto'."""
    cyr, lat = len(_CYR.findall(text)), len(_LAT.findall(text))
    if cyr and cyr >= lat:
        return "ru?"
    return "en?" if lat else "?"


# --------------------------------------------------------------------------- #
# 6. QThread: OCR-тяжёлая часть тоже уходит из GUI-потока                    #
# --------------------------------------------------------------------------- #
try:                                             # модуль можно импортировать и без PyQt6
    from PyQt6.QtCore import QThread, pyqtSignal
except ImportError:                              # pragma: no cover — для автотестов CI
    QThread = object                             # type: ignore[misc,assignment]

    class _NoSignals:                            # type: ignore
        def __init__(self, *a, **k): pass
        def emit(self, *a, **k): pass
    pyqtSignal = lambda *a, **k: _NoSignals()    # type: ignore[assignment]


class CaptureWorker(QThread):
    """Полный цикл: image -> OCR -> clean -> translate. Сигналы в GUI-поток.

    Сигналы:
        progress(str)        — этап для попапа («Распознаю…», «Перевожу…»)
        finished_ok(object)  — CaptureResult
        failed(str)          — текст ошибки (уже локализовывать на стороне GUI)
    """

    progress   = pyqtSignal(str)
    finished_ok = pyqtSignal(object)
    failed     = pyqtSignal(str)

    def __init__(self, image, ocr: OcrEngine,
                 source_lang: str, target_lang: str, parent=None) -> None:
        super().__init__(parent)
        self._image = image
        self._ocr = ocr
        self._src = source_lang
        self._tgt = target_lang

    def run(self) -> None:                        # noqa: D102
        t_start = time.perf_counter()
        try:
            self.progress.emit("ocr")
            ocr_res = self._ocr.recognize(self._image, self._src)
            if not ocr_res.text.strip():
                self.failed.emit("no_text")
                return

            self.progress.emit("translate")
            translated, detected = translate_text(ocr_res.text, self._src, self._tgt)

            total_ms = int((time.perf_counter() - t_start) * 1000)
            log.info("Pipeline total: %d ms (ocr %d + net/api %d)",
                     total_ms, ocr_res.elapsed_ms, total_ms - ocr_res.elapsed_ms)
            self.finished_ok.emit(CaptureResult(
                original=ocr_res.text,
                translated=translated,
                source_lang=self._src,
                target_lang=self._tgt,
                detected_lang=detected,
                ocr_engine=ocr_res.engine,
                ocr_ms=ocr_res.elapsed_ms,
                total_ms=total_ms,
            ))
        except Exception as exc:                  # network/API/OCR — всё сюда
            log.exception("Capture pipeline failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}")


@dataclass
class CaptureResult:
    original: str
    translated: str
    source_lang: str
    target_lang: str
    detected_lang: str
    ocr_engine: str
    ocr_ms: int
    total_ms: int


if __name__ == "__main__":                        # самопроверка без GUI/сети
    sample = ("This  function~ performs |\n"
              "transla-\ntion of the text   here.\n"
              "||\n"
              "@@@ ###\n")
    cleaned = clean_text(sample)
    assert "translation" in cleaned and "~" not in cleaned and "  " not in cleaned, cleaned
    assert extract_words("don't café Привет") == ["don't", "café", "Привет"]
    # preprocessing smoke-test (только PIL):
    from PIL import Image
    im = Image.new("RGB", (60, 20), (10, 200, 10))
    bw = preprocess_image(im)
    assert bw.mode == "L" and set(bw.getcolors(10**6)[0][1:] ) <= {0, 255}
    print("translator.py self-test OK:")
    print(repr(cleaned))
