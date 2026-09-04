#!/usr/bin/env python3
"""
Strumento di monitoraggio azioni con checklist di valutazione.

Ogni esecuzione (pensata per girare ogni 5 minuti via GitHub Actions):
  1. Risolve le previsioni passate che hanno raggiunto l'orizzonte configurato
     (confronta la direzione prevista con il prezzo attuale, per l'autovalutazione)
  2. Scarica dati di prezzo/volume per i ticker configurati (yfinance)
  3. Calcola una checklist di segnali: variazione prezzo, volume anomalo,
     incrocio medie mobili, notizie rilevanti recenti (RSS)
  4. Analizza separatamente le notizie sulle materie prime (petrolio, gas, metalli)
  5. Se il punteggio totale supera la soglia, invia un messaggio Telegram con
     un commento ragionato sulla direzione, e registra la previsione per
     poterla verificare in futuro (report.py)

NOTA IMPORTANTE: questo strumento genera segnali puramente informativi
basati su regole semplici. NON è consulenza finanziaria e non garantisce
nulla sui movimenti di mercato. Le decisioni di acquisto/vendita restano
sempre e solo tue.
"""

import feedparser
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta, timezone

from common import (
    load_config,
    trading_day_fraction_elapsed,
    send_telegram_message,
    NY_TZ,
    load_log,
    save_log,
)

# Lista di riserva se il download da Wikipedia dei componenti S&P 500 fallisse
# (sottoinsieme di grandi titoli USA, aggiornabile a mano quando serve)
SP500_FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "BRK-B", "AVGO",
    "TSLA", "LLY", "JPM", "V", "UNH", "XOM", "MA", "COST", "HD", "PG", "NFLX",
    "JNJ", "BAC", "CRM", "ABBV", "MRK", "ORCL", "CVX", "KO", "AMD", "PEP",
    "ADBE", "WMT", "TMO", "MCD", "CSCO", "ACN", "LIN", "ABT", "IBM", "GE",
    "DHR", "PM", "TXN", "INTU", "QCOM", "CAT", "VZ", "AXP", "AMGN", "NOW",
]


def get_sp500_universe() -> list:
    """Recupera l'elenco dei ticker S&P 500 da Wikipedia; usa una lista di
    riserva statica se il download fallisce (rete assente, pagina cambiata, ecc.)."""
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        symbols = tables[0]["Symbol"].tolist()
        symbols = [s.replace(".", "-") for s in symbols]
        if len(symbols) > 50:
            return symbols
    except Exception as e:
        print(f"[ATTENZIONE] Impossibile scaricare la lista S&P 500 da Wikipedia: {e}")

    print("[INFO] Uso la lista di riserva statica per lo scouting.")
    return SP500_FALLBACK


