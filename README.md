# Hlídač čepů – Galerie piva

Automaticky sleduje sekci **„Dnes na čepu“** na [galeriepiva.cz](https://www.galeriepiva.cz/)
a ukládá historii toho, co se kdy narazilo a dočepovalo.

## Soubory v `data/`

| soubor | obsah |
|---|---|
| `aktualne.json` | aktuální nabídka (pivovar, název, stupeň, % alk., styl + surový text) |
| `historie.csv` | jeden řádek na pivo: `narazeno` / `docepovano` |
| `surove.txt` | surové řádky seznamu, přesně jak jsou na webu |
| `posledni_chyba.html` | vznikne jen tehdy, když se seznam vůbec nenajde |

## Odolnost vůči změnám na webu

- Seznam se hledá třemi způsoby: podle nadpisu „na čepu“, podle obsahu seznamů (°, %)
  a nakonec z holého textu stránky.
- Každá položka se uloží **vždy celá jako `raw`**. Pivovar, stupeň, alkohol a styl se doplní,
  jen když to jde. Chybějící pomlčka nebo stupeň vyvolá jen žluté varování v Actions.
- Když se seznam nenajde vůbec, historie se **nezmění** (piva se neoznačí za dočepovaná),
  uloží se snapshot stránky a běh skončí červeně.
- Piva se v historii párují podle normalizovaného celého textu.

## Nastavení

1. Nahraj obsah do nového repa na GitHubu.
2. *Settings → Actions → General → Workflow permissions* → **Read and write permissions**.
3. (Volitelně) *Settings → Secrets and variables → Actions*: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
4. *Actions → Hlídač čepů → Run workflow* pro první běh.

Běží každé 2 hodiny přes den (6–22 h). Frekvenci změníš v `.github/workflows/hlidac.yml`.

## Přehled a analýza (`index.html`)

Zapni GitHub Pages: *Settings → Pages → Build and deployment → Deploy from a branch*,
větev `main`, složka `/ (root)`. Za minutu bude stránka na
`https://TVUJ-UCET.github.io/galerie-piva-hlidac/` a sama si načte `data/historie.csv`
(obnoví se s každým commitem hlídače). Pages na bezplatném účtu vyžadují veřejné repo.

Záložky: **Přehled** (teď na čepu, žebříček pivovarů, stylů, síly), **Pivovary**, **Piva**
(unikátní piva), **Historie** (každé naražení, export do Excelu) a **Časová osa**.
Filtry období, hledání a klik na pivovar/styl platí pro všechny záložky.

Lokálně stačí otevřít `index.html` a vybrat `historie.csv` ručně.

## Lokálně (skript)

```bash
pip install -r requirements.txt
python scrape.py
```
