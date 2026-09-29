#!/usr/bin/env python3
"""
Hlídač čepů – Galerie piva (https://www.galeriepiva.cz/)

Stáhne sekci „Dnes na čepu“ a zapíše:
  data/log.jsonl      – každý stav čepů přesně jak byl na webu (základ všeho)
  data/historie.csv   – každé pivo s datem naražení a dočepování (odvozeno z logu)
  data/aktualne.json  – aktuální nabídka
  data/surove.txt     – aktuální surové řádky
  data/stav.json      – kdy hlídač naposled běžel, chyby, nečinnost

`python scrape.py --rebuild` přepočítá historie.csv z logu (např. po vylepšení parseru).

Zásada: data se neztrácí kvůli parsování. Každá položka má vždy pole `raw`
s celým textem; pivovar/stupeň/% se doplní jen tam, kde to jde.
"""

import csv
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = "https://www.galeriepiva.cz/"
DATA = Path(__file__).parent / "data"
TZ = ZoneInfo("Europe/Prague")
HIST_COLS = ["klic", "pivovar", "nazev", "stupen", "alkohol", "styl",
             "raw", "narazeno", "docepovano"]


# ---------- pomocné ----------

def norm(text: str) -> str:
    """Normalizace pro porovnání: bez diakritiky, malá písmena, jen alfanum."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def warn(msg: str):
    # GitHub Actions ho zobrazí jako žlutou anotaci, lokálně jen vypíše
    print(f"::warning::{msg}")


# ---------- stažení ----------

def fetch() -> str:
    last = None
    for _ in range(3):
        try:
            r = requests.get(URL, timeout=30, headers={
                "User-Agent": "galerie-piva-hlidac (GitHub Actions; osobní archiv)"
            })
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            last = e
    raise RuntimeError(f"Stránku se nepodařilo stáhnout: {last}")


# ---------- vytažení řádků (víc strategií, od nejpřesnější) ----------

BEER_HINT = re.compile(r"\d\s*°|\d\s*%")
NUMBERING = re.compile(r"^\s*\d{1,2}\s*[.)]\s*")
HEADING = re.compile(r"^h[1-6]$")


def strip_num(text: str) -> str:
    return NUMBERING.sub("", text).strip()


def looks_like_beers(lines: list[str]) -> bool:
    """Pojistka proti nesmyslům (menu, sociální sítě): aspoň polovina řádků
    musí mít stupeň nebo procenta. Jedna položka bez čísel nevadí."""
    hits = sum(1 for l in lines if BEER_HINT.search(l))
    return hits >= 1 and hits >= len(lines) / 2


def lines_after_heading(h) -> list[str]:
    """Text všech bloků za nadpisem až po další nadpis (odstavce i seznamy)."""
    out = []
    for el in h.find_next_siblings():
        if HEADING.match(el.name or ""):
            break
        lis = el.find_all("li")
        texts = [li.get_text(" ") for li in lis] if lis else [el.get_text(" ")]
        for t in texts:
            t = strip_num(clean(t))
            if t:
                out.append(t)
    return out


def extract_lines(html: str) -> tuple[list[str], str]:
    soup = BeautifulSoup(html, "html.parser")

    # 1) nadpis obsahující „na čepu“ → všechny bloky pod ním do dalšího nadpisu
    for h in soup.find_all(re.compile(r"^h[1-6]$|^p$|^strong$")):
        if "na cepu" in norm(h.get_text()) and len(h.get_text()) < 40:
            anchor = h if h.find_next_siblings() else h.parent
            lines = lines_after_heading(anchor)
            if lines and looks_like_beers(lines):
                return lines, "nadpis"

    # 2) celá stránka: odstavce/položky, které vypadají jako piva (°, %)
    #    (najde je, i kdyby nadpis zmizel nebo se přejmenoval)
    lines = []
    for el in soup.find_all(["p", "li"]):
        if el.find(["p", "li"]):
            continue  # jen „listové“ bloky, ať se text nezdvojuje
        t = strip_num(clean(el.get_text(" ")))
        if t and BEER_HINT.search(t) and len(t) < 300 and t not in lines:
            lines.append(t)
    if lines:
        return lines, "podle-obsahu"

    return [], "nic"


# ---------- rozparsování jedné položky (vše volitelné) ----------

SEP = re.compile(r"\s+[–—-]\s+|\s*[–—]\s*")
DEG = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*°")
ABV = re.compile(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%")


def parse_item(raw: str, quiet: bool = False) -> dict:
    out = {"raw": raw, "pivovar": "", "nazev": "", "stupen": "",
           "alkohol": "", "styl": ""}
    rest = raw

    parts = SEP.split(raw, maxsplit=1)
    if len(parts) == 2:
        out["pivovar"], rest = parts[0].strip(), parts[1].strip()

    d, a = DEG.search(rest), ABV.search(rest)
    if d:
        out["stupen"] = d.group(1).replace(",", ".")
    if a:
        out["alkohol"] = a.group(1).replace(",", ".")

    marks = [m for m in (d, a) if m]
    if marks:
        first = min(m.start() for m in marks)
        last = max(m.end() for m in marks)
        out["nazev"] = rest[:first].strip(" /,-")
        out["styl"] = rest[last:].strip(" /,-")
    else:
        out["nazev"] = rest  # bez čísel – celý zbytek je název

    missing = [k for k in ("pivovar", "stupen", "alkohol") if not out[k]]
    if missing and not quiet:
        warn(f"Neúplně rozparsováno ({', '.join(missing)}): {raw}")
    return out


# ---------- soubory ----------

HIST = DATA / "historie.csv"
LOG = DATA / "log.jsonl"          # každý stav čepů, jak byl na webu (nic se nepřepisuje)
STAV = DATA / "stav.json"         # heartbeat a chyby hlídače
FAILS_BEFORE_ALERT = 3            # ~6 hodin výpadku při běhu po 2 h
STALE_DAYS = 10                   # tak dlouho beze změny = podezřelé


def load_history() -> list[dict]:
    if not HIST.exists():
        return []
    with HIST.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def save_history(rows: list[dict]):
    with HIST.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HIST_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_if_changed(p: Path, text: str):
    if not p.exists() or p.read_text(encoding="utf-8") != text:
        p.write_text(text, encoding="utf-8")


def read_log() -> list[dict]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            warn(f"Poškozený řádek v logu přeskočen: {line[:80]}")
    return out


def append_log(entry: dict):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def seed_log_from_history(hist: list[dict]):
    """Starší historie vznikla bez logu – zrekonstruuje z ní jednotlivé stavy."""
    times = sorted({t for r in hist for t in (r["narazeno"], r["docepovano"]) if t})
    for t in times:
        lines = [r["raw"] for r in hist
                 if r["narazeno"] <= t and (not r["docepovano"] or r["docepovano"] > t)]
        append_log({"cas": t, "radky": lines, "metoda": "rekonstrukce"})


# ---------- párování piv ----------

def beer_key(b: dict) -> str:
    if b["pivovar"] and b["nazev"]:
        return norm(b["pivovar"]) + "|" + norm(b["nazev"])
    return norm(b["raw"])


def ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def same_beer(row: dict, b: dict) -> bool:
    """Je nový řádek z webu totéž pivo jako řádek, který je v historii na čepu?"""
    if row["klic"] == b["klic"]:
        return True
    rn, bn = norm(row["nazev"]), norm(b["nazev"])
    rp, bp = norm(row["pivovar"]), norm(b["pivovar"])
    if rn and bn:
        if rn == bn and (not rp or not bp or ratio(rp, bp) >= 0.7):
            return True          # stejný název, pivovar s překlepem / doplněný
        if rp and rp == bp and ratio(rn, bn) >= 0.85:
            return True          # stejný pivovar, překlep v názvu
    # jedna strana nejde rozparsovat (chybí čárka, stupeň…) → porovnat celý text
    if not (rp and bp and rn and bn):
        if ratio(norm(row["raw"]), norm(b["raw"])) >= 0.85:
            return True
    return False


def apply_state(hist: list[dict], lines: list[str], when: str, quiet=False):
    """Promítne jeden stav čepů do historie. Vrací (piva, nová, dočepovaná, upravená)."""
    beers = []
    for i, raw in enumerate(lines, 1):
        b = parse_item(raw, quiet=quiet)
        b["pozice"] = i
        b["klic"] = beer_key(b)
        beers.append(b)

    waiting = [r for r in hist if not r["docepovano"]]
    new, updated, pending = [], [], beers

    # 1. kolo: přesná shoda, 2. kolo: tolerantní – každý řádek historie max. jednou
    for exact in (True, False):
        left = []
        for b in pending:
            match = next((r for r in waiting
                          if (r["klic"] == b["klic"] if exact else same_beer(r, b))), None)
            if match:
                waiting.remove(match)
                if match["raw"] != b["raw"]:
                    # rozparsované údaje nepřepisovat prázdnými (obsluha smaže čárku…)
                    for k in ("pivovar", "nazev", "stupen", "alkohol", "styl"):
                        if b[k] and (b["pivovar"] or not match["pivovar"]):
                            match[k] = b[k]
                    if b["pivovar"] and b["nazev"]:
                        match["klic"] = b["klic"]
                    match["raw"] = b["raw"]
                    updated.append(b)
            else:
                left.append(b)
        pending = left

    for b in pending:
        hist.append({**{k: b.get(k, "") for k in HIST_COLS},
                     "narazeno": when, "docepovano": ""})
        new.append(b)
    for r in waiting:
        r["docepovano"] = when
    return beers, new, waiting, updated


def rebuild() -> int:
    log = read_log()
    if not log:
        print("::error::data/log.jsonl je prázdný, není z čeho přepočítat.")
        return 1
    hist = []
    for e in log:
        apply_state(hist, e["radky"], e["cas"], quiet=True)
    save_history(hist)
    print(f"Historie přepočítána z {len(log)} stavů → {len(hist)} naražení.")
    return 0


# ---------- upozornění ----------

def telegram(text: str):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=20)
    except requests.RequestException as e:
        warn(f"Telegram selhal: {e}")


# ---------- hlavní běh ----------

def main() -> int:
    DATA.mkdir(exist_ok=True)
    now_dt = datetime.now(TZ)
    now = now_dt.strftime("%Y-%m-%d %H:%M")
    stav = load_json(STAV, {})
    stav_before = json.dumps(stav, sort_keys=True)

    def save_stav():
        if json.dumps(stav, sort_keys=True) != stav_before:
            STAV.write_text(json.dumps(stav, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")

    # --- stažení a vytažení; jakákoli chyba se jen započítá ---
    html, lines, method, err = "", [], "nic", ""
    try:
        html = fetch()
        lines, method = extract_lines(html)
        if not lines:
            err = "sekce „Dnes na čepu“ nenalezena"
    except Exception as e:  # výpadek webu, síť…
        err = str(e)

    if not lines:
        n = stav.get("chyby_v_rade", 0) + 1
        stav.update(chyby_v_rade=n, posledni_chyba=now, chyba=err[:300])
        if html:
            (DATA / "posledni_chyba.html").write_text(html, encoding="utf-8")
        save_stav()
        if n < FAILS_BEFORE_ALERT:
            # Krátký výpadek – historie se nemění, běh zůstane zelený.
            warn(f"Chyba {n}/{FAILS_BEFORE_ALERT - 1} tolerovaných: {err}")
            return 0
        print(f"::error::Hlídač selhává {n}× po sobě: {err} "
              "(historie ponechána beze změny)")
        if n == FAILS_BEFORE_ALERT:
            telegram(f"⚠️ Hlídač Galerie piva selhává už {n}× po sobě:\n{err}")
        return 1

    # --- úspěch ---
    if stav.get("chyby_v_rade", 0) >= FAILS_BEFORE_ALERT:
        telegram("✅ Hlídač Galerie piva zase funguje.")
    stav["chyby_v_rade"] = 0
    stav.pop("chyba", None)
    (DATA / "posledni_chyba.html").unlink(missing_ok=True)
    # jen datum → maximálně jeden „heartbeat“ commit denně, repo zůstane aktivní
    stav["posledni_kontrola"] = now_dt.strftime("%Y-%m-%d")
    stav["metoda"] = method
    stav["pocet_piv"] = len(lines)
    if method != "nadpis":
        warn(f"Použita záložní metoda vytažení: {method}")

    hist = load_history()
    log = read_log()
    if not log and hist:
        seed_log_from_history(hist)
        log = read_log()

    changed = not log or log[-1]["radky"] != lines
    if changed:
        append_log({"cas": now, "radky": lines, "metoda": method})
        beers, new, gone, updated = apply_state(hist, lines, now)
        save_history(hist)
        write_if_changed(DATA / "surove.txt", "\n".join(lines) + "\n")
        (DATA / "aktualne.json").write_text(json.dumps({
            "zdroj": URL, "zmeneno": now, "metoda": method,
            "piva": [{k: b[k] for k in ("pozice", "pivovar", "nazev", "stupen",
                                         "alkohol", "styl", "raw")} for b in beers],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if new or gone:
            stav["posledni_zmena"] = now
            stav.pop("upozorneno_necinnost", None)
        msg = [f"🍺 Galerie piva – změna na čepu ({now})"]
        msg += [f"➕ {b['raw']}" for b in new]
        msg += [f"➖ {r['raw']}" for r in gone]
        msg += [f"✏️ upraveno: {b['raw']}" for b in updated]
        print("\n".join(msg))
        if new or gone:
            telegram("\n".join(msg))
    else:
        print(f"Beze změny ({len(lines)} piv, metoda {method}).")

    stav.setdefault("posledni_zmena", now)
    # dlouho beze změny → jednorázové upozornění (web se možná neaktualizuje)
    last = datetime.strptime(stav["posledni_zmena"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    idle = (now_dt - last).days
    if idle >= STALE_DAYS and not stav.get("upozorneno_necinnost"):
        warn(f"Nabídka se nezměnila {idle} dní.")
        telegram(f"🤔 Galerie piva: nabídka na webu se nezměnila {idle} dní. "
                 "Buď se netočí, nebo web nikdo neaktualizuje.")
        stav["upozorneno_necinnost"] = now

    save_stav()
    return 0


if __name__ == "__main__":
    sys.exit(rebuild() if "--rebuild" in sys.argv else main())
