"""
Playwright async scraper per astalegale.net.
Sostituisce Selenium — più veloce, più stabile, headless nativo.
"""
import asyncio
import random
import re
from typing import Optional
from playwright.async_api import async_playwright, Page, BrowserContext
from config import (
    DELAY_TRA_ANNUNCI, DELAY_TRA_COMUNI,
    SCRAPER_TIMEOUT, CATEGORIA_RESIDENZIALE,
)

BASE_URL = "https://www.astalegale.net"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# Jitter casuale sui delay per sembrare più umano
def _jitter(base: float, pct: float = 0.3) -> float:
    return base + random.uniform(-base * pct, base * pct)

# ─────────────────────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────────────────────

def _parse_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    clean = re.sub(r"[^\d,.]", "", str(text)).replace(".", "").replace(",", ".")
    try:
        return float(clean)
    except ValueError:
        return None


def _codice_da_url(url: str) -> Optional[str]:
    m = re.search(r"/Detail/([A-Z0-9]+)", url)
    return m.group(1) if m else None


# ─────────────────────────────────────────────────────────────
# RACCOLTA LINK DA PAGINA LISTA
# ─────────────────────────────────────────────────────────────

async def _raccogli_links(page: Page, comune: str, categoria: str) -> list[dict]:
    """Naviga alla pagina di ricerca e raccoglie tutti i link agli annunci."""
    citta_url = comune.lower().replace(" ", "%20")
    url = f"{BASE_URL}/Immobili?categories={categoria}&luoghi={citta_url}"
    print(f"    🔍 {url}")

    # Retry su 429 con backoff esponenziale
    for attempt in range(3):
        resp = await page.goto(url, wait_until="networkidle", timeout=SCRAPER_TIMEOUT)
        await page.wait_for_timeout(_jitter(2500))

        if resp and resp.status == 429:
            wait_s = 60 * (attempt + 1)
            print(f"    ⚠️ 429 Rate limit — attendo {wait_s}s (tentativo {attempt+1}/3)")
            await asyncio.sleep(wait_s)
            continue
        break  # ok, usciamo dal loop

    # Cerca link /Aste/Detail/ — funziona sia con card che con lista
    hrefs: list[str] = await page.eval_on_selector_all(
        'a[href*="/Aste/Detail/"]',
        "els => [...new Set(els.map(e => e.href))]",
    )

    annunci = []
    for href in hrefs:
        codice = _codice_da_url(href)
        if codice:
            annunci.append({"codice": codice, "link_dettaglio": href, "comune": comune})

    # Gestione paginazione (se presente)
    next_page = await page.query_selector('a[aria-label="Next"], a.next, [rel="next"]')
    if next_page and len(hrefs) > 0:
        next_href = await next_page.get_attribute("href")
        if next_href:
            next_url = next_href if next_href.startswith("http") else BASE_URL + next_href
            await page.goto(next_url, wait_until="networkidle", timeout=SCRAPER_TIMEOUT)
            await page.wait_for_timeout(1500)
            hrefs2: list[str] = await page.eval_on_selector_all(
                'a[href*="/Aste/Detail/"]',
                "els => [...new Set(els.map(e => e.href))]",
            )
            for href in hrefs2:
                codice = _codice_da_url(href)
                if codice and not any(a["codice"] == codice for a in annunci):
                    annunci.append({"codice": codice, "link_dettaglio": href, "comune": comune})

    print(f"    📊 Trovati {len(annunci)} annunci")
    return annunci


# ─────────────────────────────────────────────────────────────
# ESTRAZIONE DETTAGLIO SINGOLO ANNUNCIO
# ─────────────────────────────────────────────────────────────

# Selettore che indica che la SPA Vue ha caricato i dati dell'asta
_READY_SELECTOR = '[md-value="Prezzo base"]'

_EXTRACT_JS = """
() => {
    const r = {};

    // astalegale.net espone i dati come attributi md-value="..." sugli <span>.
    // È molto più robusto del matching label+sibling.
    const map = {};
    for (const el of document.querySelectorAll('[md-value]')) {
        const k = el.getAttribute('md-value').trim().toLowerCase();
        const v = el.textContent.trim();
        if (v && !(k in map)) map[k] = v;
    }
    const get = (...keys) => {
        for (const k of keys) {
            const v = map[k.toLowerCase()];
            if (v) return v;
        }
        return null;
    };

    r.indirizzo_immobile  = get('indirizzo lotto', 'indirizzo', 'titolo');
    r.indirizzo_asta      = get('indirizzo vendita');
    r.tipologia           = get('tipologia');
    r.tipologia_ministeriale = get('tipologia ministeriale');
    r.prezzo_text         = get('prezzo base');
    r.offerta_minima_text = get('offerta minima');
    r.rialzo_minimo_text  = get('rialzo minimo');
    r.data_asta           = get('data asta');
    r.termine_offerte     = get('termine presentazione offerte');
    r.modalita_gara       = get('modalità gara');
    r.tribunale           = get('tribunale');
    r.numero_procedura    = get('numero procedura');
    r.anno_procedura      = get('anno procedura');
    r.lotto               = get('codice lotto');
    r.descrizione         = get('descrizione testuale', 'descrizione');
    r.stato_occ_listino   = get('stato occupazione');
    r.custode             = get('custode');

    // Documenti (non usano md-value: si scansionano le list-group-item)
    r.link_avviso_vendita = null;
    r.link_perizia        = null;
    r.link_ordinanza      = null;
    const planimetrie     = [];

    for (const item of document.querySelectorAll('.list-group-item')) {
        let docText = '';
        for (const s of item.querySelectorAll('span')) {
            const t = s.textContent.trim();
            if (t && t.length > 4) { docText = t.toLowerCase(); break; }
        }
        let href = null;
        for (const a of item.querySelectorAll('a')) {
            const h = a.href || '';
            if (h.includes('documents.astalegale.net') && !h.includes('?cd=true')) {
                href = h; break;
            }
        }
        if (!href) continue;

        if (docText.includes('avviso') && !r.link_avviso_vendita) {
            r.link_avviso_vendita = href;
        } else if (docText.includes('perizia') && !r.link_perizia) {
            r.link_perizia = href;
        } else if (docText.includes('ordinanza') && !r.link_ordinanza) {
            r.link_ordinanza = href;
        } else if (docText.includes('planimetria')) {
            planimetrie.push(href);
        }
    }
    r.link_planimetrie = planimetrie.join(', ') || null;

    return r;
}
"""


