"""
Scraper PVP — Portale Vendite Pubbliche (pvp.giustizia.it).

Fonte-madre di tutte le aste giudiziarie italiane (astalegale.net ne è solo un
rivenditore). Usa l'API JSON pubblica del portale — nessun browser, solo
`requests` — sullo stile di scraper_api.py. Schema API documentato nella
reference PVP in memoria.

Pipeline per comune:
  1. ricerca_comune()  -> lista sintetica lotti IMMOBILI (filtro esatto + attivi)
  2. dettaglio()       -> dato completo del lotto + link perizia
  3. to_asta()         -> dict pronto per database.inserisci_asta()

I lotti sono marcati con codice "PVP-<idVendita>" per non collidere con i
codici astalegale (P4588884...) e poter filtrare/rollbackare per fonte.
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
import urllib.parse
from datetime import date
from typing import Optional

import requests

# ──────────────────────────────────────────────────────────────
# Endpoint. I segmenti con token (ric-.../ve-...) sono id di bundle Entando:
# possono cambiare a un redeploy del portale, e in quel caso le chiamate
# iniziano a dare 404. Sono sovrascrivibili da env (PVP_RIC_TOKEN /
# PVP_VE_TOKEN) così si rimedia senza toccare il codice.
#
# Come recuperarli se cambiano: aprire https://pvp.giustizia.it/pvp/ con la
# rete del browser aperta e leggere il path delle chiamate XHR
# (".../ric-<hash>/ric-ms/ricerca/vendite" e ".../ve-<hash>/ve-ms/vendite/...").
# Il token `ve-` si trova anche nel bundle `main.*.js` della pagina.
# ──────────────────────────────────────────────────────────────
PVP_HOST = "https://pvp.giustizia.it"
PVP_RESOURCE_HOST = "https://resource-pvp.giustizia.it"
RIC_TOKEN = os.getenv("PVP_RIC_TOKEN", "ric-496b258c-986a1b71")
VE_TOKEN = os.getenv("PVP_VE_TOKEN", "ve-3f723b85-986a1b71")

URL_RICERCA = f"{PVP_HOST}/{RIC_TOKEN}/ric-ms/ricerca/vendite"
URL_DETTAGLIO = f"{PVP_HOST}/{VE_TOKEN}/ve-ms/vendite/{{id}}/restricted"

_ERRORE_TOKEN = (
    "PVP ha risposto 404 su {url}\n"
    "    → probabile cambio dei token di bundle del portale.\n"
    "    Recuperali dalla rete del browser su https://pvp.giustizia.it/pvp/ e\n"
    "    impostali via env: PVP_RIC_TOKEN=ric-<hash> PVP_VE_TOKEN=ve-<hash>"
)

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Origin": PVP_HOST,
    "Referer": f"{PVP_HOST}/pvp/it/lista_annunci.page",
}

# Parole non distintive: se usate come termine di ricerca fanno esplodere il
# match fuzzy di PVP (es. "La Thuile" cercato come "la" torna 13k risultati).
_CONNETTORI = {
    "di", "in", "al", "della", "del", "dei", "delle", "d", "da", "e", "su",
    "la", "lo", "le", "san", "santa", "santo", "sant",
}

# Qualificatori geografici: parole lunghe ma condivise da molti comuni. Se
# scelte come termine di ricerca saturano la pagina di risultati con comuni
# omonimi e i lotti veri finiscono fuori (es. "venegono-inferiore" cercato
# come "inferiore" → 0 risultati utili). Vanno escluse dalla scelta del token.
_QUALIFICATORI = {
    "inferiore", "superiore", "marittimo", "marittima", "marina", "mare",
    "ligure", "monte", "valle", "terme", "bagni", "piano", "alta", "alto",
    "bassa", "basso", "nuovo", "nuova", "vecchio", "vecchia", "grande",
    "piccolo", "piccola", "centro", "borgo", "villa", "casa", "colle",
}

PAGE_SIZE = 50
MAX_PAGINE = 6           # cap di sicurezza; le attive stanno in cima (sort desc)
DELAY_TRA_CHIAMATE = 0.4


# ──────────────────────────────────────────────────────────────
# Helpers nome comune
# ──────────────────────────────────────────────────────────────

def slug_to_nome(slug: str) -> str:
    """'busto-arsizio' -> 'Busto Arsizio', 'cortina-d-ampezzo' -> \"Cortina d'Ampezzo\"."""
    parti = slug.split("-")
    out = []
    for i, p in enumerate(parti):
        if p == "d":
            out.append("d'")
        elif p in _CONNETTORI and i != 0:
            out.append(p)
        else:
            out.append(p.capitalize())
    s = ""
    for tok in out:
        if tok == "d'":
            s = s.rstrip() + " d'"
        elif s.endswith("d'"):
            s += tok
        else:
            s += (" " if s else "") + tok
    return s


