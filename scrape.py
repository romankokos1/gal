#!/usr/bin/env python3
"""
Hlídač čepů – Galerie piva (https://www.galeriepiva.cz/)

Stáhne sekci „Dnes na čepu“ a zapíše:
  data/aktualne.json  – aktuální nabídka (jen když se změní)
  data/historie.csv   – každé pivo s datem naražení a dočepování
  data/surove.txt     – surové řádky seznamu (záloha nezávislá na parsování)

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


def parse_item(raw: str) -> dict:
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
    if missing:
        warn(f"Neúplně rozparsováno ({', '.join(missing)}): {raw}")
    return out


# ---------- zápis ----------

def load_history() -> list[dict]:
    p = DATA / "historie.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def save_history(rows: list[dict]):
    with (DATA / "historie.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HIST_COLS)
        w.writeheader()
        w.writerows(rows)


def telegram(text: str):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=20)
    except requests.RequestException as e:
        warn(f"Telegram selhal: {e}")


def main() -> int:
    DATA.mkdir(exist_ok=True)
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

    html = fetch()
    lines, method = extract_lines(html)

    if not lines:
        # Nic jsme nenašli: NEMĚNÍME historii (nechceme vše označit za dočepované),
        # jen uložíme stránku pro diagnostiku a skončíme chybou.
        (DATA / "posledni_chyba.html").write_text(html, encoding="utf-8")
        print("::error::Sekce „Dnes na čepu“ nenalezena – stránka uložena "
              "do data/posledni_chyba.html, historie ponechána beze změny.")
        telegram("⚠️ Hlídač Galerie piva: nenašel jsem seznam čepů, zkontroluj parser.")
        return 1

    if method != "nadpis":
        warn(f"Použita záložní metoda vytažení: {method}")

    # Surová záloha – vždy, bez ohledu na parsování
    (DATA / "surove.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    beers = []
    for i, raw in enumerate(lines, 1):
        b = parse_item(raw)
        b["pozice"] = i
        b["klic"] = norm(raw)
        beers.append(b)

    # --- historie ---
    hist = load_history()
    open_rows = {r["klic"]: r for r in hist if not r["docepovano"]}
    current = {b["klic"] for b in beers}

    new_beers = []
    for b in beers:
        if b["klic"] not in open_rows:
            hist.append({**{k: b.get(k, "") for k in HIST_COLS},
                         "narazeno": now, "docepovano": ""})
            new_beers.append(b)
    gone = [r for k, r in open_rows.items() if k not in current]
    for r in gone:
        r["docepovano"] = now

    if not new_beers and not gone:
        print(f"Beze změny ({len(beers)} piv, metoda {method}).")
        return 0

    save_history(hist)

    (DATA / "aktualne.json").write_text(json.dumps({
        "zdroj": URL,
        "zmeneno": now,
        "metoda": method,
        "piva": [{k: b[k] for k in ("pozice", "pivovar", "nazev", "stupen",
                                     "alkohol", "styl", "raw")} for b in beers],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # chybový snapshot už není aktuální
    (DATA / "posledni_chyba.html").unlink(missing_ok=True)

    msg = [f"🍺 Galerie piva – změna na čepu ({now})"]
    msg += [f"➕ {b['raw']}" for b in new_beers]
    msg += [f"➖ {r['raw']}" for r in gone]
    print("\n".join(msg))
    telegram("\n".join(msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