def scout_top_movers(cfg: dict) -> list:
    """Scarica dati giornalieri in blocco per l'universo configurato e
    individua i titoli con variazione %, volume o range intraday più anomali."""
    scouting_cfg = cfg.get("scouting", {})
    if not scouting_cfg.get("enabled"):
        return []

    universe_name = scouting_cfg.get("universe", "sp500")
    if universe_name != "sp500":
        print(f"[ATTENZIONE] Universo di scouting '{universe_name}' non supportato, salto lo scouting.")
        return []

    universe = get_sp500_universe()
    print(f"[INFO] Scouting su {len(universe)} titoli dell'universo {universe_name}...")

    try:
        # threads=False (non il default): con molti titoli in parallelo,
        # yfinance usa una cache SQLite interna che su Ubuntu/GitHub Actions
        # può bloccarsi con errori "database is locked", causando fallimenti
        # silenziosi per alcuni titoli. Più lento ma affidabile.
        data = yf.download(
            tickers=universe,
            period="25d",
            interval="1d",
            group_by="ticker",
            threads=False,
            progress=False,
        )
    except Exception as e:
        print(f"[ERRORE] Download bulk per lo scouting fallito: {e}")
        return []

    day_fraction = trading_day_fraction_elapsed()

    candidates = []
    falliti = []
    for ticker in universe:
        try:
            df = data[ticker] if isinstance(data.columns, pd.MultiIndex) else data
            df = df.dropna()
            if len(df) < 2:
                falliti.append(ticker)
                continue

            last_close = float(df["Close"].iloc[-1])
            prev_close = float(df["Close"].iloc[-2])
            change_pct = (last_close - prev_close) / prev_close * 100

            today_open = float(df["Open"].iloc[-1])
            today_high = float(df["High"].iloc[-1])
            today_low = float(df["Low"].iloc[-1])
            intraday_range_pct = (today_high - today_low) / today_open * 100 if today_open > 0 else 0

            avg_full_day_volume = df["Volume"].iloc[:-1].mean()
            today_volume_so_far = float(df["Volume"].iloc[-1])
            expected_volume_by_now = avg_full_day_volume * day_fraction
            volume_ratio = (
                today_volume_so_far / expected_volume_by_now if expected_volume_by_now > 0 else 0
            )

            passes_change = abs(change_pct) >= scouting_cfg.get("min_abs_change_pct", 2.0)
            passes_volume = volume_ratio >= scouting_cfg.get("min_volume_ratio", 1.5)
            passes_range = intraday_range_pct >= scouting_cfg.get("min_intraday_range_pct", 3.0)

            if passes_change or passes_volume or passes_range:
                interest_score = abs(change_pct) + volume_ratio + intraday_range_pct
                candidates.append((ticker, interest_score, change_pct, volume_ratio, intraday_range_pct))
        except Exception as e:
            falliti.append(ticker)
            continue

    if falliti:
        print(f"[ATTENZIONE] Dati mancanti/non validi per {len(falliti)} titoli su {len(universe)} nello scouting: {falliti[:15]}{'...' if len(falliti) > 15 else ''}")

    candidates.sort(key=lambda x: x[1], reverse=True)
    top_n = scouting_cfg.get("top_n", 10)
    top_tickers = [c[0] for c in candidates[:top_n]]

    for t, score, chg, vol, rng in candidates[:top_n]:
        print(
            f"  [SCOUT] {t}: var {chg:+.2f}%, volume {vol:.1f}x atteso, "
            f"range oggi {rng:.1f}% -> interesse {score:.2f}"
        )

    return top_tickers


def get_instant_quote(ticker: str):
    """Recupera un prezzo il più possibile "istantaneo" per il ticker.

    A differenza di tk.history(), che senza prepost=True NON include
    pre-market/after-hours (il prezzo resta "congelato" all'ultima chiusura
    di sessione regolare finché il mercato non riapre), qui interroghiamo
    l'endpoint di QUOTAZIONE di Yahoo Finance, che espone separatamente:
      - marketState: "PRE" / "REGULAR" / "POST" / "POSTPOST" / "CLOSED"
      - preMarketPrice / postMarketPrice / regularMarketPrice

    Usiamo il prezzo del segmento di mercato effettivamente in corso ora.
    Restituisce None se non riusciamo a recuperare nulla (chi chiama questa
    funzione deve prevedere un fallback, es. il prezzo dai dati storici).
    """
    try:
        info = yf.Ticker(ticker).info
    except Exception as e:
        print(f"[ATTENZIONE] Impossibile recuperare la quotazione istantanea di {ticker}: {e}")
        return None

    if not info:
        return None

    market_state = info.get("marketState", "")

    if market_state == "PRE" and info.get("preMarketPrice") is not None:
        return float(info["preMarketPrice"])
    if market_state in ("POST", "POSTPOST") and info.get("postMarketPrice") is not None:
        return float(info["postMarketPrice"])
    if info.get("regularMarketPrice") is not None:
        return float(info["regularMarketPrice"])

    return None


