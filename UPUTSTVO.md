# TCW220 Monitor – uputstvo

Program prikazuje trenutna mjerenja Teracom TCW220 dataloggera (osvježavanje svakih 30 s),
čita snimljene zapise s uređaja i snima ih u CSV fajl koji se otvara u Excelu.

## Pokretanje (korisnici)
Dva fajla u istom folderu: `TCW220Monitor.exe` i `config.json`. Dvoklik na exe.
Ako `config.json` ne postoji, program napravi primjer i traži da ga uredite.

## Dodavanje novog dataloggera
Otvorite `config.json` u Notepadu, kopirajte jedan blok između `{ ... }` u listi
`dataloggers`, odvojite ga zarezom od prethodnog i promijenite `name` i `host`.
`host` može biti IP adresa ili ime (npr. `datalogger2.firma.local`).
Nakon izmjene ponovo pokrenite program.

| Polje | Značenje |
|---|---|
| `refresh_seconds` | Razmak osvježavanja u sekundama (najmanje 5) |
| `archive_folder` | Folder za CSV fajlove (relativno uz exe ili puna putanja) |
| `decimal_separator` | `,` ili `.` – za prikaz i CSV |
| `record_live` | `true` = program dodatno upisuje svako očitanje u `uzivo_<naziv>.csv` |
| `name`, `host`, `port` | Naziv na ekranu, adresa uređaja, port (obično 80) |
| `username`, `password` | Prijava na uređaj |
| `enabled` | `false` = datalogger se ne očitava |
| `groups` | Šta prikazati: `sensors`, `analog`, `digital`, `relays` |
| `hide_channels` | Sakrij mjerenja, npr. `["AI1","AI2"]` |
| `channel_names` | Vlastita imena, npr. `{"S1.1": "Temperatura"}` |

Oznake mjerenja: `S1.1` = senzor 1, prva veličina (temperatura), `S1.2` = druga veličina (vlaga),
`AI1`, `AI2` = analogni ulazi, `DI1`, `DI2` = digitalni ulazi, `R1`, `R2` = releji.
Lozinka je u config.json zapisana običnim tekstom, pa fajl dijelite samo s korisnicima programa.

## Arhiva
Dugme **Arhiva…** → odaberite datalogger, period (dd.mm.gggg) i mjerenja → **Učitaj iz uređaja**
→ **Spremi u CSV…**. Na uređaju mora biti uključen Logger (SERVICES → Logger).
Uređaj čuva do 70 000 zapisa; kad se napuni, najstariji se brišu, pa CSV povremeno snimite.

## Pravljenje exe fajla (jednom, na računaru s Pythonom)
Dvoklik na `build_exe.bat`. Rezultat je u folderu `dist`.
Ako antivirus označi exe, dodajte izuzetak za folder (poznat problem s PyInstallerom).

## Problemi
- Dnevnik rada: `tcw220_monitor.log` pored exe fajla.
- "Pogrešno korisničko ime ili lozinka" – provjerite polja u config.json.
- "Nije moguće spojiti se" – provjerite IP/ime i da je računar u istoj mreži; probajte adresu u browseru.
