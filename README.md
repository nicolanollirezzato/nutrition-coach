# Monitoraggio Azioni con Alert Telegram

Strumento automatico che controlla titoli azionari ogni 5 minuti e invia
un alert su Telegram quando una checklist di segnali (prezzo, volume,
medie mobili, notizie) supera una soglia configurabile. Include anche
un'analisi separata sulle notizie di materie prime e un report periodico
di autovalutazione sull'accuratezza delle previsioni passate.

⚠️ **Disclaimer**: questo è uno strumento informativo basato su regole
semplici. Non è consulenza finanziaria, non prevede il futuro e non
garantisce nulla. Le decisioni di investimento restano sempre tue.

---

## 1. Crea il bot Telegram

1. Su Telegram, cerca **@BotFather** e avvia una chat.
2. Invia `/newbot` e segui le istruzioni (nome e username del bot).
3. BotFather ti darà un **token** tipo `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxx`. Salvalo.
4. Cerca il tuo nuovo bot su Telegram e invia un qualsiasi messaggio (es. "ciao") per attivare la chat.
5. Recupera il tuo **chat_id** aprendo nel browser (sostituendo il token):
   ```
   https://api.telegram.org/bot<IL_TUO_TOKEN>/getUpdates
   ```
   Nel JSON di risposta cerca `"chat":{"id": ...}` — quel numero è il tuo chat_id.

## 2. Crea il repository su GitHub

1. Crea un nuovo repository (può essere privato) su GitHub.
2. Carica tutti i file "piatti" di questo progetto: `main.py`, `common.py`,
   `report.py`, `config.yaml`, `requirements.txt`.
3. Crea i due file di workflow **manualmente tramite "Add file > Create new
   file"** (non trascinandoli, per evitare che il browser salti la cartella
   nascosta `.github`): `.github/workflows/monitor.yml` e
   `.github/workflows/report.yml`, incollando il contenuto che trovi in
   questo progetto.

## 3. Configura i "Secrets" del repository

Su GitHub: **Settings → Secrets and variables → Actions → New repository secret**

Aggiungi due secret:
- `TELEGRAM_BOT_TOKEN` → il token ottenuto da BotFather
- `TELEGRAM_CHAT_ID` → il chat_id ottenuto al punto 1.5

## 4. Personalizza `config.yaml`

- Modifica la lista `tickers` con i titoli fissi che vuoi seguire sempre
  (usa il formato Yahoo Finance, es. `AAPL`, `MSFT`, `NVDA`).
- Regola le soglie in `thresholds` secondo la tua sensibilità (soglie più
  basse = più alert, anche meno significativi).
- Personalizza `news_keywords` con termini rilevanti per i tuoi settori.

### Scouting automatico (sezione `scouting`)

Oltre alla watchlist fissa, il bot fa "scouting" sui titoli **S&P 500**
(mercato USA): ad ogni ciclo scarica in blocco i dati giornalieri di tutto
l'indice, individua i titoli con variazione % o volume più anomali del
momento (i "top mover"), e applica loro la stessa checklist dettagliata.
Ricevi così alert sia sui tuoi titoli fissi (📌 Watchlist) sia su nuove
opportunità trovate automaticamente (🔍 Scouting).