def analyze_price_and_volume(ticker: str, cfg: dict) -> dict:
    """Scarica dati intraday e giornalieri e calcola i segnali quantitativi.

    Oltre a "reasons" (elenco testuale per l'alert), popola anche
    "bullish_signals" e "bearish_signals": servono al commento ragionato per
    capire se i fattori attivati puntano più verso un rialzo o un ribasso.
    Il volume anomalo non viene conteggiato come direzionale (può
    accompagnare sia un rialzo che un ribasso), resta solo in "reasons".
    """
    result = {
        "ticker": ticker,
        "score": 0,
        "reasons": [],
        "bullish_signals": [],
        "bearish_signals": [],
        "factors": [],  # elenco strutturato {code, direction} per analisi future per-fattore
        "last_price": None,
        "today_open": None,
    }

    try:
        tk = yf.Ticker(ticker)

        # prepost=True: include pre-market/after-hours, altrimenti i dati
        # restano "congelati" all'ultima sessione regolare fuori dall'orario
        # 9:30-16:00 ET (causa della staleness segnalata).
        intraday = tk.history(period="2d", interval="5m", prepost=True)
        daily = tk.history(period="60d", interval="1d")

        if intraday.empty or daily.empty:
            result["reasons"].append("Dati insufficienti da Yahoo Finance.")
            return result

        last_price = float(intraday["Close"].iloc[-1])
        result["last_price"] = round(last_price, 2)

        # --- Fattore 1: variazione prezzo ultimi 5 minuti ---
        if len(intraday) >= 2:
            prev_price = float(intraday["Close"].iloc[-2])
            change_5m_pct = (last_price - prev_price) / prev_price * 100
            if abs(change_5m_pct) >= cfg["thresholds"]["price_change_5m_pct"]:
                result["score"] += 1
                direzione = "rialzo" if change_5m_pct > 0 else "ribasso"
                detail = f"Movimento rapido: {change_5m_pct:+.2f}% negli ultimi 5 min ({direzione})."
                result["reasons"].append(detail)
                fdir = "bullish" if change_5m_pct > 0 else "bearish"
                (result["bullish_signals"] if change_5m_pct > 0 else result["bearish_signals"]).append(detail)
                result["factors"].append({"code": "price_5m", "direction": fdir})

        # --- Fattore 2: variazione prezzo giornaliera ---
        today_open = float(daily["Open"].iloc[-1])
        result["today_open"] = today_open
        change_1d_pct = (last_price - today_open) / today_open * 100
        if abs(change_1d_pct) >= cfg["thresholds"]["price_change_1d_pct"]:
            result["score"] += 1
            direzione = "rialzo" if change_1d_pct > 0 else "ribasso"
            detail = f"Variazione giornaliera: {change_1d_pct:+.2f}% ({direzione})."
            result["reasons"].append(detail)
            fdir = "bullish" if change_1d_pct > 0 else "bearish"
            (result["bullish_signals"] if change_1d_pct > 0 else result["bearish_signals"]).append(detail)
            result["factors"].append({"code": "price_1d", "direction": fdir})

        # --- Fattore 3: volume anomalo (corretto per l'orario di borsa) ---
        # Sommiamo le barre di OGGI dalla serie intraday (ora con
        # prepost=True): la riga "oggi" dei dati giornalieri non si
        # aggiornava fuori dalla sessione regolare, restando congelata.
        last_bar_date = intraday.index[-1].date()
        today_intraday = intraday[intraday.index.date == last_bar_date]
        today_volume = float(today_intraday["Volume"].sum())

        avg_volume = daily["Volume"].iloc[:-1].mean()  # media storica di sessione regolare, invariata
        expected_volume_by_now = avg_volume * trading_day_fraction_elapsed()
        if expected_volume_by_now > 0:
            volume_ratio = today_volume / expected_volume_by_now
            if volume_ratio >= cfg["thresholds"]["volume_spike_ratio"]:
                result["score"] += 1
                result["reasons"].append(
                    f"Volume anomalo: {volume_ratio:.1f}x rispetto all'atteso per quest'ora."
                )
                result["factors"].append({"code": "volume", "direction": "neutral"})

        # --- Fattore 4: incrocio medie mobili (SMA breve vs lunga) ---
        short_p = cfg["thresholds"]["sma_short"]
        long_p = cfg["thresholds"]["sma_long"]
        if len(daily) >= long_p + 1:
            sma_short_series = daily["Close"].rolling(short_p).mean()
            sma_long_series = daily["Close"].rolling(long_p).mean()

            sma_short_today, sma_long_today = sma_short_series.iloc[-1], sma_long_series.iloc[-1]
            sma_short_yday, sma_long_yday = sma_short_series.iloc[-2], sma_long_series.iloc[-2]

            crossed_up = sma_short_yday <= sma_long_yday and sma_short_today > sma_long_today
            crossed_down = sma_short_yday >= sma_long_yday and sma_short_today < sma_long_today

            if crossed_up:
                result["score"] += 1
                detail = f"Incrocio rialzista: media mobile {short_p}gg ha superato quella a {long_p}gg."
                result["reasons"].append(detail)
                result["bullish_signals"].append(detail)
                result["factors"].append({"code": "sma_cross", "direction": "bullish"})
            elif crossed_down:
                result["score"] += 1
                detail = f"Incrocio ribassista: media mobile {short_p}gg è scesa sotto quella a {long_p}gg."
                result["reasons"].append(detail)
                result["bearish_signals"].append(detail)
                result["factors"].append({"code": "sma_cross", "direction": "bearish"})

    except Exception as e:
        print(f"[ERRORE] Analisi quantitativa di {ticker} fallita: {e}")
        result["reasons"].append(f"Errore durante l'analisi quantitativa: {e}")

    return result


