"""Ноябрики → ЛС + ИиКТ: пересылка важного и напоминания по ПЛАНЧИКу."""
import asyncio
import datetime as dt
import json
import logging
import re
import os
import shutil
import sys
import time
from pathlib import Path

from telethon import Button, TelegramClient, events
from telethon.tl import functions, types
import plan as planmod

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("ikt")


def load_env():
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

API_ID = int(ENV["API_ID"])
API_HASH = ENV["API_HASH"]
OWNER = int(CFG.get("owner_id") or ENV.get("OWNER_ID") or 0)
CHANNEL = int(CFG["channel_id"])
GROUP = int(CFG["group_id"])
EXTRA_GROUPS = [int(x) for x in CFG.get("extra_groups", [])]
GROUPS = [GROUP] + EXTRA_GROUPS
TARGETS = [GROUP]                     # пишем только в ИиКТ (задание владельца)

# связь часто пропадает (мобильный интернет/Termux): телетон должен переподключаться
# бесконечно, а не сдаться после 5 попыток и похоронить бота
CONNECT = dict(connection_retries=None, retry_delay=5, request_retries=8)
bot = TelegramClient(str(ROOT / "sessions" / "bot"), API_ID, API_HASH, **CONNECT)
ub = TelegramClient(str(ROOT / "sessions" / "ikt-bot"), API_ID, API_HASH,
                    device_model="ikt-bot", system_version="Termux", app_version="1.0",
                    **CONNECT)

STATE = {
    "plan": None,          # {"date": date, "events": [...]}
    "prev_plan": None,     # прошлый планчик — держим в памяти
    "plans": {},           # архив: все планчики, которые бот видел (date -> план)
    "seen": set(),         # id сообщений Ноябриков
    "sent": set(),         # ключи отправленных напоминаний
}
SENT_FILE = ROOT / "sessions" / ".sent.json"
SEEN_FILE = ROOT / "sessions" / ".seen.json"
RECENT_FILE = ROOT / "sessions" / ".recent.json"
MUTED_FILE = ROOT / "sessions" / ".muted.json"
PLAN_FILE = ROOT / "sessions" / ".plan.json"
PLAN_PREV_FILE = ROOT / "sessions" / ".prev_plan.json"
PLANS_FILE = ROOT / "sessions" / ".plans.json"   # архив всех планчиков
LOCK_FILE = ROOT / ".run" / "bot.lock"