Parametri configurabili:
- `top_n`: quanti top mover analizzare in dettaglio ad ogni ciclo (default 10)
- `min_abs_change_pct`: variazione % giornaliera minima per essere "interessante"
- `min_volume_ratio`: volume minimo (corretto per l'orario) per essere "interessante"
- `min_intraday_range_pct`: range high-low di oggi minimo per essere "interessante"

**Nota su affidabilità e tempi**: la lista dei componenti S&P 500 viene
scaricata da Wikipedia ad ogni esecuzione; se il sito non è raggiungibile
il bot usa automaticamente una lista di riserva statica (meno aggiornata
ma funzionante). Lo scan dell'intero indice richiede una chiamata "bulk"
a Yahoo Finance che può richiedere qualche decina di secondi.

### Come viene usato il movimento intraday

Due correzioni rispetto alla prima versione, per non perdere movimenti
rapidi che il solo confronto "chiusura di oggi vs chiusura di ieri" perderebbe:

1. **Range high-low di oggi**: se un titolo ha un balzo (o crollo) intraday
   e poi torna quasi al punto di partenza prima del nostro prossimo check,
   il cambio % giornaliero non lo vede più — ma il range tra il massimo e il
   minimo di oggi sì. Questo è ora un fattore di scouting a sé stante.
2. **Volume corretto per l'orario**: il volume "di oggi" a metà mattina è
   fisiologicamente solo una frazione di una giornata intera. Invece di
   confrontarlo con la media di giornate complete (che lo farebbe sembrare
   sempre basso), lo confrontiamo con quanto ci si aspetterebbe fino a
   quest'ora, in base all'orario di mercato USA (9:30-16:00 ora di New York).
   Questa correzione si applica sia allo scouting sia alla checklist dettagliata.

### Azione suggerita negli alert

Ogni alert Telegram include ora una sezione "💡 Lettura dei segnali" che:
- Indica una direzione di massima (rialzista / ribassista / incerta) in base
  a quanti fattori attivati puntano in una direzione o nell'altra
- Elenca considerazioni di processo generiche per un eventuale acquisto o
  vendita (dimensionamento della posizione, stop-loss concettuale, ecc.)

**Scelta di design importante**: questa sezione NON include mai importi,
quantità di azioni o livelli di prezzo precisi da eseguire. Lo strumento
non conosce il tuo capitale, il tuo orizzonte temporale o la tua tolleranza
al rischio, quindi calcolare cifre specifiche sarebbe un consiglio
finanziario personalizzato che non può dare in modo responsabile. Ti dà il
ragionamento dietro il segnale; la decisione ed esecuzione restano tue.

## 5. Analisi Materie Prime (sezione `commodities`)

Indipendentemente dai singoli titoli, ad ogni ciclo il bot controlla anche
feed RSS specializzati in materie prime (petrolio, gas naturale, metalli
preziosi, metalli industriali). Se in una categoria trova almeno
`min_count_for_alert` notizie pertinenti nelle ultime `lookback_hours` ore,
invia un alert Telegram **separato** dedicato a quella categoria.

Personalizza in `config.yaml`:
- `categories`: aggiungi/modifica le parole chiave per ogni categoria
- `feeds`: aggiungi altri feed RSS specializzati se ne conosci di validi

**Nota**: come per le notizie sui singoli titoli, il matching è per parole
chiave, non analisi del sentiment — una notizia con "petrolio" nel titolo
può essere sia rialzista che ribassista per i prezzi, va sempre letta.
Verifica anche ogni tanto che gli URL dei feed configurati siano ancora
validi (i siti a volte cambiano struttura).

## 6. Autovalutazione: report giornaliero/settimanale

Il bot registra ogni alert inviato (ticker, direzione prevista, prezzo al
momento dell'alert) in uno storico (`data/alerts_log.jsonl`, salvato
automaticamente nel repository). Ad ogni ciclo, **prima** di generare nuovi
segnali, controlla se qualche previsione passata ha raggiunto le 24 ore
(configurabile in `tracking.horizon_hours`) e verifica se il prezzo si è
mosso nella direzione prevista, segnandola come corretta o errata.

Un **secondo workflow** (`report.yml`), indipendente dal monitoraggio ogni
5 minuti, gira **una volta al giorno** e invia su Telegram un riepilogo con:
- Statistiche sulle previsioni risolte nelle **ultime 24 ore**
- Statistiche sulle previsioni risolte negli **ultimi 7 giorni**

**Come viene definita una previsione "corretta"**: il prezzo si è mosso
nella direzione indicata (su per un alert rialzista, giù per uno
ribassista) entro l'orizzonte configurato, indipendentemente da quanto si
è mosso — non misura il guadagno potenziale, solo la direzione. Gli alert
con lettura "incerta" (nessuna direzione chiara nei fattori attivati) sono
esclusi dal calcolo, perché non c'era una previsione direzionale da verificare.

**Limite di semplificazione da tenere presente**: l'orizzonte di 24 ore è
in tempo reale (wall-clock), non "la prossima chiusura di borsa" — un
alert generato di venerdì pomeriggio, ad esempio, viene verificato durante
il weekend quando il mercato è chiuso, con il prezzo dell'ultima chiusura
disponibile. È una scelta di semplicità, non una misura perfettamente
allineata ai giorni di borsa.

Il file di log viene salvato automaticamente dal workflow di monitoraggio
(non serve crearlo a mano); viene anche "ripulito" periodicamente,
scartando le previsioni risolte più vecchie di 120 giorni.

### Segnali ripetuti nello stesso giorno: "aggiornamento", non duplicato

Se un titolo (watchlist o scouting) supera di nuovo la soglia più volte
nello stesso giorno di borsa, solo la **prima occorrenza** genera una
previsione nuova da tracciare. Le occorrenze successive nello stesso giorno
diventano un messaggio "🔄 Aggiornamento": mostrano il prezzo attuale e la
variazione rispetto al primo segnale, ma **non creano una seconda
previsione** — quella originale (direzione, prezzo di riferimento) resta
l'unica usata per il report di autovalutazione. Questo evita sia lo spam
di alert quasi identici sia la distorsione delle statistiche di accuratezza
(che altrimenti conterebbero più volte lo stesso movimento di mercato).

### Schema del log (per analisi future)

Ogni voce dello storico include, oltre a direzione/prezzo/esito:
- `factors`: elenco strutturato `{code, direction}` dei fattori specifici
  che hanno contribuito (es. `price_1d`, `volume`, `sma_cross`, `news`),
  utile in futuro per capire quali fattori sono più affidabili di altri
- `update_count`, `last_update_at`, `last_update_price`: tracciano quante
  volte un segnale si è "ripetuto" in giornata e con quale prezzo più recente

### Prezzo istantaneo e doppia variazione (correzione pre/after-market)

**Problema risolto**: i dati storici a barre (`tk.history()`), senza
l'opzione `prepost=True`, NON includono le contrattazioni pre-market e
after-hours — il prezzo (e il volume) restavano quindi "congelati"
all'ultima sessione regolare finché il mercato non riapriva. Questo non
riguardava solo il prezzo mostrato nei messaggi, ma **anche i fattori
stessi della checklist** ("Variazione giornaliera" e "Volume anomalo"),
che di conseguenza non si ricalcolavano correttamente da un ciclo
all'altro fuori dall'orario di mercato regolare.

**Correzione (due parti)**:
1. I dati intraday usati dalla checklist ora includono `prepost=True`, così
   riflettono le contrattazioni pre-market/after-hours invece di restare
   fermi all'ultima chiusura regolare. Questo corregge alla radice sia il
   fattore "Variazione giornaliera" sia il fattore "Movimento 5 minuti" —
   e di conseguenza anche il **punteggio checklist**, che ora si ricalcola
   correttamente ad ogni ciclo anche fuori orario di mercato regolare.
2. Il fattore "Volume anomalo" ora somma le barre intraday di **oggi**
   (che includono pre/after-market) invece di leggere la singola riga
   "oggi" dei dati giornalieri, che non si aggiornava fuori sessione regolare.
3. Il prezzo mostrato nei messaggi (separatamente) viene recuperato
   dall'endpoint di **quotazione** di Yahoo Finance (campo `marketState` +
   prezzo pre-market/after-hours/regolare dedicato), per un valore ancora
   più istantaneo di quanto forniscano le barre a 5 minuti.