def check_relevant_news(ticker: str, cfg: dict) -> dict:
    """Controlla i feed RSS configurati per notizie recenti con parole chiave rilevanti
    per il singolo titolo."""
    result = {"score": 0, "reasons": [], "matched_titles": [], "factors": []}

    lookback = timedelta(hours=cfg["thresholds"]["news_lookback_hours"])
    now = datetime.now(timezone.utc)
    keywords = [k.lower() for k in cfg["news_keywords"]]
    ticker_name = ticker.split(".")[0].lower()

    matches = []
    for feed_info in cfg["rss_feeds"]:
        try:
            feed = feedparser.parse(feed_info["url"])
        except Exception as e:
            print(f"[ATTENZIONE] Impossibile leggere il feed {feed_info['name']}: {e}")
            result["reasons"].append(f"Impossibile leggere il feed {feed_info['name']}: {e}")
            continue

        for entry in feed.entries:
            title = getattr(entry, "title", "") or ""
            title_lower = title.lower()

            published = getattr(entry, "published_parsed", None)
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if now - pub_dt > lookback:
                    continue

            keyword_hit = any(k in title_lower for k in keywords)
            ticker_hit = ticker_name in title_lower

            if keyword_hit or ticker_hit:
                matches.append(f"[{feed_info['name']}] {title}")

    if len(matches) >= cfg["thresholds"]["news_min_count"]:
        result["score"] += 1
        result["reasons"].append(
            f"Trovate {len(matches)} notizie potenzialmente rilevanti nelle ultime "
            f"{cfg['thresholds']['news_lookback_hours']} ore."
        )
        result["matched_titles"] = matches[:5]
        # Direzione "neutral": il matching per parole chiave non permette di
        # stabilire se la notizia sia positiva o negativa per il prezzo.
        result["factors"].append({"code": "news", "direction": "neutral"})

    return result


def check_commodity_news(cfg: dict) -> dict:
    """Analisi specializzata sulle notizie relative alle materie prime
    (petrolio, gas, metalli preziosi/industriali, ...), indipendente dai
    singoli titoli della watchlist/scouting.

    NOTA: come per le notizie sui titoli, il matching è per parole chiave,
    NON per sentiment: una notizia può menzionare "petrolio" in un contesto
    sia positivo che negativo per i prezzi.
    """
    commodities_cfg = cfg.get("commodities", {})
    if not commodities_cfg.get("enabled"):
        return {"triggered": {}}

    lookback = timedelta(hours=commodities_cfg.get("lookback_hours", 6))
    now = datetime.now(timezone.utc)
    min_count = commodities_cfg.get("min_count_for_alert", 2)

    # Raccogliamo una sola volta tutte le entry recenti da tutti i feed,
    # poi le confrontiamo con le parole chiave di ciascuna categoria.
    recent_entries = []
    for feed_info in commodities_cfg.get("feeds", []):
        try:
            feed = feedparser.parse(feed_info["url"])
        except Exception as e:
            print(f"[ATTENZIONE] Impossibile leggere il feed materie prime {feed_info['name']}: {e}")
            continue

        for entry in feed.entries:
            title = getattr(entry, "title", "") or ""
            published = getattr(entry, "published_parsed", None)
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if now - pub_dt > lookback:
                    continue
            recent_entries.append((feed_info["name"], title))

    triggered = {}
    for category, cat_cfg in commodities_cfg.get("categories", {}).items():
        keywords = [k.lower() for k in cat_cfg.get("keywords", [])]
        matches = [
            f"[{feed_name}] {title}"
            for feed_name, title in recent_entries
            if any(k in title.lower() for k in keywords)
        ]
        if len(matches) >= min_count:
            triggered[category] = matches[:5]

    return {"triggered": triggered}