async def _estrai_dettaglio(page: Page, asta: dict) -> dict:
    """Visita la pagina dettaglio ed estrae tutti i dati strutturati."""
    await page.goto(asta["link_dettaglio"], wait_until="networkidle", timeout=SCRAPER_TIMEOUT)
    # Attendi che la SPA renderizzi i dati (evita risultati N/D per timing)
    try:
        await page.wait_for_selector(_READY_SELECTOR, timeout=8000)
    except Exception:
        await page.wait_for_timeout(1500)  # fallback: pagine senza prezzo

    dati = await page.evaluate(_EXTRACT_JS)

    # Combina numero/anno procedura → "192/2022"
    if dati.get("numero_procedura") and dati.get("anno_procedura"):
        dati["numero_procedura"] = f"{dati['numero_procedura']}/{dati['anno_procedura']}"
    dati.pop("anno_procedura", None)

    asta.update(dati)
    asta["prezzo_base"] = _parse_price(dati.get("prezzo_text"))
    asta["offerta_minima"] = _parse_price(dati.get("offerta_minima_text"))

    # Log sintetico
    prezzo_str = f"€{asta['prezzo_base']:,.0f}" if asta.get("prezzo_base") else "N/D"
    print(
        f"      ✓ {asta['codice']} — {asta.get('indirizzo_immobile') or 'N/D'} — {prezzo_str}"
    )
    return asta


# ─────────────────────────────────────────────────────────────
# ENTRY POINT PRINCIPALE
# ─────────────────────────────────────────────────────────────

async def run_scraper(
    comuni: list[str],
    categoria: str,
    codici_esistenti: set,
    sheet_type: str = "residenziale",
    traccia_esistenti: bool = True,
) -> dict:
    """
    Scrapa tutti i comuni e restituisce un dizionario:
        {
          "nuovi": [...],              # annunci nuovi (dati completi)
          "esistenti": [...],          # annunci già nel DB, ri-scrapati (per i prezzi)
          "codici_per_comune": {comune: set(codici)},  # tutti i codici visti
        }

    Con traccia_esistenti=False, gli annunci già presenti non vengono ri-visitati
    (modalità leggera "solo nuovi", più veloce ma senza tracking dei prezzi).
    """
    nuovi_annunci: list[dict] = []
    esistenti_annunci: list[dict] = []
    codici_per_comune: dict[str, set] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context: BrowserContext = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
        )

        page = await context.new_page()
        # Blocca solo media pesanti (video/audio) — non immagini per sembrare umano
        await page.route("**/*.{mp4,webm,ogg,mp3,wav}", lambda r: r.abort())

        try:
            for idx, comune in enumerate(comuni, 1):
                print(f"\n  [{idx}/{len(comuni)}] {comune.upper()}")

                try:
                    annunci = await _raccogli_links(page, comune, categoria)
                    codici_per_comune[comune] = {a["codice"] for a in annunci}

                    # Decidi quali visitare: tutti, o solo i nuovi
                    da_visitare = annunci if traccia_esistenti else [
                        a for a in annunci if a["codice"] not in codici_esistenti
                    ]
                    n_nuovi = sum(1 for a in annunci if a["codice"] not in codici_esistenti)
                    print(f"    ✅ Nuovi: {n_nuovi} | Già presenti: {len(annunci) - n_nuovi}")

                    for i, asta in enumerate(da_visitare, 1):
                        e_nuovo = asta["codice"] not in codici_esistenti
                        tag = "NUOVO" if e_nuovo else "aggiorno"
                        print(f"    [{i}/{len(da_visitare)}] {tag} {asta['codice']}...")
                        try:
                            asta["sheet_type"] = sheet_type
                            asta = await _estrai_dettaglio(page, asta)
                            (nuovi_annunci if e_nuovo else esistenti_annunci).append(asta)
                            await asyncio.sleep(_jitter(DELAY_TRA_ANNUNCI))
                        except Exception as e:
                            print(f"      ❌ Errore dettaglio {asta.get('codice')}: {e}")

                    if idx < len(comuni):
                        await asyncio.sleep(_jitter(DELAY_TRA_COMUNI))

                except Exception as e:
                    print(f"    ❌ Errore comune {comune}: {e}")

        finally:
            await browser.close()

    return {
        "nuovi": nuovi_annunci,
        "esistenti": esistenti_annunci,
        "codici_per_comune": codici_per_comune,
    }