def token_distintivo(slug: str) -> str:
    """
    Il token più specifico da dare a `localita`: la parola più lunga che non sia
    un connettore né un qualificatore geografico condiviso (vedi _QUALIFICATORI).
    A parità di lunghezza vince il primo, di norma il nome proprio del comune.
    """
    parti = [p for p in slug.split("-") if p not in _CONNETTORI and len(p) > 2]
    specifici = [p for p in parti if p not in _QUALIFICATORI]
    cand = specifici or parti
    return max(cand, key=len) if cand else slug.replace("-", " ")


def _norm(s: Optional[str]) -> str:
    """Normalizza per confronto: senza accenti, minuscolo, apostrofi/trattini -> spazio."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return " ".join(s.replace("'", " ").replace("-", " ").split())


def _comune_combacia(nome_atteso: str, citta_lotto: Optional[str]) -> bool:
    """
    Match tollerante ai comuni fusi: 'San Martino di Castrozza' combacia con
    'Primiero San Martino di Castrozza'. Evita falsi positivi tipo
    Venegono Superiore vs Inferiore (nessuno dei due è sottostringa dell'altro).
    """
    a, b = _norm(nome_atteso), _norm(citta_lotto)
    if not a or not b:
        return False
    return a == b or a in b or b in a


# ──────────────────────────────────────────────────────────────
# Chiamate API
# ──────────────────────────────────────────────────────────────

def _post_ricerca(termine: str, pagina: int) -> dict:
    qs = urllib.parse.urlencode({
        "isPreview": "false", "language": "it",
        "page": pagina, "size": PAGE_SIZE, "sort": "dataVendita,desc",
    })
    body = {"tipoLotto": "IMMOBILI", "flagRicerca": 1, "localita": termine, "indirizzo": ""}
    r = requests.post(f"{URL_RICERCA}?{qs}", headers=_HEADERS, json=body, timeout=30)
    if r.status_code == 404:
        raise RuntimeError(_ERRORE_TOKEN.format(url=URL_RICERCA))
    r.raise_for_status()
    return r.json().get("body", {}) or {}


# Categorie tenute: solo residenziale, coerente con lo scoring dello strumento
# (commerciale/industriale/terreni distorcono formula e analisi). Modificabile
# per casi d'uso diversi.
CATEGORIE_RESIDENZIALE = {"IMMOBILE_RESIDENZIALE"}


def ricerca_comune(slug: str, oggi: Optional[str] = None,
                   categorie: Optional[set] = None) -> list[dict]:
    """
    Lotti IMMOBILI ATTIVI del comune (dataVendita >= oggi), filtrati per nome
    comune esatto e per categoria (default: solo residenziale). Pagina finché
    trova attivi (ordine dataVendita desc), poi si ferma. Ritorna i record
    sintetici della ricerca (id, prezzi, indirizzo…).
    """
    oggi = oggi or date.today().isoformat()
    categorie = CATEGORIE_RESIDENZIALE if categorie is None else categorie
    nome = slug_to_nome(slug)
    termine = token_distintivo(slug)
    trovati: list[dict] = []

    for pagina in range(MAX_PAGINE):
        body = _post_ricerca(termine, pagina)
        content = body.get("content", []) or []
        if not content:
            break
        # se in questa pagina (ordinata desc) non c'è più nessun attivo, stop:
        # tutte le pagine successive sono ancora più vecchie.
        if all((c.get("dataVendita") or "") < oggi for c in content):
            break
        for c in content:
            if (c.get("dataVendita") or "") < oggi:
                continue
            if categorie and c.get("categoriaLotto") not in categorie:
                continue
            if _comune_combacia(nome, (c.get("indirizzo") or {}).get("citta")):
                trovati.append(c)
        if body.get("last"):
            break
        time.sleep(DELAY_TRA_CHIAMATE)

    return trovati


def dettaglio(id_vendita: int) -> Optional[dict]:
    """Dato completo del lotto (prezzi, scadenza, allegati/perizia, coordinate)."""
    r = requests.get(URL_DETTAGLIO.format(id=id_vendita), headers=_HEADERS, timeout=30)
    if r.status_code != 200:
        return None
    return (r.json() or {}).get("body")


def _url_allegato(a: dict) -> str:
    return PVP_RESOURCE_HOST + urllib.parse.quote(a["linkAllegato"], safe="/?=&")


# La perizia/relazione di stima non sta sempre sotto codiceTipoAllegato=="PERIZ":
# spesso è "STIMA" (relazione di stima del CTU) o è caricata come "ALTRO" con un
# nome-file eloquente. Cerchiamo in ordine di affidabilità decrescente.
_PERIZIA_NOME_RE = re.compile(r"perizi|relazione\s+di\s+stima|\bc\.?t\.?u\b|stima", re.I)


def link_perizia_da_dettaglio(det: dict) -> Optional[str]:
    """
    URL assoluto del PDF perizia. Priorità: tipo PERIZ → tipo STIMA →
    qualunque allegato col nome-file che sa di perizia/stima/CTU.
    """
    alleg = [a for a in (det.get("allegati") or []) if a.get("linkAllegato")]
    for tipo in ("PERIZ", "STIMA"):
        for a in alleg:
            if a.get("codiceTipoAllegato") == tipo:
                return _url_allegato(a)
    for a in alleg:
        if _PERIZIA_NOME_RE.search(a.get("nomeFile") or ""):
            return _url_allegato(a)
    return None


def _link_per_tipo(det: dict, tipo: str) -> Optional[str]:
    for a in (det.get("allegati") or []):
        if a.get("codiceTipoAllegato") == tipo and a.get("linkAllegato"):
            return _url_allegato(a)
    return None


# ──────────────────────────────────────────────────────────────
# Mapping -> schema `aste`
# ──────────────────────────────────────────────────────────────

# disponibilita PVP -> enum stato_occupazione dello scorer. Solo i codici
# NON ambigui: "OCCUP" (Occupato, generico) resta None perché non distingue
# debitore/con-titolo — differenza cruciale per lo score — la decide l'analisi
# della perizia/avviso.
_OCCUPAZIONE = {
    "LIBER": "LIBERO",
    "OCCST": "OCCUPATO_SENZA_TITOLO",
}


def _mappa_occupazione(cod: Optional[str]) -> Optional[str]:
    return _OCCUPAZIONE.get(cod)


def _numero_procedura(det: Optional[dict], sommario: dict) -> Optional[str]:
    """'357/2025' da procedura.numeRg + numeAnnoRg (dettaglio); fallback sommario."""
    proc = (det or {}).get("procedura") or {}
    rg, anno = proc.get("numeRg"), proc.get("numeAnnoRg")
    if rg and anno:
        return f"{rg}/{anno}"
    if rg:
        return str(rg)
    s = sommario.get("procedura")
    return str(s) if s else None


def _prima_foto(det: Optional[dict]) -> Optional[str]:
    """URL della prima foto reale dell'immobile (allegati-bene 'IMMAGINE BENE')."""
    for bene in ((det or {}).get("beni") or []):
        for a in (bene.get("allegati") or []):
            if a.get("descrizione") == "IMMAGINE BENE" and a.get("linkAllegato"):
                return _url_allegato(a)
    return None


def _bene_principale(det: Optional[dict]) -> dict:
    beni = (det or {}).get("beni") or []
    return beni[0] if beni else {}


def _to_float(v):
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _norm_data_asta(v: Optional[str]) -> Optional[str]:
    """
    Normalizza la data asta a 'DD/MM/YYYY' — il formato su cui
    database._data_asta_passata fa il match. Il dettaglio PVP dà già
    '07/10/2026', il sommario dà ISO '2026-10-07[T..]': uniformiamo.
    """
    if not v:
        return None
    v = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v)          # ISO -> DD/MM/YYYY
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return v


