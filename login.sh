#!/data/data/com.termux/files/usr/bin/bash
# login.sh — сессии проекта techpark (сессия tgu НЕ затрагивается).
#
#   ./login.sh              вход БОТА по BOT_TOKEN → sessions/bot.session
#   ./login.sh check        проверить бота и доступ к чатам
#   ./login.sh userbot      вход ЮЗЕРБОТА (телефон + код, ловим код сами)
#   ./login.sh userbot '<пароль 2FA>'
#
# Нужны в .env: BOT_TOKEN, API_ID, API_HASH
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "✗ нет .env — создай его (BOT_TOKEN, API_ID, API_HASH)"; exit 1
fi

set -a
source .env 2>/dev/null || true
set +a

: "${API_ID:?в .env нет API_ID}"
: "${API_HASH:?в .env нет API_HASH}"

mkdir -p sessions

CHATS_PY='
CHATS = {"Ноябрики 2026": -1004350388356, "ИиКТ": -1004071852625, "ИКТ без тимлидов": -1003880554750}
'

case "${1:-bot}" in

check)
python - <<PY
import asyncio, os, sys
from telethon import TelegramClient
$CHATS_PY

async def main():
    c = TelegramClient("sessions/bot", int(os.environ["API_ID"]), os.environ["API_HASH"])
    await c.connect()
    if not await c.is_user_authorized():
        print("✗ сессия бота не авторизована — ./login.sh"); return
    me = await c.get_me()
    print(f"✓ бот: {me.first_name} (@{me.username}, id {me.id})")
    for name, cid in CHATS.items():
        try:
            ent = await c.get_entity(cid)
            print(f"✓ {name}: доступ есть — «{getattr(ent, 'title', name)}»")
        except Exception as e:
            print(f"✗ {name}: {type(e).__name__} — добавь бота в этот чат")
    await c.disconnect()

asyncio.run(main())
PY
;;

code)
python - "${2:-}" "${3:-}" <<'PY'
import asyncio, os, sys
from pathlib import Path
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError

code = sys.argv[1] if len(sys.argv) > 1 else ""
pwd  = sys.argv[2] if len(sys.argv) > 2 else ""
PN, HC = Path("sessions/.phone"), Path("sessions/.codehash")

async def main():
    if not code:
        print("нужен код: ./login.sh code 12345 [пароль]"); return
    if not PN.exists() or not HC.exists():
        print("кода не запрашивали — сначала ./login.sh userbot"); return
    cli = TelegramClient("sessions/ikt-bot", int(os.environ["API_ID"]), os.environ["API_HASH"],
                         device_model="ikt-bot", system_version="Termux", app_version="1.0")
    await cli.connect()
    if await cli.is_user_authorized():
        me = await cli.get_me()
        print(f"сессия уже авторизована: {me.first_name} (@{me.username}, id {me.id})")
        await cli.disconnect(); return
    try:
        await cli.sign_in(phone=PN.read_text().strip(), code=code,
                          phone_code_hash=HC.read_text().strip())
    except SessionPasswordNeededError:
        if not pwd:
            print("◐ нужен пароль 2FA: ./login.sh code <код> '<пароль>'")
            await cli.disconnect(); return
        await cli.sign_in(password=pwd)
    except FloodWaitError as e:
        print(f"✗ флуд-лимит, подожди {e.seconds} сек")
        await cli.disconnect(); return
    me = await cli.get_me()
    print(f"✓ юзербот: {me.first_name} (@{me.username}, id {me.id}) | sessions/ikt-bot.session")
    await cli.disconnect()

asyncio.run(main())
PY
;;

userbot)
python - "${2:-}" <<'PY'
import asyncio, os, re, shutil, sys
from pathlib import Path
from telethon import TelegramClient, events
from telethon.tl import types
from telethon.errors import (SessionPasswordNeededError, FloodWaitError,
                             SendCodeUnavailableError)

pwd = sys.argv[1] if len(sys.argv) > 1 else ""
SESSION = Path("sessions/ikt-bot")
PHONE_FILE = Path("sessions/.phone")
TMP = Path("sessions/.tgu_copy")

api_id, api_hash = int(os.environ["API_ID"]), os.environ["API_HASH"]


def phone():
    if PHONE_FILE.exists():
        return PHONE_FILE.read_text().strip()
    p = input("телефон (+375...): ").strip()
    PHONE_FILE.write_text(p + "\n")
    return p


