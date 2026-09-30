[README.md](https://github.com/user-attachments/files/32853068/README.md)
# Hlídač čepů

Automaticky sleduje nabídku **„Dnes na čepu“** ve více podnicích, ukládá historii toho,
co se kdy narazilo a dočepovalo, a zobrazuje ji na přehledové stránce.

| podnik | web | složka |
|---|---|---|
| Galerie piva | [galeriepiva.cz](https://www.galeriepiva.cz/) | `data/galerie/` |
| sedm° | [sedmstupnu.cz](https://www.sedmstupnu.cz/) | `data/sedm/` |
| Pivnice Clock | [pivniceclock.cz/nabidka](https://www.pivniceclock.cz/nabidka/) | `data/clock/` |

Každý podnik má vlastní data a běží nezávisle: když jeden web spadne, druhý se sbírá dál.

## Soubory

| soubor | obsah |
|---|---|
| `data/<podnik>/log.jsonl` | **každý stav čepů přesně tak, jak byl na webu**. Základ všeho, nic se v něm nepřepisuje. |
| `data/<podnik>/historie.csv` | každé naražení s datem `narazeno` / `docepovano` (odvozené z logu) |
| `data/<podnik>/aktualne.json` | aktuální nabídka |
| `data/<podnik>/stav.json` | kdy hlídač naposled úspěšně běžel, chyby v řadě, nečinnost |
| `data/<podnik>/posledni_chyba.html` | snímek stránky, když se seznam nepodařilo najít |
| `data/podniky.json` | seznam podniků pro stránku (zapisuje ho skript) |
| `aliasy.json` | ruční opravy pro přehled (sloučení pivovarů, opravy stylů), společné pro všechny podniky |
| `index.html` | přehled a analýza |

## Jak se hlídač chrání před chybami

- **Nic se neztratí kvůli parsování.** Každý stav webu jde nejdřív do `log.jsonl` jako surový
  text. Historii jde kdykoli přepočítat z logu (`python scrape.py --rebuild`), třeba po vylepšení
  parseru, a nic se neztratí.
- **Tři způsoby, jak najít nabídku:** přesný výběr prvků nastavený pro podnik, pak podle nadpisu
  „na čepu“, nakonec podle obsahu (stupně, procenta). Když se web změní, obvykle zabere další
  způsob a v Actions se objeví jen žluté varování.
- **Překlep ≠ nové pivo.** Když obsluha u běžícího piva opraví překlep, doplní chmel nebo smaže
  čárku, pivo zůstane v historii jako jedno. Už rozparsované údaje se nepřepíšou prázdnými.
- **Nesmysly se nezapíšou.** Seznam musí vypadat jako piva. Menu nebo odkazy na sociální sítě se
  nikdy nedostanou do dat.
- **Krátký výpadek webu nevadí.** Dvě chyby po sobě se jen poznamenají, historie se nemění.
  Od třetí (~6 hodin) je běh červený a GitHub pošle e-mail.
- **Tiché selhání se pozná.** `stav.json` se aktualizuje jednou denně, takže stránka pozná, že
  hlídač neběží. Když se nabídka přes týden nezmění, ukáže stránka upozornění. Denní commit
  zároveň drží repo aktivní, aby GitHub plánované běhy nevypnul.

U Pivnice Clock se bere jen tabulka „Na čepu“ (ne lahve, plechovky ani jídlo), ceny se
ignorují, takže změna ceny se nepočítá jako změna piva. Pivovar se tam neuvádí, doplňuje se
„Clock“. Nealko a limonáda z čepu se ukládají taky; na stránce mají vlastní styl.

Časy jsou přesné zhruba na 2 hodiny a odpovídají změně na webu, ne na výčepu.
Hlídač běží zhruba od 6:00 do půlnoci.

## Nastavení

1. Nahraj obsah do repa na GitHubu (včetně skryté složky `.github`).
2. *Settings → Actions → General → Workflow permissions* → **Read and write permissions**.
3. *Settings → Pages* → Deploy from a branch → `main`, `/ (root)`. Stránka pak běží na
   `https://TVUJ-UCET.github.io/NAZEV-REPA/` (na bezplatném účtu musí být repo veřejné).
4. *Actions → Hlídač čepů → Run workflow* pro první běh.

E-maily o neúspěšných bězích: *GitHub → Settings → Notifications → Actions*.

## Přehledová stránka

Nahoře se přepíná podnik (volba se pamatuje v adrese, např. `…/#sedm`). Záložky **Přehled**
(teď na čepu, žebříčky pivovarů, stylů a síly), **Pivovary**, **Piva**, **Historie** (s exportem
do Excelu) a **Časová osa**. Filtry období, hledání a klik na pivovar nebo styl platí napříč
záložkami. Upozornění se objeví, když hlídač neběží, selhává nebo se nabídka dlouho nezměnila.

Názvy jako „Pivovar Clock“ a „Clock“ se sloučí samy. Podobné názvy (překlepy) stránka nabídne
v záložce Pivovary; sloučíš je v `aliasy.json`:

```json
{
  "pivovary": { "Zichovek": "Zichovec" },
  "styly":    { "Muselo Stout": "Stout" }
}
```

## Přidání dalšího podniku

Do seznamu `PODNIKY` na začátku `scrape.py` přidej řádek s `id` (název složky, pak už neměnit),
`nazev` a `url`. Pokud web používá běžný nadpis „Dnes na čepu“, stačí to. Přesný výběr prvků
(`polozky`, `pole`) je volitelný, podívej se na sedm° jako příklad.

## Lokálně

```bash
pip install -r requirements.txt
python scrape.py              # jeden běh
python scrape.py --rebuild    # přepočítat historie.csv z logu
```
