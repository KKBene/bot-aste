"""
Stima di mercato da annunci comparabili (immobiliare.it).

Perché serve: la perizia del CTU è spesso conservativa e datata (a volte di
anni), quindi il margine reale di un'asta può essere più alto di quello che
risulta dalla stima. E per ~1 lotto su 10 la stima non c'è proprio. Questo
modulo affianca alla stima del perito un secondo riferimento indipendente:
il prezzo al m² effettivamente chiesto sul mercato, oggi, in quel comune.

Metodo: si prendono gli annunci di vendita del comune, si tiene solo ciò che
è comparabile (stessa fascia di superficie, tipologie residenziali), si
calcola la **mediana** del €/m² — non la media, che una villa di lusso o un
box svenduto sposterebbero — e la si moltiplica per la superficie del lotto.

Il risultato è una stima *dell'usato in vendita*, quindi un valore di
richiesta: va letto come tetto ottimistico, non come prezzo di realizzo.
"""
from __future__ import annotations

import json
import re
import statistics
import time
import unicodedata
from typing import Optional

from curl_cffi import requests as creq

BASE_URL = "https://www.immobiliare.it/vendita-case/{comune}/"
# impersonation che passa il bot-detection senza proxy a pagamento
_IMPERSONATE = "safari17_2_ios"
_HEADERS = {"Accept-Language": "it-IT,it;q=0.9"}
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# Tipologie non residenziali o non confrontabili con un appartamento/villa:
# terreni, box e "progetti" hanno un €/m² che falserebbe la mediana.
_TIPOLOGIE_ESCLUSE = {
    "garage", "box", "posto auto", "terreno", "progetto", "magazzino",
    "capannone", "negozio", "ufficio", "laboratorio", "palazzo", "stabile",
}
# Fascia di superficie considerata comparabile: ±50% di quella del lotto.
_TOLLERANZA_SUPERFICIE = 0.5
MIN_COMPARABILI = 5
DELAY_TRA_RICHIESTE = 1.5


def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return s.lower().strip()


def comune_to_slug(comune: Optional[str]) -> str:
    """'Venegono Superiore' -> 'venegono-superiore' (formato URL immobiliare.it)."""
    s = _norm(comune).replace("'", " ").replace("’", " ")
    return "-".join(t for t in re.split(r"[^a-z0-9]+", s) if t)


def _trova_risultati(nodo, profondita: int = 0):
    """Il payload Next.js annida i risultati in punti che cambiano: cerca la lista."""
    if profondita > 8:
        return None
    if isinstance(nodo, dict):
        if isinstance(nodo.get("results"), list) and nodo["results"]:
            return nodo["results"]
        for valore in nodo.values():
            trovato = _trova_risultati(valore, profondita + 1)
            if trovato:
                return trovato
    elif isinstance(nodo, list):
        for valore in nodo[:8]:
            trovato = _trova_risultati(valore, profondita + 1)
            if trovato:
                return trovato
    return None


def _superficie(raw) -> Optional[float]:
    """'85 m²' -> 85.0"""
    if raw is None:
        return None
    m = re.search(r"(\d[\d.]*)", str(raw).replace(".", ""))
    return float(m.group(1)) if m else None


