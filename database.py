"""
Supabase database layer.
Tutte le operazioni CRUD sulle aste passano da qui.
"""
import json
from datetime import datetime
from typing import Optional
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_KEY

_client: Optional[Client] = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


# ─────────────────────────────────────────────────────────────
# ASTE
# ─────────────────────────────────────────────────────────────

def get_codici_esistenti() -> set:
    """Restituisce tutti i codici asta già nel DB."""
    res = get_client().table("aste").select("codice").execute()
    return {r["codice"] for r in (res.data or [])}


def inserisci_asta(asta: dict) -> bool:
    """Inserisce un'asta nuova. Ritorna True se inserita, False se già esiste."""
    payload = {
        "codice": asta["codice"],
        "comune": asta.get("comune"),
        "prezzo_base": asta.get("prezzo_base"),
        "offerta_minima": asta.get("offerta_minima"),
        "indirizzo_immobile": asta.get("indirizzo_immobile"),
        "indirizzo_asta": asta.get("indirizzo_asta"),
        "tipologia": asta.get("tipologia"),
        "data_asta": asta.get("data_asta"),
        "termine_offerte": asta.get("termine_offerte"),
        "modalita_gara": asta.get("modalita_gara"),
        "descrizione": asta.get("descrizione"),
        "tribunale": asta.get("tribunale"),
        "numero_procedura": asta.get("numero_procedura"),
        "lotto": asta.get("lotto"),
        "link_dettaglio": asta.get("link_dettaglio"),
        "link_avviso_vendita": asta.get("link_avviso_vendita"),
        "link_perizia": asta.get("link_perizia"),
        "link_ordinanza": asta.get("link_ordinanza"),
        "link_planimetrie": asta.get("link_planimetrie"),
        "posizione_lat": asta.get("posizione_lat"),
        "posizione_lng": asta.get("posizione_lng"),
        "immagine_url": asta.get("immagine_url"),
        "sheet_type": asta.get("sheet_type", "residenziale"),
        "categoria_localita": asta.get("categoria_localita"),
        "scraping_date": datetime.now().isoformat(),
        "stato_annuncio": "attivo",
        "prima_vista": datetime.now().isoformat(),
        "ultima_vista": datetime.now().isoformat(),
        "prezzo_base_iniziale": asta.get("prezzo_base"),
    }
    try:
        get_client().table("aste").insert(payload).execute()
        return True
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            return False
        raise


# ─────────────────────────────────────────────────────────────
# CICLO DI VITA: prezzi e disponibilità
# ─────────────────────────────────────────────────────────────

def get_aste_attive() -> dict:
    """
    Mappa {codice: {prezzo_base, offerta_minima, comune}} delle aste attive.
    Usata per confrontare i prezzi e rilevare annunci spariti.
    """
    res = (
        get_client()
        .table("aste")
        .select("codice, comune, prezzo_base, offerta_minima")
        .eq("stato_annuncio", "attivo")
        .execute()
    )
    return {r["codice"]: r for r in (res.data or [])}