COMMODITY_LABELS = {
    "petrolio": "🛢️ Petrolio",
    "gas_naturale": "🔥 Gas naturale",
    "metalli_preziosi": "🥇 Metalli preziosi",
    "metalli_industriali": "⚙️ Metalli industriali",
}


def build_commodity_alert_message(triggered: dict) -> str:
    lines = ["<b>🌍 Materie Prime — notizie rilevanti</b>", ""]
    for category, matches in triggered.items():
        label = COMMODITY_LABELS.get(category, category.replace("_", " ").capitalize())
        lines.append(f"<b>{label}</b>")
        for m in matches:
            lines.append(f"  • {m}")
        lines.append("")
    lines.append(
        "⚠️ Match per parole chiave, non per sentiment: leggi i titoli prima "
        "di interpretarli come positivi o negativi per i prezzi."
    )
    return "\n".join(lines)


def compute_bias(quant: dict) -> str:
    """Determina una direzione di massima (rialzista/ribassista/incerta) in
    base a quanti fattori attivati puntano in una direzione o nell'altra."""
    n_bull = len(quant.get("bullish_signals", []))
    n_bear = len(quant.get("bearish_signals", []))
    if n_bull > n_bear:
        return "rialzista"
    if n_bear > n_bull:
        return "ribassista"
    return "incerta"


def generate_action_commentary(quant: dict, news: dict) -> list:
    """Genera un commento ragionato sulla base dei fattori attivati.

    IMPORTANTE PER CHI MODIFICA QUESTO CODICE: questa funzione produce
    un'INTERPRETAZIONE dei segnali tecnici (rialzista/ribassista/incerta) con
    considerazioni di processo generiche, NON un ordine di trading. Non
    include mai importi, quantità di azioni o livelli di prezzo precisi da
    eseguire: lo strumento non conosce il capitale, l'orizzonte temporale o
    la tolleranza al rischio della persona, quindi non può calcolarli in
    modo responsabile. Chi usa lo strumento decide sempre in autonomia.
    """
    bullish = quant.get("bullish_signals", [])
    bearish = quant.get("bearish_signals", [])
    bias = compute_bias(quant)

    lines = ["", "<b>💡 Lettura dei segnali</b>"]

    if bias == "rialzista":
        lines.append("Direzione prevalente: <b>rialzista</b>.")
        lines.append("Fattori a supporto:")
        lines += [f"  • {r}" for r in bullish]
        if bearish:
            lines.append("In controtendenza:")
            lines += [f"  • {r}" for r in bearish]
        lines.append(
            "Se stessi valutando un ACQUISTO, di solito si ragiona su: livello "
            "di ingresso rispetto ai prezzi recenti (non necessariamente subito "
            "'a mercato'), dimensionamento della posizione in proporzione al "
            "tuo capitale e alla tua tolleranza al rischio, ed eventuale "
            "stop-loss impostato in anticipo sotto un supporto tecnico recente."
        )
    elif bias == "ribassista":
        lines.append("Direzione prevalente: <b>ribassista</b>.")
        lines.append("Fattori a supporto:")
        lines += [f"  • {r}" for r in bearish]
        if bullish:
            lines.append("In controtendenza:")
            lines += [f"  • {r}" for r in bullish]
        lines.append(
            "Se possiedi il titolo e stessi valutando una VENDITA (totale o "
            "parziale), di solito si ragiona su: se il calo sembra strutturale "
            "o transitorio, se alleggerire solo una parte della posizione, ed "
            "eventualmente un trailing stop per proteggere i guadagni già "
            "maturati. Se invece stavi valutando un acquisto, questi segnali "
            "suggeriscono cautela nell'ingresso ora."
        )
    else:
        lines.append("Direzione: <b>segnali contrastanti o poco chiari</b>.")
        lines.append(
            "I fattori attivati non danno una lettura univoca (es. volume "
            "anomalo senza una chiara direzione di prezzo, o segnali "
            "rialzisti e ribassisti in pari numero). In casi così, spesso "
            "conviene attendere ulteriori conferme prima di agire."
        )

    if news.get("matched_titles"):
        lines.append(
            "⚠️ Le notizie sono individuate per parole chiave, NON per "
            "sentiment: leggi i titoli prima di interpretarli come positivi "
            "o negativi (es. 'acquisizione' può riferirsi a un'acquisizione "
            "saltata, non conclusa)."
        )

    lines.append(
        "🚫 Non è un consiglio di investimento: è un'interpretazione "
        "automatica di regole tecniche semplici, che non conosce il tuo "
        "portafoglio né la tua tolleranza al rischio. Verifica sempre in "
        "autonomia o consulta un consulente finanziario abilitato."
    )

    return lines


