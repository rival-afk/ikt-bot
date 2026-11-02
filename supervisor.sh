#!/data/data/com.termux/files/usr/bin/bash
# supervisor.sh — держит main.py живым: упал → перезапуск через 5 сек.
cd "$(dirname "$0")"
echo "[supervisor] старт $(date '+%d.%m %H:%M:%S')"
pkill -9 -f "[p]ython main.py" 2>/dev/null || true   # осиротевшие инстансы
while true; do
  echo "[supervisor] запуск main.py $(date '+%d.%m %H:%M:%S')"
  python main.py >> bot.log 2>&1
  code=$?
  echo "[supervisor] main.py вышел (код $code), перезапуск через 5 сек — $(date '+%d.%m %H:%M:%S')"
  sleep 5
done
