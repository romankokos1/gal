[README.md](https://github.com/user-attachments/files/32806606/README.md)
# Hlídač čepů – Galerie piva

Automaticky sleduje sekci **„Dnes na čepu“** na [galeriepiva.cz](https://www.galeriepiva.cz/),
ukládá historii toho, co se kdy narazilo a dočepovalo, a zobrazuje ji na přehledové stránce.

## Soubory

| soubor | obsah |
|---|---|
| `data/log.jsonl` | **každý stav čepů přesně tak, jak byl na webu**. Základ všeho, nic se v něm nepřepisuje. |
| `data/historie.csv` | každé naražení s datem `narazeno` / `docepovano` (odvozené z logu) |
| `data/aktualne.json` | aktuální nabídka |
| `data/stav.json` | kdy hlídač naposled úspěšně běžel, chyby v řadě, nečinnost |
| `data/posledni_chyba.html` | snímek stránky, když se seznam nepodařilo najít |
| `aliasy.json` | ruční opravy pro přehled (sloučení pivovarů, opravy stylů) |
| `index.html` | přehled a analýza |

## Jak se hlídač chrání před chybami

- **Nic se neztratí kvůli parsování.** Každý stav webu jde nejdřív do `log.jsonl` jako surový
  text. Historii jde kdykoli přepočítat z logu (`python scrape.py --rebuild`), třeba po vylepšení
  parseru, a nic se neztratí.
- **Překlep ≠ nové pivo.** Když obsluha u běžícího piva opraví překlep, doplní chmel nebo smaže
  čárku, pivo zůstane v historii jako jedno. Už rozparsované údaje se nepřepíšou prázdnými.
- **Nesmysly se nezapíšou.** Seznam musí vypadat jako piva (stupně, procenta). Menu nebo odkazy
  na sociální sítě se nikdy nedostanou do dat.
- **Krátký výpadek webu nevadí.** Dvě chyby po sobě se jen poznamenají (běh zůstane zelený),
  historie se nemění. Od třetí (~6 hodin) je běh červený a přijde upozornění do Telegramu,
  po obnovení zpráva „zase funguje“.
- **Tiché selhání se pozná.** `stav.json` se aktualizuje jednou denně, takže stránka pozná, že
  hlídač neběží. Když se nabídka nezmění 10 dní, přijde upozornění, že web možná nikdo
  neaktualizuje. Denní commit zároveň drží repo aktivní, aby GitHub plánované běhy nevypnul.

Časy jsou přesné zhruba na 2 hodiny a odpovídají změně na webu, ne na výčepu.

## Nastavení

1. Nahraj obsah do repa na GitHubu (včetně skryté složky `.github`).
2. *Settings → Actions → General → Workflow permissions* → **Read and write permissions**.
3. *Settings → Pages* → Deploy from a branch → `main`, `/ (root)`. Stránka pak běží na
   `https://TVUJ-UCET.github.io/NAZEV-REPA/` (na bezplatném účtu musí být repo veřejné).
4. (Volitelně) *Settings → Secrets and variables → Actions*: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
5. *Actions → Hlídač čepů → Run workflow* pro první běh.

## Přehledová stránka

Záložky **Přehled** (teď na čepu, žebříčky pivovarů, stylů a síly), **Pivovary**, **Piva**,
**Historie** (s exportem do Excelu) a **Časová osa**. Filtry období, hledání a klik na pivovar
nebo styl platí napříč záložkami. Nahoře se objeví upozornění, když hlídač neběží, selhává nebo
se nabídka dlouho nezměnila.

Názvy jako „Pivovar Clock“ a „Clock“ se sloučí samy. Podobné názvy (překlepy) stránka nabídne
v záložce Pivovary; sloučíš je v `aliasy.json`:

```json
{
  "pivovary": { "Zichovek": "Zichovec" },
  "styly":    { "Muselo Stout": "Stout" }
}
```

U stylů stačí kus textu položky (třeba název piva) a styl, který se má použít místo
automatického odhadu. Surová data se aliasy nemění.

## Lokálně

```bash
pip install -r requirements.txt
python scrape.py              # jeden běh
python scrape.py --rebuild    # přepočítat historie.csv z logu
```

`index.html` jde otevřít i lokálně, `historie.csv` pak vybereš ručně.
