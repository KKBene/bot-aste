"""
Scraper basato sull'API JSON di astalegale.net (molto più veloce di Playwright).

Architettura:
  - Lista lotti per comune  →  POST api.astalegale.net/Search  (JSON puro)
  - Dettaglio (link perizia, termine offerte, indirizzo)  →  GET pagina HTML SSR
    e parsing degli attributi md-value + link documenti.

Entrambi via `requests`, senza browser. Il download dei PDF resta in
pdf_analyzer.py (richiede la sessione browser per i cookie).

Strategia efficiente:
  - i prezzi correnti di TUTTI i lotti arrivano dalla sola Search (1 richiesta
    per comune) → price tracking e rilevamento "spariti/venduti" gratis;
  - il dettaglio HTML si scarica solo per i lotti NUOVI (per il link perizia).
"""
import html
import re
import time
import requests
from typing import Optional
from config import DELAY_TRA_COMUNI

BASE = "https://www.astalegale.net"
API = "https://api.astalegale.net"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
_HEADERS = {"User-Agent": UA, "Origin": BASE, "Referer": BASE + "/"}


# ─────────────────────────────────────────────────────────────
# PARSING
# ─────────────────────────────────────────────────────────────

def _parse_price(text) -> Optional[float]:
    """'€ 33.000,00' / 75793.1 → float. None se non valido."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = re.sub(r"[^\d,.]", "", str(text))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _norm_data(s: Optional[str]) -> Optional[str]:
    """'28/07/2026 - 11:00' → '28/07/2026 11:00'."""
    if not s:
        return None
    return re.sub(r"\s*-\s*", " ", str(s)).strip()


def _clean(s):
    """Decodifica entità HTML (es. &#39; → ') e normalizza spazi."""
    if not s:
        return s
    return re.sub(r"\s+", " ", html.unescape(str(s))).strip()


def _normalizza_item(item: dict, comune: str) -> dict:
    """Mappa un item dell'API Search sullo schema interno."""
    codice = item.get("id")
    friendly = item.get("friendlyId") or codice
    pos = item.get("posizione") or {}
    return {
        "codice": codice,
        "comune": comune,
        "link_dettaglio": f"{BASE}/Aste/Detail/{friendly}",
        "posizione_lat": pos.get("lat"),
        "posizione_lng": pos.get("lng"),
        "indirizzo_immobile": _clean(item.get("titolo")),
        "tipologia": _clean(item.get("tipologia")),
        "descrizione": _clean(item.get("descrizione")),
        "data_asta": _norm_data(item.get("dataAsta")),
        "tribunale": item.get("tribunale"),
        "numero_procedura": item.get("proceduraNumeroAnno"),
        "lotto": item.get("codiceLotto"),
        "prezzo_base": _parse_price(item.get("prezzoNum") or item.get("prezzo")),
        "offerta_minima": _parse_price(item.get("offertaMinima")),
        "immagine_url": item.get("urlImmaginePrincipale") or f"https://documents.astalegale.net/asta/0/{codice}",
        "_friendly_id": friendly,
    }


# ─────────────────────────────────────────────────────────────
# API: lista lotti per comune
# ─────────────────────────────────────────────────────────────

def cerca_comune(comune: str, categoria: str = "residenziali",
                 page_size: int = 100) -> list[dict]:
    """Restituisce i lotti di un comune via API Search (normalizzati)."""
    payload = {
        "categories": [categoria],
        "luoghi": [comune],
        "tipoDiRicerca": "Immobili",
        "ambitoVendita": [],
        "page": 1,
        "pageSize": page_size,
    }
    for attempt in range(3):
        try:
            r = requests.post(f"{API}/Search", headers=_HEADERS, json=payload, timeout=30)
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"    ⚠️ 429 rate limit — attendo {wait}s")
                time.sleep(wait)
                continue
            # 4xx (comune inesistente, richiesta invalida) → inutile ritentare
            if 400 <= r.status_code < 500:
                if r.status_code != 404:
                    print(f"    ⚠️ Search {comune}: HTTP {r.status_code}")
                return []
            r.raise_for_status()
            items = r.json().get("results", {}).get("currentPage", []) or []
            return [_normalizza_item(it, comune) for it in items if it.get("id")]
        except Exception as e:
            print(f"    ⚠️ Search {comune} fallita (tentativo {attempt+1}): {e}")
            time.sleep(3)
    return []


# ─────────────────────────────────────────────────────────────
# DETTAGLIO: md-value + link documenti dall'HTML SSR
# ─────────────────────────────────────────────────────────────

def _md_values(html: str) -> dict:
    """Estrae gli attributi md-value='chiave'>valore< dall'HTML SSR."""
    out = {}
    for m in re.finditer(r'md-value="([^"]+)"[^>]*>([^<]*)<', html):
        k = m.group(1).strip().lower()
        v = m.group(2).strip()
        if v and k not in out:
            out[k] = v
    return out