def is_same_ny_day(iso_timestamp: str, reference: datetime) -> bool:
    """Confronta se un timestamp ISO cade nello stesso giorno di borsa
    (calendario di New York) del momento di riferimento."""
    try:
        ts = datetime.fromisoformat(iso_timestamp)
    except Exception:
        return False
    return ts.astimezone(NY_TZ).date() == reference.astimezone(NY_TZ).date()


def find_todays_entry(ticker: str, entries: list, now: datetime):
    """Cerca nello storico una previsione già registrata OGGI per questo
    ticker (a prescindere dalla provenienza watchlist/scouting): se c'è,
    un nuovo segnale sullo stesso titolo va trattato come "aggiornamento"
    invece che come previsione nuova, per non duplicare il tracciamento
    nel report di autovalutazione."""
    for e in entries:
        if e.get("ticker") == ticker and is_same_ny_day(e.get("timestamp", ""), now):
            return e
    return None


def get_active_today_tickers(entries: list, now: datetime, exclude: set) -> set:
    """Ritorna i ticker con una previsione di OGGI ancora non risolta, per
    continuare ad analizzarli anche se non rientrano più tra i "top mover"
    dello scouting in questo specifico ciclo. Senza questo, un titolo
    scovato stamattina smetterebbe di essere controllato non appena altri
    titoli si muovono di più, anche se ha ancora una previsione aperta oggi."""
    tickers = set()
    for e in entries:
        ticker = e.get("ticker")
        if not ticker or ticker in exclude or e.get("resolved"):
            continue
        if is_same_ny_day(e.get("timestamp", ""), now):
            tickers.add(ticker)
    return tickers


def format_open_variation_line(display_price: float, today_open):
    """Riga 'Variazione da apertura', calcolata sul prezzo istantaneo
    (non su quello potenzialmente non aggiornato dei dati storici)."""
    if today_open is None or not today_open:
        return None
    delta_open_pct = (display_price - today_open) / today_open * 100
    return f"Variazione da apertura: {delta_open_pct:+.2f}%"


def build_update_message(ticker: str, quant: dict, news: dict, source: str, original_entry: dict, display_price) -> str:
    """Messaggio "leggero" per un titolo già segnalato oggi: mostra il
    prezzo istantaneo e DUE variazioni distinte (da apertura e dall'ultimo
    rilevamento), SENZA generare una nuova previsione da tracciare (quella
    originale resta quella valida)."""
    label = "📌 Watchlist" if source == "watchlist" else "🔍 Scouting"
    lines = [f"<b>🔄 Aggiornamento su {ticker}</b> ({label})"]

    original_price = original_entry.get("price_at_alert")
    try:
        orario_primo_segnale = datetime.fromisoformat(original_entry["timestamp"]).astimezone(NY_TZ).strftime("%H:%M")
    except Exception:
        orario_primo_segnale = "N/D"

    if original_price is not None:
        lines.append(f"Primo segnale oggi: {orario_primo_segnale} (prezzo {original_price})")

    if display_price is not None:
        lines.append(f"Prezzo attuale (istantaneo): {display_price}")

        # Variazione 1: rispetto all'apertura odierna
        open_line = format_open_variation_line(display_price, quant.get("today_open"))
        if open_line:
            lines.append(open_line)

        # Variazione 2: rispetto all'ultimo rilevamento (ultima "ronda" di
        # analisi su questo titolo, non il primissimo segnale della giornata)
        reference_price = original_entry.get("last_update_price") or original_price
        if reference_price:
            delta_last_pct = (display_price - reference_price) / reference_price * 100
            lines.append(f"Variazione dall'ultimo rilevamento: {delta_last_pct:+.2f}%")

    lines.append(f"Punteggio checklist: {quant['score'] + news['score']}")
    lines.append("")
    for r in quant["reasons"] + news["reasons"]:
        lines.append(f"• {r}")

    lines.append("")
    lines.append(
        f"ℹ️ La previsione originale (<b>{original_entry.get('bias', 'n/d')}</b>, delle "
        f"{orario_primo_segnale}) resta quella monitorata per l'autovalutazione — questo "
        "è solo un aggiornamento informativo e non genera una nuova previsione da verificare."
    )
    return "\n".join(lines)