def merge_deterministici(dati_llm: dict, riga_db: dict) -> dict:
    """
    Fonde l'estrazione LLM con i campi deterministici ufficiali PVP già in DB:
    dove PVP ha un valore affidabile, non lasciarlo sovrascrivere dall'LLM.
      - superficie_mq / stato_occupazione: si preservano se presenti in DB.
      - valore_mercato: si preserva SOLO se è una stima vera, cioè diversa dal
        prezzo base (impoStima==base è spesso un placeholder → lì la perizia
        analizzata dall'LLM è più affidabile).
    Per fonti senza dati deterministici (valori None) è un no-op sicuro.
    Muta e ritorna `dati_llm`.
    """
    for campo in ("superficie_mq", "stato_occupazione"):
        if riga_db.get(campo) is not None:
            dati_llm[campo] = riga_db[campo]
    vm = riga_db.get("valore_mercato")
    if vm is not None and vm != riga_db.get("prezzo_base"):
        dati_llm["valore_mercato"] = vm
    return dati_llm


def to_asta(sommario: dict, det: Optional[dict], categoria: str) -> dict:
    """
    Costruisce il dict per database.inserisci_asta() da ricerca + dettaglio.
    Prende ogni campo dalla fonte più affidabile e pre-riempie i campi
    deterministici che PVP fornisce già (valore_mercato, superficie,
    occupazione, foto) — così non dipendono dall'analisi LLM.
    """
    id_vendita = sommario["id"]
    lotto = det.get("lotto", {}) if det else {}
    bene = _bene_principale(det)
    ind_lotto = (lotto.get("indirizzo") if det else None) or bene.get("indirizzo") or sommario.get("indirizzo") or {}
    # coordinate: prima fonte non-nulla tra dettaglio-lotto, bene e sommario
    coord = ind_lotto.get("coordinate") or {}
    if not coord.get("latitudine"):
        coord = (sommario.get("indirizzo") or {}).get("coordinate") or coord

    termine = None
    if det and det.get("dataTermPresOff"):
        termine = f"{det['dataTermPresOff']} {det.get('oraTermPresOff') or ''}".strip()

    # occupazione: prima dal bene (dettaglio), poi dal sommario (lista disponibilita)
    disp_cod = bene.get("disponibilita")
    if not disp_cod:
        d = sommario.get("disponibilita")
        disp_cod = d[0] if isinstance(d, list) and d else None

    return {
        "codice": f"PVP-{id_vendita}",
        "comune": (ind_lotto.get("descComune") or sommario.get("indirizzo", {}).get("citta")),
        "prezzo_base": (det.get("impoBaseAsta") if det else None) or sommario.get("prezzoBaseAsta"),
        "offerta_minima": (det.get("impoOffertaMinima") if det else None) or sommario.get("offertaMinima"),
        # impoStima = stima ufficiale del perito registrata a PVP = valore di
        # mercato, deterministico (dove presente). I 35 punti sconto dello score.
        "valore_mercato": det.get("impoStima") if det else None,
        "superficie_mq": _to_float(bene.get("superficie")),
        "stato_occupazione": _mappa_occupazione(disp_cod),
        "indirizzo_immobile": ind_lotto.get("via") or bene.get("indirizzo", {}).get("via") or sommario.get("indirizzo", {}).get("via"),
        "tipologia": lotto.get("descTipoCategLotto") or sommario.get("categoriaLotto"),
        "tipologia_immobile": bene.get("descTipologiaBene") or lotto.get("descTipoCategLotto"),
        "data_asta": _norm_data_asta((det.get("dataVendita") if det else None) or sommario.get("dataVendita")),
        "termine_offerte": termine,
        "descrizione": bene.get("descrizione") or lotto.get("descLotto") or sommario.get("descLotto"),
        # tribunale affidabile dal sommario ("Tribunale di GENOVA"); modalita di
        # gara da descModVendita ("Sincrona Mista"), NON da descTipoVendita.
        "tribunale": sommario.get("tribunale"),
        "modalita_gara": det.get("descModVendita") if det else None,
        "numero_procedura": _numero_procedura(det, sommario),
        # la fonte è codificata nel prefisso del codice ("PVP-"): la tabella
        # `aste` non ha una colonna dedicata.
        "lotto": lotto.get("codLotto") or sommario.get("numeroLotto"),
        "link_dettaglio": f"{PVP_HOST}/pvp/it/detail_annuncio.page?idAnnuncio={id_vendita}",
        "link_perizia": link_perizia_da_dettaglio(det) if det else None,
        "link_avviso_vendita": _link_per_tipo(det, "AVEND") if det else None,
        "link_ordinanza": _link_per_tipo(det, "ORDIN") if det else None,
        "immagine_url": _prima_foto(det),
        "posizione_lat": coord.get("latitudine"),
        "posizione_lng": coord.get("longitudine"),
        "categoria_localita": categoria,
        "sheet_type": "residenziale" if categoria == "citta" else categoria,
    }


