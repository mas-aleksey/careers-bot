#!/usr/bin/env python3
"""Мульти-теги по роли И по стеку: что даёт объединение, что пересечение."""
import json, glob, re, collections

D = json.load(open("/data/scaling/data.json"))
P = {}
for f in glob.glob("/data/scaling/out_*.json"):
    d = json.load(open(f))
    for p in (d.get("items", []) if isinstance(d, dict) else d):
        if isinstance(p, dict) and p.get("url"):
            P[p["url"]] = p
jobs = {j["url"]: j for j in D["jobs"]}

# Закрытый словарь стековых тегов: синоним -> тег
STACK = {"python": "python", "go": "go", "golang": "go", "java": "java",
         "kotlin": "jvm", "scala": "jvm", "rust": "rust", "node": "node",
         "typescript": "node", "javascript": "node", "ruby": "ruby", "php": "php",
         "c#": "dotnet", ".net": "dotnet",
         "kubernetes": "k8s", "k8s": "k8s", "docker": "k8s", "helm": "k8s",
         "terraform": "iac", "ansible": "iac",
         "aws": "aws", "gcp": "gcp", "google cloud": "gcp", "azure": "azure",
         "postgres": "sql", "postgresql": "sql", "mysql": "sql", "sql": "sql",
         "oracle": "sql", "clickhouse": "olap", "snowflake": "olap",
         "bigquery": "olap", "spark": "bigdata", "airflow": "bigdata",
         "dbt": "bigdata", "kafka": "kafka", "rabbitmq": "kafka",
         "elasticsearch": "search", "mongodb": "nosql", "redis": "nosql",
         "grpc": "grpc", "rest": "rest", "graphql": "rest"}
ROLE_RX = [("backend", r"backend|back-end|software engineer|server|api|microservice"),
           ("platform", r"platform|infrastructure|cloud engineer"),
           ("devops-sre", r"\bsre\b|site reliability|devops"),
           ("data-engineer", r"data engineer|analytics engineer|etl"),
           ("ml-engineer", r"\bml\b|machine learning|ai engineer"),
           ("analyst", r"analyst|analytics")]
norm = lambda x: re.sub(r"[^a-z0-9+#./ -]", " ", str(x or "").lower())


def stack_tags(words):
    txt = " " + " ".join(norm(w) for w in words) + " "
    return {t for k, t in STACK.items() if re.search(rf"[\s,;/(]{re.escape(k)}[\s,;/)]", txt)}


def role_tags(u):
    t = {(P[u].get("role_family") or "other").lower()}
    title = norm(jobs[u]["title"])
    for name, rx in ROLE_RX:
        if re.search(rx, title):
            t.add(name)
    return t


def job_stack(u):
    return stack_tags((P[u].get("must") or []) + (P[u].get("nice") or []))


def run(tg, want_roles):
    prof = D["profiles"][tg]
    mine = stack_tags(prof.get("stack") or [])
    pairs = [(x["url"], x["pct"] >= 70) for x in D["labels"]
             if str(x["tg"]) == tg and x["url"] in P]
    tot = sum(1 for _, y in pairs if y)
    print(f"\n=== профиль {tg}: роли {sorted(want_roles)}, стек-теги {sorted(mine)} ===")
    print(f"    пар {len(pairs)}, подходящих {tot}")
    if not tot:
        return
    def show(name, pred):
        ins = [(u, y) for u, y in pairs if pred(u)]
        hit = sum(1 for _, y in ins if y)
        print(f"   {name:38s} вакансий {len(ins):3d} ({len(ins)/len(pairs):4.0%}), "
              f"подходящих {hit}/{tot} ({hit/tot:4.0%})")
    show("роль ИЛИ стек (любое совпадение)",
         lambda u: bool(role_tags(u) & want_roles) or bool(job_stack(u) & mine))
    show("только роль", lambda u: bool(role_tags(u) & want_roles))
    show("только стек", lambda u: bool(job_stack(u) & mine))
    show("роль И хотя бы один стек-тег",
         lambda u: bool(role_tags(u) & want_roles) and bool(job_stack(u) & mine))
    show("роль И два стек-тега",
         lambda u: bool(role_tags(u) & want_roles) and len(job_stack(u) & mine) >= 2)
    show("роль И три стек-тега",
         lambda u: bool(role_tags(u) & want_roles) and len(job_stack(u) & mine) >= 3)
    miss = [u for u, y in pairs if y and not (bool(role_tags(u) & want_roles)
                                              and bool(job_stack(u) & mine))]
    if miss:
        print("   подходящие, которые теряет «роль И стек»:")
        for u in miss[:6]:
            print(f"     {jobs[u]['title'][:52]:54s} стек вакансии: {sorted(job_stack(u)) or '—'}")


run("profile-backend", {"backend", "platform", "devops-sre"})
run("profile-data", {"data-engineer", "analyst", "backend"})