def build_alert_message(ticker: str, quant: dict, news: dict, source: str, display_price) -> str:
    total_score = quant["score"] + news["score"]
    label = "📌 Watchlist" if source == "watchlist" else "🔍 Scouting"
    lines = [f"<b>⚠️ Segnale su {ticker}</b> ({label})"]

    if display_price is not None:
        lines.append(f"Prezzo attuale (istantaneo): {display_price}")
        open_line = format_open_variation_line(display_price, quant.get("today_open"))
        if open_line:
            lines.append(open_line)

    lines.append(f"Punteggio checklist: {total_score}")
    lines.append("")
    for r in quant["reasons"] + news["reasons"]:
        lines.append(f"• {r}")
    if news["matched_titles"]:
        lines.append("")
        lines.append("Notizie correlate:")
        for t in news["matched_titles"]:
            lines.append(f"  - {t}")

    lines += generate_action_commentary(quant, news)

    return "\n".join(lines)


def resolve_pending_predictions(cfg: dict):
    """Controlla le previsioni registrate in passato che hanno raggiunto
    l'orizzonte temporale configurato (default 24h) e le segna come
    corrette/errate confrontando la direzione prevista con il prezzo attuale.

    "Corretto" = il prezzo si è mosso nella direzione indicata (su per un
    segnale rialzista, giù per uno ribassista) entro l'orizzonte, a
    prescindere dall'entità del movimento. I segnali con bias "incerta" non
    vengono classificati come corretti/errati (non c'era una previsione
    direzionale da verificare).
    """
    entries = load_log()
    if not entries:
        return

    horizon_hours = cfg.get("tracking", {}).get("horizon_hours", 24)
    horizon = timedelta(hours=horizon_hours)
    now = datetime.now(timezone.utc)

    due = []
    for e in entries:
        if e.get("resolved"):
            continue
        try:
            alert_time = datetime.fromisoformat(e["timestamp"])
        except Exception:
            continue
        if now - alert_time >= horizon:
            due.append(e)

    if not due:
        return

    tickers_needed = sorted(set(e["ticker"] for e in due))
    print(f"[INFO] Risolvo {len(due)} previsioni in sospeso su {len(tickers_needed)} titoli...")

    try:
        data = yf.download(
            tickers=tickers_needed,
            period="5d",
            interval="1d",
            group_by="ticker",
            threads=False,  # vedi nota in scout_top_movers: evita "database is locked" su Ubuntu
            progress=False,
        )
    except Exception as e:
        print(f"[ATTENZIONE] Impossibile scaricare i prezzi per la risoluzione: {e}")
        return

    current_prices = {}
    for t in tickers_needed:
        try:
            df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
            df = df.dropna()
            if not df.empty:
                current_prices[t] = float(df["Close"].iloc[-1])
        except Exception as e:
            print(f"[ATTENZIONE] Prezzo di risoluzione non disponibile per {t}: {e}")
            continue

    resolved_count = 0
    for e in due:
        price_now = current_prices.get(e["ticker"])
        if price_now is None:
            continue  # dati non disponibili ora: si riproverà al prossimo ciclo

        e["resolved"] = True
        e["resolved_at"] = now.isoformat()
        e["price_at_resolution"] = round(price_now, 2)

        if e.get("bias") == "incerta" or e.get("price_at_alert") is None:
            e["outcome"] = "non_classificabile"
        else:
            went_up = price_now > e["price_at_alert"]
            predicted_up = (e["bias"] == "rialzista")
            e["outcome"] = "corretto" if went_up == predicted_up else "errato"
        resolved_count += 1

    if resolved_count:
        save_log(entries)
        print(f"[INFO] {resolved_count} previsioni risolte e salvate nello storico.")