Ogni messaggio mostra **due variazioni distinte**:
- **Variazione da apertura**: rispetto al prezzo di apertura della sessione
  regolare odierna (presente sia nel primo segnale che negli aggiornamenti)
- **Variazione dall'ultimo rilevamento**: solo negli aggiornamenti, rispetto
  all'ultima volta che quel titolo è stato analizzato in giornata — è un
  confronto "incrementale", non cumulato dall'inizio della giornata

Se la quotazione istantanea non fosse disponibile per qualche motivo (rete,
ticker non riconosciuto), il bot ripiega automaticamente sul prezzo dei dati
storici, con un avviso nei log dell'esecuzione.

**Limite residuo non completamente verificabile**: non è possibile
escludere che, nei primissimi minuti prima che Yahoo crei la riga "di oggi"
nei dati giornalieri, il valore di apertura (`today_open`) usato come
riferimento per "Variazione da apertura" rifletta ancora quello di ieri
anziché quello odierno. È un caso limite difficile da verificare senza
osservare il comportamento durante una vera sessione di pre-market.

### Un titolo scovato non "sparisce" più se esce dalla top-10

**Problema risolto**: lo scouting rifà la scansione dell'intero S&P 500 ad
ogni ciclo e prende solo i "top N" movimenti di quel momento specifico. Un
titolo trovato al mattino poteva quindi uscire dalla top-10 in un ciclo
successivo (perché altri titoli si muovevano di più in quel momento) e
smettere di ricevere aggiornamenti, pur avendo ancora una previsione
aperta in giornata da monitorare.

