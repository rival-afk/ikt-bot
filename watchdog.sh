#!/data/data/com.termux/files/usr/bin/bash
# Если бота сняли (Android убил группу процессов) — поднимаем обратно.
cd "$(dirname "$0")"
./bot.sh start >> .run/watchdog.log 2>&1