# ──────────────────────────────────────────────────────────────
# Orchestrazione
# ──────────────────────────────────────────────────────────────

def run_scraper_pvp(comuni_per_categoria: dict, codici_esistenti: Optional[set] = None,
                    con_dettaglio: bool = True, verbose: bool = True) -> dict:
    """
    Scrapa PVP per i comuni dati. Ritorna:
      {"aste": [dict pronti per inserisci_asta], "codici_per_comune": {...}, "stats": {...}}
    Non scrive sul DB: la persistenza la fa il chiamante (come scraper_api).
    """
    codici_esistenti = codici_esistenti or set()
    oggi = date.today().isoformat()
    aste, codici_per_comune = [], {}
    n_tot = n_nuovi = 0

    for categoria, slugs in comuni_per_categoria.items():
        for slug in slugs:
            try:
                lotti = ricerca_comune(slug, oggi)
            except Exception as e:
                if verbose:
                    print(f"  ⚠️ {slug}: ricerca fallita — {str(e)[:100]}")
                continue
            codici_per_comune.setdefault(categoria, {})[slug] = [f"PVP-{l['id']}" for l in lotti]
            n_tot += len(lotti)
            for lotto in lotti:
                codice = f"PVP-{lotto['id']}"
                if codice in codici_esistenti:
                    continue
                det = dettaglio(lotto["id"]) if con_dettaglio else None
                aste.append(to_asta(lotto, det, categoria))
                n_nuovi += 1
                if con_dettaglio:
                    time.sleep(DELAY_TRA_CHIAMATE)
            if verbose and lotti:
                print(f"  ✓ {slug}: {len(lotti)} attivi ({categoria})")

    return {
        "aste": aste,
        "codici_per_comune": codici_per_comune,
        "stats": {"attivi_totali": n_tot, "nuovi": n_nuovi},
    }


