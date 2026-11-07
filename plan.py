"""Разбор ПЛАНЧИКа из ИиКТ и времени сбора из Ноябриков."""
import datetime as dt
import re
import unicodedata

DATE_RE = re.compile(r"ПЛАНЧИК\s+на\s+(\d{1,2})\.(\d{1,2})", re.I)
DATE_HEAD_RE = re.compile(r"^\D*?(\d{1,2})\.(\d{1,2})\D*$")   # «🌸02.11🌸» в первой строке
LINE_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*[-\u2013\u2014]\s*(.+?)\s*$")
RANGE_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*[-\u2013\u2014]\s*(\d{1,2})[:.](\d{2})\s*[-\u2013\u2014]\s*(.+?)\s*$")
SECTIONS_PLAIN = {"икт": "ИКТ", "инф": "Инф", "информатики": "Инф"}

SECTION_RE = {
    "профильная": "профильная",
    "олимпиадная": "олимпиадная",
}


def classify(label, cfg):
    low = label.lower()
    # школа/УЛК — по границе слова («улк» и «булка» — разные вещи)
    if re.search(r"\bшкол", low) or re.search(r"\bулк\b", low):
        return "school"
    if any(k in low for k in cfg.get("skip_keywords", [])):
        return None
    return "five"          # любое другое событие — за 5 минут


def parse_plan(text, posted_at, cfg):
    """→ dict(date=date|None, events=[{when, label, kind, section}])

    Дата берётся только явно: «Планчик на DD.MM» или заголовок «🌸DD.MM🌸»
    в первых строках. Без явной даты → date=None: чужие копипасты-расписания
    (например «ПЛАН НА ДЕНЬ 07.11.2026») планчиком не считаются."""
    common = cfg.get("common_events", [])
    day = month = None
    m = DATE_RE.search(text or "")
    if m:
        day, month = int(m.group(1)), int(m.group(2))
    else:
        # «🌸02.11🌸» заголовком, без слова ПЛАНЧИК
        for raw in (text or "").splitlines()[:3]:
            hm = DATE_HEAD_RE.match(raw.strip())
            if hm:
                day, month = int(hm.group(1)), int(hm.group(2))
                break
    if day and month and 1 <= month <= 12 and 1 <= day <= 31:
        year = posted_at.year
        date = dt.date(year, month, day)
        # планчик на 31.12, опубликован 30.12 — ок; на 01.01 — следующий год
        if (date - posted_at.date()).days < -180:
            date = dt.date(year + 1, month, day)
    else:
        date = None

    events, section = [], None
    if date is None:
        return {"date": None, "events": events}
    for raw in (text or "").splitlines():
        line = raw.strip()
        low = re.sub(r"\s+", " ", line.lower()).strip()
        low = re.sub(r"[*_~`]+", " ", low).strip()   # markdown-обёртки планчика
        if low.startswith("профильн"):
            section = "профильная"
            continue
        if low.startswith("олимпиадн"):
            section = "олимпиадная"
            continue
        # новый шаблон: заголовки смен «ИКТ»/«Инф» отдельной строкой
        key = re.sub(r"[^а-яё]", "", low)
        if key in SECTIONS_PLAIN:
            section = SECTIONS_PLAIN[key]
            continue
        if not line:
            section = None   # блок смены кончился — дальше общие события
            continue
        rm = RANGE_RE.match(line)   # «14.30-16.30 - чуть-чуть учимся»
        if rm:
            h, mi = int(rm.group(1)), int(rm.group(2))
            h2, mi2, label = int(rm.group(3)), int(rm.group(4)), rm.group(5)
            until = dt.time(h2, mi2)
        else:
            tm = LINE_RE.match(line)
            if not tm:
                continue
            h, mi, label = int(tm.group(1)), int(tm.group(2)), tm.group(3).strip()
            until = None
        label = re.sub(r"[*_~`]+$", "", label).strip()
        if not (0 <= h < 24 and 0 <= mi < 60):
            continue
        when = dt.datetime.combine(date, dt.time(h, mi))
        label_low = label.lower()
        sec = None if any(k in label_low for k in common) else section
        events.append({
            "when": when,
            "label": label,
            "kind": classify(label, cfg),
            "section": sec,
            "until": until,
        })
    events.sort(key=lambda e: e["when"])
    return {"date": date, "events": events}


# ────────── время сбора из Ноябриков (в т.ч. эмодзи-цифры) ──────────

def _clean(text):
    out = []
    for ch in text or "":
        if ch.isdigit() or ch == ":":
            out.append(ch)
        elif ch in ("\ufe0f", "\u20e3") or unicodedata.combining(ch):
            continue                      # вариационные селекторы/обводки
        elif ch.isalpha() or ch.isspace():
            out.append(ch)
        else:
            out.append(" ")               # эмодзи/пунктуация → пробел
    return re.sub(r"[ \t]+", " ", "".join(out))


def extract_time(text):
    """Ищет время в тексте: 'в 14:10', 'в 8:30', 'в 1415', эмодзи-цифры."""
    s = _clean(text)
    cands = []
    for m in re.finditer(r"(\d{1,2})\s*:\s*(\d{1,2})", s):
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h < 24 and 0 <= mi < 60:
            cands.append((h, mi))
    for m in re.finditer(r"\bв\s+(\d{1,2})\s+(\d{2})\b", s):
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h < 24 and 0 <= mi < 60:
            cands.append((h, mi))
    for m in re.finditer(r"\bв\s+(\d{3,4})\b", s):
        d = m.group(1)
        h, mi = int(d[:-2]), int(d[-2:])
        if 0 <= h < 24 and 0 <= mi < 60:
            cands.append((h, mi))
    if not cands:
        return None
    return cands[-1]


def gather_time(text):
    """Время сбора, если сообщение про школу/УЛК; иначе None."""
    low = (text or "").lower()
    if not re.search(r"школ|улк|учебно-лабораторн", low):
        return None
    if not re.search(r"ждать|ждём|ждем|сбор|холл|турникет|гардероб|отправит|спуска|подойд", low):
        return None
    return extract_time(text)
