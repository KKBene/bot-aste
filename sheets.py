"""
Sync opzionale da Supabase → Google Sheets.
Sovrascrive il foglio con tutti i dati aggiornati (incluso score e analisi PDF).
"""
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from config import GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS_FILE
import database as db

HEADERS = [
    "Codice Asta", "Comune", "Prezzo Base (€)", "Offerta Minima (€)",
    "Indirizzo Immobile", "Indirizzo Asta", "Tipologia", "Data Asta",
    "Tribunale", "Numero Procedura", "Lotto",
    # PDF analysis
    "Stato Occupazione", "Superficie mq", "Stato Manutentivo",
    "Piano Ascensore", "Costi Sanatoria", "Note Critiche",
    # Score
    "Score", "Sconto %",
    # Links
    "Link Dettaglio", "Link Perizia", "Link Avviso Vendita",
    "Link Ordinanza", "Link Planimetrie",
    # Meta
    "Data Scraping", "Analisi PDF",
]

_SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


def _get_worksheet():
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_FILE, _SCOPE)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID)
    return sheet.sheet1


def _asta_to_row(asta: dict) -> list:
    prezzo_base = asta.get("prezzo_base") or 0
    offerta_min = asta.get("offerta_minima") or 0
    sconto = ""
    if prezzo_base > 0 and offerta_min > 0:
        pct = (prezzo_base - offerta_min) / prezzo_base * 100
        sconto = f"{pct:.1f}%"

    return [
        asta.get("codice", ""),
        (asta.get("comune") or "").title(),
        prezzo_base or "",
        offerta_min or "",
        asta.get("indirizzo_immobile", ""),
        asta.get("indirizzo_asta", ""),
        asta.get("tipologia", ""),
        asta.get("data_asta", ""),
        asta.get("tribunale", ""),
        asta.get("numero_procedura", ""),
        asta.get("lotto", ""),
        asta.get("stato_occupazione", ""),
        asta.get("superficie_mq", ""),
        asta.get("stato_manutentivo", ""),
        asta.get("piano_ascensore", ""),
        asta.get("costi_sanatoria", ""),
        asta.get("note_critiche", ""),
        asta.get("score", ""),
        sconto,
        asta.get("link_dettaglio", ""),
        asta.get("link_perizia", ""),
        asta.get("link_avviso_vendita", ""),
        asta.get("link_ordinanza", ""),
        asta.get("link_planimetrie", ""),
        asta.get("scraping_date", ""),
        "✅" if asta.get("analisi_pdf") else "❌",
    ]


def sync_to_sheets():
    """Sincronizza tutte le aste dal DB a Google Sheets, ordinate per score desc."""
    print("  📊 Connessione Google Sheets...")
    ws = _get_worksheet()

    # Leggi tutte le aste ordinate per score
    res = (
        db.get_client()
        .table("aste")
        .select("*")
        .order("score", desc=True)
        .execute()
    )
    aste = res.data or []

    if not aste:
        print("  ⚠️ Nessuna asta da sincronizzare")
        return

    # Pulisci e riscrivi
    ws.clear()
    ws.update("A1", [HEADERS])

    # Formatta header
    ws.format("A1:Z1", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.13, "green": 0.59, "blue": 0.95},
        "horizontalAlignment": "CENTER",
    })
    ws.freeze(rows=1)

    rows = [_asta_to_row(a) for a in aste]
    if rows:
        ws.update(f"A2:Z{len(rows) + 1}", rows)

    # Formatta colonne numeriche
    n = len(aste) + 1
    try:
        ws.format(f"C2:D{n}", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})
        ws.format(f"R2:R{n}", {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}})
    except Exception:
        pass

    print(f"  ✅ Google Sheet aggiornato: {len(aste)} righe")
    sheet_url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"
    print(f"  🔗 {sheet_url}")
    return sheet_url
