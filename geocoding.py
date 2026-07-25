"""
Geocoding degli indirizzi dei lotti (Nominatim / OpenStreetMap).

Perché serve: PVP fornisce le coordinate solo per circa un lotto su tre, e
senza coordinate la stima di mercato non può restringere i comparabili al
vicinato — nelle città grandi finisce per confrontare quartieri con prezzi
molto diversi. Geocodificare l'indirizzo colma il buco a costo zero.

Nominatim è gratuito ma chiede rispetto: max 1 richiesta al secondo e uno
User-Agent identificabile. Le risposte sono in cache per non ripetere la
stessa query nello stesso run.

Il risultato viene accettato solo se ricade nel comune atteso: un indirizzo
generico come "Via Roma" esiste in mezza Italia e senza questo controllo si
rischia di attribuire a un lotto le coordinate di un altro paese.
"""
from __future__ import annotations

import re
import time
import unicodedata
from functools import lru_cache
from typing import Optional

import requests

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "BotAste/1.0 (kamalbene40@gmail.com)"
# policy Nominatim: 1 richiesta al secondo
_RATE_LIMIT_S = 1.05
_ultima_chiamata = [0.0]

# Confine grossolano dell'Italia: scarta risposte palesemente fuori.
_BBOX_ITALIA = (35.0, 6.0, 47.5, 19.0)   # lat_min, lon_min, lat_max, lon_max


def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def _rispetta_rate_limit() -> None:
    trascorso = time.time() - _ultima_chiamata[0]
    if trascorso < _RATE_LIMIT_S:
        time.sleep(_RATE_LIMIT_S - trascorso)
    _ultima_chiamata[0] = time.time()


def pulisci_indirizzo(indirizzo: Optional[str]) -> str:
    """
    Toglie dall'indirizzo il rumore che confonde il geocoder: CAP, sigla
    provincia, "Italia" e il nome del comune ripetuto in coda — gli indirizzi
    PVP arrivano spesso come "Via Piave, 225, 21040 Cislago VA, Italia".
    """
    if not indirizzo:
        return ""
    testo = str(indirizzo)
    testo = re.sub(r"\b\d{5}\b", " ", testo)                    # CAP
    testo = re.sub(r"\bitalia\b", " ", testo, flags=re.I)
    testo = re.sub(r"\s*-\s*[A-Z]{2}\s*$", " ", testo)          # "- VA" finale
    testo = re.sub(r"[,\-]+\s*$", " ", testo)
    return " ".join(testo.split())


@lru_cache(maxsize=4096)
def _interroga(query: str) -> Optional[tuple]:
    """Una singola query a Nominatim, con rate-limit. (lat, lng, display_name)."""
    _rispetta_rate_limit()
    try:
        r = requests.get(
            NOMINATIM,
            params={"q": query, "format": "json", "limit": 1,
                    "countrycodes": "it", "addressdetails": 1},
            headers={"User-Agent": USER_AGENT, "Accept-Language": "it-IT,it"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        dati = r.json()
        if not dati:
            return None
        primo = dati[0]
        lat, lng = float(primo["lat"]), float(primo["lon"])
        lat_min, lon_min, lat_max, lon_max = _BBOX_ITALIA
        if not (lat_min <= lat <= lat_max and lon_min <= lng <= lon_max):
            return None
        return (lat, lng, primo.get("display_name", ""))
    except Exception:
        return None


def geocodifica(indirizzo: Optional[str], comune: Optional[str]) -> Optional[tuple]:
    """
    (lat, lng) dell'indirizzo, o None se non individuabile con certezza.

    Prova query via via più generiche e accetta solo i risultati che citano il
    comune atteso — senza quel controllo un "Via Roma" qualsiasi verrebbe
    piazzato dall'altra parte del paese.
    """
    if not comune:
        return None
    via = pulisci_indirizzo(indirizzo)
    comune_norm = _norm(comune)

    tentativi = []
    if via:
        tentativi.append(f"{via}, {comune}, Italia")
    tentativi.append(f"{comune}, Italia")      # centro del comune: meglio di niente

    for query in tentativi:
        esito = _interroga(query)
        if not esito:
            continue
        lat, lng, descrizione = esito
        if comune_norm and comune_norm not in _norm(descrizione):
            continue                            # ha trovato un altro posto
        return (lat, lng)
    return None
