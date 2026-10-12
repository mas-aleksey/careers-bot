#!/usr/bin/env python3
"""Сопоставление профиля с паспортом вакансии: только код, без моделей.

Проверка гипотезы из docs/scaling.md. Метка истины — балл Sonnet из matches.

Веса здесь написаны руками и ТЕРЯЮТ подходящие вакансии — см. замер в
docs/scaling.md. Рабочий вариант — подогнанные веса из fit.py рядом: те же
признаки, логистическая регрессия, 100% полноты при 36% объёма.
"""
import json, glob, re, sys, collections

D = json.load(open("/tmp/pass/data.json"))
PATTERN = sys.argv[1] if len(sys.argv) > 1 else "/tmp/pass/out_*.json"
STRICT = "--strict" in sys.argv       # первая, наивная версия правил
PASS = {}
for f in sorted(glob.glob(PATTERN)):
    for p in json.load(open(f)):
        PASS[p["url"]] = p

EUROPE = {"PT","ES","FR","DE","NL","PL","IT","CZ","SE","DK","NO","FI","CH","AT","BE",
          "IE","GB","UK","GR","RO","BG","EE","LV","LT","HU","HR","SK","SI","RS","CY",
          "MT","LU","IS","UA","MD","AL","MK","ME","BA","TR"}
SUPRA_OK = {"EU","EUROPE","EMEA","WORLDWIDE","ANYWHERE","GLOBAL","REMOTE"}
# Своя география для профиля из Португалии: Европа целиком плюс наднациональное.
ALLOWED = EUROPE | SUPRA_OK
HOME_CITIES = {"porto","oporto","matosinhos","vila nova de gaia","lisbon","lisboa"}

# Словарь синонимов — заглушка того, что в проде делают эмбеддинги терминов.
SYN = {
    "postgresql":"postgres","psql":"postgres","rdbms":"sql","relational databases":"sql",
    "k8s":"kubernetes","gke":"kubernetes","eks":"kubernetes","openshift":"kubernetes",
    "golang":"go","py":"python","python3":"python",
    "gcp":"google cloud","google cloud platform":"google cloud",
    "amazon web services":"aws","apache kafka":"kafka","apache airflow":"airflow",
    "apache spark":"spark","elastic":"elasticsearch","es":"elasticsearch",
    "ci/cd":"cicd","rest api":"rest","restful":"rest","grpc":"grpc",
    "mongo":"mongodb","clickhouse":"clickhouse","terraform":"terraform",
    "docker":"docker","containers":"docker","microservices":"microservices",
    "sql queries":"sql","t-sql":"sql","pl/sql":"sql","oracle db":"oracle",
}
def norm(t):
    t = re.sub(r"[^a-z0-9+#/ .-]", " ", (t or "").lower()).strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s*\d+(\.\d+)?$", "", t)       # Postgres 15 -> postgres
    return SYN.get(t, t)

ADJACENT = {
    "backend": {"platform","devops-sre","architect","fullstack","data-engineer","security"},
    "data-engineer": {"analyst-data","data-scientist","platform","backend","ml-engineer"},
    "analyst-business": {"analyst-product","architect","pm","analyst-data"},
}
FAMILY = {"profile-data":"data-engineer", "profile-analyst":"analyst-business", "profile-backend":"backend"}
SENIOR = {"senior","lead","staff","principal"}
LOW = {"intern","junior"}


LOC = {j["url"]: (j["location"] or "") for j in json.load(open("/tmp/pass/data.json"))["jobs"]}
ALIEN = {"sales","marketing","hr","finance","support","designer"}


