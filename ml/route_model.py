# -*- coding: utf-8 -*-
"""Pure-python инференс route-модели (без torch/numpy) — как в bbot."""
import json
import math
import os
import struct
from array import array

try:
    from .features import features_unique
except ImportError:  # pragma: no cover
    from features import features_unique

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "route_model.bin")


class RouteModel:
    def __init__(self, path: str = DEFAULT_PATH):
        with open(path, "rb") as f:
            data = f.read()
        hlen = struct.unpack_from("<i", data, 0)[0]
        self.meta = json.loads(data[4:4 + hlen].decode("utf-8"))
        self.buckets = self.meta["buckets"]
        self.dim = self.meta["dim"]
        self.hidden = self.meta["hidden"]
        self.classes = self.meta["classes"]
        pos = 4 + hlen
        size = lambda n: n * 4  # noqa: E731

        def take(n):
            nonlocal pos
            a = array("f")
            a.frombytes(data[pos:pos + size(n)])
            pos += size(n)
            return a

        self.emb = take(self.buckets * self.dim)
        self.w1 = take(self.hidden * self.dim)
        self.b1 = take(self.hidden)
        self.w2 = take(len(self.classes) * self.hidden)
        self.b2 = take(len(self.classes))
        self.wide = take(self.buckets)

    def _probs(self, text: str):
        ids = features_unique(text, self.buckets)
        dim, hid = self.dim, self.hidden

        v = [0.0] * dim
        if ids:
            for i in ids:
                base = i * dim
                for d in range(dim):
                    v[d] += self.emb[base + d]
            inv = 1.0 / len(ids)
            for d in range(dim):
                v[d] *= inv

        h = [0.0] * hid
        for j in range(hid):
            base = j * dim
            s = self.b1[j]
            for d in range(dim):
                s += self.w1[base + d] * v[d]
            h[j] = s if s > 0 else 0.0  # relu

        n_cls = len(self.classes)
        logits = []
        for c in range(n_cls):
            base = c * hid
            s = self.b2[c]
            for j in range(hid):
                s += self.w2[base + j] * h[j]
            logits.append(s)

        wide = 0.0
        if ids:
            for i in ids:
                wide += self.wide[i]
            wide /= len(ids)
        logits = [x + wide for x in logits]

        m = max(logits)
        exps = [math.exp(x - m) for x in logits]
        s = sum(exps)
        return [e / s for e in exps]

    def predict(self, text: str):
        probs = self._probs(text)
        idx = max(range(len(probs)), key=lambda c: probs[c])
        return self.classes[idx], {self.classes[c]: round(probs[c], 3) for c in range(len(probs))}


if __name__ == "__main__":
    import sys
    rm = RouteModel()
    for arg in sys.argv[1:]:
        label, probs = rm.predict(arg)
        print(label, probs, "|", arg[:60])
