#!/usr/bin/env python3
"""Теги разной ширины: сколько вакансий внутри и сколько подходящих теряется.

Истина — балл Sonnet 70+ из matches. Паспорта построены дешёвой моделью.
"""
import json, glob, re, collections

D = json.load(open("/data/scaling/data.json"))
P = {}
for f in sorted(glob.glob("/data/scaling/out_*.json")):
    d = json.load(open(f))
    for p in (d.get("items", []) if isinstance(d, dict) else d):
        if isinstance(p, dict) and p.get("url"):
            P[p["url"]] = p

LANGS = ("python", "go", "golang", "java", "kotlin", "scala", "rust", "node",
         "typescript", "javascript", "ruby", "php", "c#", ".net", "sql")
norm = lambda x: re.sub(r"[^a-z0-9+#./ -]", " ", str(x or "").lower()).strip()
SENIOR = {"senior", "lead", "staff", "principal"}
EUOK = {"PT","ES","FR","DE","NL","PL","IT","CZ","SE","DK","NO","FI","CH","AT","BE","IE",
        "GB","UK","GR","RO","BG","EE","LV","LT","HU","HR","SK","SI","RS","CY","MT","LU",
        "EU","EUROPE","EMEA","WORLDWIDE","ANYWHERE","GLOBAL"}
ADJ = {"backend": {"platform", "devops-sre", "architect", "fullstack", "security"},
       "data-engineer": {"analyst-data", "data-scientist", "platform", "ml-engineer"},
       "analyst-business": {"analyst-product", "architect", "pm", "analyst-data"}}
FAMILY = {"profile-data": "data-engineer", "profile-analyst": "analyst-business",
          "profile-backend": "backend"}


def langs_of(p):
    txt = " ".join(norm(x) for x in (p.get("must") or []) + (p.get("nice") or []))
    return {("go" if l == "golang" else l) for l in LANGS if re.search(rf"\b{re.escape(l)}\b", txt)}


def profile_langs(prof):
    txt = " ".join(norm(x) for x in (prof.get("stack") or []))
    return {("go" if l == "golang" else l) for l in LANGS if re.search(rf"\b{re.escape(l)}\b", txt)}


def main():
    for tg, prof in D["profiles"].items():
        fam = FAMILY[tg]
        mine = profile_langs(prof)
        pairs = [(x["url"], x["pct"] >= 70) for x in D["labels"]
                 if str(x["tg"]) == tg and x["url"] in P]
        tot = sum(1 for _, y in pairs if y)
        if not pairs:
            continue
        print(f"\n=== профиль {tg}: {fam}, языки {sorted(mine) or '-'} — "
              f"пар {len(pairs)}, подходящих {tot} ===")
        if not tot:
            print("   подтверждённых совпадений нет, измерять нечего")
            continue
        f_of = lambda u: norm(P[u].get("role_family")) or "other"
        variants = [
            ("роль + смежные роли", lambda u: f_of(u) == fam or f_of(u) in ADJ[fam]),
            ("только своя роль", lambda u: f_of(u) == fam),
            ("роль + любой из моих языков", lambda u: f_of(u) == fam and bool(langs_of(P[u]) & mine)),
            ("роль + один язык (самый частый)", None),
            ("роль + язык + грейд senior+", lambda u: f_of(u) == fam and bool(langs_of(P[u]) & mine)
             and (not {norm(x) for x in (P[u].get("levels") or [])} or
                  bool({norm(x) for x in (P[u].get("levels") or [])} & SENIOR))),
            ("роль + язык + грейд + гео", lambda u: f_of(u) == fam and bool(langs_of(P[u]) & mine)
             and (not {norm(x) for x in (P[u].get("levels") or [])} or
                  bool({norm(x) for x in (P[u].get("levels") or [])} & SENIOR))
             and (not {str(h).upper() for h in (P[u].get("hire") or [])} or
                  bool({str(h).upper() for h in (P[u].get("hire") or [])} & EUOK))),
        ]
        top = collections.Counter(l for u, _ in pairs for l in (langs_of(P[u]) & mine)).most_common(1)
        one = top[0][0] if top else None
        for name, pred in variants:
            if pred is None:
                if not one:
                    continue
                name = f"роль + только {one}"
                pred = lambda u, one=one: f_of(u) == fam and one in langs_of(P[u])
            inside = [(u, y) for u, y in pairs if pred(u)]
            hit = sum(1 for _, y in inside if y)
            print(f"   {name:34s} вакансий {len(inside):3d} ({len(inside)/len(pairs):4.0%}), "
                  f"подходящих {hit}/{tot} ({hit/tot:4.0%})")


main()
