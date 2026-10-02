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
import os
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
    # Klub malých pivovarů Plzeň – „Pivovar - Název, styl, 12° sv. nef., 4, 8% vol. Alc, 35 IBU“
    {"id": "kmp", "nazev": "KMP Plzeň", "url": "https://www.klubmalychpivovaru.cz/",
     "polozky": "#BeersNaCepu .name-beer:not(#BeerSlot0)"},
    # Zlatá kráva Bandaska – TV menu: pivovar | „12 PLNOTUČNÁ“ (číslo = stupně) | popis
    {"id": "zlatakrava", "nazev": "Zlatá kráva Bandaska", "url": "https://zk.vhost.cz/",
     "polozky": "tr.polozka", "pole": [".pivovar", ".nazev", ".popis"],
     "format": "pivovar|nazev|popis", "jen_selektor": True},
    # „NÁZEV – Styl (Pivovar) · abv 4,9 %“, pod nabídkou „Připraveno k naražení“
    {"id": "beerandfriends", "nazev": "Beer and Friends", "url": "https://www.beerandfriends.eu/beer-and-friends",
     "polozky": ".field-name-field-nacepu p", "format": "nazev-styl-(pivovar)"},
    # očíslované bloky „5) Název 14° styl – piv.Pivovar (Město)  0,4l  94Kč“ + odstavec s popisem
    # (obecná záloha podle obsahu by tu brala popisy jako piva, proto jen_selektor)
    {"id": "vratnice", "nazev": "Vrátnice", "url": "https://vratnice.cz/",
     "bloky": "#na-cepu p", "format": "nazev-stupen-styl-pivovar", "jen_selektor": True},
    # Webflow, záložka „Pivo“: „GLEE 11°, GLUTEN REDUCED PALE ALE“ + pivovar zvlášť
    # (na stránce jsou i zbytky šablony „FUSCE ALI“, proto jen přesný výběr)
    {"id": "polepsovna", "nazev": "Polepšovna", "url": "https://www.pivnicepolepsovna.cz/",
     "polozky": '.w-tab-pane[data-w-tab="Pivo"] .home_2_career_item',
     "pole": [".heading-style-h5", ".home_2_career_top-wrapper > div:last-child"],
     "format": "nazev-styl|pivovar", "jen_selektor": True},
]

ROOT = Path(__file__).parent / "data"
TZ = ZoneInfo("Europe/Prague")
HIST_COLS = ["klic", "pivovar", "nazev", "stupen", "alkohol", "styl",
             "raw", "narazeno", "docepovano", "dalsi"]
# Zvýšit při každé změně parsování/párování → historie se při příštím běhu
# sama přepočítá z logu (log se nikdy nemění).
PARSER_VERSION = 7
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

OWN_HEADERS = {"User-Agent": "hlidac-cepu (GitHub Actions; osobní archiv)"}
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
}
BLOCKED = {401, 403, 406, 429, 503}


def _get(url: str, headers: dict) -> requests.Response:
    r = requests.get(url, timeout=30, headers=headers)
    r.encoding = r.apparent_encoding or "utf-8"
    return r