# Ogni documento nell'HTML è: <i class="fa-...file..."></i>LABEL (&lt; X Mb)</span> … <a href="DOC_URL">
# L'ancoraggio all'icona del file evita falsi match con la parola "perizia"
# che compare anche nel testo descrittivo dell'immobile.
_DOC_PATTERN = re.compile(
    r'fa-file[^>]*></i>\s*'
    r'(Perizia|Avviso|Ordinanza|Planimetri\w*|Documento)[^<]*</span>'
    r'.*?(https://documents\.astalegale\.net/file/0/[a-f0-9]+/\d{6,}-[A-Z])',
    re.IGNORECASE | re.DOTALL,
)
_LABEL_KEY = {"perizia": "link_perizia", "avviso": "link_avviso_vendita",
              "ordinanza": "link_ordinanza"}


def _link_documenti(html: str) -> dict:
    """Associa ogni documento (perizia/avviso/ordinanza/planimetrie) al suo link."""
    docs = {}
    planimetrie = []
    for m in _DOC_PATTERN.finditer(html):
        label = m.group(1).lower()
        url = m.group(2)   # già senza ?cd=true (il pattern termina al file id)
        if label.startswith("planimetri"):
            if url not in planimetrie:
                planimetrie.append(url)
        else:
            key = _LABEL_KEY.get(label)
            if key and key not in docs:   # tieni il primo (es. "Perizia 1")
                docs[key] = url
    docs["link_planimetrie"] = ", ".join(planimetrie) or None
    return docs


def arricchisci_dettaglio(asta: dict) -> dict:
    """
    Scarica la pagina dettaglio (HTML SSR) e aggiunge: link documenti,
    termine offerte, modalità gara, indirizzo preciso. Solo per lotti nuovi.
    """
    url = asta.get("link_dettaglio")
    if not url:
        return asta
    try:
        html = requests.get(url, headers=_HEADERS, timeout=30).text
    except Exception as e:
        print(f"      ⚠️ dettaglio {asta.get('codice')} fallito: {e}")
        return asta

    mv = _md_values(html)
    asta["termine_offerte"] = _norm_data(mv.get("termine presentazione offerte"))
    asta["modalita_gara"] = _clean(mv.get("modalità gara"))
    if mv.get("indirizzo lotto"):
        asta["indirizzo_immobile"] = _clean(mv["indirizzo lotto"])
    if mv.get("indirizzo vendita"):
        asta["indirizzo_asta"] = _clean(mv["indirizzo vendita"])
    # Prezzi dal dettaglio se mancanti dalla lista
    if asta.get("prezzo_base") is None:
        asta["prezzo_base"] = _parse_price(mv.get("prezzo base"))
    if asta.get("offerta_minima") is None:
        asta["offerta_minima"] = _parse_price(mv.get("offerta minima"))

    asta.update(_link_documenti(html))
    return asta


# ─────────────────────────────────────────────────────────────
# ENTRY POINT (stessa interfaccia di scraper_pw.run_scraper)
# ─────────────────────────────────────────────────────────────

def run_scraper(
    comuni: list[str],
    categoria: str,
    codici_esistenti: set,
    sheet_type: str = "residenziale",
    traccia_esistenti: bool = True,
) -> dict:
    """
    Scrapa i comuni via API e restituisce:
        {"nuovi": [...], "esistenti": [...], "codici_per_comune": {comune: set}}

    - I lotti NUOVI vengono arricchiti col dettaglio (link perizia, termine).
    - I lotti ESISTENTI portano solo i prezzi correnti (per il tracking),
      senza scaricare il dettaglio (già noto).
    """
    nuovi: list[dict] = []
    esistenti: list[dict] = []
    codici_per_comune: dict[str, set] = {}

    for idx, comune in enumerate(comuni, 1):
        print(f"\n  [{idx}/{len(comuni)}] {comune.upper()}")
        items = cerca_comune(comune, categoria)
        codici_per_comune[comune] = {a["codice"] for a in items}
        n_nuovi = sum(1 for a in items if a["codice"] not in codici_esistenti)
        print(f"    📊 {len(items)} lotti | Nuovi: {n_nuovi} | Esistenti: {len(items) - n_nuovi}")

        for asta in items:
            asta["sheet_type"] = sheet_type
            if asta["codice"] not in codici_esistenti:
                arricchisci_dettaglio(asta)
                prezzo = f"€{asta['prezzo_base']:,.0f}" if asta.get("prezzo_base") else "N/D"
                print(f"      ✓ NUOVO {asta['codice']} — {asta.get('indirizzo_immobile') or 'N/D'} — {prezzo}")
                nuovi.append(asta)
            elif traccia_esistenti:
                esistenti.append(asta)

        if idx < len(comuni):
            time.sleep(min(DELAY_TRA_COMUNI, 1.0))  # API veloce: pausa breve

    return {"nuovi": nuovi, "esistenti": esistenti, "codici_per_comune": codici_per_comune}
