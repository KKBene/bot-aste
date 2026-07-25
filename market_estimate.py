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
import math
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
# Vicinato: su una città grande la mediana comunale mescola quartieri con
# prezzi molto diversi (a Genova, Albaro e Certosa), quindi quando il lotto ha
# coordinate si guarda prima chi gli sta davvero vicino.
RAGGIO_VICINATO_KM = 2.0
DELAY_TRA_RICHIESTE = 1.5
# Pagine di annunci da scaricare per comune (25 per pagina): in una città
# grande la sola prima pagina non basta a trovare comparabili vicini al lotto.
PAGINE_DA_SCARICARE = 4

# Confrontare un immobile da ristrutturare con annunci di ristrutturati gonfia
# la stima anche del doppio: lo stato di conservazione pesa quanto la zona.
# Mappa lo `stato_manutentivo` della perizia sulle condizioni dichiarate negli
# annunci (campo ga4Condition), tenendo le fasce adiacenti per non ridurre
# troppo il campione.
_CONDIZIONI_COMPATIBILI = {
    "OTTIMO":   {"ottimo / ristrutturato", "nuovo / in costruzione"},
    "BUONO":    {"buono / abitabile", "ottimo / ristrutturato"},
    "MEDIOCRE": {"buono / abitabile", "da ristrutturare"},
    "PESSIMO":  {"da ristrutturare"},
    "RUDERE":   {"da ristrutturare"},
}

# Quando in zona non ci sono abbastanza annunci nello stesso stato, il
# riferimento resta quello di immobili mediamente abitabili. Questi fattori
# lo riportano verso il basso per gli immobili malmessi: sono prudenziali e
# volutamente grossolani, servono a non spacciare per valore di un rudere il
# prezzo di una casa abitabile. Il report dichiara sempre quando è applicato.
_SCONTO_STATO_NON_FILTRATO = {
    "MEDIOCRE": 0.85,
    "PESSIMO": 0.70,
    "RUDERE": 0.55,
}


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


def _scarica_pagina(slug: str, pagina: int, timeout: int) -> list:
    # `pag` da solo viene rifiutato con 403: va accompagnato da `criterio`.
    url = BASE_URL.format(comune=slug) + "?criterio=rilevanza"
    if pagina > 1:
        url += f"&pag={pagina}"
    try:
        r = creq.get(url, impersonate=_IMPERSONATE, headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return []
        m = _NEXT_DATA_RE.search(r.text)
        if not m:
            return []
        return _trova_risultati(json.loads(m.group(1))) or []
    except Exception:
        return []


def scarica_annunci(comune: str, timeout: int = 25,
                    pagine: int = PAGINE_DA_SCARICARE) -> list[dict]:
    """
    Annunci di vendita del comune, normalizzati. Lista vuota se la pagina non
    è raggiungibile: la stima è un di più, non deve far fallire il run.

    Si scaricano più pagine perché in una città grande i 25 annunci della
    prima non bastano a trovarne abbastanza vicini al lotto, e il confronto
    per vicinato — quello che serve proprio lì — non scatterebbe mai.
    """
    slug = comune_to_slug(comune)
    if not slug:
        return []

    risultati, visti = [], set()
    for pagina in range(1, max(1, pagine) + 1):
        blocco = _scarica_pagina(slug, pagina, timeout)
        if not blocco:
            break                      # pagina vuota: il comune è finito
        nuovi = [r for r in blocco
                 if (r.get("realEstate") or r).get("id") not in visti]
        if not nuovi:
            break                      # pagina ripetuta: niente altro da prendere
        visti.update((r.get("realEstate") or r).get("id") for r in nuovi)
        risultati.extend(nuovi)
        if pagina < pagine:
            time.sleep(DELAY_TRA_RICHIESTE)

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
            "condizione": proprieta.get("ga4Condition") or "",
            "lat": (proprieta.get("location") or {}).get("latitude"),
            "lng": (proprieta.get("location") or {}).get("longitude"),
            "microzona": (proprieta.get("location") or {}).get("microzone") or "",
        })
    return annunci


def distanza_km(lat1, lng1, lat2, lng2) -> Optional[float]:
    """Distanza in linea d'aria (haversine) fra due punti, None se mancano dati."""
    if None in (lat1, lng1, lat2, lng2):
        return None
    try:
        lat1, lng1, lat2, lng2 = map(float, (lat1, lng1, lat2, lng2))
    except (TypeError, ValueError):
        return None
    r = 6371.0
    dlat, dlng = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _entro_raggio(annuncio: dict, coord: Optional[tuple], raggio_km: Optional[float]) -> bool:
    """L'annuncio è abbastanza vicino al lotto? Senza coordinate non filtra."""
    if not raggio_km or not coord:
        return True
    d = distanza_km(coord[0], coord[1], annuncio.get("lat"), annuncio.get("lng"))
    return d is not None and d <= raggio_km