def _quasi_uguale(a, b, tol: float = 1.0) -> bool:
    """Confronto robusto tra prezzi (None-safe)."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def sincronizza_esistente(codice: str, prezzo_base, offerta_minima,
                          prezzo_base_db, offerta_db) -> bool:
    """
    Aggiorna un annuncio già esistente: segna 'ultima_vista' e, se il prezzo è
    cambiato, registra lo storico e incrementa numero_ribassi (se è un ribasso).
    Ritorna True se è stata registrata una variazione di prezzo.
    """
    client = get_client()
    update = {
        "ultima_vista": datetime.now().isoformat(),
        "stato_annuncio": "attivo",  # se era 'sparito' ed è riapparso, riattiva
    }

    variazione = not (_quasi_uguale(prezzo_base, prezzo_base_db)
                      and _quasi_uguale(offerta_minima, offerta_db))

    if variazione:
        # Registra l'osservazione nello storico
        client.table("prezzi_storico").insert({
            "codice": codice,
            "prezzo_base": prezzo_base,
            "offerta_minima": offerta_minima,
            "rilevato_il": datetime.now().isoformat(),
        }).execute()
        update["prezzo_base"] = prezzo_base
        update["offerta_minima"] = offerta_minima
        # Ribasso = il nuovo prezzo base è inferiore al precedente
        if prezzo_base is not None and prezzo_base_db is not None and prezzo_base < prezzo_base_db:
            res = client.table("aste").select("numero_ribassi").eq("codice", codice).execute()
            attuale = (res.data or [{}])[0].get("numero_ribassi") or 0
            update["numero_ribassi"] = attuale + 1

    client.table("aste").update(update).eq("codice", codice).execute()
    return variazione


def marca_sparite(codici: list) -> int:
    """
    Marca come 'sparito' (o 'venduto' se la data asta è passata) gli annunci che
    non sono più stati trovati nello scrape. Ritorna il numero di annunci marcati.
    """
    if not codici:
        return 0
    client = get_client()
    for codice in codici:
        # Se la data asta è passata, è probabilmente venduto; altrimenti ritirato
        res = client.table("aste").select("data_asta").eq("codice", codice).execute()
        data_asta = (res.data or [{}])[0].get("data_asta")
        nuovo_stato = "venduto" if _data_asta_passata(data_asta) else "sparito"
        client.table("aste").update({"stato_annuncio": nuovo_stato}).eq("codice", codice).execute()
    return len(codici)


def _data_asta_passata(data_asta: Optional[str]) -> bool:
    """True se la data asta (formato 'DD/MM/YYYY ...') è già passata."""
    if not data_asta:
        return False
    import re
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(data_asta))
    if not m:
        return False
    try:
        g, mese, anno = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return datetime(anno, mese, g).date() < datetime.now().date()
    except ValueError:
        return False


def get_storico_prezzi(codice: str) -> list:
    """Storico delle variazioni di prezzo di un annuncio (cronologico)."""
    res = (
        get_client()
        .table("prezzi_storico")
        .select("*")
        .eq("codice", codice)
        .order("rilevato_il")
        .execute()
    )
    return res.data or []


def aggiorna_analisi_pdf(codice: str, dati: dict):
    get_client().table("aste").update({
        "stato_occupazione": dati.get("stato_occupazione"),
        "occupazione_opponibile": dati.get("occupazione_opponibile"),
        "costi_sanatoria": dati.get("costi_sanatoria"),
        "superficie_mq": dati.get("superficie_mq"),
        "stato_manutentivo": dati.get("stato_manutentivo"),
        "piano_ascensore": dati.get("piano_ascensore"),
        "distanza_stazione_km": dati.get("distanza_stazione_km"),
        "qualita_posizione": dati.get("qualita_posizione"),
        "valore_mercato": dati.get("valore_mercato"),
        "spese_condominiali_arretrate": dati.get("spese_condominiali_arretrate"),
        "spese_condominiali_annue": dati.get("spese_condominiali_annue"),
        "spese_straordinarie_deliberate": dati.get("spese_straordinarie_deliberate"),
        "rendita_catastale": dati.get("rendita_catastale"),
        "canone_locazione_annuo": dati.get("canone_locazione_annuo"),
        "pertinenze": dati.get("pertinenze"),
        "quota_proprieta": dati.get("quota_proprieta"),
        "categoria_catastale": dati.get("categoria_catastale"),
        "anno_costruzione": dati.get("anno_costruzione"),
        "classe_energetica": dati.get("classe_energetica"),
        "tipologia_immobile": dati.get("tipologia_immobile"),
        "note_critiche": dati.get("note_critiche"),
        "analisi_pdf": True,
        "data_analisi": datetime.now().isoformat(),
    }).eq("codice", codice).execute()


def aggiorna_score(codice: str, score: float, breakdown: dict):
    get_client().table("aste").update({
        "score": score,
        "score_breakdown": breakdown,
    }).eq("codice", codice).execute()


def get_aste_senza_analisi() -> list:
    """Aste con link_perizia ma senza analisi PDF completata."""
    res = (
        get_client()
        .table("aste")
        .select("codice, link_perizia")
        .eq("analisi_pdf", False)
        .not_.is_("link_perizia", "null")
        .neq("link_perizia", "")
        .execute()
    )
    return res.data or []


def get_aste_senza_score() -> list:
    """Aste senza score calcolato."""
    res = (
        get_client()
        .table("aste")
        .select("*")
        .is_("score", "null")
        .execute()
    )
    return res.data or []


def get_aste_attive_complete() -> list:
    """Record completi delle aste attive (per il ricalcolo dello score)."""
    res = (
        get_client()
        .table("aste")
        .select("*")
        .eq("stato_annuncio", "attivo")
        .execute()
    )
    return res.data or []


def get_aste_da_notificare(min_score: float, limit: int) -> list:
    """Top aste non ancora notificate, ordinate per score decrescente."""
    res = (
        get_client()
        .table("aste")
        .select("*")
        .eq("notificato", False)
        .eq("stato_annuncio", "attivo")     # mai notificare venduti/spariti
        .not_.is_("score", "null")
        .gte("score", min_score)
        .order("score", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def segna_notificate(codici: list):
    for codice in codici:
        get_client().table("aste").update({"notificato": True}).eq("codice", codice).execute()


# ─────────────────────────────────────────────────────────────
# RUNS (log esecuzioni)
# ─────────────────────────────────────────────────────────────

def start_run() -> str:
    res = get_client().table("runs").insert({
        "started_at": datetime.now().isoformat(),
        "status": "running",
    }).execute()
    return res.data[0]["id"]


def end_run(run_id: str, status: str, nuovi: int, pdf: int, errori: int):
    get_client().table("runs").update({
        "finished_at": datetime.now().isoformat(),
        "status": status,
        "nuovi_annunci": nuovi,
        "pdf_analizzati": pdf,
        "errori": errori,
    }).eq("id", run_id).execute()