def acquire_lock():
    """Защита от второго инстанса (иначе бот отвечает на всё дважды)."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)
            log.error("бот уже запущен (pid %s) — второй инстанс не стартую", pid)
            return False
        except (ValueError, ProcessLookupError, TypeError):
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except Exception:
        pass
DRY = "--test" in sys.argv


def save_sent():
    try:
        SENT_FILE.write_text(json.dumps(sorted(STATE["sent"]), ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def load_sent():
    try:
        STATE["sent"] = set(json.loads(SENT_FILE.read_text(encoding="utf-8")))
    except Exception:
        STATE["sent"] = set()


def save_muted():
    try:
        MUTED_FILE.write_text(json.dumps(sorted(STATE["muted"])), encoding="utf-8")
    except Exception:
        pass


def load_muted():
    try:
        STATE["muted"] = set(int(x) for x in json.loads(MUTED_FILE.read_text(encoding="utf-8")))
    except Exception:
        STATE["muted"] = set()


def save_seen():
    try:
        keep = sorted(STATE["seen"])[-2000:]
        SEEN_FILE.write_text(json.dumps(keep), encoding="utf-8")
    except Exception:
        pass


def load_seen():
    try:
        STATE["seen"] = set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except Exception:
        STATE["seen"] = set()
    try:
        STATE["recent"] = json.loads(RECENT_FILE.read_text(encoding="utf-8"))
    except Exception:
        STATE["recent"] = {}


def save_recent():
    try:
        RECENT_FILE.write_text(json.dumps(STATE["recent"], ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def is_duplicate(caption):
    """True, если такой же пост уже отправляли за последние 30 минут."""
    now = time.time()
    recent = STATE.setdefault("recent", {})
    for k in [k for k, t in recent.items() if now - t > 1800]:
        del recent[k]
    if caption in recent:
        return True
    recent[caption] = now
    if len(recent) > 500:
        for k in sorted(recent, key=lambda k: recent[k])[:250]:
            del recent[k]
    save_recent()
    return False


def is_important(text):
    low = (text or "").lower()
    for p in CFG["transfer_exclude"]:
        if p in low:
            return False
    return any(p in low for p in CFG["transfer_include"])


MEAL_WORDS = ("завтрак", "обед", "ужин")


async def adjust_meal_reminder(text):
    """Пост Ноябриков про еду: «зовем на ужин»/«накрыт» — еда сейчас,
    напоминание по плану отменяем; явное время в посте — сдвигаем."""
    low = (text or "").lower()
    meal = next((w for w in MEAL_WORDS if w in low), None)
    if not meal:
        return
    p = STATE["plan"]
    if not p:
        return
    now = dt.datetime.now()

    def is_match(e):
        lab = e["label"].lower()
        # новый шаблон планчика: еда называется «нутрений/обеденный/вечерний ням-ням»
        names = {"завтрак": ("завтрак", "ням-ням"),
                 "обед": ("обед", "ням-ням"),
                 "ужин": ("ужин", "ням-ням")}.get(meal, (meal,))
        if not any(n in lab for n in names):
            return False
        if meal == "ужин" and ("2-й" in lab or "второй" in lab) and "2-й" not in low:
            return False
        return True

    cands = [e for e in p["events"] if e["kind"] == "five" and is_match(e)
             and e["when"].date() == now.date()]
    if not cands:
        return
    e = min(cands, key=lambda x: abs((x["when"] - now).total_seconds()))

    tm = planmod.extract_time(text)
    if tm:
        new_when = dt.datetime.combine(e["when"].date(), dt.time(*tm))
        if new_when > now and abs((new_when - e["when"]).total_seconds()) > 120:
            log.info("еда по посту сдвинута: %s -> %s", e["when"], new_when)
            e["when"] = new_when
            return

    if e["when"] > now:
        k5 = "%s|%s|5" % (e["when"].isoformat(), e["label"])
        kstart = "%s|%s|start" % (e["when"].isoformat(), e["label"])
        if k5 in STATE["sent"] or kstart in STATE["sent"]:
            return   # напоминание уже ушло/отменено — второй раз не пишем
        # отменяем все будущие напоминания этого события (5 мин, 30 мин, старт)
        for o in (5, 30):
            STATE["sent"].add("%s|%s|%d" % (e["when"].isoformat(), e["label"], o))
        STATE["sent"].add(kstart)
        save_sent()
        log.info("напоминание про %s отменено: еда объявлена в Ноябриках раньше плана", meal)
        await send_pair("По Ноябрикам %s уже начался (план был в %s) — напоминание отменено."
                        % (e["label"], e["when"].strftime("%H:%M")))


def _rule_route(text):
    """Старые правила: ИиКТ — икт/информатикам; ИКТ-бтл — икт; общие — везде."""
    low = (text or "").lower()
    aud = CFG.get("audience", {})
    ict = any(k in low for k in aud.get("ict", []))
    # прямой адрес «икт/информатикам» — важен даже без ключей is_important
    if not is_important(low) and not ict:
        return None
    other = any(k in low for k in aud.get("other", []))
    strict = any(k in low for k in aud.get("ict_strict", []))
    general = (any(k in low for k in aud.get("general", []))
               or not (ict or other))
    r = {"group": bool(ict or general),
         "extra": bool(strict or general)}
    if not (r["group"] or r["extra"]):
        return None   # например «биотехнологии ждём» — никому из наших
    return r


ROUTE_MODEL = None


def load_route_model():
    global ROUTE_MODEL
    p = ROOT / "ml" / "route_model.bin"
    if not p.exists():
        log.warning("route-модель не найдена (%s) — работают правила", p)
        return
    try:
        sys.path.insert(0, str(ROOT / "ml"))
        from route_model import RouteModel
        ROUTE_MODEL = RouteModel(str(p))
        log.info("route-модель загружена (%s, val macroF1 %.3f, train %s)",
                 ROUTE_MODEL.meta.get("algo"),
                 ROUTE_MODEL.meta.get("val_macro_f1", -1),
                 ROUTE_MODEL.meta.get("n_train"))
    except Exception as e:
        log.warning("route-модель не загрузилась: %s", e)


def route(text):
    """Куда пересылать: {"group": ИиКТ, "extra": ИКТ-бтл} или None.
    Решает нейронка (ml/route_model.bin), guard'ы — как в bbot:
    исключения (конкурс и т.п.) рубят всегда, правила подстраховывают recall."""
    low = (text or "").lower()
    if any(p in low for p in CFG["transfer_exclude"]):
        return None
    if any(k in low for k in CFG.get("meal_force", [])):
        return {"group": True, "extra": True}   # еда — всегда в оба чата
    if ROUTE_MODEL is None:
        return _rule_route(text)
    label, probs = ROUTE_MODEL.predict(text)
    mr = {"both": {"group": True, "extra": True},
          "group": {"group": True, "extra": False},
          "none": None}.get(label)
    if mr is None:
        mr = _rule_route(text)   # правила спасают recall на важных постах
    log.info("route ml: %s %s -> %s", label, probs, "отправка" if mr else "скип")
    return mr


async def send_pair(text):
    """Напоминания/уведомления о событиях: чат с /dont-notify не получает их.
    → False, если ушло не всё из-за сети (напоминание надо повторить позже)."""
    if DRY:
        log.info("[DRY] %s", text.replace("\n", " / ")[:160])
        return True
    net_fail = False
    for chat in TARGETS:
        if chat in STATE["muted"]:
            continue
        try:
            await bot.send_message(chat, text)
        except (OSError, asyncio.TimeoutError) as e:   # сетевое — временно, повторим
            log.warning("сеть: не отправил в %s: %s", chat, e)
            net_fail = True
        except Exception as e:                        # чата нет/бот выгнан — навсегда, не повторяем
            log.warning("не отправил в %s: %s", chat, e)
    return not net_fail


# ───────────────────────── СИР и МЭС ─────────────────────────

USERS_FILE = ROOT / "sessions" / ".users.json"
SIR_FILE = ROOT / "sessions" / ".sir.json"
BOT_LINK = "https://t.me/rivalikt_bot"
ACTS = ["учебная", "трудовая", "спортивная", "познавательная"]
MOODS = [("🟢", "отличное"), ("🟡", "хорошее"), ("🔵", "спокойное"), ("🔴", "грустное")]
FIO_RE = re.compile(r"^[А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)?$")


def load_users():
    try:
        STATE["users"] = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        STATE["users"] = {}
    except Exception as e:
        # битый файл — не затираем данные в памяти, чтобы не потерять эмодзи
        log.warning("users.json не прочитался (%s) — держу старые данные", e)
        STATE["users"] = STATE.get("users") or {}


def save_users():
    """Атомарно: .tmp -> rename, прежний файл остаётся как .bak."""
    try:
        if USERS_FILE.exists():
            shutil.copy2(USERS_FILE, USERS_FILE.with_suffix(".bak"))
        tmp = USERS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(STATE["users"], ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(USERS_FILE)
    except Exception:
        pass


def load_sir():
    try:
        STATE["sir"] = json.loads(SIR_FILE.read_text(encoding="utf-8"))
    except Exception:
        STATE["sir"] = {}
    today = dt.date.today().isoformat()
    if STATE["sir"].get("date") != today:          # новый день — чистый лист
        STATE["sir"] = {"date": today, "filled": {}, "slots": []}


def save_sir():
    try:
        SIR_FILE.write_text(json.dumps(STATE["sir"], ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def sir_mention(pid, username, name):
    """HTML: у зарегистрированных — эмодзи-ссылка на юзера, иначе @юз / имя."""
    prof = STATE["users"].get(str(pid))
    if prof and prof.get("emoji"):
        return '<a href="tg://user?id=%d">%s</a>' % (pid, prof["emoji"])
    if username:
        return "@" + username
    return (name or "юзер").strip() or "юзер"


async def sir_people():
    """Участники ИиКТ без тимлидов и ботов: {uid: {username, name}}. Кеш 10 мин."""
    cache = STATE.get("people_cache")
    if cache and time.time() - cache[0] < 600:
        return cache[1]
    people = {}
    try:
        # у Telethon iter_participants отдаёт User напрямую; тимлиды — админы группы
        admin_ids = set()
        async for u in ub.iter_participants(GROUP,
                                            filter=types.ChannelParticipantsAdmins):
            admin_ids.add(u.id)
        async for u in ub.iter_participants(GROUP):
            if getattr(u, "bot", False) or getattr(u, "deleted", False):
                continue
            if u.id in admin_ids:
                continue
            people[u.id] = {"username": getattr(u, "username", None),
                            "name": ((getattr(u, "first_name", "") or "") + " "
                                     + (getattr(u, "last_name", "") or "")).strip()}
    except Exception as e:
        log.warning("не смог получить участников для СИР: %s", e)
        return cache[1] if cache else {}
    STATE["people_cache"] = (time.time(), people)
    return people


async def sir_round():
    """Напоминание: счёт в ИиКТ + ЛС каждому не прошедшему."""
    people = await sir_people()
    if not people:
        return
    opted = {int(k) for k, v in STATE["users"].items() if v.get("nosir")}
    filled = STATE["sir"].get("filled", {})
    missed = [dict(v, id=k) for k, v in people.items()
              if str(k) not in filled and k not in opted]
    if not missed:
        if not STATE["sir"].get("all_posted"):
            STATE["sir"]["all_posted"] = True
            save_sir()
            if not DRY:
                await bot.send_message(GROUP, "Все заполнили сир и мэс сегодня.")
            log.info("СИР и МЭС: все заполнили (%d)", len(people))
        return
    if STATE["sir"].get("all_posted"):
        return   # уже оповестили «все заполнили» — не спамим разворотом
    names = "\n".join("• " + sir_mention(u["id"], u["username"], u["name"]) for u in missed)
    text = "%d/%d не заполнили сир и мэс сегодня\nНе прошли:\n%s" % (
        len(missed), len(people), names)
    if DRY:
        log.info("[DRY] СИР: %s", text.replace("\n", " / ")[:160])
    else:
        await bot.send_message(GROUP, text, parse_mode="html",
                               buttons=[[Button.url("Пройти сир и мэс",
                                                    BOT_LINK + "?start=sir")]])


async def sir_rollover():
    """Смена дня: чистый лист; не заполнивших за вчерашний день — списком в ИиКТ."""
    today = dt.date.today().isoformat()
    if STATE["sir"].get("date") == today:
        return False
    people = await sir_people()
    opted = {int(k) for k, v in STATE["users"].items() if v.get("nosir")}
    filled = STATE["sir"].get("filled", {})
    late = [dict(v, id=k) for k, v in (people or {}).items()
            if str(k) not in filled and k not in opted]
    STATE["sir"] = {"date": today, "filled": {}, "slots": [], "all_posted": False}
    save_sir()
    # вчерашний планчик moves в prev_plan, обновления ждут нового
    pl = STATE.get("plan")
    if pl and pl["date"].isoformat() < today:
        STATE["prev_plan"] = pl
        STATE["plan"] = None
        log.info("прошлый план (%s) сохранён в prev_plan", pl["date"].strftime("%d.%m"))
    if late:
        names = "\n".join("• " + sir_mention(u["id"], u["username"], u["name"]) for u in late)
        text = "Не заполнили сир и мэс за прошлый день:\n%s" % names
        if DRY:
            log.info("[DRY] СИР-ролловер: %s", text.replace("\n", " / ")[:160])
        else:
            try:
                await bot.send_message(GROUP, text, parse_mode="html")
            except Exception as e:
                log.warning("ролловер СИР не ушел: %s", e)
        log.info("СИР: %d не заполнили прошлый день", len(late))
    return True


async def sir_loop():
    """Раунды СИР/МЭС: 20:30, 21:15, 22:00, 23:00; смена дня — разбор в 00:00."""
    if not CFG.get("sir_enabled", True):
        log.info("сир и мэс отключены глобально (sir_enabled=false)")
        return
    while True:
        try:
            if STATE.get("sir") and not DRY:
                await sir_rollover()
                slot = dt.datetime.now().strftime("%H:%M")
                if slot in CFG.get("sir_times", []) and slot not in STATE["sir"].get("slots", []):
                    STATE["sir"].setdefault("slots", []).append(slot)
                    save_sir()
                    await sir_round()
        except Exception:
            log.exception("ошибка напоминаний СИР")
        await asyncio.sleep(30)


def act_keyboard():
    return [[Button.inline(a, ("siract:" + a).encode())] for a in ACTS]


def mood_keyboard():
    return [[Button.inline("%s %s" % (e, m), ("sirmood:%s|%s" % (e, m)).encode())]
            for e, m in MOODS]


async def sir_launch(uid, send):
    """Следующий шаг заполнения в ЛС. send = функция отправки (ev.reply / dm)."""
    STATE["sir_session"] = STATE.get("sir_session") or {}
    prof = STATE["users"].get(str(uid)) or {}
    if not prof.get("name"):
        STATE["sir_session"][uid] = {"stage": "fio"}
        await send("Фамилия и имя через пробел (например: Иванов Иван).\n"
                   "Ровно два слова, только русские буквы.")
        return
    if not prof.get("emoji"):
        STATE["sir_session"][uid] = {"stage": "emoji"}
        if prof.get("nosir"):
            await send("Отправь эмодзи, которым тебя удобно упоминать в чатах — "
                       "это всё, что нужно (сир и мэс отключены).")
        else:
            await send("Отправь эмодзи, которым тебя удобно упоминать в чатах.")
        return
    STATE["sir_session"].pop(uid, None)
    if prof.get("nosir"):
        await send("Готово: тебя можно упоминать эмодзи %s. Сир и мэс отключены."
                   % prof["emoji"])
        return
    await send("Выбери свою деятельность:", buttons=act_keyboard())


@bot.on(events.NewMessage(pattern=r"^/sir(@\w+)?(\s|$)"))
async def cmd_sir(ev):
    if spam_blocked(ev, "sir"): return
    """Заполнение СИР и МЭС — только в ЛС."""
    if not ev.is_private:
        try:
            await ev.reply("Заполнение сир и мэс — только в ЛС с ботом.",
                           buttons=[[Button.url("Пройти сир и мэс",
                                                BOT_LINK + "?start=sir")]])
        except Exception:
            pass
        return
    # /sir — это согласие на заполнение: личное отключение снимаем
    STATE["users"].setdefault(str(ev.sender_id), {}).pop("nosir", None)
    save_users()
    await sir_launch(ev.sender_id, ev.reply)


@bot.on(events.NewMessage(pattern=r"^/nosir(@\w+)?(\s|$)"))
async def cmd_nosir(ev):
    if spam_blocked(ev, "nosir"): return
    """Отключить сир и мэс для себя — остаётся только выбор эмодзи."""
    if not ev.is_private:
        try:
            await ev.reply("/nosir работает только в ЛС с ботом.",
                           buttons=[[Button.url("Открыть бота", BOT_LINK)]])
        except Exception:
            pass
        return
    STATE["users"].setdefault(str(ev.sender_id), {})["nosir"] = True
    save_users()
    log.info("сир и мэс отключены для %s", ev.sender_id)
    await sir_launch(ev.sender_id, ev.reply)


@bot.on(events.NewMessage(pattern=r"^/emoji(@\w+)?(\s|$)"))
async def cmd_emoji(ev):
    if spam_blocked(ev, "emoji"): return
    """Выбор/перевыбор личного эмодзи — только в ЛС."""
    if not ev.is_private:
        try:
            await ev.reply("/emoji работает только в ЛС с ботом.",
                           buttons=[[Button.url("Открыть боту", BOT_LINK)]])
        except Exception:
            pass
        return
    STATE["sir_session"] = STATE.get("sir_session") or {}
    STATE["sir_session"][ev.sender_id] = {"stage": "emoji", "only": True}
    await ev.reply("Отправь эмодзи, которым тебя будут упоминать:")


@bot.on(events.NewMessage)
async def sir_pm(ev):
    """ЛС: машина состояний СИР/МЭС (имя, эмодзи, кнопки). Слэш-команды не трогаем."""
    if not ev.is_private:
        return
    text = (ev.raw_text or "").strip()
    if text.startswith("/"):
        return
    session = (STATE.get("sir_session") or {}).get(ev.sender_id)

    if session and session["stage"] == "fio":
        words = text.split()
        ok = len(words) == 2 and all(FIO_RE.match(w) for w in words)
        if not ok:
            await ev.reply("Нужно ровно два слова и только русские буквы. Ещё раз:")
            return
        STATE["users"].setdefault(str(ev.sender_id), {})["name"] = " ".join(
            w.capitalize() for w in words)
        save_users()
        session["stage"] = "emoji"
        await ev.reply("Записал: %s.\nТеперь отправь эмодзи, которым тебя удобно "
                       "упоминать в чатах." % STATE["users"][str(ev.sender_id)]["name"])
        return

    if session and session["stage"] == "emoji":
        if not text or len(text) > 8 or re.search(r"[A-Za-z0-9А-ЯЁа-яё]", text):
            await ev.reply("Это не похоже на эмодзи. Отправь один эмодзи:")
            return
        STATE["users"].setdefault(str(ev.sender_id), {})["emoji"] = text
        save_users()
        if session.get("only"):               # /emoji — только перевыбор
            STATE["sir_session"].pop(ev.sender_id, None)
            await ev.reply("Принято: теперь тебя можно упоминать эмодзи %s." % text)
            return
        await ev.reply("Принято.")
        await sir_launch(ev.sender_id, ev.reply)
        return

    # без сессии: приглашение заполнить — только если СИР/МЭС включён
    if not CFG.get("sir_enabled", True):
        return
    filled = STATE.get("sir", {}).get("filled", {})
    if str(ev.sender_id) not in filled and ev.sender_id in (await sir_people()):
        await sir_launch(ev.sender_id, ev.reply)


@bot.on(events.CallbackQuery(data=re.compile(rb"sir(act|mood):(.+)")))
async def sir_callback(ev):
    """Кнопки деятельность/настроение."""
    uid = ev.sender_id
    prof = STATE["users"].get(str(uid)) or {}
    # без регистрации (имя+эмодзи) кнопки не активны
    if not (prof.get("name") and prof.get("emoji")):
        await ev.answer("Сначала регистрация: введи фамилию и имя.", alert=True)
        await ev.edit("Фамилия и имя через пробел (например: Иванов Иван).")
        STATE["sir_session"] = STATE.get("sir_session") or {}
        STATE["sir_session"][uid] = {"stage": "fio"}
        return
    kind, _, payload = ev.data.decode().partition(":")
    STATE["sir_session"] = STATE.get("sir_session") or {}
    session = STATE["sir_session"].setdefault(uid, {})
    if kind == "siract":
        session["act"] = payload
        await ev.edit("Твоя деятельность: %s.\nТеперь выбери настроение:" % payload,
                      buttons=mood_keyboard())
        return
    act = session.get("act")
    if not act:
        await ev.edit("Сначала выбери деятельность:", buttons=act_keyboard())
        return
    mood = payload.replace("|", " ")
    STATE["sir"]["filled"][str(uid)] = {"act": act, "mood": mood}
    save_sir()
    STATE["sir_session"].pop(uid, None)
    await ev.edit("Записал: деятельность — %s, настроение — %s." % (act, mood),
                  buttons=None)



# ───────────────────────── пересылка Ноябриков ─────────────────────────

async def process_channel_message(m):
    """Маршрутизация и отправка одного поста Ноябриков (нового или пропущенного)."""
    if m is None or m.action is not None:
        return
    text = m.message or ""
    if m.id in STATE["seen"]:
        return
    r = route(text)
    if not r:
        STATE["seen"].add(m.id)
        save_seen()
        return
    # пересылка Ноябриков отключена — только служебная реакция (сдвиг/отмена еды)
    if not CFG.get("transfer_noyabrki", True):
        STATE["seen"].add(m.id)
        save_seen()
        await adjust_meal_reminder(text)
        return

    when = m.date.astimezone().strftime("%d.%m %H:%M")
    caption = "Ноябрики · %s" % when
    if text:
        caption += "\n\n" + text
    if is_duplicate(caption):
        log.info("дубль [%s] — скип", m.id)
        STATE["seen"].add(m.id)
        save_seen()
        return
    log.info("пересылаю [%s] (ИиКТ=%s, ИКТ-бтл=%s) %.60s",
             m.id, r["group"], r["extra"], text)

    if DRY:
        log.info("[DRY] переслал бы: %s", caption.replace("\n", " / ")[:160])
        return

    targets = [GROUP] if r["group"] else []   # только ИиКТ
    if not targets:
        STATE["seen"].add(m.id)
        save_seen()
        return   # ни одному чату не нужно

    # медиа скачиваем в файл с правильным расширением (raw m.media бот шлём не умеет)
    media_path = None
    if m.photo or m.video or m.video_note or m.gif:
        try:
            d = ROOT / ".run" / "media"
            d.mkdir(parents=True, exist_ok=True)
            media_path = await m.download_media(file=str(d / "media"))
        except Exception as e:
            log.warning("медиа не скачалось: %s", e)

    try:
        ok = True
        for chat in targets:
            try:
                if media_path:
                    await bot.send_file(chat, media_path, caption=caption)
                else:
                    await bot.send_message(chat, caption)
            except Exception as e:
                ok = False
                log.warning("не отправил в %s: %s", chat, e)
    finally:
        if media_path:
            try:
                os.remove(media_path)
            except OSError:
                pass
    if ok:
        STATE["seen"].add(m.id)
        save_seen()
    await adjust_meal_reminder(text)


@ub.on(events.NewMessage(chats=CHANNEL))
async def on_channel(ev):
    await process_channel_message(ev.message)


async def catchup():
    """Догоняем посты, пришедшие пока бот был выключен/перезапускался."""
    try:
        msgs = await ub.get_messages(CHANNEL, limit=40)
    except Exception as e:
        log.warning("catch-up не смог прочитать Ноябрики: %s", e)
        return
    now = dt.datetime.now(dt.timezone.utc)
    for m in reversed(list(msgs)):
        if not m or not (m.message or "").strip():
            continue
        if m.id in STATE["seen"]:
            continue
        age_min = (now - m.date).total_seconds() / 60
        if age_min > 90:
            continue
        log.info("catch-up: [%s] %.0f мин назад", m.id, age_min)
        await process_channel_message(m)


# ───────────────────────── ПЛАНЧИК ─────────────────────────

async def load_plan():
    msgs = []
    for gid in GROUPS:
        try:
            found = await ub.get_messages(gid, limit=400)
        except Exception as e:
            log.warning("не смог прочитать %s: %s", gid, e)
            continue
        for m in found:
            if not m or (m.message or "").startswith("Ноябрики ·"):
                continue
            if getattr(m.sender, "bot", False):
                continue                      # ответы самого бота (/plan) — не планчик
            txt = m.message or ""
            if not (planmod.has_explicit_date(txt)
                    or sum(1 for ln in txt.splitlines()
                           if planmod.LINE_RE.match(ln) or planmod.RANGE_RE.match(ln)) >= 5):
                continue
            msgs.append(m)
    if not msgs:
        if STATE.get("plan"):
            log.info("планчик в чатах не найден — держу сохранённый на %s",
                     STATE["plan"]["date"].strftime("%d.%m"))
        else:
            log.warning("планчик не найден в чатах")
        return
    for m in sorted(msgs, key=lambda x: x.date, reverse=True):
        try:
            p = planmod.parse_plan(m.message, m.date.astimezone(), CFG)
        except Exception:
            continue                      # битый «похожий на планчик» пост не убивает бот
        if not p["events"]:
            continue                      # «Планчик на 31.10» в ответе бота — не планчик
        if p["date"] is None:             # чужие копипасты-расписания без нашей даты
            continue
        p["_src"] = m.id
        if plan_ok_for_today(p):
            archive_plan(p)
            accept_plan(p)
            log.info("загружен планчик на %s: %d событий", p["date"].strftime("%d.%m"), len(p["events"]))
            return
        archive_plan(p)                   # старый — только в архив, активным не делаем
    if STATE.get("plan"):
        log.info("активного планчика в чатах не нашлось — держу сохранённый на %s",
                 STATE["plan"]["date"].strftime("%d.%m"))
    else:
        log.warning("актуального планчика в чатах не нашлось")


# ───────────────────────── напоминания ─────────────────────────

def _ev_to_dict(e):
    d = dict(e)
    d["when"] = e["when"].isoformat()
    if e.get("gather"):
        d["gather"] = {"time": e["gather"]["time"].isoformat(), "text": e["gather"]["text"]}
    return d


def _ev_from_dict(d):
    e = dict(d)
    e["when"] = dt.datetime.fromisoformat(d["when"])
    if d.get("gather"):
        g = dict(d["gather"]); g["time"] = dt.datetime.fromisoformat(g["time"]); e["gather"] = g
    return e


def plan_to_dict(p):
    return {"date": p["date"].isoformat(), "_src": p.get("_src"),
            "events": [_ev_to_dict(e) for e in p["events"]]}


def plan_from_dict(d):
    try:
        return {"date": dt.date.fromisoformat(d["date"]),
                "_src": d.get("_src"),
                "events": [_ev_from_dict(e) for e in d["events"]]}
    except Exception:
        return None


def save_plan():
    """Планчик живёт в файле — рестарт бота больше не стирает его."""
    try:
        if STATE.get("plan"):
            PLAN_FILE.write_text(json.dumps(plan_to_dict(STATE["plan"]), ensure_ascii=False),
                                 encoding="utf-8")
        if STATE.get("prev_plan"):
            PLAN_PREV_FILE.write_text(json.dumps(plan_to_dict(STATE["prev_plan"]),
                                                  ensure_ascii=False),
                                      encoding="utf-8")
    except Exception:
        pass


def load_saved_plan():
    """Поднять планчик из файла (чат может не содержать оригинала — он удалён)."""
    for key, f in (("plan", PLAN_FILE), ("prev_plan", PLAN_PREV_FILE)):
        try:
            p = plan_from_dict(json.loads(f.read_text(encoding="utf-8")))
            if p:
                STATE[key] = p
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("не прочитал %s", f.name)
    p = STATE.get("plan")
    if p:
        log.info("планчик на %s восстановлен из файла (%d событий)",
                 p["date"].strftime("%d.%m"), len(p["events"]))


def save_archive():
    try:
        data = {k: plan_to_dict(v) for k, v in STATE["plans"].items()}
        PLANS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def load_archive():
    try:
        raw = json.loads(PLANS_FILE.read_text(encoding="utf-8"))
        STATE["plans"] = {k: p for k, p in
                          ((k, plan_from_dict(v)) for k, v in raw.items()) if p}
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("не прочитал архив планчиков")


def archive_plan(p):
    """Каждый планчик, который бот принимал/видел, попадает в архив."""
    if not p or not p.get("date"):
        return
    STATE["plans"][p["date"].isoformat()] = p
    save_archive()


def plan_ok_for_today(p):
    """Сверка с датой: планчик должен быть сегодняшним или ближайшим будущим."""
    today = dt.date.today()
    if p["date"] < today:
        log.info("планчик на %s уже в прошлом — активным не ставлю",
                 p["date"].strftime("%d.%m"))
        return False
    if (p["date"] - today).days > 10:
        log.info("планчик на %s слишком далеко впереди — активным не ставлю",
                 p["date"].strftime("%d.%m"))
        return False
    return True


def accept_plan(p):
    """План заменяет текущий, прежний остаётся в памяти (prev_plan)."""
    old = STATE.get("plan")
    if old is not None and old is not p:
        STATE["prev_plan"] = old
    STATE["plan"] = p
    save_plan()


def offset_for(ev):
    return 5 if ev["kind"] == "five" else 30


def base_time(ev):
    g = ev.get("gather")
    return g["time"] if g else ev["when"]


def sec_name(s):
    """профильная/олимпиадная → «профильная смена»; ИКТ/Инф — как есть."""
    if not s:
        return ""
    return "%s смена" % s if s in ("профильная", "олимпиадная") else s


def render(ev, off=5, start=False):
    when = base_time(ev)
    t = when.strftime("%H:%M")
    sec = " (%s)" % sec_name(ev["section"]) if (ev["kind"] == "school" and ev.get("section")) else ""
    if start:
        msg = "Началось: %s (%s)" % (ev["label"], t)
    else:
        msg = "Через %d мин — %s%s (%s)" % (off, ev["label"], sec, t)
    if not start and ev["kind"] == "school" and ev.get("gather"):
        msg += "\n\nСбор: %s" % ev["gather"]["text"]
    return msg


async def scan_gather(now):
    p = STATE["plan"]
    if not p:
        return
    window = dt.timedelta(hours=CFG.get("gather_window_hours", 3))
    # выбросить прежние чужие сборы (например, вместо нашей смены — олимпиадников)
    for e in p["events"]:
        g = e.get("gather")
        if g and "проектн" not in (g.get("text") or "").lower():
            log.info("снял непригодный сбор для %r", e["label"])
            e.pop("gather", None)
    pending = [e for e in p["events"]
               if e["kind"] == "school" and not e.get("gather")
               and now - dt.timedelta(minutes=30) < e["when"] < now + window]
    if not pending:
        return
    try:
        msgs = await ub.get_messages(CHANNEL, limit=CFG.get("gather_scan_limit", 60))
    except Exception:
        return
    for e in pending:
        kw = "школ" if "школ" in e["label"].lower() else "улк"
        day_start = dt.datetime.combine(e["when"].date(), dt.time(0, 0))
        for m in msgs:
            t = m.message or ""
            tlow = t.lower()
            if "проектн" not in tlow:    # чужая смена (олимпиадники/учебная) — не берём
                continue
            if kw not in tlow:
                continue
            if m.date and m.date.astimezone().replace(tzinfo=None) < day_start:
                continue          # только сегодняшние посты, не вчерашние
            tm = planmod.gather_time(t)
            if not tm:
                continue
            cand = dt.datetime.combine(e["when"].date(), dt.time(*tm))
            if abs((cand - e["when"]).total_seconds()) > 2 * 3600:
                continue
            e["gather"] = {"time": cand, "text": t.strip()[:200]}
            log.info("точный сбор для %r: %s", e["label"], cand.strftime("%H:%M"))
            break


async def scheduler():
    while True:
        try:
            now = dt.datetime.now()
            p = STATE["plan"]
            if p:
                for e in p["events"]:
                    if not e["kind"]:
                        continue
                    base = base_time(e)
                    # напоминания заранее: школа за 30 мин, все события за 5 мин
                    offs = [30, 5] if e["kind"] == "school" else [5]
                    for off in offs:
                        okey = "%s|%s|%d" % (base.isoformat(), e["label"], off)
                        if okey in STATE["sent"]:
                            continue
                        rem = base - dt.timedelta(minutes=off)
                        if now < rem or now > base:
                            continue
                        log.info("напоминание (%d мин): %s", off, e["label"])
                        if await send_pair(render(e, off=off)):
                            STATE["sent"].add(okey)
                            save_sent()
                        else:
                            log.warning("напоминание не ушло (сеть) — повторю позже")
                    # старт события — сразу после наступления
                    skey = "%s|%s|start" % (base.isoformat(), e["label"])
                    if (skey not in STATE["sent"]
                            and 0 <= (now - base).total_seconds() < 300):
                        log.info("старт: %s", e["label"])
                        if await send_pair(render(e, start=True)):
                            STATE["sent"].add(skey)
                            save_sent()
                        else:
                            log.warning("старт не ушёл (сеть) — повторю позже")
            await scan_gather(now)
        except Exception:
            log.exception("ошибка планировщика")
        await asyncio.sleep(20)


async def poll_channel():
    """Страховка: lив-ивенты канала иногда не приходят часами —
    раз в 90 сек опрашиваем Ноябрики руками и прогоняем через обработчик."""
    while True:
        try:
            msgs = await ub.get_messages(CHANNEL, limit=8)
            now_utc = dt.datetime.now(dt.timezone.utc)
            for m in reversed(msgs or []):
                if m and m.message is not None:
                    age = (now_utc - m.date).total_seconds()
                    if 0 <= age < 600:          # только свежие (10 мин), seen режет дубли
                        await process_channel_message(m)
        except Exception:
            log.exception("ошибка опроса канала")
        await asyncio.sleep(90)


async def poll_group():
    """Страховка приёма планчика: жив-ивенты группы иногда провисают —
    раз в 2 мин опрашиваем группы руками (свежие 30 мин, дедуп по id поста)."""
    while True:
        try:
            now_utc = dt.datetime.now(dt.timezone.utc)
            for gid in GROUPS:
                msgs = await ub.get_messages(gid, limit=8)
                for m in reversed(msgs or []):
                    if not m or not m.message or getattr(m.sender, "bot", False):
                        continue
                    age = (now_utc - m.date).total_seconds()
                    if 0 <= age < 1800:
                        maybe_accept_plan(m.message, m.date.astimezone(),
                                          False, mid=m.id)
        except Exception:
            log.exception("ошибка опроса группы")
        await asyncio.sleep(120)


async def plan_watchdog():
    """Если планчик не прогрузился (сеть лежала при старте) — пробуем раз в 5 минут."""
    while True:
        await asyncio.sleep(300)
        p = STATE["plan"]
        if p and p["date"] >= dt.date.today():
            continue
        try:
            await load_plan()
        except Exception:
            log.exception("планчик: повторная загрузка не удалась")


def maybe_accept_plan(txt, posted_at, sender_is_bot=False, mid=0):
    """Похоже ли сообщение на планчик; да — принимаем, если дата новее. → bool."""
    if not txt or txt.startswith("Ноябрики ·") or sender_is_bot:
        return False
    lines = txt.splitlines()
    first = lines[0].strip() if lines else ""
    timed = sum(1 for ln in lines
                if planmod.LINE_RE.match(ln) or planmod.RANGE_RE.match(ln))
    if not planmod.has_explicit_date(txt) or timed < 5:
        return False
    try:
        p = planmod.parse_plan(txt, posted_at, CFG)
    except Exception:
        return False
    if len(p["events"]) < 5:
        return False
    old = STATE["plan"]
    if p["date"] is None or not plan_ok_for_today(p):
        archive_plan(p)
        return False
    # тот же пост уже принят (поллер ходит раз в 2 мин) — не переезжаем
    if old and p["date"] == old["date"] and mid and mid == old.get("_src"):
        archive_plan(p)
        return False
    p["_src"] = mid
    archive_plan(p)
    accept_plan(p)
    log.info("планчик на %s принят: %d событий", p["date"].strftime("%d.%m"), len(p["events"]))
    return True


SPAM = {}   # (uid, bucket) -> unix-отметки вызовов


def spam_blocked(ev, bucket, limit=3, window=60):
    """Спамблок для команд в группах: не более limit вызовов от одного человека
    за window секунд. Плюс общий потолок 15 команд в минуту на человека."""
    if not ev.is_group and not ev.is_channel:
        return False                     # в ЛС не ограничиваем
    uid = ev.sender_id or 0
    now = dt.datetime.now().timestamp()
    g = [t for t in SPAM.get((uid, "*"), []) if now - t < 60]
    if len(g) >= 15:
        SPAM[(uid, "*")] = g
        log.info("спам: %s заблокирован (%s, общий потолок)", uid, bucket)
        return True
    lst = [t for t in SPAM.get((uid, bucket), []) if now - t < window]
    if len(lst) >= limit:
        SPAM[(uid, bucket)] = lst
        g.append(now)
        SPAM[(uid, "*")] = g
        log.info("спам: %s заблокирован (%s, %d/%d за %dс)", uid, bucket, len(lst), limit, window)
        return True
    lst.append(now)
    g.append(now)
    SPAM[(uid, bucket)] = lst
    SPAM[(uid, "*")] = g
    return False


# ───────────────────────── команды бота ─────────────────────────

@bot.on(events.NewMessage(chats=GROUP))
async def plan_watch(ev):
    """Новый планчик в ИиКТ live — сразу загружаем."""
    maybe_accept_plan(ev.raw_text or "",
                      ev.message.date.astimezone(),
                      bool(getattr(ev.sender, "bot", False)),
                      mid=ev.message.id)


@ub.on(events.NewMessage(chats=GROUPS))
async def on_plan(ev):
    t = ev.raw_text or ""
    if not re.search(r"планчик\s+на\s+\d{1,2}\.\d{1,2}", t, re.I):
        return
    p = planmod.parse_plan(t, ev.message.date.astimezone(), CFG)
    if p["date"] is None or not p.get("events") or not plan_ok_for_today(p):
        archive_plan(p)
        return
    p["_src"] = ev.message.id
    archive_plan(p)
    accept_plan(p)
    log.info("планчик на %s (юзербот): %d событий", p["date"].strftime("%d.%m"), len(p["events"]))


@bot.on(events.NewMessage(pattern=r"^/start(@\w+)?(\s|$)"))
async def cmd_start(ev):
    if ev.is_private:
        # deep-link кнопки «Пройти сир и мэс»: /start sir — сразу к заполнению
        payload = (ev.raw_text or "").strip().split()
        if len(payload) > 1 and payload[1].lower() == "sir":
            await sir_launch(ev.sender_id, ev.reply)
            return
        await ev.reply("Привет! Я слежу за Ноябриками и ПЛАНЧИКом.\n"
                       "Пересылаю важное в ЛС и в ИиКТ, напоминаю о событиях.\n\n"
                       "/plan — план на день\n/next — ближайшее напоминание\n"
                       "/dont-notify — выключить напоминания (в этом чате)\n"
                       "/notify — включить напоминания обратно\n"
                       "/sir — заполнить сир и мэс\n"
                       "/nosir — отключить сир и мэс (только эмодзи)\n"
                       "/emoji — выбрать эмодзи для упоминаний\n"
                       "/ping <юз> <n> — n сообщений с одним юзо\n"
                       "/vonyat — проверка: пинг и жив ли юзербот")


@bot.on(events.NewMessage(pattern=r"^/(notify|dontnotify|dont-notify|dont_notify)(@\w+)?(\s|$)"))
async def cmd_ctrl_notify(ev):
    if spam_blocked(ev, "notify"): return
    """Уведомления о событиях: /dont-notify выключить в чате, /notify включить."""
    if not ev.is_private and not await _admin_only(ev):
        await ev.reply("Команда только для админов.")
        return
    body = (ev.raw_text or "").split()[0].split("@")[0].lower()
    if body in ("/dont-notify", "/dont_notify", "/dontnotify"):
        STATE["muted"].add(ev.chat_id)
        save_muted()
        log.info("уведомления выключены в %s", ev.chat_id)
        await ev.reply("Напоминания о событиях выключены в этом чате. Вернуть: /notify")
    else:
        STATE["muted"].discard(ev.chat_id)
        save_muted()
        log.info("уведомления включены в %s", ev.chat_id)
        await ev.reply("Напоминания о событиях включены обратно.")


@bot.on(events.NewMessage(pattern=r"^/(vonyat|вонять)(@\w+)?(\s|$)"))
async def cmd_vonyat(ev):
    if spam_blocked(ev, "vonyat"): return
    lines = ["ИКТ тут", ""]

    async def ping(client):
        t0 = time.monotonic()
        try:
            await client(functions.help.GetConfigRequest())
            return int((time.monotonic() - t0) * 1000)
        except Exception:
            return None

    p_bot = await ping(bot)
    lines.append("Telegram (бот): %s" % ("%d мс" % p_bot if p_bot is not None else "нет связи"))

    connected = ub.is_connected()
    authorized = connected and await ub.is_user_authorized()
    p_ub = await ping(ub) if authorized else None
    if authorized:
        status = "жив, читает Ноябрики" if p_ub is not None else "подключён, но не отвечает"
    else:
        status = "не авторизован" if connected else "отключён"
    lines.append("Юзербот: %s%s" % (status, (" (%d мс)" % p_ub) if p_ub is not None else ""))

    await ev.reply("\n".join(lines))


async def _admin_only(ev):
    if ev.sender_id == OWNER:
        return True
    try:
        return (await bot.get_permissions(ev.chat_id, ev.sender_id)).is_admin
    except Exception:
        return False


async def _wash_target(ev, body):
    """→ (target_id, entity, err). Цель — реплай на сообщение или @юзернейм."""
    if ev.reply_to_msg_id:
        rep = await ev.get_reply_message()
        if rep and rep.sender:
            if getattr(rep.sender, "bot", False):
                return None, None, "Ботов не трогаю."
            return rep.sender_id, rep.sender, None
    m = re.search(r"@([A-Za-z0-9_]{4,})", body)
    if m:
        try:
            ent = await bot.get_entity("@" + m.group(1))
        except Exception:
            return None, None, "Не нашёл @%s." % m.group(1)
        if getattr(ent, "bot", False):
            return None, None, "Ботов не трогаю."
        return ent.id, ent, None
    return None, None, None


@bot.on(events.NewMessage(pattern=r"(?i)^помыть(\s|$)"))
async def cmd_wash(ev):
    if spam_blocked(ev, "pomyt", 2, 60): return
    """помыть [7 д|3 ч|15 м] [@цель] + причина отдельной строкой. Цель — реплай или @юзер."""
    if not (ev.is_group or ev.is_channel):
        return
    if not await _admin_only(ev):
        await ev.reply("Команда только для админов.")
        return

    lines = (ev.raw_text or "").splitlines()
    body = lines[0].strip()[len("помыть"):].strip()
    reason = "\n".join(x.strip() for x in lines[1:] if x.strip())

    target, ent, err = await _wash_target(ev, body)
    if err:
        await ev.reply(err)
        return
    if target is None:
        await ev.reply("Кого мыть? Реплай на сообщение или @юзернейм. Срок: 7 д / 3 ч / 15 м.")
        return
    if target == ev.sender_id:
        await ev.reply("Себя не мою.")
        return
    try:
        if (await bot.get_permissions(ev.chat_id, target)).is_admin:
            await ev.reply("Админа не мою.")
            return
    except Exception:
        pass

    mult = {"д": 86400, "дн": 86400, "дней": 86400,
            "ч": 3600, "час": 3600, "часов": 3600,
            "м": 60, "мин": 60, "минут": 60, "минуты": 60}
    dur = None
    dm = re.search(r"(\d+)\s*([а-яa-z]+)", body, re.I)
    if dm:
        u = dm.group(2).lower()
        if u not in mult:
            await ev.reply("Не понял срок %r. Форматы: 7 д / 3 ч / 15 м." % dm.group(0))
            return
        dur = int(dm.group(1)) * mult[u]

    ts = int(time.time()) + dur if dur else None
    try:
        ent = ent or await bot.get_entity(target)
        await bot(functions.channels.EditBannedRequest(
            ev.chat_id, ent,
            types.ChatBannedRights(until_date=ts, view_messages=True)))
    except Exception as e:
        await ev.reply("Не вышло: %s (боту нужны права админа с баном)" % type(e).__name__)
        return

    who = ("@" + ent.username) if getattr(ent, "username", None) else (getattr(ent, "first_name", None) or str(target))
    out = "Помыт: %s" % who
    out += " до %s" % dt.datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else " навсегда"
    if reason:
        out += "\nПричина: %s" % reason
    log.info("помыт %s в %s (до %s, причина: %s)", target, ev.chat_id, ts, reason or "-")
    await ev.reply(out)


@bot.on(events.NewMessage(pattern=r"(?i)^разбан(\s|$)"))
async def cmd_unwash(ev):
    if spam_blocked(ev, "razban", 2, 60): return
    if not (ev.is_group or ev.is_channel):
        return
    if not await _admin_only(ev):
        await ev.reply("Команда только для админов.")
        return
    body = (ev.raw_text or "").splitlines()[0].strip()[len("разбан"):].strip()
    target, ent, err = await _wash_target(ev, body)
    if err:
        await ev.reply(err)
        return
    if target is None:
        await ev.reply("Кого разбанить? Реплай на сообщение или @юзернейм.")
        return
    try:
        ent = ent or await bot.get_entity(target)
        await bot(functions.channels.EditBannedRequest(
            ev.chat_id, ent, types.ChatBannedRights(until_date=None)))
    except Exception as e:
        await ev.reply("Не вышло: %s (нужны права админа с баном)" % type(e).__name__)
        return
    who = ("@" + ent.username) if getattr(ent, "username", None) else (getattr(ent, "first_name", None) or str(target))
    log.info("разбан %s в %s", target, ev.chat_id)
    await ev.reply("Разбанен: %s" % who)


def render_plan_text(p):
    lines = ["Планчик на %s" % p["date"].strftime("%d.%m")]
    last_sec = "INIT"
    for e in p["events"]:
        sec = e.get("section")
        if sec != last_sec:
            lines.append("")
            if sec:
                lines.append("%s:" % sec_name(sec))
            last_sec = sec
        add = ""
        if e.get("gather"):
            add = "  (сбор %s)" % e["gather"]["time"].strftime("%H:%M")
        lines.append("%s — %s%s" % (e["when"].strftime("%H:%M"), e["label"], add))
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines)


@bot.on(events.NewMessage(pattern=r"^/plan(@\w+)?(?:\s+(\d{1,2}\.\d{1,2}))?(\s|$)"))
async def cmd_plan(ev):
    if spam_blocked(ev, "plan"): return
    arg = ev.pattern_match.group(2)
    if arg:
        dd, mm = (int(x) for x in arg.split("."))
        if not (1 <= mm <= 12 and 1 <= dd <= 31):
            await ev.reply("Неверная дата: %s (жду ДД.ММ, например /plan 07.11)" % arg)
            return
        today = dt.date.today()
        date = dt.date(today.year, mm, dd)
        if (date - today).days < -180:            # «01.01» после декабря — следующий год
            date = dt.date(today.year + 1, mm, dd)
        p = STATE["plan"] if (STATE["plan"] and STATE["plan"]["date"] == date) \
            else STATE["plans"].get(date.isoformat())
        if not p:
            known = [d for d in sorted(STATE["plans"])][-5:]
            if known:
                await ev.reply("Планчик на %s не найден.\nВ архиве есть: %s" % (
                    date.strftime("%d.%m"),
                    ", ".join("%s.%s" % (d[8:10], d[5:7]) for d in known)))
            else:
                await ev.reply("Планчик на %s не найден, архив планчиков пуст." %
                               date.strftime("%d.%m"))
            return
        await ev.reply(render_plan_text(p))
        return
    p = STATE["plan"]
    if not p or not p.get("events"):
        await load_plan()
        p = STATE["plan"]
    if not p or not p.get("events"):
        await ev.reply("Планчик не найден в ИиКТ.")
        return
    await ev.reply(render_plan_text(p))


@bot.on(events.NewMessage(pattern=r"^/all(@\w+)?(\s|$)"))
async def cmd_all(ev):
    if spam_blocked(ev, "all"): return
    if not (ev.is_group or ev.is_channel):
        await ev.reply("Эта команда работает в группе.")
        return
    items, seen = [], set()
    try:
        async for u in ub.iter_participants(ev.chat_id):
            if getattr(u, "bot", False) or getattr(u, "deleted", False):
                continue
            prof = STATE["users"].get(str(u.id))
            if prof and prof.get("emoji"):          # зарегистрированный — эмодзи-юз
                mention = sir_mention(u.id, getattr(u, "username", None),
                                      getattr(u, "first_name", ""))
                kind = "emoji"
            elif getattr(u, "username", None):
                mention, kind = "@" + u.username, "user"
            else:
                continue                             # без юза и без регистрации не упомянуть
            if u.id in seen:
                continue
            seen.add(u.id)
            items.append((mention, kind))
    except Exception as e:
        await ev.reply("Не могу получить участников: %s" % type(e).__name__)
        return

    if not items:
        await ev.reply("В чате нет участников с юзернеймами.")
        return

    # эмодзи-юзы — по 5 в ряд через пробел, обычные @юзы — по одному на строке
    lines, row = [], []
    for mention, kind in items:
        if kind == "emoji":
            row.append(mention)
            if len(row) == 5:
                lines.append(" ".join(row))
                row = []
        else:
            if row:
                lines.append(" ".join(row))
                row = []
            lines.append(mention)
    if row:
        lines.append(" ".join(row))

    chunks, cur, curlen = [], "", 0
    for ln in lines:
        if curlen + len(ln) + 1 > 3500:
            chunks.append(cur)
            cur, curlen = "", 0
        cur += ("\n" if cur else "") + ln
        curlen += len(ln) + 1
    chunks.append(cur)

    for c in chunks:
        await ev.reply(c, parse_mode="html")


@bot.on(events.NewMessage(pattern=r"^/next(@\w+)?(\s|$)"))
async def cmd_next(ev):
    if spam_blocked(ev, "next"): return
    p = STATE["plan"]
    now = dt.datetime.now()
    if not p:
        await ev.reply("Планчик пока не найден.")
        return
    upcoming = [e for e in p["events"] if e["kind"] and base_time(e) > now]
    if not upcoming:
        await ev.reply("На сегодня напоминаний больше нет.")
        return
    e = upcoming[0]
    await ev.reply("Следующее: %s (в %s)" % (
        e["label"], base_time(e).strftime("%H:%M")))


@bot.on(events.NewMessage(pattern=r"^/ping(@\w+)?(\s|$)"))
async def cmd_ping(ev):
    if spam_blocked(ev, "ping", 1, 30): return
    """/ping <юз> <n> — бот отправит n сообщений, каждое только из юза.
    На сам /ping не отвечает — просто пишет."""
    m = re.match(r"^/ping(?:@\w+)?\s+@?([A-Za-z0-9_]{3,32})\s+(\d{1,3})(\s|$)",
                 ev.raw_text or "")
    if not m:
        return                       # битая команда — молчим
    username, n = m.group(1), min(int(m.group(2)), 20)
    if n < 1:
        return
    sent = 0
    try:
        for _ in range(n):
            await ev.reply("@" + username)
            sent += 1
    except Exception as e:
        log.warning("ping: %d из %d отправлено, дальше не вышло: %s", sent, n, e)
        return
    log.info("ping: %d сообщений @%s от %s в %s", sent, username, ev.sender_id, ev.chat_id)


# ───────────────────────── main ─────────────────────────

async def main():
    if not acquire_lock():
        return
    try:
        await _run()
    finally:
        release_lock()


async def _run():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    load_sent()
    load_seen()
    load_muted()
    load_users()
    load_sir()

    await bot.start(bot_token=ENV["BOT_TOKEN"])
    await ub.connect()
    if not await ub.is_user_authorized():
        log.error("юзербот (sessions/ikt-bot) не авторизован")
        return

    load_route_model()
    load_saved_plan()          # план переживает рестарт даже без оригинала в чате
    load_archive()             # все планчики, что бот видел, — для /plan <дата>
    await load_plan()
    await catchup()

    if "--test" in sys.argv:
        p = STATE["plan"]
        if p:
            for e in p["events"]:
                print("%s %-8s %-10s %s" % (e["when"].strftime("%d.%m %H:%M"), e["kind"], e["section"] or "-", e["label"]))
        await scan_gather(dt.datetime.now())
        print("dry-run ок")
        return

    log.info("работаю: канал=%s, группа=%s, владелец=%s", CHANNEL, GROUP, OWNER)
    asyncio.create_task(scheduler())
    asyncio.create_task(poll_channel())
    asyncio.create_task(poll_group())
    asyncio.create_task(plan_watchdog())
    asyncio.create_task(sir_loop())
    await asyncio.gather(bot.run_until_disconnected(), ub.run_until_disconnected())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