**Correzione**: ad ogni ciclo, oltre alla watchlist fissa e ai top mover
di *questo* scouting, vengono ricontrollati anche tutti i titoli con una
previsione ancora aperta **oggi** (anche se non più tra i top mover del
momento). Se il loro punteggio scende sotto soglia, semplicemente non
arriva un nuovo messaggio quel ciclo (comportamento corretto) — ma non
vengono più "persi" fino a fine giornata o fino alla risoluzione a 24 ore.

**Nota sui costi**: nei giorni con molta attività di mercato, questo può
far crescere il numero di titoli controllati ad ogni ciclo (uno in più per
ogni nuovo titolo scovato durante la giornata, mantenuto fino a fine
giornata), quindi qualche chiamata a Yahoo Finance in più con il passare
delle ore. Per l'uso personale a cui è pensato lo strumento non dovrebbe
essere un problema, ma è bene saperlo.

### Affidabilità: errori Yahoo Finance ora visibili, non più silenziosi

**Problema risolto**: yfinance usa una cache SQLite interna che, quando si
scaricano molti titoli in parallelo (`threads=True`), può bloccarsi con
errori `database is locked` — un problema noto e documentato della
libreria, che si presenta in particolare su Ubuntu (il sistema operativo
usato da GitHub Actions). Quando questo accadeva durante l'analisi
dettagliata di un titolo, l'errore veniva **inghiottito silenziosamente**:
il titolo risultava con "Punteggio totale: 0", identico in tutto e per
tutto a un titolo genuinamente calmo, senza nessuna traccia nei log.

**Correzione**:
1. Le chiamate "bulk" (scouting e risoluzione delle previsioni) ora usano
   `threads=False`: più lente ma non soggette al blocco del database.
2. Ogni fallimento nel recupero dati per un singolo titolo viene ora
   **stampato esplicitamente nei log** dell'esecuzione (cercalo con
   `[ERRORE]` o `[ATTENZIONE]`), invece di produrre silenziosamente un
   "nessun segnale" indistinguibile da un mercato davvero calmo.

**Nota sui tempi**: disattivare il parallelismo rende lo scouting
sull'intero S&P 500 più lento (da pochi secondi a probabilmente qualche
decina di secondi in più). Se in futuro dovesse risultare troppo lento per
la cadenza a 5 minuti, si può valutare un compromesso (es. un numero
limitato di thread invece di disattivarli del tutto).

### Nota minore: lista S&P 500 da Wikipedia bloccata (403)

Nei log potresti vedere "Impossibile scaricare la lista S&P 500 da
Wikipedia: HTTP Error 403 Forbidden" — Wikipedia blocca le richieste che
non sembrano provenire da un browser. Non è un problema bloccante: lo
strumento usa automaticamente la lista di riserva statica (50 titoli
principali), quindi lo scouting continua a funzionare, solo su un
universo più ristretto e meno aggiornato nel tempo rispetto all'intero
indice. Se in futuro vuoi che la lista live torni a funzionare, si può
correggere aggiungendo un User-Agent alla richiesta.

## 7. Attiva entrambi i workflow

1. Vai nella tab **Actions** del repository.
2. Se richiesto, abilita i workflow.
3. Dovresti vedere DUE workflow nell'elenco: **"Monitoraggio Azioni"** (ogni 5
   minuti) e **"Report Autovalutazione"** (una volta al giorno). Puoi avviarli
   entrambi manualmente per un primo test da Actions → [nome workflow] →
   "Run workflow" (per il report, la prima volta probabilmente dirà che non
   ci sono ancora dati sufficienti: è normale, servono alert già risolti).

## Note importanti

- **Frequenza reale**: GitHub Actions esegue i cron "best effort": nei
  momenti di alto carico può ritardare di qualche minuto rispetto ai 5 previsti.