async def main():
    cli = TelegramClient(str(SESSION), api_id, api_hash, device_model="ikt-bot", system_version="Termux", app_version="1.0")
    await cli.connect()
    if await cli.is_user_authorized():
        me = await cli.get_me()
        print(f"сессия уже авторизована: {me.first_name} (@{me.username}, id {me.id})")
        await cli.disconnect(); return

    ph = phone()

    # копия сессии tgu — только для прослушки кода, оригинал не трогаем
    src = Path.home() / ".config" / "tgu" / "session.session"
    got = asyncio.get_running_loop().create_future()
    listener = None
    if src.exists():
        try:
            shutil.copy2(src, TMP.with_suffix(".session"))
            j = src.with_suffix(".session-journal")
            if j.exists():
                shutil.copy2(j, TMP.with_suffix(".session-journal"))
            listener = TelegramClient(str(TMP), api_id, api_hash)
            await listener.connect()
            if not await listener.is_user_authorized():
                listener = None
        except Exception:
            listener = None

    def grab(text):
        m = re.search(r"(?<!\d)(\d{4,6})(?!\d)", text or "")
        if m and not got.done():
            got.set_result(m.group(1))

    if listener:
        @listener.on(events.NewMessage(from_users=777000))
        async def _msg(ev):
            grab(ev.raw_text)
        @listener.on(events.Raw(types.UpdateServiceNotification))
        async def _svc(upd):
            grab(getattr(upd, "message", ""))

    print("— запрашиваю код…")
    try:
        sent = await cli.send_code_request(ph, force_sms=True)
        print("— тип доставки:", type(getattr(sent, "type", None)).__name__)
        Path("sessions/.codehash").write_text(sent.phone_code_hash)
    except FloodWaitError as e:
        print(f"✗ Telegram просит подождать {e.seconds} сек перед новым кодом.")
        await cli.disconnect()
        if listener: await listener.disconnect()
        return
    except SendCodeUnavailableError:
        print("✗ доставка кода сейчас недоступна (все каналы уже использованы).")
        await cli.disconnect()
        if listener: await listener.disconnect()
        return

    print("— ждём код (в приложении Telegram / СМС)…")
    code = ""
    try:
        # ждём пуш-апдейт + параллельно опрашиваем чат Telegram (777000)
        import time as _time
        start_epoch = _time.time()
        while not got.done():
            if _time.time() - start_epoch > 120:
                break
            if listener:
                try:
                    msgs = await listener.get_messages(777000, limit=1)
                    if msgs:
                        m = msgs[0]
                        mt = m.raw_text or ""
                        ts = m.date.timestamp() if m.date else 0
                        if ts >= start_epoch - 5 and "код" in mt.lower():
                            grab(mt)
                except Exception:
                    pass
            await asyncio.sleep(3)
        if got.done():
            code = got.result()
    except Exception:
        pass

    if code:
        print(f"✓ код пойман: {code}")
    else:
        print("… код не поймали автоматически.")

    hashcode = sent.phone_code_hash

    async def signin(c):
        await cli.sign_in(phone=ph, code=c, phone_code_hash=hashcode)

    try:
        if code:
            try:
                await signin(code)
            except SessionPasswordNeededError:
                if not pwd:
                    print("◐ на аккаунте 2FA. Повтори: ./login.sh userbot '<пароль>'")
                    return
                await cli.sign_in(password=pwd)
        else:
            # ручной ввод в терминале (работает только при живом stdin)
            if not sys.stdin.isatty():
                print("✗ код не пойман, а stdin недоступен (запуск через чат).")
                print("  Либо открой настоящий терминал Termux и запусти ./login.sh userbot,")
                print("  либо пришли код в чат — запущу ./login.sh userbot с кодовым режимом.")
                return
            c = input("код из Telegram: ").strip()
            try:
                await signin(c)
            except SessionPasswordNeededError:
                p2 = input("пароль 2FA: ").strip()
                await cli.sign_in(password=p2 if p2 else pwd)

        me = await cli.get_me()
        print(f"✓ юзербот: {me.first_name} (@{me.username}, id {me.id}) | {SESSION}.session")
    finally:
        await cli.disconnect()
        if listener:
            await listener.disconnect()


asyncio.run(main())
PY
;;

*)
python - <<PY
import asyncio, os, sys
from telethon import TelegramClient
$CHATS_PY

async def main():
    c = TelegramClient("sessions/bot", int(os.environ["API_ID"]), os.environ["API_HASH"])
    if not (await c.connect() and await c.is_user_authorized()):
        print("— вход по BOT_TOKEN…")
        await c.start(bot_token=os.environ["BOT_TOKEN"])
    me = await c.get_me()
    print(f"✓ бот: {me.first_name} (@{me.username}, id {me.id}) | sessions/bot.session")
    for name, cid in CHATS.items():
        try:
            await c.get_entity(cid)
            print(f"✓ {name}: доступ есть")
        except Exception:
            print(f"✗ {name}: добавь бота в этот чат")
    await c.disconnect()

asyncio.run(main())
PY
;;
esac