def match(profile, fam, psp):
    """Возвращает (балл 0-100, причина). Балл строится по весам SCORE_SYSTEM:
    стек 40, роль и уровень 35, география и формат 25."""
    why = []
    hire = {str(x).upper() for x in (psp.get("hire") or [])}
    unc = {str(x) for x in (psp.get("uncertain") or [])}
    # --- блокеры ---
    # Гео: решаем, только если страна названа и в ней нет уверенности сомнений.
    if hire and not (hire & ALLOWED) and "hire" not in unc:
        return 0, f"найм только {', '.join(sorted(hire))[:40]}"
    mode = (psp.get("work_mode") or "not-stated").lower()
    city = (psp.get("office_city") or "").lower()
    loc = LOC.get(psp.get("url"), "").lower()
    # «Не знаю» — не блокер. Поле помечено uncertain, или в локации с доски
    # стоит remote — значит извлекатель мог ошибиться, пропускаем дальше.
    unsure_mode = "work_mode" in unc or "remote" in loc or "remote" in city
    if mode == "onsite" and (STRICT or not unsure_mode):
        return 0, "офис обязателен"
    known_city = city and not unsure_mode and len(city) > 2
    if mode == "hybrid" and known_city and not any(h in city for h in HOME_CITIES):
        return 0, f"гибрид в {city[:30]}"
    lv = {str(x).lower() for x in (psp.get("levels") or [])}
    if lv and lv <= LOW and str(profile.get("level","")).lower() in SENIOR:
        return 0, "грейд ниже профиля"
    fam_v = (psp.get("role_family") or "other").lower()
    alien = fam_v in ALIEN if not STRICT else fam_v != fam and fam_v not in ADJACENT.get(fam, set())
    if alien and "role_family" not in unc:
        return 0, f"другая профессия: {fam_v}"
    # --- баллы ---
    stack = {norm(s) for s in (profile.get("stack") or [])}
    must = [norm(m) for m in (psp.get("must") or [])]
    hit = [m for m in must if m in stack or any(m in s or s in m for s in stack if len(s) > 3)]
    cov = len(hit)/min(len(must), 6) if must else 0.6   # длинный список требований не наказывает
    nice = [norm(m) for m in (psp.get("nice") or [])]
    nhit = [m for m in nice if m in stack]
    stack_score = min(1.0, cov + 0.1*len(nhit)/max(1,len(nice)))
    role_score = 1.0 if fam_v == fam else (0.8 if fam_v in ADJACENT.get(fam, set()) else 0.5)
    doms = {str(d).lower() for d in (psp.get("domains") or [])}
    pdoms = " ".join(str(d).lower() for d in (profile.get("domains") or []))
    dom_hit = any(d in pdoms or any(w in pdoms for w in d.split("-")) for d in doms)
    geo = 1.0 if (hire & ALLOWED) else (0.8 if not hire else 0.6)
    if mode == "remote": geo = min(1.0, geo + 0.0)
    elif mode == "not-stated": geo *= 0.9
    pct = round(40*stack_score + 35*role_score*(1.0 if lv & SENIOR or not lv else 0.85)
                + 25*geo + (5 if dom_hit else 0))
    why.append(f"стек {len(hit)}/{len(must) or '?'}")
    if dom_hit: why.append("домен совпал")
    return min(100, pct), ", ".join(why)


def main():
    prof = D["profiles"]; thr = D["thresholds"]
    res = collections.defaultdict(list)
    missing = 0
    # Истина — только прямые оценки Sonnet. Баллы, разнесённые spread на группу
    # дублей, в выборку не берём: там у US-only вакансии стоит балл её
    # удалённого двойника, и сравнивать с ними код бессмысленно.
    direct = json.load(open("/tmp/pass/direct.json"))
    for lab in direct:
        tg = str(lab["tg"]); psp = PASS.get(lab["url"])
        if not psp:
            missing += 1; continue
        pct, why = match(prof[tg], FAMILY[tg], psp)
        truth_pos = lab["pct"] >= 70
        code_pos = pct >= (thr[tg] or 70)
        res[(truth_pos, code_pos)].append((tg, lab["url"], lab["pct"], pct, why))
    tp=len(res[(True,True)]); fn=len(res[(True,False)])
    fp=len(res[(False,True)]); tn=len(res[(False,False)])
    print(f"паспортов нет у {missing} пар")
    print(f"Sonnet сказал ДА, код сказал ДА : {tp}")
    print(f"Sonnet сказал ДА, код сказал НЕТ: {fn}   <- ложные отказы, дорогая ошибка")
    print(f"Sonnet сказал НЕТ, код сказал ДА : {fp}  <- лишние вакансии, дешёвая ошибка")
    print(f"Sonnet сказал НЕТ, код сказал НЕТ: {tn}")
    if tp+fn: print(f"полнота кода: {tp/(tp+fn):.1%}")
    if tp+fp: print(f"точность кода: {tp/(tp+fp):.1%}")
    print("\nложные отказы (до 15):")
    for tg,u,sp,cp,why in res[(True,False)][:15]:
        print(f"  Sonnet {sp} -> код {cp} | {why} | {u[:70]}")
    print("\nлишние (до 10):")
    for tg,u,sp,cp,why in res[(False,True)][:10]:
        print(f"  Sonnet {sp} -> код {cp} | {why} | {u[:70]}")

main()
