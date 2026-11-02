#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обучение route-модели: 3 класса (none / group / both).

Архитектура как в bbot: hashed char/word n-gram EmbeddingBag (mean) +
wide linear skip + MLP. Экспорт в route_model.bin для pure-python инференса.
"""
import json
import random
import struct
import sys
import time
import zlib
from pathlib import Path

import torch
import torch.nn as nn

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from features import features_unique, BUCKETS, FEATURE_VERSION  # noqa: E402

CLASSES = ["none", "group", "both"]
DIM = 24
HIDDEN = 32
EPOCHS = 80
LR = 3e-3
BATCH = 32
DROPOUT = 0.15
SEED = 13
VAL_PCT = 15

torch.manual_seed(SEED)
random.seed(SEED)


class Net(nn.Module):
    def __init__(self, n_cls):
        super().__init__()
        self.emb = nn.EmbeddingBag(BUCKETS, DIM, mode="mean")
        self.wide = nn.EmbeddingBag(BUCKETS, 1, mode="mean")
        nn.init.zeros_(self.wide.weight)
        self.fc1 = nn.Linear(DIM, HIDDEN)
        self.drop = nn.Dropout(DROPOUT)
        self.fc2 = nn.Linear(HIDDEN, n_cls)

    def forward(self, ids, offsets):
        v = self.emb(ids, offsets)
        h = torch.relu(self.fc1(v))
        h = self.drop(h)
        logits = self.fc2(h)
        return logits + self.wide(ids, offsets)


def load_rows(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            ids = features_unique(r["text"])
            if ids:
                rows.append((ids, CLASSES.index(r["label"]), r["text"]))
    return rows


def batches(rows, bs, shuffle):
    idx = list(range(len(rows)))
    if shuffle:
        random.shuffle(idx)
    for i in range(0, len(idx), bs):
        chunk = [rows[j] for j in idx[i:i + bs]]
        flat, offsets, labels = [], [], []
        pos = 0
        for ids, y, _ in chunk:
            offsets.append(pos)
            flat.extend(ids)
            labels.append(y)
            pos += len(ids)
        yield (torch.tensor(flat, dtype=torch.long),
               torch.tensor(offsets, dtype=torch.long),
               torch.tensor(labels, dtype=torch.long))


@torch.no_grad()
def predict(model, rows):
    model.eval()
    out = []
    for ids, _, _ in rows:
        x = torch.tensor(ids, dtype=torch.long)
        off = torch.tensor([0], dtype=torch.long)
        out.append(model(x, off))
    return out


def macro_f1(y_true, y_pred):
    f1s = []
    for c in range(len(CLASSES)):
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == c and p == c)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != c and p == c)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == c and p != c)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(f1s) / len(f1s)


def confusion(y_true, y_pred):
    n = len(CLASSES)
    m = [[0] * n for _ in range(n)]
    for t, p in zip(y_true, y_pred):
        m[t][p] += 1
    return m


def main():
    train = load_rows(HERE / "train.jsonl")
    test = load_rows(HERE / "test.jsonl")
    print("train", len(train), "| test", len(test),
          "| классы:", {c: sum(1 for _, y, _ in train + test if CLASSES[y] == c) for c in CLASSES})

    fit = [r for r in train if zlib.crc32(("v:%s" % r[2][:40].lower()).encode()) % 100 >= VAL_PCT]
    val = [r for r in train if zlib.crc32(("v:%s" % r[2][:40].lower()).encode()) % 100 < VAL_PCT]
    if not val:
        fit, val = train, train

    model = Net(len(CLASSES))
    # веса классов: редкие классы важнее
    cnt = [max(1, sum(1 for _, y, _ in fit if y == c)) for c in range(len(CLASSES))]
    w = torch.tensor([sum(cnt) / c for c in cnt], dtype=torch.float32)
    opt = torch.optim.Adam(model.parameters(), lr=LR, weight_decay=1e-5)
    lossfn = nn.CrossEntropyLoss(weight=w)

    best = None
    for epoch in range(1, EPOCHS + 1):
        t0 = time.time()
        model.train()
        total = nb = 0
        for ids, off, y in batches(fit, BATCH, True):
            opt.zero_grad()
            loss = lossfn(model(ids, off), y)
            loss.backward()
            opt.step()
            total += loss.item()
            nb += 1
        y_val = [y for _, y, _ in val]
        preds = [int(p.argmax()) for p in predict(model, val)]
        f1 = macro_f1(y_val, preds)
        print("epoch %2d loss %.4f %4.1fs val macroF1 %.3f" % (epoch, total / max(nb, 1), time.time() - t0, f1))
        if best is None or f1 > best[0]:
            best = (f1, epoch, {k: v.clone() for k, v in model.state_dict().items()})

    f1_best, best_epoch, state = best
    print("лучший epoch %d macroF1 %.3f" % (best_epoch, f1_best))
    model.load_state_dict(state)

    # доучиваем на всём train то же число эпох
    model = Net(len(CLASSES))
    opt = torch.optim.Adam(model.parameters(), lr=LR, weight_decay=1e-5)
    for epoch in range(1, best_epoch + 1):
        for ids, off, y in batches(train, BATCH, True):
            opt.zero_grad()
            lossfn(model(ids, off), y).backward()
            opt.step()

    y_test = [y for _, y, _ in test]
    preds = [int(p.argmax()) for p in predict(model, test)]
    print("test macroF1 %.3f" % macro_f1(y_test, preds))
    m = confusion(y_test, preds)
    print("матрица (строки=истина %s):" % CLASSES)
    for i, row in enumerate(m):
        print("  %-5s %s" % (CLASSES[i], row))
    for (_, y, text), p in zip(test, preds):
        if p != y:
            print("ошибка: %s→%s %r" % (CLASSES[y], CLASSES[p], text.replace("\n", " ")[:90]))

    emb = model.emb.weight.detach().numpy().astype("float32")
    w1 = model.fc1.weight.detach().numpy().astype("float32")
    b1 = model.fc1.bias.detach().numpy().astype("float32")
    w2 = model.fc2.weight.detach().numpy().astype("float32")
    b2 = model.fc2.bias.detach().numpy().astype("float32")
    wide = model.wide.weight.detach().numpy().astype("float32").reshape(-1)

    meta = dict(buckets=BUCKETS, dim=DIM, hidden=HIDDEN, classes=CLASSES,
                feature_version=FEATURE_VERSION, val_macro_f1=f1_best,
                test_macro_f1=macro_f1(y_test, preds),
                n_train=len(train), n_test=len(test),
                algo="wide-hashed-char-word-mlp-3class")
    header = json.dumps(meta).encode("utf-8")
    out = HERE / "route_model.bin"
    with open(out, "wb") as f:
        f.write(struct.pack("<i", len(header)))
        f.write(header)
        f.write(emb.tobytes())
        f.write(w1.tobytes())
        f.write(b1.tobytes())
        f.write(w2.tobytes())
        f.write(b2.tobytes())
        f.write(wide.tobytes())
    print("экспортирован", out, out.stat().st_size, "байт")
    (HERE / "route_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
