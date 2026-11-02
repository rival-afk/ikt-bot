#!/data/data/com.termux/files/usr/bin/bash
# bot.sh — управление ботом techpark: start | stop | restart | status | log
set -uo pipefail
cd "$(dirname "$0")"

RUN="$PWD/.run"
mkdir -p "$RUN"
SUP_PID="$RUN/supervisor.pid"

alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1" 2>/dev/null)" 2>/dev/null; }

start() {
  if alive "$SUP_PID"; then
    echo "уже работает (supervisor $(cat "$SUP_PID"))"
    return
  fi
  pkill -9 -f "[p]ython main.py" 2>/dev/null || true
  command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock 2>/dev/null || true
  nohup ./supervisor.sh >> bot.log 2>&1 &
  echo $! > "$SUP_PID"
  sleep 2
  if alive "$SUP_PID"; then
    echo "запущено (supervisor $(cat "$SUP_PID"))"
  else
    echo "не запустилось — смотри bot.log"
  fi
}

stop() {
  if alive "$SUP_PID"; then
    kill "$(cat "$SUP_PID")" 2>/dev/null || true
    echo "supervisor остановлен"
  fi
  rm -f "$SUP_PID"
  pkill -9 -f "[p]ython main.py" 2>/dev/null || true
  echo "бот остановлен"
}

status() {
  if alive "$SUP_PID"; then
    echo "работает (supervisor $(cat "$SUP_PID"))"
  else
    echo "не работает"
  fi
  pgrep -f "python main.py" >/dev/null 2>&1 && echo "main.py: жив" || echo "main.py: нет"
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  log)     tail -n "${2:-30}" bot.log ;;
  *)       echo "использование: bot.sh {start|stop|restart|status|log [N]}" ;;
esac
