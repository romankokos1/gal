#!/usr/bin/env python3
"""
Hlídač čepů – sleduje „Dnes na čepu“ ve více podnicích.

Pro každý podnik ukládá do data/<id>/:
  log.jsonl      – každý stav čepů přesně jak byl na webu (základ všeho)
  historie.csv   – každé pivo s datem naražení a dočepování (odvozeno z logu)
  aktualne.json  – aktuální nabídka
  stav.json      – kdy hlídač naposled běžel, chyby, nečinnost
a do data/podniky.json seznam podniků pro přehledovou stránku.

`python scrape.py --rebuild` přepočítá historie.csv všech podniků z logu.

Zásada: data se neztrácí kvůli parsování. Každá položka má vždy pole `raw`
s celým textem; pivovar/stupeň/% se doplní jen tam, kde to jde.
"""

import csv
import json
import re
import shutil
import sys
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---------- podniky ----------
# id = název složky v data/ (neměnit, jinak se historie rozdělí)
# polozky/pole = volitelný přesný výběr prvků; když selže, použijí se obecné metody
PODNIKY = [
    {"id": "galerie", "nazev": "Galerie piva", "url": "https://www.galeriepiva.cz/"},
    {"id": "sedm", "nazev": "sedm°", "url": "https://www.sedmstupnu.cz/",
     "polozky": "ul.elementor-price-list > li",
     "pole": [".elementor-price-list-title", ".elementor-price-list-price",
              ".elementor-price-list-description"]},
    # pivovarská pivnice – na čepu jen vlastní piva, pivovar se v textu neuvádí
    {"id": "clock", "nazev": "Pivnice Clock", "url": "https://www.pivniceclock.cz/nabidka/",
     "pivovar": "Clock"},
]

ROOT = Path(__file__).parent / "data"
TZ = ZoneInfo("Europe/Prague")
HIST_COLS = ["klic", "pivovar", "nazev", "stupen", "alkohol", "styl",
             "raw", "narazeno", "docepovano"]
FAILS_BEFORE_ERROR = 3            # ~6 hodin výpadku při běhu po 2 h
STALE_DAYS = 10                   # tak dlouho beze změny = podezřelé


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

def fetch(url: str) -> str:
    last = None
    for _ in range(3):
        try:
            r = requests.get(url, timeout=30, headers={
                "User-Agent": "hlidac-cepu (GitHub Actions; osobní archiv)"
            })
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            last = e
    raise RuntimeError(f"Stránku se nepodařilo stáhnout: {last}")


# ---------- vytažení řádků (víc strategií, od nejpřesnější) ----------

BEER_HINT = re.compile(r"\d\s*°|\d\s*%")
PRICE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(?:,-)?\s*(?:Kč|CZK|,-)\s*$", re.I)
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
        if HEADING.match(el.name or "") or el.find(HEADING):
            break
        lis = el.find_all("li")
        trs = [tr for tr in el.find_all("tr") if tr.find("td")]
        if trs:
            texts = [row_text(tr) for tr in trs]
        elif lis:
            texts = [li.get_text(" ") for li in lis]
        else:
            texts = [el.get_text(" ")]
        for t in texts:
            t = strip_num(clean(t))
            if t:
                out.append(t)
    return out


def row_text(tr) -> str:
    """Řádek tabulky bez buněk s cenou (změna ceny není změna piva)."""
    cells = [clean(td.get_text(" ")) for td in tr.find_all("td")]
    return " | ".join(c for c in cells if c and not PRICE.match(c))


def by_selector(soup, p) -> list[str]:
    if not p.get("polozky"):
        return []
    lines = []
    for el in soup.select(p["polozky"]):
        parts = []
        for sel in p.get("pole", []):
            f = el.select_one(sel)
            if f and clean(f.get_text(" ")):
                parts.append(clean(f.get_text(" ")))
        t = strip_num(" | ".join(parts) if parts else clean(el.get_text(" ")))
        if t:
            lines.append(t)
    return lines


def extract_lines(html: str, p: dict) -> tuple[list[str], str]:
    soup = BeautifulSoup(html, "html.parser")

    # 0) přesný výběr nastavený pro podnik
    lines = by_selector(soup, p)
    if lines and looks_like_beers(lines):
        return lines, "selektor"

    # 1) nadpis obsahující „na čepu“ → bloky pod ním do dalšího nadpisu
    #    (nadpis bývá zabalený v divech, proto se hledá i o pár úrovní výš)
    for h in soup.find_all(re.compile(r"^h[1-6]$|^p$|^strong$")):
        if "na cepu" in norm(h.get_text()) and len(h.get_text()) < 40:
            anchor = h
            for _ in range(5):
                if anchor is None:
                    break
                if anchor.find_next_sibling():
                    lines = lines_after_heading(anchor)
                    if lines and looks_like_beers(lines):
                        return lines, "nadpis"
                anchor = anchor.parent

    # 2) celá stránka: odstavce/položky, které vypadají jako piva (°, %)
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
NUM = r"(\d{1,2}(?:[.,]\d{1,2})?)"
DEG = re.compile(NUM + r"\s*°")
ABV_LABEL = re.compile(r"(?:ABV|alk\.?|alkohol)\s*:?\s*" + NUM + r"\s*%", re.I)
PCT = re.compile(NUM + r"\s*%")