- **Repository inattivi**: GitHub disabilita automaticamente i workflow
  schedulati dopo 60 giorni senza attività sul repository. Basta un
  commit o un avvio manuale per riattivarli.
- **Dati**: i prezzi arrivano da Yahoo Finance tramite la libreria
  `yfinance` (gratuita, con qualche minuto di ritardo tipico per i dati
  intraday, non tick-by-tick).
- **Le news** vengono lette da feed RSS pubblici: puoi aggiungerne altri
  modificando `rss_feeds` in `config.yaml`.
- **Costi**: GitHub Actions è gratuito fino a un certo numero di minuti/mese
  per repository pubblici e privati (piano free), sufficiente per questo uso.
- **Commit automatici**: vedrai comparire nella cronologia del repository
  commit automatici firmati "github-actions[bot]" che aggiornano
  `data/alerts_log.jsonl` — è il meccanismo con cui lo strumento "ricorda"
  le previsioni passate tra un'esecuzione e l'altra. Sono normali e
  avvengono solo quando c'è un nuovo alert o una previsione da risolvere,
  non ad ogni singolo ciclo di 5 minuti.
- **Aggiornare un file di workflow esistente**: se devi modificare
  `monitor.yml` o `report.yml` dopo la prima creazione, apri il file su
  GitHub, clicca l'icona a forma di matita (Edit), sostituisci il
  contenuto e fai Commit — stessa logica di "Add file > Create new file",
  ma su un file già esistente.

## Come leggere gli alert

Ogni messaggio Telegram di alert su un titolo mostra:
- Il ticker e il prezzo attuale
- Il punteggio totale della checklist
- L'elenco dei motivi che hanno fatto scattare l'alert
- Eventuali notizie correlate trovate
- Una sezione "💡 Lettura dei segnali" con la direzione di massima
  (rialzista/ribassista/incerta) e considerazioni generiche di processo

Il punteggio è la somma di 4 possibili fattori (prezzo a 5 minuti, prezzo
giornaliero, volume anomalo, incrocio medie mobili) + 1 fattore news.
Modifica `alert_score_threshold` in `config.yaml` per decidere quanto
essere selettivo.

Riceverai inoltre, separatamente:
- Alert **🌍 Materie Prime** quando ci sono notizie rilevanti su petrolio,
  gas o metalli (indipendenti dai singoli titoli)
- Un **📊 Report di autovalutazione** una volta al giorno, con le
  statistiche di accuratezza delle previsioni degli ultimi 1 e 7 giorni

## Roadmap (idee per il futuro, non ancora implementate)

Idee discusse e tenute da parte per non perderle, da valutare quando si
vorrà svilupparle:

- **Aggiornamenti ravvicinati sui titoli attivi in giornata**: oltre al
  meccanismo di "aggiornamento" già presente (che scatta solo se il titolo
  ri-supera la soglia della checklist), inviare un aggiornamento periodico
  sulla fluttuazione di prezzo (es. ogni 5 minuti) per i titoli con un
  alert attivo in giornata, anche senza un nuovo superamento soglia. Punti
  da definire prima di implementarla: per quanto tempo tenere un titolo
  "sotto osservazione ravvicinata" dopo l'alert iniziale (fino a chiusura
  mercato? un numero fisso di ore?) e come evitare che diventi eccessivo
  se il titolo resta volatile tutto il giorno.

- **Agente di analisi delle performance**: uno script/report che legga lo
  storico e lo scomponga per fattore attivato (usando il campo `factors`,
  già presente nello schema), direzione e provenienza (watchlist/scouting),
  da portare in una conversazione con Claude per discutere eventuali
  modifiche alle soglie o ai fattori della checklist. Limiti da tenere
  presente quando lo si userà: con il volume di alert di uno strumento
  personale, servono probabilmente settimane/mesi di dati prima che una
  scomposizione per fattore sia statisticamente significativa (un buon
  report dovrebbe sempre mostrare anche la dimensione del campione, non
  solo la percentuale); inoltre questo tipo di analisi è ragionamento
  euristico su dati osservati live, non un backtest rigoroso su storico —
  per quello servirebbe un esercizio diverso (simulare le regole su dati
  passati, non osservare gli esiti via via che arrivano).