def main():
    cfg = load_config()
    threshold = cfg["thresholds"]["alert_score_threshold"]
    now = datetime.now(timezone.utc)

    # Prima di generare nuovi segnali, risolviamo le previsioni passate che
    # hanno raggiunto l'orizzonte configurato (serve al report di autovalutazione).
    resolve_pending_predictions(cfg)

    # Carichiamo lo storico UNA volta: lo aggiorniamo in memoria (sia con
    # nuove previsioni sia con aggiornamenti a previsioni di oggi già
    # esistenti) e lo salviamo con una sola scrittura a fine ciclo.
    all_entries = load_log()
    log_changed = False

    watchlist = list(cfg.get("tickers", []))
    scouted = scout_top_movers(cfg)
    scouted_unique = [t for t in scouted if t not in watchlist]

    # Titoli con una previsione ancora aperta OGGI (es. trovati dallo
    # scouting in un ciclo precedente) che vanno ricontrollati anche se
    # NON rientrano più tra i top mover di QUESTO specifico ciclio di scouting.
    already_covered = set(watchlist) | set(scouted_unique)
    carryover = sorted(get_active_today_tickers(all_entries, now, exclude=already_covered))
    if carryover:
        print(f"[INFO] Titoli con previsione aperta oggi non più in top-mover, ricontrollati comunque: {carryover}")

    all_targets = (
        [(t, "watchlist") for t in watchlist]
        + [(t, "scouting") for t in scouted_unique]
        + [(t, "scouting") for t in carryover]
    )

    any_alert = False

    for ticker, source in all_targets:
        print(f"--- Analisi {ticker} ({source}) ---")
        quant = analyze_price_and_volume(ticker, cfg)
        news = check_relevant_news(ticker, cfg)
        total_score = quant["score"] + news["score"]
        print(f"Punteggio totale: {total_score} (soglia: {threshold})")

        if total_score >= threshold:
            any_alert = True

            # Prezzo istantaneo (endpoint di quotazione, gestisce pre/after-market):
            # usato per la VISUALIZZAZIONE e per il tracciamento del prezzo di
            # riferimento. Se non disponibile, ripieghiamo sul prezzo dei dati
            # storici già calcolato dentro quant (meglio di niente).
            instant_price = get_instant_quote(ticker)
            display_price = instant_price if instant_price is not None else quant.get("last_price")
            if instant_price is None:
                print(f"[ATTENZIONE] Quotazione istantanea non disponibile per {ticker}, uso il prezzo dei dati storici come riserva.")

            existing_entry = find_todays_entry(ticker, all_entries, now)

            if existing_entry is None:
                # Primo segnale di oggi su questo titolo: previsione nuova da tracciare.
                bias = compute_bias(quant)
                message = build_alert_message(ticker, quant, news, source, display_price)
                send_telegram_message(message)

                all_entries.append({
                    "timestamp": now.isoformat(),
                    "ticker": ticker,
                    "source": source,
                    "bias": bias,
                    "score": total_score,
                    "factors": quant["factors"] + news["factors"],
                    "price_at_alert": display_price,
                    "resolved": False,
                    "resolved_at": None,
                    "price_at_resolution": None,
                    "outcome": None,
                    "update_count": 0,
                    "last_update_at": None,
                    "last_update_price": None,
                })
                log_changed = True
                print(f"[INFO] Nuovo segnale registrato per {ticker}.")
            else:
                # Titolo già segnalato oggi: aggiornamento informativo, la
                # previsione originale (bias/prezzo/esito) NON viene toccata.
                message = build_update_message(ticker, quant, news, source, existing_entry, display_price)
                send_telegram_message(message)

                existing_entry["update_count"] = existing_entry.get("update_count", 0) + 1
                existing_entry["last_update_at"] = now.isoformat()
                existing_entry["last_update_price"] = display_price
                log_changed = True
                print(f"[INFO] Aggiornamento (n. {existing_entry['update_count']}) per {ticker}, previsione originale invariata.")
        else:
            print("Nessun segnale rilevante al momento.")

    # Analisi specializzata sulle materie prime (indipendente dai singoli titoli)
    commodity_result = check_commodity_news(cfg)
    if commodity_result["triggered"]:
        any_alert = True
        send_telegram_message(build_commodity_alert_message(commodity_result["triggered"]))
        print(f"[INFO] Alert materie prime inviato: {list(commodity_result['triggered'].keys())}")

    if log_changed:
        save_log(all_entries)
        print("[INFO] Storico previsioni aggiornato e salvato.")

    if not any_alert:
        print("Ciclo completato: nessun alert da inviare.")


if __name__ == "__main__":
    main()
