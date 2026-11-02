# -*- coding: utf-8 -*-
"""Feature hashing для route-модели (по образцу bbot/ml/features.py).

char/word n-граммы + семантические маркеры домена Ноябриков.
"""
import re

BUCKETS = 32768
FNV_OFFSET = 0x811C9DC5
FNV_PRIME = 0x01000193
FEATURE_VERSION = 1

_url_re = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+", re.IGNORECASE)
_mention_re = re.compile(r"@[a-zA-Z0-9_]{3,}")
_punct_re = re.compile(r"[^\w\s?]+", re.UNICODE)
_space_re = re.compile(r"\s+")
_keycap_re = re.compile(r"([\d])[\ufe0f\u20e3]+")

# семантические маркеры (широкие: решает модель, маркеры лишь помогают)
_M = {
    "ict": re.compile(r"икт|информатик", re.I),
    "olimp": re.compile(r"олимпиадн|учебн\w* смен", re.I),
    "juni": re.compile(r"джуниор", re.I),
    "project": re.compile(r"проектн", re.I),
    "meal": re.compile(r"завтрак|обед|ужин|аппетит|накрыт|зовём на|зовем на|столов", re.I),
    "gather": re.compile(r"сбор|ждать|ждём|ждем|жди|холл|турникет|гардероб|спуск|подойд|отправит", re.I),
    "school": re.compile(r"школ", re.I),
    "ulc": re.compile(r"улк|учебно-лабораторн", re.I),
    "everyone": re.compile(r"всех|всем|всему|вся", re.I),
    "time": re.compile(r"\d{1,2}\s*:\s*\d{2}|\d{3,4}", re.I),
    "sport": re.compile(r"спорт|тренажер|тренажёр|зал открыт", re.I),
    "contest": re.compile(r"конкурс|лектор|марафон|заявк|форм[ау] google", re.I),
    "lost": re.compile(r"потеряш", re.I),
    "cleanup": re.compile(r"уберите|убери|мусор|за собой", re.I),
    "morning": re.compile(r"доброе утро|утречко", re.I),
    "evening": re.compile(r"вечерн|дискотек|отбой|сон", re.I),
    "tech": re.compile(r"техно|корпорац|осув|лекци", re.I),
}


def normalize(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = _keycap_re.sub(r"\1", text)
    text = _url_re.sub(" link ", text)
    text = _mention_re.sub(" mention ", text)
    text = _punct_re.sub(" ", text)
    return _space_re.sub(" ", text).strip()


def _fnv(text: str, buckets: int) -> int:
    h = FNV_OFFSET
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * FNV_PRIME) & 0xFFFFFFFF
    return h % buckets


def features(text: str, buckets: int = BUCKETS):
    raw = text or ""
    norm = normalize(raw)
    if not norm:
        norm = "?"
    words = norm.split()
    ids = []

    def mark(name):
        ids.append(_fnv("m:" + name, buckets))

    def cross(name):
        ids.append(_fnv("x:" + name, buckets))

    flags = {name: bool(rx.search(norm)) for name, rx in _M.items()}
    ids.append(_fnv("wc:%d" % min(len(words), 20), buckets))
    ids.append(_fnv("nl:%d" % min(raw.count("\n"), 3), buckets))
    ids.append(_fnv("len:%d" % (0 if len(words) <= 3 else 1 if len(words) <= 10 else 2), buckets))
    for name, present in flags.items():
        if present:
            mark(name)

    # полезные пересечения
    if flags["ict"] and (flags["gather"] or flags["school"] or flags["ulc"]):
        cross("ict_gather")
    if flags["ict"] and flags["cleanup"]:
        cross("ict_cleanup")
    if flags["olimp"] and flags["gather"]:
        cross("olimp_gather")
    if flags["juni"] and flags["gather"]:
        cross("juni_gather")
    if flags["meal"] and flags["everyone"]:
        cross("meal_everyone")
    if flags["gather"] and flags["everyone"]:
        cross("gather_everyone")
    if flags["school"] and flags["time"]:
        cross("school_time")
    if flags["ulc"] and flags["time"]:
        cross("ulc_time")

    if words:
        ids.append(_fnv("f:" + words[0], buckets))
        ids.append(_fnv("l:" + words[-1], buckets))
    for w in words:
        ids.append(_fnv("w:" + w, buckets))
    for i in range(len(words) - 2):
        ids.append(_fnv("t:" + words[i] + "_" + words[i + 1] + "_" + words[i + 2], buckets))
    for i in range(len(words) - 1):
        ids.append(_fnv("b:" + words[i] + "_" + words[i + 1], buckets))
    for w in words:
        padded = "#" + w + "$"
        n = len(padded)
        for size in (3, 4, 5):
            for i in range(n - size + 1):
                ids.append(_fnv("c%d:%s" % (size, padded[i:i + size]), buckets))
    return ids


def features_unique(text: str, buckets: int = BUCKETS):
    seen = set()
    out = []
    for i in features(text, buckets):
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out