PAREN = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


def parse_item(raw: str, quiet: bool = False, default_brewery: str = "") -> dict:
    out = {"raw": raw, "pivovar": "", "nazev": "", "stupen": "",
           "alkohol": "", "styl": ""}
    rest = raw

    parts = SEP.split(raw, maxsplit=1)
    if len(parts) == 2:
        out["pivovar"], rest = parts[0].strip(" |"), parts[1].strip()

    d = DEG.search(rest)
    a = ABV_LABEL.search(rest)
    if a:
        # „11% | Lager, ABV 4,6%“ – procento bez ABV je ve skutečnosti stupeň
        if not d:
            d = next((m for m in PCT.finditer(rest)
                      if m.end() <= a.start() or m.start() >= a.end()), None)
            if d and float(d.group(1).replace(",", ".")) < 7:
                d = None
    else:
        a = PCT.search(rest)
    if d:
        out["stupen"] = d.group(1).replace(",", ".")
    if a:
        out["alkohol"] = a.group(1).replace(",", ".")

    marks = sorted((m for m in (d, a) if m), key=lambda m: m.start())
    if marks:
        out["nazev"] = rest[:marks[0].start()].strip(" |/,-")
        pieces, pos = [], marks[0].start()
        for m in marks:
            pieces.append(rest[pos:m.start()])
            pos = m.end()
        pieces.append(rest[pos:])
        out["styl"] = " ".join(x.strip(" |/,-") for x in pieces if x.strip(" |/,-"))
        if not out["nazev"] and out["styl"]:
            # „10° Hektor (Výčepní)“ – stupeň na začátku, styl v závorce
            m = PAREN.match(out["styl"])
            out["nazev"], out["styl"] = (m.group(1), m.group(2)) if m and m.group(1) \
                else (out["styl"], "")
    else:
        m = PAREN.match(rest.strip(" |"))
        if m and m.group(1):
            out["nazev"], out["styl"] = m.group(1).strip(" |"), m.group(2)
        else:
            out["nazev"] = rest.strip(" |")  # bez čísel – celý zbytek je název

    if not out["pivovar"] and default_brewery:
        out["pivovar"] = default_brewery

    missing = [k for k in ("pivovar", "nazev") if not out[k]]
    if not (out["stupen"] or out["alkohol"]):
        missing.append("stupeň i %")
    if missing and not quiet:
        warn(f"Neúplně rozparsováno ({', '.join(missing)}): {raw}")
    return out


# ---------- soubory ----------

def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_if_changed(p: Path, text: str):
    if not p.exists() or p.read_text(encoding="utf-8") != text:
        p.write_text(text, encoding="utf-8")


class Store:
    """Soubory jednoho podniku v data/<id>/."""

    def __init__(self, pid: str):
        self.dir = ROOT / pid
        self.hist = self.dir / "historie.csv"
        self.log = self.dir / "log.jsonl"
        self.stav = self.dir / "stav.json"

    def load_history(self) -> list[dict]:
        if not self.hist.exists():
            return []
        with self.hist.open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def save_history(self, rows: list[dict]):
        with self.hist.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=HIST_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    def read_log(self) -> list[dict]:
        if not self.log.exists():
            return []
        out = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                warn(f"Poškozený řádek v logu přeskočen: {line[:80]}")
        return out

    def append_log(self, entry: dict):
        with self.log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def seed_log_from_history(self, hist: list[dict]):
        """Starší historie vznikla bez logu – zrekonstruuje z ní jednotlivé stavy."""
        times = sorted({t for r in hist for t in (r["narazeno"], r["docepovano"]) if t})
        for t in times:
            lines = [r["raw"] for r in hist
                     if r["narazeno"] <= t and (not r["docepovano"] or r["docepovano"] > t)]
            self.append_log({"cas": t, "radky": lines, "metoda": "rekonstrukce"})


def migrate_old_layout():
    """Původní verze ukládala Galerii přímo do data/ – přesune ji do data/galerie/."""
    target = ROOT / "galerie"
    old = [ROOT / n for n in ("historie.csv", "log.jsonl", "stav.json", "aktualne.json",
                              "posledni_chyba.html")]
    if any(p.exists() for p in old) and not (target / "historie.csv").exists():
        target.mkdir(parents=True, exist_ok=True)
        for p in old:
            if p.exists():
                shutil.move(str(p), target / p.name)
        print("Data Galerie piva přesunuta do data/galerie/.")
    (ROOT / "surove.txt").unlink(missing_ok=True)


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