def run_scraper(comuni: list, categoria: Optional[str] = None,
                codici_esistenti: Optional[set] = None,
                categoria_localita: Optional[dict] = None,
                verbose: bool = True) -> dict:
    """
    Drop-in compatibile con scraper_api.run_scraper per main.py. Ritorna
    {"nuovi", "esistenti", "codici_per_comune"}:
      - nuovi:     list[dict to_asta] per i codici NON già nel DB (con dettaglio)
      - esistenti: list[{codice, prezzo_base, offerta_minima, comune}] per i codici
                   già nel DB (solo dal sommario, niente dettaglio → economico),
                   per il tracking prezzi di database.sincronizza_esistente
      - codici_per_comune: {comune_salvato: [codici]} con la stessa chiave che
                   finisce in `comune` (la citta del lotto), così la rilevazione
                   "spariti" in main.py combacia.
    `categoria` è ignorato (PVP filtra residenziale internamente); c'è per
    compatibilità di firma. `categoria_localita` mappa {slug_comune: categoria}.
    """
    codici_esistenti = codici_esistenti or set()
    categoria_localita = categoria_localita or {}
    oggi = date.today().isoformat()
    nuovi, esistenti, codici_per_comune = [], [], {}

    for slug in comuni:
        cat = categoria_localita.get(slug, "citta")
        try:
            lotti = ricerca_comune(slug, oggi)
        except Exception as e:
            if verbose:
                print(f"  ⚠️ {slug}: ricerca PVP fallita — {str(e)[:100]}")
            continue
        for lotto in lotti:
            codice = f"PVP-{lotto['id']}"
            citta = (lotto.get("indirizzo") or {}).get("citta") or slug
            codici_per_comune.setdefault(citta, []).append(codice)
            if codice in codici_esistenti:
                esistenti.append({
                    "codice": codice,
                    "prezzo_base": lotto.get("prezzoBaseAsta"),
                    "offerta_minima": lotto.get("offertaMinima"),
                    "comune": citta,
                })
            else:
                det = dettaglio(lotto["id"])
                nuovi.append(to_asta(lotto, det, cat))
                time.sleep(DELAY_TRA_CHIAMATE)
        if verbose and lotti:
            print(f"  ✓ {slug}: {len(lotti)} attivi ({cat})")

    return {"nuovi": nuovi, "esistenti": esistenti, "codici_per_comune": codici_per_comune}
