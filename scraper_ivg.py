"""
Scraper IVG — Istituti Vendite Giudiziarie (piattaforma astagiudiziaria.com).

Perché una fonte in più, se PVP è la fonte-madre: gli IVG pubblicano i nuovi
esperimenti di vendita sul proprio calendario, e capita che su PVP lo stesso
immobile risulti fermo a una vendita già passata. Verificato: su un campione
di lotti IVG "In vendita", la maggior parte non compariva tra le aste attive
di PVP, e in un caso (Vaie) lo stesso importo su PVP era legato a un'asta del
2024. Sono quindi opportunità che PVP da solo non mostra.

I dati arrivano dall'indice Typesense che alimenta la ricerca del sito: una
sola chiamata HTTP per comune, nessun browser. Il dettaglio (perizia, avviso)
si legge dalla pagina pubblica dell'annuncio.

I lotti sono marcati `IVG-<id>`; il dedup verso PVP è a carico del chiamante
(vedi `possibile_duplicato`), perché una parte dei lotti sta su entrambe le
fonti con lo stesso prezzo.
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from typing import Optional

import requests

# Endpoint pubblico dell'indice di ricerca. La chiave è quella che il sito
# stesso espone nel payload della pagina (search-only, sola lettura): se
# cambia, si rilegge da https://www.ivgvarese.it/ricerca/immobili cercando
# `tskey`. Sovrascrivibile da env senza toccare il codice.
_HEADERS_HTML = {"User-Agent": "Mozilla/5.0", "Accept-Language": "it-IT,it;q=0.9"}

TYPESENSE_HOST = "https://typesense.astagiudiziaria.com"
TYPESENSE_COLLECTION = os.getenv("IVG_COLLECTION", "astagiudiziaria-prod-v3")
# Ultima chiave nota: fa da punto di partenza, ma il sito la ruota (successo:
# tutte le query hanno iniziato a dare 401 nel giro di un'ora). Quella vera si
# rilegge dalla pagina di ricerca, che la pubblica nel proprio payload.
_KEY_FALLBACK = os.getenv("IVG_TYPESENSE_KEY", "0uDldTEPDPuvGkN3Suvj1qVI9s75GEGm")
URL_RICERCA = f"{TYPESENSE_HOST}/collections/{TYPESENSE_COLLECTION}/documents/search"
PAGINA_CHIAVE = "https://www.ivgvarese.it/ricerca/immobili"
_RE_TSKEY = re.compile(r'tskey\s*:\s*"([A-Za-z0-9]{20,})"')

# chiave in uso, aggiornata a runtime quando quella corrente scade
_chiave_corrente = [_KEY_FALLBACK]


def _leggi_chiave_dal_sito() -> Optional[str]:
    """Rilegge la search-key pubblica dal payload della pagina di ricerca."""
    try:
        r = requests.get(PAGINA_CHIAVE, headers=_HEADERS_HTML, timeout=30)
        if r.status_code != 200:
            return None
        m = _RE_TSKEY.search(r.text)
        return m.group(1) if m else None
    except Exception:
        return None


def _cerca_typesense(params: dict, timeout: int = 30):
    """
    Interroga l'indice, rinnovando la chiave se il sito l'ha ruotata. Senza
    questo lo scraper resterebbe muto (401 su ogni chiamata) fino a un
    intervento manuale.
    """
    for tentativo in (1, 2):
        r = requests.get(URL_RICERCA,
                         headers={"X-TYPESENSE-API-KEY": _chiave_corrente[0],
                                  "User-Agent": "Mozilla/5.0"},
                         params=params, timeout=timeout)
        if r.status_code != 401 or tentativo == 2:
            return r
        nuova = _leggi_chiave_dal_sito()
        if not nuova or nuova == _chiave_corrente[0]:
            return r
        _chiave_corrente[0] = nuova
    return r


# Dominio buono per TUTTI gli IVG: i domini dei singoli istituti
# (ivgvarese.it, ...) rispondono 404 sugli annunci degli altri.
HOST_DETTAGLIO = "https://www.astagiudiziaria.com"

# Categorie residenziali, coerenti col filtro applicato su PVP.
CATEGORIE_RESIDENZIALE = {"IMMOBILE RESIDENZIALE"}

# La classificazione IVG non è affidabile quanto quella PVP: si trovano lotti
# marcati "IMMOBILE RESIDENZIALE / APPARTAMENTO" il cui titolo è "Deposito
# fabbricato costruito per esigenze commerciali" (e che PVP classifica infatti
# come commerciale). Il titolo è più vicino alla realtà della categoria.
_TITOLO_NON_RESIDENZIALE = re.compile(
    r"\b(deposito|capannone|commercial|industrial|negozio|ufficio|opificio|"
    r"laboratorio|magazzino|terreno|box auto|posto auto|autorimessa|"
    r"area edificabile|agricol)\w*", re.I)


DELAY_TRA_CHIAMATE = 0.4
MAX_PER_COMUNE = 50


def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def slug_to_citta(slug: str) -> str:
    """'busto-arsizio' -> 'BUSTO ARSIZIO' (nell'indice le città sono maiuscole)."""
    return slug.replace("-", " ").upper()


def titolo_non_residenziale(doc: dict) -> bool:
    """True se il testo dell'annuncio smentisce la categoria residenziale."""
    testo = f"{doc.get('title') or ''} {doc.get('descrizione') or ''}"
    return bool(_TITOLO_NON_RESIDENZIALE.search(testo))


def comune_incoerente(doc: dict) -> bool:
    """
    True se il testo dell'annuncio colloca l'immobile in un comune diverso da
    quello indicato. Capita: un lotto marcato "Caronno Pertusella" il cui
    titolo recita "Appartamento sito a Cassano Jonio" (Calabria). Inserirlo
    manderebbe a vedere un immobile dall'altra parte del paese.
    """
    citta = _norm(doc.get("city"))
    if not citta:
        return True
    testo = f"{doc.get('title') or ''} {doc.get('descrizione') or ''}"
    m = re.search(r"sit[ou]\s+(?:a|in)\s+([A-ZÀ-Ù][\w'’]+(?:\s+[A-ZÀ-Ù][\w'’]+){0,2})", testo)
    if not m:
        return False
    dichiarato = _norm(m.group(1))
    if not dichiarato:
        return False
    return dichiarato not in citta and citta not in dichiarato


def cerca_comune(slug: str, categorie: Optional[set] = None) -> list[dict]:
    """
    Lotti immobiliari in vendita nel comune. Solo annunci con un prezzo vero:
    una parte dell'indice ha price=0 (asta senza base pubblicata) e non è
    valutabile né confrontabile.
    """
    categorie = CATEGORIE_RESIDENZIALE if categorie is None else categorie
    citta = slug_to_citta(slug)
    filtro = (f"genre:IMMOBILI && status:`In vendita` && price:>0 "
              f"&& city:=`{citta}`")
    try:
        r = _cerca_typesense({"q": "*", "per_page": MAX_PER_COMUNE, "filter_by": filtro})
        if r.status_code != 200:
            return []
        hits = r.json().get("hits", [])
    except Exception:
        return []

    lotti = []
    for h in hits:
        doc = h.get("document") or {}
        if categorie and (doc.get("category") or "").upper() not in categorie:
            continue
        # senza data di vendita non sappiamo se l'asta è ancora giocabile, e il
        # ciclo di vita (marca_scadute) non ha su cosa lavorare
        if not doc.get("sellStartDate"):
            continue
        if titolo_non_residenziale(doc) or comune_incoerente(doc):
            continue
        lotti.append(doc)
    return lotti


# I documenti sull'annuncio sono marcati con un alt parlante.
_RE_DOC = re.compile(
    r'alt="(?P<eti>[^"]{3,40})"\s+href="(?P<url>https://library\.astagiudiziaria\.com/pdf/[a-f0-9]+\.pdf)"',
    re.I)


def documenti_lotto(permalink: Optional[str]) -> dict:
    """{'perizia': url, 'avviso': url} dalla pagina pubblica dell'annuncio."""
    if not permalink:
        return {}
    try:
        r = requests.get(f"{HOST_DETTAGLIO}/{permalink}", headers=_HEADERS_HTML, timeout=30)
        if r.status_code != 200:
            return {}
        html = r.text
    except Exception:
        return {}

    trovati = {}
    for m in _RE_DOC.finditer(html):
        etichetta = m.group("eti").lower()
        if "perizia" in etichetta and "perizia" not in trovati:
            trovati["perizia"] = m.group("url")
        elif "avviso" in etichetta and "avviso" not in trovati:
            trovati["avviso"] = m.group("url")
    return trovati


def link_dettaglio(doc: dict) -> Optional[str]:
    permalink = doc.get("permalink")
    return f"{HOST_DETTAGLIO}/{permalink}" if permalink else None


def _norm_data(valore: Optional[str]) -> Optional[str]:
    """'12/11/2026 16:45' -> '12/11/2026 16:45' (già nel formato atteso dal DB)."""
    if not valore:
        return None
    v = str(valore).strip()
    return v if re.match(r"^\d{1,2}/\d{1,2}/\d{4}", v) else None


def to_asta(doc: dict, documenti: Optional[dict], categoria: str) -> dict:
    """Costruisce il dict per database.inserisci_asta() da un documento IVG."""
    documenti = documenti or {}
    gallery = doc.get("gallery") or []
    sub = doc.get("subcategory") or []
    return {
        "codice": f"IVG-{doc.get('id')}",
        "comune": (doc.get("city") or "").title(),
        "prezzo_base": doc.get("price"),
        "offerta_minima": doc.get("minimumOffer"),
        "indirizzo_immobile": (doc.get("address") or "").strip().rstrip(","),
        "descrizione": doc.get("descrizione") or doc.get("title"),
        "tipologia": doc.get("category"),
        "tipologia_immobile": (sub[0] if sub else None),
        "data_asta": _norm_data(doc.get("sellStartDate")),
        "modalita_gara": doc.get("sellType"),
        "tribunale": doc.get("tribunal"),
        "numero_procedura": doc.get("numero_procedura"),
        "lotto": str(doc.get("lotto_code")) if doc.get("lotto_code") is not None else None,
        "link_dettaglio": link_dettaglio(doc),
        "link_perizia": documenti.get("perizia"),
        "link_avviso_vendita": documenti.get("avviso"),
        "immagine_url": gallery[0] if gallery else None,
        "categoria_localita": categoria,
        "sheet_type": "residenziale" if categoria == "citta" else categoria,
    }


def possibile_duplicato(doc: dict, aste_esistenti: list[dict]) -> bool:
    """
    True se un'asta già nel DB descrive lo stesso immobile: stesso comune e
    un importo che coincide (base o offerta minima). Una parte dei lotti IVG
    è anche su PVP, e senza questo controllo finirebbero due volte nel report.
    """
    citta = _norm(doc.get("city"))
    importi = {round(float(v)) for v in (doc.get("price"), doc.get("minimumOffer")) if v}
    if not citta or not importi:
        return False
    for asta in aste_esistenti:
        if _norm(asta.get("comune")) != citta:
            continue
        altri = {round(float(v)) for v in (asta.get("prezzo_base"), asta.get("offerta_minima")) if v}
        if importi & altri:
            return True
    return False


def run_scraper(comuni: list, categoria: Optional[str] = None,
                codici_esistenti: Optional[set] = None,
                categoria_localita: Optional[dict] = None,
                aste_esistenti: Optional[list] = None,
                verbose: bool = True) -> dict:
    """
    Stesso contratto di scraper_pvp.run_scraper: {nuovi, esistenti,
    codici_per_comune}. `aste_esistenti` (record già nel DB, anche di altre
    fonti) serve a non reinserire lotti che PVP ha già portato.
    """
    codici_esistenti = codici_esistenti or set()
    categoria_localita = categoria_localita or {}
    aste_esistenti = aste_esistenti or []
    nuovi, esistenti, codici_per_comune = [], [], {}

    for slug in comuni:
        cat = categoria_localita.get(slug, "citta")
        lotti = cerca_comune(slug)
        for doc in lotti:
            codice = f"IVG-{doc.get('id')}"
            citta = (doc.get("city") or slug).title()
            codici_per_comune.setdefault(citta, []).append(codice)
            if codice in codici_esistenti:
                esistenti.append({
                    "codice": codice,
                    "prezzo_base": doc.get("price"),
                    "offerta_minima": doc.get("minimumOffer"),
                    "comune": citta,
                })
                continue
            if possibile_duplicato(doc, aste_esistenti):
                continue                     # già presente da un'altra fonte
            nuovi.append(to_asta(doc, documenti_lotto(doc.get("permalink")), cat))
            time.sleep(DELAY_TRA_CHIAMATE)
        if verbose and lotti:
            print(f"  ✓ {slug}: {len(lotti)} lotti IVG ({cat})")

    return {"nuovi": nuovi, "esistenti": esistenti, "codici_per_comune": codici_per_comune}
