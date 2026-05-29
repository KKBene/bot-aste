"""
Configurazione centralizzata del Bot Aste.
Tutti i parametri sono qui — non toccare gli altri file per configurare.

Le chiavi possono essere lette da variabili d'ambiente o da un file `.env`
nella cartella del progetto. I valori hardcoded restano come fallback locale.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent


def _load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    """Carica un .env semplice senza dipendenze esterne."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "si", "on"}


_load_dotenv()

# ────────────────────────────────────────────────
# SUPABASE (database principale)
# ────────────────────────────────────────────────
SUPABASE_URL = _env("SUPABASE_URL", "https://mrvucjvehtofarflobvt.supabase.co")
SUPABASE_KEY = _env("SUPABASE_KEY")

# ────────────────────────────────────────────────
# TELEGRAM
# ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")   # da .env o Secrets, niente default in repo pubblico

# ────────────────────────────────────────────────
# GEMINI (analisi PDF — primario)
# ────────────────────────────────────────────────
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_MODEL_FALLBACK = _env("GEMINI_MODEL_FALLBACK", "gemini-flash-lite-latest")

# ────────────────────────────────────────────────
# GROQ (backup gratuito se Gemini esaurisce quota)
# 14.400 req/giorno gratis — registrati su console.groq.com
# ────────────────────────────────────────────────
GROQ_API_KEY = _env("GROQ_API_KEY")
GROQ_MODEL = _env("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# ────────────────────────────────────────────────
# MISTRAL OCR (opzionale per PDF scansionati)
# ────────────────────────────────────────────────
MISTRAL_API_KEY = _env("MISTRAL_API_KEY")
MISTRAL_OCR_MODEL = _env("MISTRAL_OCR_MODEL", "mistral-ocr-latest")

# Router AI:
# - "groq": testo estratto → Groq, fallback Gemini
# - "gemini": testo estratto → Gemini, fallback Groq
PDF_TEXT_PROVIDER = _env("PDF_TEXT_PROVIDER", "groq").lower()
GROQ_MAX_PROMPT_CHARS = int(_env("GROQ_MAX_PROMPT_CHARS", "18000") or "18000")
USE_MISTRAL_OCR = _env_bool("USE_MISTRAL_OCR", True)
# Per PDF scansionati:
# - "gemini": usa solo Gemini Vision (baseline validata)
# - "best": confronta Mistral OCR+LLM con Gemini Vision e tiene il risultato migliore
# - "mistral": usa Mistral OCR+LLM, Gemini Vision solo fallback
# Default conservativo: non cambiare metodo sulle scansioni finché i benchmark
# non dimostrano un miglioramento netto.
PDF_SCAN_ANALYSIS_MODE = _env("PDF_SCAN_ANALYSIS_MODE", "gemini").lower()

# ────────────────────────────────────────────────
# GOOGLE SHEETS (export opzionale)
# ────────────────────────────────────────────────
GOOGLE_SHEET_ID = "1QvK3ZNr264vnEjDgr4_T9EnuvJMM_OcpkrliE-yhVfg"
GOOGLE_SHEET_MONTAGNA_ID = "1_RXyeIdLejkZ2p7Jl9RwI7NY-DdUjLsZGtOFHchw3XE"
GOOGLE_CREDENTIALS_FILE = str(BASE_DIR / "credentials.json")
SYNC_TO_SHEETS = _env_bool("SYNC_TO_SHEETS", True)

# ────────────────────────────────────────────────
# COMUNI DA SCRAPARE — organizzati per TIPO DI LOCALITÀ
# Ogni comune appartiene a una sola categoria (citta | montagna | mare).
# Per attivare/disattivare un'intera categoria usa SCRAPA_LOCALITA sotto.
# ────────────────────────────────────────────────
COMUNI_PER_LOCALITA = {
    # Città / hinterland Varese-Como-Milano (zona di interesse principale)
    "citta": [
        "tradate", "saronno", "uboldo", "caronno-pertusella", "cislago",
        "mozzate", "gallarate", "rovello-porro", "venegono-inferiore",
        "venegono-superiore", "castellanza", "busto-arsizio", "carbonate",
        "locate-varesino", "gerenzano", "turate", "rovellasca",
    ],

    # Montagna — Alpi, principali località sciistiche
    "montagna": [
        # Lombardia
        "bormio", "valdidentro", "valdisotto", "livigno", "madesimo",
        "campodolcino", "aprica", "ponte-di-legno", "temu", "vezza-d-oglio",
        "edolo", "chiesa-in-valmalenco", "foppolo", "castione-della-presolana",
        # Valle d'Aosta
        "courmayeur", "la-thuile", "valtournenche", "ayas", "champoluc", "cogne",
        "gressoney-la-trinite", "gressoney-saint-jean", "pila",
        # Piemonte (Vialattea + Limone)
        "sestriere", "sauze-d-oulx", "bardonecchia", "pragelato", "claviere",
        "limone-piemonte", "prato-nevoso", "alagna-valsesia", "macugnaga",
        # Trentino
        "madonna-di-campiglio", "pinzolo", "canazei", "moena", "predazzo",
        "san-martino-di-castrozza", "andalo", "fai-della-paganella", "folgaria",
        # Alto Adige
        "selva-di-val-gardena", "ortisei", "santa-cristina-valgardena",
        "corvara-in-badia", "badia", "plan-de-corones",
        # Veneto / Friuli
        "cortina-d-ampezzo", "arabba", "falcade", "tarvisio", "sappada",
    ],

    # Mare — Liguria di Ponente e Levante
    "mare": [
        # Ponente
        "ventimiglia", "bordighera", "sanremo", "ospedaletti", "imperia",
        "diano-marina", "alassio", "laigueglia", "albenga", "loano",
        "pietra-ligure", "finale-ligure", "noli", "spotorno", "varazze", "savona",
        # Levante
        "genova", "camogli", "santa-margherita-ligure", "portofino", "rapallo",
        "zoagli", "chiavari", "lavagna", "sestri-levante", "moneglia",
        "levanto", "monterosso-al-mare", "vernazza", "riomaggiore",
        "la-spezia", "lerici", "portovenere",
    ],
}

# Quali categorie includere nel run (toggle indipendenti)
SCRAPA_LOCALITA = {
    "citta": _env_bool("SCRAPA_CITTA", True),
    "montagna": _env_bool("SCRAPA_MONTAGNA", True),
    "mare": _env_bool("SCRAPA_MARE", True),
}

# Tipo immobile cercato sull'API astalegale (NON cambiare salvo bisogni reali)
CATEGORIA_RESIDENZIALE = "residenziali"

# Alias retro-compatibili (codice/test legacy)
COMUNI_RESIDENZIALI = COMUNI_PER_LOCALITA["citta"]
COMUNI_MONTAGNA = COMUNI_PER_LOCALITA["montagna"]
SCRAPA_MONTAGNA = SCRAPA_LOCALITA["montagna"]

# ────────────────────────────────────────────────
# TIMING SCRAPER
# ────────────────────────────────────────────────
DELAY_TRA_ANNUNCI = 1.5   # secondi tra un annuncio e l'altro
DELAY_TRA_COMUNI = 3.0    # secondi tra un comune e l'altro
SCRAPER_TIMEOUT = 30000   # ms (30s timeout Playwright)

# ────────────────────────────────────────────────
# PDF ANALYSIS
# ────────────────────────────────────────────────
MAX_PDF_PAGES = 15
MAX_PDF_CHARS = 50_000    # Tronca testo PDF a questo limite per Gemini
PDF_RETRY_ATTEMPTS = 2

# Rilevamento PDF scansionati: se il testo reale (al netto del watermark
# astalegale.net) è sotto queste soglie, il PDF è una scansione → Gemini Vision.
MIN_TESTO_REALE = 400      # caratteri reali totali minimi
MIN_CHAR_PER_PAGINA = 80   # caratteri reali medi per pagina minimi

# ────────────────────────────────────────────────
# SCORING & NOTIFICHE
# ────────────────────────────────────────────────
SCORE_MINIMO_NOTIFICA = 45   # Score minimo per apparire nel digest Telegram
TOP_N_NOTIFICA = 8           # Max offerte nel digest settimanale

# ── Modello economico (ROI risk-adjusted) ──────────
# Costo stimato di ristrutturazione in €/mq per stato manutentivo
COSTO_RISTRUTTURAZIONE_MQ = {
    "OTTIMO": 0,
    "BUONO": 100,
    "MEDIOCRE": 350,
    "PESSIMO": 700,
    "RUDERE": 1100,
}
COSTO_RISTRUTTURAZIONE_DEFAULT_MQ = 300   # stato ignoto

# Imposte di registro sull'acquisto (investitore = 9%; prima casa = 2%)
IMPOSTE_ACQUISTO_PCT = 0.09
# Oneri accessori forfettari (notaio/voltura/spese tecniche)
ONERI_ACCESSORI_EUR = 3000

# Costo+rischio stimato di liberazione per stato di occupazione (€)
COSTO_LIBERAZIONE = {
    "LIBERO": 0,
    "OCCUPATO_DEBITORE": 2000,           # si libera al decreto
    "OCCUPATO_CON_TITOLO_NON_OPP": 2500, # contratto non opponibile
    "OCCUPATO_SENZA_TITOLO": 5000,       # sfratto
    "OCCUPATO_CON_TITOLO": 9000,         # contratto opponibile: attesa lunga
}
COSTO_LIBERAZIONE_DEFAULT = 2500          # occupazione ignota

# Margine % (vs valore di mercato) che mappa al punteggio economico pieno
MARGINE_TARGET_PCT = 0.35   # ≥35% di margine → punti economici massimi

# Costi di possesso durante il flip (acquisto → ristrutturazione → rivendita)
MESI_POSSESSO_STIMA = 12    # mesi medi di detenzione → costi ricorrenti pro-quota
# IMU annua stimata = rendita_catastale × IMU_MOLTIPLICATORE × IMU_ALIQUOTA
IMU_MOLTIPLICATORE = 168    # cat. A: rivalutazione 5% × moltiplicatore 160
IMU_ALIQUOTA = 0.0106       # aliquota IMU seconda casa tipica (10,6‰)

# ────────────────────────────────────────────────
# LOG
# ────────────────────────────────────────────────
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
