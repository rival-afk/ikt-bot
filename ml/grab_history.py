#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обновляет ml/history.jsonl последними постами Ноябриков.

Запускать при ОСТАНОВЛЕННОМ боте (сессия юзербота занята):
    ./bot.sh stop
    python ml/grab_history.py
    python ml/build_dataset.py
    python ml/train_model.py
    ./bot.sh start
"""
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import main  # noqa: E402


async def t():
    await main.ub.connect()
    out = open(HERE / "history.jsonl", "w", encoding="utf-8")
    n = 0
    async for m in main.ub.iter_messages(main.CHANNEL, limit=500):
        if not m or m.action is not None:
            continue
        text = m.message or ""
        if not text.strip():
            continue
        out.write(json.dumps({"id": m.id,
                              "date": m.date.astimezone().strftime("%Y-%m-%d %H:%M"),
                              "text": text}, ensure_ascii=False) + "\n")
        n += 1
    out.close()
    print("собрано:", n)
    await main.ub.disconnect()


asyncio.run(t())