def apply_state(hist: list[dict], lines: list[str], when: str, quiet=False,
                default_brewery: str = ""):
    """Promítne jeden stav čepů do historie. Vrací (piva, nová, dočepovaná, upravená)."""
    beers = []
    for i, raw in enumerate(lines, 1):
        b = parse_item(raw, quiet=quiet, default_brewery=default_brewery)
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
    migrate_old_layout()
    for p in PODNIKY:
        st = Store(p["id"])
        log = st.read_log()
        if not log:
            print(f"{p['nazev']}: log je prázdný, přeskakuji.")
            continue
        hist = []
        for e in log:
            apply_state(hist, e["radky"], e["cas"], quiet=True,
                        default_brewery=p.get("pivovar", ""))
        st.save_history(hist)
        print(f"{p['nazev']}: přepočítáno z {len(log)} stavů → {len(hist)} naražení.")
    return 0


# ---------- jeden podnik ----------

def run_venue(p: dict, now_dt: datetime) -> bool:
    """Vrací False, když podnik selhává déle, než je tolerance."""
    name = p["nazev"]
    st = Store(p["id"])
    st.dir.mkdir(parents=True, exist_ok=True)
    now = now_dt.strftime("%Y-%m-%d %H:%M")
    stav = load_json(st.stav, {})
    stav_before = json.dumps(stav, sort_keys=True)

    def save_stav():
        if json.dumps(stav, sort_keys=True) != stav_before:
            st.stav.write_text(json.dumps(stav, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")

    # --- stažení a vytažení; jakákoli chyba se jen započítá ---
    html, lines, method, err = "", [], "nic", ""
    try:
        html = fetch(p["url"])
        lines, method = extract_lines(html, p)
        if not lines:
            err = "sekce „Dnes na čepu“ nenalezena"
    except Exception as e:  # výpadek webu, síť…
        err = str(e)

    if not lines:
        n = stav.get("chyby_v_rade", 0) + 1
        stav.update(chyby_v_rade=n, posledni_chyba=now, chyba=err[:300])
        if html:
            (st.dir / "posledni_chyba.html").write_text(html, encoding="utf-8")
        save_stav()
        if n < FAILS_BEFORE_ERROR:
            # Krátký výpadek – historie se nemění, běh zůstane zelený.
            warn(f"{name}: chyba {n}/{FAILS_BEFORE_ERROR - 1} tolerovaných: {err}")
            return True
        print(f"::error::{name}: hlídač selhává {n}× po sobě: {err} "
              "(historie ponechána beze změny)")
        return False

    # --- úspěch ---
    stav["chyby_v_rade"] = 0
    stav.pop("chyba", None)
    (st.dir / "posledni_chyba.html").unlink(missing_ok=True)
    # jen datum → maximálně jeden „heartbeat“ commit denně, repo zůstane aktivní
    stav["posledni_kontrola"] = now_dt.strftime("%Y-%m-%d")
    stav["metoda"] = method
    stav["pocet_piv"] = len(lines)
    if method not in ("selektor", "nadpis"):
        warn(f"{name}: použita záložní metoda vytažení: {method}")

    hist = st.load_history()
    log = st.read_log()
    if not log and hist:
        st.seed_log_from_history(hist)
        log = st.read_log()

    if not log or log[-1]["radky"] != lines:
        st.append_log({"cas": now, "radky": lines, "metoda": method})
        beers, new, gone, updated = apply_state(hist, lines, now,
                                                default_brewery=p.get("pivovar", ""))
        st.save_history(hist)
        write_if_changed(st.dir / "aktualne.json", json.dumps({
            "zdroj": p["url"], "zmeneno": now, "metoda": method,
            "piva": [{k: b[k] for k in ("pozice", "pivovar", "nazev", "stupen",
                                         "alkohol", "styl", "raw")} for b in beers],
        }, ensure_ascii=False, indent=2) + "\n")
        if new or gone:
            stav["posledni_zmena"] = now
            stav.pop("upozorneno_necinnost", None)
        msg = [f"🍺 {name} – změna na čepu ({now})"]
        msg += [f"➕ {b['raw']}" for b in new]
        msg += [f"➖ {r['raw']}" for r in gone]
        msg += [f"✏️ upraveno: {b['raw']}" for b in updated]
        print("\n".join(msg))
    else:
        print(f"{name}: beze změny ({len(lines)} piv, metoda {method}).")

    stav.setdefault("posledni_zmena", now)
    last = datetime.strptime(stav["posledni_zmena"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    idle = (now_dt - last).days
    if idle >= STALE_DAYS and not stav.get("upozorneno_necinnost"):
        warn(f"{name}: nabídka se nezměnila {idle} dní.")
        stav["upozorneno_necinnost"] = now

    save_stav()
    return True


def main() -> int:
    ROOT.mkdir(exist_ok=True)
    migrate_old_layout()
    write_if_changed(ROOT / "podniky.json", json.dumps(
        [{k: p[k] for k in ("id", "nazev", "url")} for p in PODNIKY],
        ensure_ascii=False, indent=2) + "\n")
    now_dt = datetime.now(TZ)
    ok = True
    for p in PODNIKY:          # jeden rozbitý podnik neblokuje ostatní
        try:
            ok = run_venue(p, now_dt) and ok
        except Exception as e:
            print(f"::error::{p['nazev']}: neočekávaná chyba: {e}")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(rebuild() if "--rebuild" in sys.argv else main())
