#!/usr/bin/env python3
"""Калибровка весов сопоставления с честной проверкой.

Отличия от первой версии (fit.py):
- блоки кросс-валидации режутся по группе дублей, а не по строкам: у ClickHouse
  и vCluster по четыре вакансии с дословно одинаковым текстом, и если они
  разъедутся между обучением и проверкой, полнота окажется завышенной;
- десять разных разбиений вместо одного: на 19 положительных примерах одно
  разбиение ничего не говорит, нужен разброс;
- считается на обоих наборах паспортов, слабой модели и сильной: в проде
  паспорт будет строить дешёвая, и калибровка должна работать на ней.
"""
import json, glob, math, random, statistics, sys

src = open("/tmp/pass/match.py").read().replace("\nmain()", "")
ns = {}; sys.argv = ["match.py"]
exec(compile(src, "match", "exec"), ns)
norm, ADJACENT, FAMILY, SENIOR = ns["norm"], ns["ADJACENT"], ns["FAMILY"], ns["SENIOR"]
D, match, ALLOWED = ns["D"], ns["match"], ns["ALLOWED"]
NAMES = ["покрытие стека", "роль", "география", "домен", "грейд"]


def feats(profile, fam, psp):
    stack = {norm(s) for s in (profile.get("stack") or [])}
    must = [norm(m) for m in (psp.get("must") or [])]
    hit = [m for m in must if m in stack or any(m in s or s in m for s in stack if len(s) > 3)]
    cov = len(hit)/min(len(must), 6) if must else 0.6
    nice = [norm(m) for m in (psp.get("nice") or [])]
    nhit = sum(1 for m in nice if m in stack)
    fam_v = (psp.get("role_family") or "other").lower()
    role = 1.0 if fam_v == fam else (0.6 if fam_v in ADJACENT.get(fam, set()) else 0.2)
    hire = {str(x).upper() for x in (psp.get("hire") or [])}
    geo = 1.0 if (hire & ALLOWED) else (0.6 if not hire else 0.2)
    doms = {str(d).lower() for d in (psp.get("domains") or [])}
    pdoms = " ".join(str(d).lower() for d in (profile.get("domains") or []))
    dom = 1.0 if any(d in pdoms or any(w in pdoms for w in d.split("-")) for d in doms) else 0.0
    lv = {str(x).lower() for x in (psp.get("levels") or [])}
    sen = 1.0 if (lv & SENIOR or not lv) else 0.6
    return [min(1.0, cov + 0.1*nhit/max(1, len(nice))), role, geo, dom, sen]


def build(pattern):
    P = {}
    for f in glob.glob(pattern):
        for p in json.load(open(f)):
            P[p["url"]] = p
    rows, blocked_pos = [], 0
    for lab in json.load(open("/tmp/pass/direct.json")):
        tg = str(lab["tg"]); psp = P.get(lab["url"])
        if not psp:
            continue
        hard, _ = match(D["profiles"][tg], FAMILY[tg], psp)
        y = 1 if lab["pct"] >= 70 else 0
        if hard == 0:                      # решено жёстким блокером, в обучение не идёт
            blocked_pos += y
            continue
        rows.append((feats(D["profiles"][tg], FAMILY[tg], psp), y,
                     f'{tg}|{lab["dedup"]}'))
    return rows, blocked_pos


def train(data, steps=4000, lr=0.5, l2=0.01):
    w = [0.0]*(len(data[0][0])+1)
    pos = sum(y for _, y in data) or 1
    wpos = (len(data) - pos)/pos
    for _ in range(steps):
        g = [0.0]*len(w)
        for x, y in data:
            z = w[0] + sum(wi*xi for wi, xi in zip(w[1:], x))
            p = 1/(1 + math.exp(-max(-30, min(30, z))))
            k = (wpos if y else 1.0)*(p - y)
            g[0] += k
            for i, xi in enumerate(x):
                g[i+1] += k*xi
        n = len(data)
        w = [wi - lr*(gi/n + l2*wi) for wi, gi in zip(w, g)]
    return w


def prob(w, x):
    z = w[0] + sum(wi*xi for wi, xi in zip(w[1:], x))
    return 1/(1 + math.exp(-max(-30, min(30, z))))


def cv(rows, seeds=10, k=5):
    """Возвращает {порог: (список полнот, список объёмов)}. Блоки — по группам."""
    groups = sorted({g for _, _, g in rows})
    tot = sum(y for _, y, _ in rows)
    acc = {t: ([], []) for t in (0.6, 0.5, 0.4, 0.3, 0.2)}
    for seed in range(seeds):
        rnd = random.Random(seed)
        gs = groups[:]; rnd.shuffle(gs)
        folds = [set(gs[i::k]) for i in range(k)]
        oof = {}
        for f in folds:
            tr = [(x, y) for x, y, g in rows if g not in f]
            if not tr or not any(y for _, y in tr):
                continue
            w = train(tr)
            for i, (x, y, g) in enumerate(rows):
                if g in f:
                    oof[i] = prob(w, x)
        for t in acc:
            keep = [i for i in oof if oof[i] >= t]
            acc[t][0].append(sum(rows[i][1] for i in keep)/tot if tot else 0)
            acc[t][1].append(len(keep)/len(oof) if oof else 0)
    return acc, tot


for name, pat in (("слабый", "/tmp/pass/out_*.json"), ("сильный", "/tmp/pass/s_out_*.json")):
    rows, blocked_pos = build(pat)
    acc, tot = cv(rows)
    print(f"\n=== извлекатель {name}: строк {len(rows)}, положительных {tot}, "
          f"ещё {blocked_pos} сняты жёсткими блокерами ===")
    print("  порог   объём к модели      полнота (медиана и разброс по 10 разбиениям)")
    for t in sorted(acc, reverse=True):
        rec, vol = acc[t]
        print(f"  p>={t:.1f}   {statistics.median(vol):5.0%}            "
              f"{statistics.median(rec):5.0%}   [{min(rec):.0%}–{max(rec):.0%}]")
    w = train([(x, y) for x, y, _ in rows])
    print("  веса на всех данных: сдвиг %+.2f, " % w[0]
          + ", ".join(f"{n} {wi:+.2f}" for n, wi in zip(NAMES, w[1:])))