def scarica_annunci(comune: str, timeout: int = 25) -> list[dict]:
    """
    Annunci di vendita del comune, normalizzati a
    {prezzo, superficie, tipologia, citta}. Lista vuota se la pagina non è
    raggiungibile: la stima è un di più, non deve far fallire il run.
    """
    slug = comune_to_slug(comune)
    if not slug:
        return []
    try:
        r = creq.get(BASE_URL.format(comune=slug), impersonate=_IMPERSONATE,
                     headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return []
        m = _NEXT_DATA_RE.search(r.text)
        if not m:
            return []
        risultati = _trova_risultati(json.loads(m.group(1))) or []
    except Exception:
        return []

    annunci = []
    for item in risultati:
        immobile = item.get("realEstate") or item
        prezzo = (immobile.get("price") or {}).get("value")
        proprieta = (immobile.get("properties") or [{}])[0]
        annunci.append({
            "prezzo": prezzo,
            "superficie": _superficie(proprieta.get("surface")),
            "tipologia": ((proprieta.get("typology") or {}).get("name") or ""),
            "citta": ((proprieta.get("location") or {}).get("city") or ""),
        })
    return annunci


def _comparabile(annuncio: dict, superficie_target: Optional[float],
                 comune: Optional[str]) -> bool:
    prezzo, superficie = annuncio.get("prezzo"), annuncio.get("superficie")
    if not prezzo or not superficie or superficie <= 0:
        return False
    tipologia = _norm(annuncio.get("tipologia"))
    if any(escluso in tipologia for escluso in _TIPOLOGIE_ESCLUSE):
        return False
    # €/m² fuori scala: annuncio malformato o immobile non confrontabile
    prezzo_mq = prezzo / superficie
    if not (300 <= prezzo_mq <= 15_000):
        return False
    if comune and _norm(annuncio.get("citta")) and _norm(comune) not in _norm(annuncio["citta"]):
        return False
    if superficie_target:
        minimo = superficie_target * (1 - _TOLLERANZA_SUPERFICIE)
        massimo = superficie_target * (1 + _TOLLERANZA_SUPERFICIE)
        if not (minimo <= superficie <= massimo):
            return False
    return True


def prezzo_mq_zona(comune: str, superficie_target: Optional[float] = None,
                   annunci: Optional[list[dict]] = None) -> Optional[dict]:
    """
    Mediana del €/m² richiesto nel comune, sui soli annunci comparabili.
    Ritorna None se i comparabili non bastano a dire qualcosa di sensato.
    """
    annunci = scarica_annunci(comune) if annunci is None else annunci
    validi = [a for a in annunci if _comparabile(a, superficie_target, comune)]
    # se la fascia di superficie è troppo stretta, riprova senza quel vincolo
    if len(validi) < MIN_COMPARABILI and superficie_target:
        validi = [a for a in annunci if _comparabile(a, None, comune)]
    if len(validi) < MIN_COMPARABILI:
        return None
    prezzi_mq = sorted(a["prezzo"] / a["superficie"] for a in validi)
    return {
        "prezzo_mq_mediano": round(statistics.median(prezzi_mq)),
        "campione": len(validi),
        "prezzo_mq_min": round(prezzi_mq[0]),
        "prezzo_mq_max": round(prezzi_mq[-1]),
    }


def stima_da_comparabili(comune: str, superficie_mq: Optional[float],
                         annunci: Optional[list[dict]] = None) -> Optional[dict]:
    """
    Valore di mercato stimato = €/m² mediano della zona × superficie del lotto.
    Serve la superficie: senza, il €/m² non è convertibile in un valore.
    Ritorna {valore_stimato, prezzo_mq_mediano, campione, ...} o None.
    """
    if not superficie_mq or superficie_mq <= 0:
        return None
    zona = prezzo_mq_zona(comune, superficie_mq, annunci)
    if not zona:
        return None
    return {"valore_stimato": round(zona["prezzo_mq_mediano"] * superficie_mq), **zona}


def stima_lotti(lotti: list[dict], verbose: bool = True) -> dict:
    """
    Stima una lista di lotti raggruppandoli per comune, così ogni comune viene
    scaricato una volta sola. Ritorna {codice: stima}.
    """
    per_comune: dict[str, list[dict]] = {}
    for lotto in lotti:
        per_comune.setdefault(lotto.get("comune") or "", []).append(lotto)

    stime: dict[str, dict] = {}
    for comune, gruppo in per_comune.items():
        if not comune:
            continue
        annunci = scarica_annunci(comune)
        if verbose:
            print(f"  {comune}: {len(annunci)} annunci di mercato")
        for lotto in gruppo:
            stima = stima_da_comparabili(comune, lotto.get("superficie_mq"), annunci)
            if stima:
                stime[lotto["codice"]] = stima
        time.sleep(DELAY_TRA_RICHIESTE)
    return stime