def _condizione_compatibile(annuncio: dict, stato: Optional[str]) -> bool:
    """L'annuncio è in uno stato di conservazione confrontabile col lotto?"""
    if not stato:
        return True                       # stato del lotto ignoto: non filtrare
    ammesse = _CONDIZIONI_COMPATIBILI.get(str(stato).upper().strip())
    if not ammesse:
        return True
    cond = _norm(annuncio.get("condizione"))
    if not cond:
        return False                      # senza condizione dichiarata non è confrontabile
    return cond in ammesse


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
                   annunci: Optional[list[dict]] = None,
                   stato: Optional[str] = None,
                   coord: Optional[tuple] = None) -> Optional[dict]:
    """
    Mediana del €/m² richiesto, sui soli annunci comparabili.

    Se il lotto ha coordinate si parte dal vicinato (`RAGGIO_VICINATO_KM`):
    su una città grande la mediana comunale mescola quartieri con prezzi
    molto diversi e non dice nulla di utile. I criteri si allentano per gradi
    — prima il raggio, poi la superficie, poi lo stato — e il livello
    raggiunto viene riportato in `base_confronto`, così chi legge sa se sta
    guardando immobili davvero simili o solo la media del comune.
    """
    annunci = scarica_annunci(comune) if annunci is None else annunci
    vicino = RAGGIO_VICINATO_KM if coord else None

    tentativi = [
        ("in zona, stato e superficie simili", vicino, superficie_target, stato),
        ("in zona, stato simile", vicino, None, stato),
        ("in zona", vicino, None, None),
        ("stato e superficie simili", None, superficie_target, stato),
        ("stato simile", None, None, stato),
        ("superficie simile", None, superficie_target, None),
        ("comune", None, None, None),
    ]
    for etichetta, raggio, sup, st in tentativi:
        # senza coordinate del lotto il filtro per vicinato non è applicabile:
        # saltare quei tentativi, altrimenti restituirebbero l'etichetta
        # "in zona" pur avendo confrontato tutto il comune.
        if etichetta.startswith("in zona") and not vicino:
            continue
        validi = [a for a in annunci
                  if _comparabile(a, sup, comune)
                  and _condizione_compatibile(a, st)
                  and _entro_raggio(a, coord, raggio)]
        if len(validi) >= MIN_COMPARABILI:
            prezzi_mq = sorted(a["prezzo"] / a["superficie"] for a in validi)
            return {
                "prezzo_mq_mediano": round(statistics.median(prezzi_mq)),
                "campione": len(validi),
                "prezzo_mq_min": round(prezzi_mq[0]),
                "prezzo_mq_max": round(prezzi_mq[-1]),
                "base_confronto": etichetta,
            }
    return None


def stima_da_comparabili(comune: str, superficie_mq: Optional[float],
                         annunci: Optional[list[dict]] = None,
                         stato: Optional[str] = None,
                         coord: Optional[tuple] = None) -> Optional[dict]:
    """
    Valore di mercato stimato = €/m² mediano della zona × superficie del lotto.
    Serve la superficie: senza, il €/m² non è convertibile in un valore.
    `stato` è lo stato_manutentivo del lotto: senza, la stima confronta con
    immobili di qualunque condizione e tende a sovrastimare un immobile da
    ristrutturare. Ritorna {valore_stimato, prezzo_mq_mediano, campione, ...}.
    """
    if not superficie_mq or superficie_mq <= 0:
        return None
    zona = prezzo_mq_zona(comune, superficie_mq, annunci, stato, coord)
    if not zona:
        return None

    valore = zona["prezzo_mq_mediano"] * superficie_mq
    # Se non siamo riusciti a filtrare per stato (comparabili insufficienti),
    # il riferimento resta quello di immobili mediamente abitabili: applicarlo
    # tale e quale a un immobile malmesso lo sopravvaluta. Meglio uno sconto
    # prudenziale, dichiarato, che una cifra gonfiata.
    sconto = None
    if stato and "stato" not in (zona.get("base_confronto") or ""):
        sconto = _SCONTO_STATO_NON_FILTRATO.get(str(stato).upper().strip())
        if sconto:
            valore *= sconto

    risultato = {"valore_stimato": round(valore), **zona}
    if sconto:
        risultato["sconto_stato"] = sconto
    return risultato


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
            lat, lng = lotto.get("posizione_lat"), lotto.get("posizione_lng")
            stima = stima_da_comparabili(
                comune, lotto.get("superficie_mq"), annunci,
                lotto.get("stato_manutentivo"), (lat, lng) if lat and lng else None)
            if stima:
                stime[lotto["codice"]] = stima
        time.sleep(DELAY_TRA_RICHIESTE)
    return stime