def fetch_browser(url: str) -> str:
    """Poslední možnost: skutečný prohlížeč (Playwright), když web odmítá skripty."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if not os.getenv("GITHUB_ACTIONS"):
            raise
        # v GitHub Actions se prohlížeč doinstaluje jen tehdy, když je opravdu potřeba
        import subprocess
        print("Instaluji Playwright + Chromium (web odmítá běžné stažení)…")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "playwright"], check=True)
        subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
                       check=True, stdout=subprocess.DEVNULL)
        from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        try:
            pg = b.new_page(locale="cs-CZ", user_agent=BROWSER_HEADERS["User-Agent"])
            resp = pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(1500)
            if resp and resp.status >= 400:
                raise RuntimeError(f"prohlížeč dostal HTTP {resp.status}")
            return pg.content()
        finally:
            b.close()


def fetch(url: str, p: dict | None = None) -> str:
    """Stáhne stránku. Když web odmítne skript (401/403…), zkusí hlavičky prohlížeče
    a nakonec skutečný prohlížeč. Úspěšnou cestu si pamatuje v p['_cesta']."""
    p = p if p is not None else {}
    last = None
    order = ["vlastni", "hlavicky", "prohlizec"]
    if p.get("_cesta") in ("hlavicky", "prohlizec"):  # minule vlastní hlavička neprošla
        order = order[1:]
    for way in order:
        for _ in range(2 if way != "prohlizec" else 1):
            try:
                if way == "prohlizec":
                    html = fetch_browser(url)
                else:
                    r = _get(url, OWN_HEADERS if way == "vlastni" else BROWSER_HEADERS)
                    if r.status_code in BLOCKED:
                        last = f"HTTP {r.status_code} ({way})"
                        break                         # opakovat stejně nemá smysl → další způsob
                    r.raise_for_status()
                    html = r.text
                p["_cesta"] = way
                return html
            except ImportError:
                last = f"{last}; prohlížeč (Playwright) není nainstalovaný"
                break
            except Exception as e:                    # síť, timeout…
                last = f"{e} ({way})"
    raise RuntimeError(f"Stránku se nepodařilo stáhnout: {last}")


# ---------- vytažení řádků (víc strategií, od nejpřesnější) ----------

BEER_HINT = re.compile(r"\d\s*°|\d\s*%")
PRICE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(?:,-)?\s*(?:Kč|CZK|,-)\s*$", re.I)
NUMBERING = re.compile(r"^\s*\d{1,2}\s*[.)]\s*")
HEADING = re.compile(r"^h[1-6]$")


def strip_num(text: str) -> str:
    return NUMBERING.sub("", text).strip()


def is_junk(line: str) -> bool:
    """Prázdný kohout / oddělovač („---“, „–“, „volno“…) není pivo."""
    letters = re.findall(r"[^\W\d_]", line)
    if re.search(r"na\s+čepu|on\s+tap", line, re.I) and not re.search(r"\d", line):
        return True    # nadpis sekce („TENTO TÝDEN NA ČEPU / ON TAP THIS WEEK“)
    return len(letters) < 2 or bool(re.fullmatch(r"\s*(prázdn\w*|volno|empty|tbd)\s*", line, re.I))


UPCOMING = re.compile(r"připraveno|pripraveno|ready\s+to\s+tap|coming\s+soon|brzy\s+na\s+čepu|chystáme", re.I)


def split_upcoming(lines: list[str]) -> tuple[list[str], list[str]]:
    """Odřízne seznam „Připraveno k naražení“ – ten ještě neteče."""
    for i, l in enumerate(lines):
        if UPCOMING.search(l) and not re.search(r"\d\s*[°%]", l):
            return lines[:i], lines[i + 1:]
    return lines, []


def looks_like_beers(lines: list[str]) -> bool:
    """Pojistka proti nesmyslům (menu, sociální sítě): aspoň polovina řádků
    musí mít stupeň nebo procenta. Jedna položka bez čísel nevadí."""
    hits = sum(1 for l in lines if BEER_HINT.search(l))
    if any(len(re.findall(r"\d\s*°", l)) > 2 for l in lines):
        return False   # víc piv slepených do jednoho řádku – špatně vytaženo
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


BLOCK_HEAD = re.compile(r"^\s*\d{1,2}\s*\)\s*")
VOLUME_TAIL = re.compile(r"\s+\d+(?:[.,]\d+)?\s*l\b.*$", re.I)       # „0,5l / 0,3l 59Kč / 49Kč“
ABV_DESC = re.compile(r"(?:ABV|Alc|Alk|alkoholu)\.?\s*:?\s*(\d{1,2}(?:[.,]\d{1,2})?)\s*%", re.I)


def by_blocks(soup, sel: str) -> list[str]:
    """Očíslovaná hlavička piva + první odstavec pod ní (popis), z něj jen % alkoholu."""
    items, cur = [], None
    for el in soup.select(sel):
        t = clean(el.get_text(" "))
        if not t:
            continue
        if BLOCK_HEAD.match(t):
            cur = {"h": VOLUME_TAIL.sub("", BLOCK_HEAD.sub("", t)).strip(), "d": ""}
            items.append(cur)
        elif cur is not None and not cur["d"]:
            cur["d"] = t
    out = []
    for it in items:
        m = ABV_DESC.search(it["d"])
        out.append(it["h"] + (f" · ABV {m.group(1)} %" if m else ""))
    return out


def extract_lines(html: str, p: dict) -> tuple[list[str], str]:
    lines, method = _extract(html, p)
    return [l for l in lines if not is_junk(l)], method


def _extract(html: str, p: dict) -> tuple[list[str], str]:
    soup = BeautifulSoup(html, "html.parser")

    # 0) přesný výběr nastavený pro podnik
    if p.get("bloky"):
        for sel in (p["bloky"], "p"):
            lines = by_blocks(soup, sel)
            if lines and looks_like_beers(lines):
                return lines, "bloky"
    lines = by_selector(soup, p)
    if lines and (looks_like_beers(lines) or
                  (p.get("jen_selektor") and not any(len(l) > 300 for l in lines))):
        return lines, "selektor"
    if p.get("jen_selektor"):
        return [], "nic"

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
COLON = re.compile(r"^([^\d:]{2,30}):\s+(.+)$")      # „PINTA: El Bandido 12°…“
# „… //NEXT// další pivo“ – poznámka, co poteče potom; do údajů piva nepatří
DEC_SPACE = re.compile(r"(\d),\s+(\d{1,2})(?=\s*%)")   # „4, 8%“ → „4,8%“
STYLE_NOISE = re.compile(r"\b\d+\s*IBU\b|\bvol\.?(?:\s*alc\.?)?|\b(?:sv|tm|pol|polotm|nef|filtr)\.(?=\s|,|$)", re.I)
NEXT_RE = re.compile(r"\s*/{1,3}\s*(?:next|další|dalsi|následuje|nasleduje|pak|potom)\s*:?\s*/{0,3}\s*", re.I)
NUM = r"(\d{1,2}(?:[.,]\d{1,2})?)"
DEG = re.compile(NUM + r"\s*°")
ABV_LABEL = re.compile(r"(?:ABV|alk\.?|alkohol)\s*:?\s*" + NUM + r"\s*%", re.I)
PCT = re.compile(NUM + r"\s*%")


PAREN = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


NSB = re.compile(r"^(?P<nazev>.+?)\s+[–—-]\s+(?P<styl>.*?)\s*\((?P<pivovar>[^()]+)\)\s*(?P<rest>.*)$")


def parse_item(raw: str, quiet: bool = False, default_brewery: str = "", fmt: str = "") -> dict:
    out = {"raw": raw, "pivovar": "", "nazev": "", "stupen": "",
           "alkohol": "", "styl": "", "dalsi": ""}
    main = raw
    nx = NEXT_RE.split(raw, maxsplit=1)
    if len(nx) == 2:
        main, out["dalsi"] = nx[0].strip(), nx[1].strip()
    out["main"] = main
    main = DEC_SPACE.sub(r"\1,\2", main)
    rest = main

    if fmt == "pivovar|nazev|popis" and " | " in main:
        parts = [x.strip() for x in main.split(" | ")]
        out["pivovar"], out["nazev"] = parts[0], parts[1] if len(parts) > 1 else ""
        m = re.match(r"^(\d{1,2}(?:[.,]\d)?)\s*°?\s+(.+)$", out["nazev"])
        if m:                                   # „12 PLNOTUČNÁ“ → 12°, Plnotučná
            out["stupen"], out["nazev"] = m.group(1).replace(",", "."), m.group(2)
        popis = parts[2] if len(parts) > 2 else ""
        # styl = první věta popisu (ne „7. varianta…“), zkrácená na celé slovo
        st = re.split(r"(?<=\w{3}[.!?])\s", popis, maxsplit=1)[0].rstrip(".")
        out["styl"] = st if len(st) <= 90 else st[:90].rsplit(" ", 1)[0].rstrip(",") + "…"
        a = ABV_LABEL.search(popis)
        if a:
            out["alkohol"] = a.group(1).replace(",", ".")
        return out

    if fmt == "nazev-styl|pivovar" and " | " in main:
        left, _, brew = main.rpartition(" | ")
        out["pivovar"] = brew.strip()
        d = DEG.search(left)
        if d:
            out["stupen"] = d.group(1).replace(",", ".")
            out["nazev"] = left[:d.start()].strip(" ,")
            out["styl"] = left[d.end():].strip(" ,")
        else:
            nm, _, st = left.partition(",")
            out["nazev"], out["styl"] = nm.strip(" ,"), st.strip(" ,")
        a = ABV_LABEL.search(left) or PCT.search(left)
        if a:
            out["alkohol"] = a.group(1).replace(",", ".")
        return out

    if fmt == "nazev-stupen-styl-pivovar":
        core, _, tail = main.partition(" · ")
        seps = list(re.finditer(r"\s*[–—]\s*|\s+-\s+", core))
        if seps:
            left, right = core[:seps[-1].start()], core[seps[-1].end():]
            brew = re.sub(r"^(?:pivovar|pivov\.|piv\.|p\.)\s*", "", right.strip(), flags=re.I)
            out["pivovar"] = re.sub(r"\s*\([^)]*\)\s*$", "", brew).strip()
            d = DEG.search(left)
            if d:
                out["stupen"] = d.group(1).replace(",", ".")
                out["nazev"] = left[:d.start()].strip(" ,")
                out["styl"] = left[d.end():].strip(" ,")
            else:
                out["nazev"] = left.strip()
            # „Vrátnice 10°“ a „Vrátnice 11°“ – stejný název jako pivovar, rozliší je stupeň
            if out["stupen"] and (not out["nazev"] or norm(out["nazev"]) == norm(out["pivovar"])):
                out["nazev"] = f"{out['nazev']} {out['stupen'].replace('.', ',')}°".strip()
            a = ABV_LABEL.search(tail) or PCT.search(tail)
            if a:
                out["alkohol"] = a.group(1).replace(",", ".")
            if not out["pivovar"] and default_brewery:
                out["pivovar"] = default_brewery
            return out

    if fmt == "nazev-styl-(pivovar)":
        m = NSB.match(main)
        if m:
            out["pivovar"], out["nazev"] = m["pivovar"].strip(), m["nazev"].strip()
            out["styl"] = m["styl"].strip(" ,·|")
            d, a = DEG.search(m["rest"]), ABV_LABEL.search(m["rest"]) or PCT.search(m["rest"])
            if d:
                out["stupen"] = d.group(1).replace(",", ".")
            if a:
                out["alkohol"] = a.group(1).replace(",", ".")
            if not out["pivovar"] and default_brewery:
                out["pivovar"] = default_brewery
            if not (out["stupen"] or out["alkohol"]) and not quiet:
                warn(f"Neúplně rozparsováno (stupeň i %): {main}")
            return out

    parts = SEP.split(main, maxsplit=1)
    if len(parts) == 2:
        out["pivovar"], rest = parts[0].strip(" |"), parts[1].strip()
    else:
        m = COLON.match(main)
        if m:
            out["pivovar"], rest = m.group(1).strip(), m.group(2).strip()

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

    # „Název, styl, …“ – styl je za první čárkou názvu
    if ", " in out["nazev"]:
        out["nazev"], extra = out["nazev"].split(", ", 1)
        out["styl"] = ", ".join(x for x in (extra.strip(" ,"), out["styl"]) if x)
    if out["styl"]:
        st = STYLE_NOISE.sub(" ", out["styl"])
        st = re.sub(r"\s*,\s*(?:,\s*)+", ", ", re.sub(r"\s+", " ", st))
        out["styl"] = st.strip(" ,|/-")

    if not out["pivovar"] and default_brewery:
        out["pivovar"] = default_brewery

    missing = [k for k in ("pivovar", "nazev") if not out[k]]
    if not (out["stupen"] or out["alkohol"]):
        missing.append("stupeň i %")
    if missing and not quiet:
        warn(f"Neúplně rozparsováno ({', '.join(missing)}): {main}")
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


def similar_brewery(a: str, b: str) -> bool:
    """„ONE HOPE“ = „HOPE“, „SKJUBRU & X“ = „SKJUBRU“, překlep v názvu."""
    if not a or not b or ratio(a, b) >= 0.7:
        return True
    ta, tb = set(a.split()), set(b.split())
    return ta <= tb or tb <= ta


def main_text(row: dict) -> str:
    return norm(NEXT_RE.split(row["raw"], maxsplit=1)[0])


def same_beer(row: dict, b: dict) -> bool:
    """Je nový řádek z webu totéž pivo jako řádek, který je v historii na čepu?"""
    if row["klic"] == b["klic"]:
        return True
    rn, bn = norm(row["nazev"]), norm(b["nazev"])
    rp, bp = norm(row["pivovar"]), norm(b["pivovar"])
    if rn and bn:
        if rn == bn and similar_brewery(rp, bp):
            return True          # stejný název, pivovar s překlepem / doplněný / zkrácený
        if rp and rp == bp and ratio(rn, bn) >= 0.85:
            return True          # stejný pivovar, překlep v názvu
    # jedna strana nejde rozparsovat (chybí čárka, stupeň…) → porovnat celý text
    if not (rp and bp and rn and bn):
        if ratio(main_text(row), norm(b["main"])) >= 0.85:
            return True
    return False


def apply_state(hist: list[dict], lines: list[str], when: str, quiet=False,
                default_brewery: str = "", fmt: str = ""):
    """Promítne jeden stav čepů do historie. Vrací (piva, nová, dočepovaná, upravená)."""
    beers = []
    for i, raw in enumerate([l for l in lines if not is_junk(l)], 1):
        b = parse_item(raw, quiet=quiet, default_brewery=default_brewery, fmt=fmt)
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
                    match["dalsi"] = b["dalsi"]
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
                        default_brewery=p.get("pivovar", ""), fmt=p.get("format", ""))
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
    html, lines, upcoming, method, err = "", [], [], "nic", ""
    try:
        p["_cesta"] = stav.get("zpusob_stazeni", "")
        html = fetch(p["url"], p)
        if p["_cesta"] == "vlastni":
            stav.pop("zpusob_stazeni", None)
        else:
            stav["zpusob_stazeni"] = p["_cesta"]
        lines, method = extract_lines(html, p)
        lines, upcoming = split_upcoming(lines)
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

    # připraveno k naražení – jen aktuální stav, do historie nepatří
    write_if_changed(st.dir / "pripraveno.json", json.dumps([
        {k: b[k] for k in ("pivovar", "nazev", "stupen", "alkohol", "styl", "raw")}
        for b in (parse_item(l, quiet=True, default_brewery=p.get("pivovar", ""),
                             fmt=p.get("format", "")) for l in upcoming)
    ], ensure_ascii=False, indent=2) + "\n")

    hist = st.load_history()
    log = st.read_log()
    if not log and hist:
        st.seed_log_from_history(hist)
        log = st.read_log()

    if not log or log[-1]["radky"] != lines:
        st.append_log({"cas": now, "radky": lines, "metoda": method})
        beers, new, gone, updated = apply_state(hist, lines, now,
                                                default_brewery=p.get("pivovar", ""), fmt=p.get("format", ""))
        st.save_history(hist)
        write_if_changed(st.dir / "aktualne.json", json.dumps({
            "zdroj": p["url"], "zmeneno": now, "metoda": method,
            "piva": [{k: b[k] for k in ("pozice", "pivovar", "nazev", "stupen",
                                         "alkohol", "styl", "dalsi", "raw")} for b in beers],
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
    ver = ROOT / "verze_parseru.txt"
    if not ver.exists() or ver.read_text().strip() != str(PARSER_VERSION):
        print(f"Nová verze parseru ({PARSER_VERSION}) – přepočítávám historii z logu.")
        rebuild()
        ver.write_text(f"{PARSER_VERSION}\n")
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
