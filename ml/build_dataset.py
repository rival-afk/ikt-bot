#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка датасета для route-модели.

Слабая разметка — текущими правилами (route() из main.py),
поверх — ручные метки из ml/labels_manual.json  {"<msg_id>": "both|group|none"}.
"""
import json
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from main import route  # noqa: E402  (правила = источник слабой разметки)

MANUAL = HERE / "labels_manual.json"
HISTORY = HERE / "history.jsonl"
CLASSES = ["none", "group", "both"]


def weak_label(text):
    r = route(text)
    if r is None:
        return "none"
    if r["group"] and r["extra"]:
        return "both"
    if r["group"]:
        return "group"
    return "none"


def main():
    manual = {}
    if MANUAL.exists():
        manual = json.loads(MANUAL.read_text(encoding="utf-8"))

    rows = []
    if HISTORY.exists():
        for line in HISTORY.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            label = manual.get(str(r["id"]), weak_label(r["text"]))
            rows.append({"id": r["id"], "text": r["text"], "label": label})

    train, test = [], []
    for r in rows:
        key = "%s:%s" % (r["id"], r["text"][:40].lower())
        bucket = zlib.crc32(("route-v1:" + key).encode()) % 100
        (test if bucket < 15 else train).append(r)

    with open(HERE / "train.jsonl", "w", encoding="utf-8") as f:
        for r in train:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(HERE / "test.jsonl", "w", encoding="utf-8") as f:
        for r in test:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter
    print("всего:", len(rows), "| train:", len(train), "| test:", len(test))
    print("классы (всего):", dict(Counter(r["label"] for r in rows)))


if __name__ == "__main__":
    main()
