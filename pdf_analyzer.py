"""
Analisi PDF perizie con router AI.

Pipeline:
  - PDF con testo estraibile: Groq (default) -> Gemini fallback.
  - PDF scansionati: Mistral OCR -> Groq sul testo OCR -> Gemini Vision fallback.
  - Output normalizzato e, dove supportato, vincolato a JSON schema.
"""
import base64
import json
import re
import time
from pathlib import Path
from typing import Optional
import fitz  # PyMuPDF
import requests
from google import genai
from config import (
    GEMINI_API_KEY, GEMINI_MODEL, MAX_PDF_PAGES, MAX_PDF_CHARS, PDF_RETRY_ATTEMPTS,
    PDF_TEXT_PROVIDER, GROQ_MAX_PROMPT_CHARS, USE_MISTRAL_OCR, PDF_SCAN_ANALYSIS_MODE,
)
try:
    from config import MIN_TESTO_REALE, MIN_CHAR_PER_PAGINA
except ImportError:
    MIN_TESTO_REALE = 400
    MIN_CHAR_PER_PAGINA = 80
try:
    from config import GEMINI_MODEL_FALLBACK
except ImportError:
    GEMINI_MODEL_FALLBACK = "gemini-flash-lite-latest"

try:
    from config import GROQ_API_KEY, GROQ_MODEL
except ImportError:
    GROQ_API_KEY = ""
    GROQ_MODEL = "llama-3.3-70b-versatile"

try:
    from config import MISTRAL_API_KEY, MISTRAL_OCR_MODEL
except ImportError:
    MISTRAL_API_KEY = ""
    MISTRAL_OCR_MODEL = "mistral-ocr-latest"

_JSON_SCHEMA = {
    "name": "analisi_perizia_asta",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "stato_occupazione": {
                "type": ["string", "null"],
                "enum": ["LIBERO", "OCCUPATO_DEBITORE", "OCCUPATO_CON_TITOLO", "OCCUPATO_SENZA_TITOLO", None],
            },
            "occupazione_opponibile": {"type": ["boolean", "null"]},
            "costi_sanatoria": {"type": ["number", "null"]},
            "superficie_mq": {"type": ["number", "null"]},
            "stato_manutentivo": {
                "type": ["string", "null"],
                "enum": ["OTTIMO", "BUONO", "MEDIOCRE", "PESSIMO", "RUDERE", None],
            },
            "piano_ascensore": {"type": ["string", "null"]},
            "valore_mercato": {"type": ["number", "null"]},
            "spese_condominiali_arretrate": {"type": ["number", "null"]},
            "spese_condominiali_annue": {"type": ["number", "null"]},
            "spese_straordinarie_deliberate": {"type": ["number", "null"]},
            "rendita_catastale": {"type": ["number", "null"]},
            "canone_locazione_annuo": {"type": ["number", "null"]},
            "pertinenze": {"type": ["string", "null"]},
            "quota_proprieta": {"type": ["string", "null"]},
            "categoria_catastale": {"type": ["string", "null"]},
            "anno_costruzione": {"type": ["number", "null"]},
            "classe_energetica": {"type": ["string", "null"]},
            "tipologia_immobile": {"type": ["string", "null"]},
            "distanza_stazione_km": {"type": ["number", "null"]},
            "qualita_posizione": {
                "type": ["string", "null"],
                "enum": ["OTTIMA", "BUONA", "MEDIA", "SCARSA", None],
            },
            "note_critiche": {"type": "string"},
        },
        "required": [
            "stato_occupazione", "occupazione_opponibile", "costi_sanatoria",
            "superficie_mq", "stato_manutentivo", "piano_ascensore",
            "valore_mercato", "spese_condominiali_arretrate",
            "spese_condominiali_annue", "spese_straordinarie_deliberate",
            "rendita_catastale", "canone_locazione_annuo", "pertinenze",
            "quota_proprieta", "categoria_catastale", "anno_costruzione",
            "classe_energetica", "tipologia_immobile", "distanza_stazione_km",
            "qualita_posizione", "note_critiche",
        ],
    },
    "strict": True,
}

_ISTRUZIONI = """\
Sei un analista immobiliare esperto in aste giudiziarie italiane.
Analizza la perizia CTU e restituisci SOLO un oggetto JSON, nessun testo prima o dopo.

ISTRUZIONI DI ESTRAZIONE:

1. stato_occupazione (string, OBBLIGATORIO):
   Scegli ESATTAMENTE uno tra: LIBERO | OCCUPATO_DEBITORE | OCCUPATO_CON_TITOLO | OCCUPATO_SENZA_TITOLO
   - LIBERO: vuoto, con custode, o libero da persone.
   - OCCUPATO_DEBITORE: abita l'esecutato/famiglia (si libera al decreto di trasferimento).
   - OCCUPATO_CON_TITOLO: contratto affitto registrato o diritto reale opponibile.
   - OCCUPATO_SENZA_TITOLO: occupazione abusiva o contratto scaduto non rinnovato.

2. occupazione_opponibile (boolean o null):
   SOLO se occupato con contratto. true se il contratto è OPPONIBILE all'acquirente
   (registrato PRIMA del pignoramento), false se NON opponibile (registrato DOPO il
   pignoramento → l'immobile di fatto si libera). null se non applicabile o non chiaro.

3. costi_sanatoria (number):
   Somma in euro per sanare difformità urbanistiche/catastali/impiantistiche.
   Include "Spese di regolarizzazione delle difformità" e dichiarazioni di conformità.
   0 se conforme. Solo il numero.

4. superficie_mq (number):
   "Consistenza commerciale complessiva unità principali" o superficie commerciale principale in mq.

5. stato_manutentivo (string):
   Scegli ESATTAMENTE uno tra: OTTIMO | BUONO | MEDIOCRE | PESSIMO | RUDERE
   Mappa: "nella media"→BUONO, "al di sotto della media"/"mediocre"→MEDIOCRE.

6. piano_ascensore (string):
   Formato: "Piano X - Ascensore SI/NO/ND". Es: "Secondo - Ascensore NO".

7. valore_mercato (number):
   "Valore di Mercato dell'immobile" (OMV) stimato dal perito, in euro. Solo il numero.
   NON confondere con valore di vendita giudiziaria. 0 se non trovato.
   Se nel documento compaiono più valori, usa il valore finale della sezione
   "VALORE DI MERCATO (OMV)" o "Valore di Mercato dell'immobile nello stato di
   fatto e di diritto in cui si trova", cioè DOPO decurtazioni tecniche/sanatorie.
   NON usare il solo "Valore superficie principale" se più avanti c'è un OMV finale.

8. spese_condominiali_arretrate (number):
   "Spese condominiali scadute ed insolute" che l'acquirente dovrà pagare (art. 568 cpc).
   In euro. 0 se nessuna o immobile non condominiale.

8b. spese_condominiali_annue (number):
   "Spese ordinarie annue di gestione dell'immobile" — i costi condominiali fissi
   RICORRENTI per anno (NON gli arretrati). In euro. 0 se non condominiale/non indicato.

8c. spese_straordinarie_deliberate (number):
   "Spese straordinarie di gestione già deliberate ma non ancora scadute" — costi
   futuri che l'acquirente erediterà. In euro. 0 se non indicato.

8d. rendita_catastale (number):
   "Rendita" catastale in euro (es. "Rendita Euro 383,47" → 383.47). Serve a stimare
   l'IMU. Se più unità, somma le rendite. null se non indicata.

8e. canone_locazione_annuo (number):
   SOLO se l'immobile è affittato: il canone annuo dichiarato nel contratto
   (es. "importo dichiarato di 5.400,00" → 5400). null se non affittato/non indicato.

8f. pertinenze (string):
   Pertinenze e accessori inclusi: cantina, box, autorimessa, posto auto, giardino,
   soffitta, terrazzo. Elenco breve separato da virgola. "" se nessuna.

9. quota_proprieta (string):
   Quota e diritto venduto. Es: "1/1 piena proprietà", "1/2 piena proprietà", "nuda proprietà".
   Default "1/1 piena proprietà" se non specificato diversamente.

10. categoria_catastale (string):
    Categoria catastale dell'unità principale. Es: "A/2", "A/3", "A/7". null se assente.

11. anno_costruzione (number o null):
    Anno di costruzione dell'edificio. Riconosci varie forme:
    "costruito nel 1968"→1968; "ante 1967"→1967; "anni '60"→1960;
    "epoca di costruzione: 1950 circa"→1950; "risalente agli anni Settanta"→1970.
    null SOLO se davvero non desumibile.

12. classe_energetica (string o null):
    Classe energetica APE: la LETTERA (A4,A3,A2,A1,A,B,C,D,E,F,G).
    Cerca "classe energetica X" / "classe X". NON confonderla col NUMERO
    di protocollo dell'APE. null se è indicato solo il numero APE senza la classe.

13. tipologia_immobile (string):
    Es: "appartamento", "villa singola", "villetta a schiera", "bilocale". null se non chiaro.

14. distanza_stazione_km (number o null):
    Distanza in KM dalla stazione ferroviaria (sezione COLLEGAMENTI/SERVIZI).
    Cerca "ferrovia", "stazione", "treno". CONVERTI i metri in km: "300 m" → 0.3;
    "km 1,50" → 1.5. null se non indicata.

15. qualita_posizione (string):
    Valuta la zona dalle sezioni DESCRIZIONE ZONA / SERVIZI / COLLEGAMENTI / rating.
    Scegli ESATTAMENTE uno tra: OTTIMA | BUONA | MEDIA | SCARSA
    (centrale e ben servita=OTTIMA; periferica/isolata/servizi scarsi=SCARSA).

16. note_critiche (string):
    Max 120 caratteri. SOLO problemi gravi: inagibilità, amianto, abusi NON sanabili
    (da demolire), quota parziale, contratto opponibile lungo, debiti condominiali elevati.
    Se nessun problema grave: stringa vuota "".

FORMATO RISPOSTA (solo JSON):
{
  "stato_occupazione": "...",
  "occupazione_opponibile": null,
  "costi_sanatoria": 0,
  "superficie_mq": 0,
  "stato_manutentivo": "...",
  "piano_ascensore": "...",
  "valore_mercato": 0,
  "spese_condominiali_arretrate": 0,
  "spese_condominiali_annue": 0,
  "spese_straordinarie_deliberate": 0,
  "rendita_catastale": null,
  "canone_locazione_annuo": null,
  "pertinenze": "",
  "quota_proprieta": "1/1 piena proprietà",
  "categoria_catastale": null,
  "anno_costruzione": null,
  "classe_energetica": null,
  "tipologia_immobile": null,
  "distanza_stazione_km": null,
  "qualita_posizione": "MEDIA",
  "note_critiche": ""
}
"""

def _build_prompt(testo: str, hints: Optional[dict] = None) -> str:
    """Costruisce il prompt testuale (evita str.format per via delle graffe nel JSON)."""
    hint_txt = ""
    if hints:
        hint_txt = (
            "\nHINT DETERMINISTICI AD ALTA CONFIDENZA:\n"
            f"{json.dumps(hints, ensure_ascii=False)}\n"
            "Se un hint è presente, usalo al posto di valori intermedi nel documento.\n"
        )
    return f"{_ISTRUZIONI}{hint_txt}\nTESTO PERIZIA:\n<document>\n{testo}\n</document>\n"


_KEYWORDS_RILEVANTI = [
    "valore di mercato", "valore dell'immobile", "omv", "valutazione complessiva",
    "calcolo del valore", "spese di regolarizzazione", "difformità", "sanatoria",
    "stato di possesso", "occupazione", "libero", "occupato", "contratto",
    "superficie", "consistenza", "condominiali", "rendita", "catastale",
    "quota", "piena proprietà", "nuda proprietà", "stato manutentivo",
    "conservazione", "ascensore", "classe energetica", "ape", "pertinenze",
    "stazione", "collegamenti", "servizi", "zona",
]


def _riduci_testo_per_groq(testo: str, max_chars: int = GROQ_MAX_PROMPT_CHARS) -> str:
    """
    Riduce testi OCR lunghi mantenendo le righe più utili per l'estrazione.
    Serve a stare sotto il payload Groq e a ridurre il rischio che il modello
    scelga valori intermedi invece delle sezioni finali della perizia.
    """
    if len(testo) <= max_chars:
        return testo

    righe = testo.splitlines()
    selezionate: list[str] = []
    prese = set()

    def aggiungi_range(center: int, radius: int = 4) -> None:
        start = max(0, center - radius)
        end = min(len(righe), center + radius + 1)
        for j in range(start, end):
            if j not in prese:
                selezionate.append(righe[j])
                prese.add(j)

    # Testa e coda mantengono identificativi e riepiloghi finali.
    for i in range(min(80, len(righe))):
        aggiungi_range(i, 0)
    for i in range(max(0, len(righe) - 140), len(righe)):
        aggiungi_range(i, 0)

    for i, riga in enumerate(righe):
        low = riga.lower()
        if any(k in low for k in _KEYWORDS_RILEVANTI):
            aggiungi_range(i)

    ridotto = "\n".join(selezionate)
    if len(ridotto) > max_chars:
        # Priorità alla parte finale: contiene spesso OMV e riepilogo costi.
        head = ridotto[: max_chars // 3]
        tail = ridotto[-(max_chars - len(head) - 80):]
        ridotto = head + "\n[... testo OCR ridotto ...]\n" + tail
    return ridotto


def _parse_euro(val: str) -> Optional[float]:
    s = re.sub(r"[^\d,.\-]", "", val or "")
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


def _hints_deterministici(testo: str) -> dict:
    """Estrae valori economici finali con regex ad alta precisione dal testo/OCR."""
    hints = {}
    patterns = {
        "valore_mercato": [
            r"Valore di Mercato dell'immobile nello stato di fatto e di diritto in cui si trova:\s*€\.?\s*([\d.,]+)",
            r"Valore di mercato \(calcolato in quota e diritto al netto degli aggiustamenti\):\s*€\.?\s*([\d.,]+)",
        ],
        "costi_sanatoria": [
            r"Spese di regolarizzazione delle difformità[^:]*:\s*€\.?\s*([\d.,]+)",
            r"Costi? (?:stimati? )?per sanatoria:\s*€?\s*([\d.,]+)",
        ],
        "spese_condominiali_arretrate": [
            r"Spese condominiali scadute[^:]*:\s*€\.?\s*([\d.,]+)",
        ],
        "spese_condominiali_annue": [
            r"Spese ordinarie annue di gestione dell'immobile:\s*€\.?\s*([\d.,]+)",
        ],
    }
    for campo, pats in patterns.items():
        for pat in pats:
            valori = []
            for m in re.finditer(pat, testo, re.IGNORECASE):
                val = _parse_euro(m.group(1))
                if val is not None and val > 0:
                    valori.append(val)
            if valori:
                # Per il valore_mercato le perizie multi-lotto elencano
                # più OMV (es. villa €489k + terreno €15k): prendi il MAX,
                # che corrisponde all'unità principale. Per gli altri costi
                # (sanatoria, spese) prendi l'ultimo (= valore di riepilogo).
                hints[campo] = max(valori) if campo == "valore_mercato" else valori[-1]
                break

    # Quota di proprietà frazionata (es. "per la quota di 1/3"): è un fattore di
    # rischio critico (moltiplicatore di score) che gli LLM tendono a mancare,
    # defaultando a "1/1". Se il documento indica una frazione <1 vicino a
    # "quota", la imponiamo deterministicamente.
    mq = re.search(r"(?:quota|quote)\b[^\n]{0,45}?(\d+)\s*/\s*(\d+)", testo, re.IGNORECASE)
    if mq:
        num, den = int(mq.group(1)), int(mq.group(2))
        if 0 < num < den <= 12:   # frazione plausibile e < 1
            hints["quota_proprieta"] = f"{num}/{den} piena proprietà"

    return hints


# Watermark fisso che astalegale.net stampa su ogni pagina — va ignorato
# per capire se la pagina contiene testo reale o è una scansione.
_WATERMARK = "Astalegale.net"


class PDFAnalyzer:
    def __init__(self, api_key: str = GEMINI_API_KEY, model: str = GEMINI_MODEL):
        self.client = genai.Client(api_key=api_key) if api_key else None
        self.model = model
        self.text_provider = PDF_TEXT_PROVIDER if PDF_TEXT_PROVIDER in {"groq", "gemini"} else "groq"
        print(f"✅ PDFAnalyzer pronto — testo: {self.text_provider} | Gemini: {self.model}")

    def scarica_pdf(self, url: str, cookies: Optional[dict] = None) -> Optional[Path]:
        """
        Scarica PDF da URL.
        Prova prima con requests (veloce); se 401/403 usa sessione browser Playwright.
        cookies: dict opzionale con cookie della sessione (estratti da Playwright).
        """
        if not url or not url.strip():
            return None

        path = Path(f"/tmp/asta_pdf_{int(time.time())}.pdf")

        # Tentativo 1: requests con headers e cookies di sessione
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Referer": "https://www.astalegale.net/",
            "Accept": "application/pdf,application/octet-stream,*/*",
        }
        try:
            resp = requests.get(url, headers=headers, cookies=cookies or {}, timeout=45, stream=True)
            if resp.status_code in (200, 206):
                with open(path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                size_kb = path.stat().st_size / 1024
                print(f"    📥 PDF scaricato: {size_kb:.0f} KB")
                return path
            elif resp.status_code in (401, 403):
                print(f"    🔑 Auth richiesta ({resp.status_code}) — uso browser Playwright")
            else:
                print(f"    ⚠️ HTTP {resp.status_code} da requests")
        except Exception as e:
            print(f"    ⚠️ requests fallito: {e}")

        # Tentativo 2: download tramite browser Playwright (porta i cookie della sessione)
        return self._scarica_con_playwright(url, path)

    def _scarica_con_playwright(self, url: str, save_path: Path,
                                detail_page_url: Optional[str] = None) -> Optional[Path]:
        """
        Scarica PDF usando Playwright — visita prima la pagina dettaglio
        per ottenere i cookie di sessione necessari per documents.astalegale.net.
        """
        try:
            import asyncio
            from playwright.async_api import async_playwright

            # Ricava l'URL della pagina dettaglio dal link documento
            # Formato: documents.astalegale.net/file/... → cerca il codice asta nell'URL
            page_to_visit = detail_page_url or "https://www.astalegale.net"

            async def _download():
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    context = await browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                        accept_downloads=True,
                    )
                    page = await context.new_page()

                    # Visita prima la pagina dettaglio per ottenere cookies di sessione
                    await page.goto(page_to_visit, timeout=30000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(1500)

                    # Ora fai la richiesta del PDF con i cookie della sessione
                    resp = await page.request.get(url, headers={
                        "Referer": page_to_visit,
                        "Accept": "application/pdf,*/*",
                    })

                    if resp.ok:
                        content = await resp.body()
                        if len(content) < 100:
                            await browser.close()
                            return False
                        save_path.write_bytes(content)
                        await browser.close()
                        return True

                    print(f"    ❌ Playwright HTTP {resp.status}")
                    await browser.close()
                    return False

            ok = asyncio.run(_download())
            if ok and save_path.exists() and save_path.stat().st_size > 1000:
                size_kb = save_path.stat().st_size / 1024
                print(f"    📥 PDF scaricato via browser: {size_kb:.0f} KB")
                return save_path
        except Exception as e:
            print(f"    ❌ Download Playwright fallito: {e}")

        return None

    def estrai_testo(self, pdf_path: Path) -> Optional[str]:
        """
        Estrae testo dal PDF con PyMuPDF, ignorando il watermark astalegale.net.

        Ritorna None se il PDF è scansionato (solo immagini/watermark): in quel
        caso il chiamante deve usare l'analisi nativa Gemini Vision.
        """
        try:
            doc = fitz.open(str(pdf_path))
            pagine = min(len(doc), MAX_PDF_PAGES)
            parti = []
            char_reali = 0  # caratteri al netto del watermark

            for i in range(pagine):
                page = doc[i]
                testo = page.get_text("text").strip()
                if not testo:
                    continue
                # Rimuovi le righe di watermark per misurare il testo reale
                righe_reali = [r for r in testo.splitlines() if _WATERMARK not in r]
                testo_reale = "\n".join(righe_reali).strip()
                char_reali += len(testo_reale)
                if testo_reale:
                    parti.append(f"=== PAGINA {i+1} ===\n{testo_reale}")

            doc.close()

            # Una perizia text-based ha centinaia di char/pagina; una scansione
            # ne ha ~0 (solo watermark). Il discriminante è la media per pagina,
            # con un minimo assoluto per scartare documenti del tutto vuoti.
            media_per_pagina = char_reali / max(pagine, 1)
            if char_reali < MIN_TESTO_REALE and media_per_pagina < MIN_CHAR_PER_PAGINA:
                print(f"    ⚠️ PDF scansionato ({char_reali} char reali su {pagine} pag, "
                      f"{media_per_pagina:.0f}/pag) — uso analisi nativa Gemini Vision")
                return None

            testo_completo = "\n\n".join(parti)
            if len(testo_completo) > MAX_PDF_CHARS:
                testo_completo = testo_completo[:MAX_PDF_CHARS] + "\n[... testo troncato ...]"

            print(f"    📄 Testo estratto: {len(testo_completo):,} caratteri reali ({pagine} pagine)")
            return testo_completo

        except Exception as e:
            print(f"    ❌ Estrazione testo fallita: {e}")
            return None

    def analizza_pdf_nativo(self, pdf_path: Path) -> Optional[dict]:
        """
        Analizza il PDF inviandolo direttamente a Gemini Vision (lettura nativa).
        Usato per PDF scansionati dove l'estrazione testo fallisce.
        """
        if not self.client:
            print("    ⚠️ Gemini non configurato — salto Vision fallback")
            return None
        try:
            from google.genai import types
        except ImportError:
            print("    ❌ google.genai.types non disponibile per analisi nativa")
            return None

        try:
            pdf_bytes = pdf_path.read_bytes()
        except Exception as e:
            print(f"    ❌ Lettura PDF fallita: {e}")
            return None

        # Gemini ha un limite ~20MB per il PDF inline
        if len(pdf_bytes) > 20 * 1024 * 1024:
            print(f"    ⚠️ PDF troppo grande per analisi nativa ({len(pdf_bytes)//1024//1024} MB)")
            return None

        from google.genai import types

        modelli = [self.model]
        if GEMINI_MODEL_FALLBACK and GEMINI_MODEL_FALLBACK != self.model:
            modelli.append(GEMINI_MODEL_FALLBACK)

        for modello in modelli:
            for attempt in range(1, PDF_RETRY_ATTEMPTS + 1):
                try:
                    response = self.client.models.generate_content(
                        model=modello,
                        contents=[
                            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                            _ISTRUZIONI,
                        ],
                        config=self._gemini_json_config(),
                    )
                    dati = self._parse_json_risposta(response.text.strip(), f"{modello}/vision")
                    return dati
                except Exception as e:
                    err = str(e)
                    if ("429" in err or "RESOURCE_EXHAUSTED" in err) and attempt < PDF_RETRY_ATTEMPTS:
                        secs = self._parse_retry_delay(err) or 30
                        print(f"    ⏱️  Rate limit Vision {modello} — attendo {secs}s...")
                        time.sleep(secs + 2)
                    else:
                        print(f"    ⚠️ Vision {modello} fallito (tent. {attempt}): {err[:120]}")
                        if attempt < PDF_RETRY_ATTEMPTS:
                            time.sleep(3)
                        else:
                            break  # prova prossimo modello

        print("    ❌ Analisi nativa Gemini Vision fallita")
        return None

    def ocr_con_mistral(self, pdf_path: Path) -> Optional[str]:
        """
        Estrae markdown da un PDF scansionato con Mistral OCR.
        Usa l'endpoint REST ufficiale /v1/ocr con data URI base64.
        """
        if not USE_MISTRAL_OCR or not MISTRAL_API_KEY:
            return None

        try:
            pdf_bytes = pdf_path.read_bytes()
        except Exception as e:
            print(f"    ❌ Lettura PDF per Mistral OCR fallita: {e}")
            return None

        if len(pdf_bytes) > 50 * 1024 * 1024:
            print(f"    ⚠️ PDF troppo grande per Mistral OCR ({len(pdf_bytes)//1024//1024} MB)")
            return None

        data_uri = "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode("ascii")
        payload = {
            "model": MISTRAL_OCR_MODEL,
            "document": {"type": "document_url", "document_url": data_uri},
            "table_format": "markdown",
            "include_image_base64": False,
        }
        try:
            print(f"    🔎 Mistral OCR [{MISTRAL_OCR_MODEL}]...")
            resp = requests.post(
                "https://api.mistral.ai/v1/ocr",
                headers={
                    "Authorization": f"Bearer {MISTRAL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            pages = data.get("pages") or []
            parti = []
            for page in pages:
                markdown = (page.get("markdown") or "").strip()
                if markdown:
                    idx = page.get("index")
                    parti.append(f"=== PAGINA {idx} OCR ===\n{markdown}")
            testo = "\n\n".join(parti).strip()
            if not testo:
                print("    ⚠️ Mistral OCR non ha restituito testo utile")
                return None
            if len(testo) > MAX_PDF_CHARS:
                testo = testo[:MAX_PDF_CHARS] + "\n[... testo OCR troncato ...]"
            print(f"    ✅ Mistral OCR: {len(testo):,} caratteri da {len(pages)} pagine")
            return testo
        except Exception as e:
            print(f"    ⚠️ Mistral OCR fallito: {str(e)[:180]}")
            return None

    def analizza_pdf_scansionato(self, pdf_path: Path) -> Optional[dict]:
        """
        Analizza un PDF scansionato scegliendo il risultato migliore.

        In modalità "best" confronta:
          A) Mistral OCR -> router testo
          B) Gemini Vision diretto
        e usa Mistral solo se ha qualità strettamente superiore. A parità,
        resta Gemini Vision: la baseline attuale non viene peggiorata.
        """
        mode = PDF_SCAN_ANALYSIS_MODE if PDF_SCAN_ANALYSIS_MODE in {"best", "mistral", "gemini"} else "best"

        if mode == "gemini":
            # Gemini Vision primario; se non disponibile (quota esaurita) →
            # overflow gratuito su Mistral OCR + router testo.
            risultato = self.analizza_pdf_nativo(pdf_path)
            if risultato is not None:
                return risultato
            print("    🔄 Gemini Vision non disponibile → overflow Mistral OCR")
            testo_ocr = self.ocr_con_mistral(pdf_path)
            return self.analizza_testo(testo_ocr) if testo_ocr else None

        mistral_result = None
        testo_ocr = self.ocr_con_mistral(pdf_path)
        if testo_ocr:
            mistral_result = self.analizza_testo(testo_ocr)
            if mistral_result:
                print(f"    🧪 Qualità Mistral OCR+LLM: {self._score_qualita(mistral_result):.1f}")

        if mode == "mistral":
            return mistral_result or self.analizza_pdf_nativo(pdf_path)

        gemini_result = self.analizza_pdf_nativo(pdf_path)
        return self._scegli_migliore(mistral_result, gemini_result)

    def _scegli_migliore(self, mistral_result: Optional[dict],
                         gemini_result: Optional[dict]) -> Optional[dict]:
        """Sceglie il JSON più completo; Mistral deve battere Gemini per sostituirlo."""
        if not mistral_result:
            return gemini_result
        if not gemini_result:
            print("    ✅ Uso Mistral OCR+LLM: Gemini Vision non disponibile")
            return mistral_result

        s_mistral = self._score_qualita(mistral_result)
        s_gemini = self._score_qualita(gemini_result)
        print(f"    🧪 Confronto qualità — Mistral {s_mistral:.1f} vs Gemini Vision {s_gemini:.1f}")
        if s_mistral > s_gemini:
            print("    ✅ Uso Mistral OCR+LLM: qualità superiore alla baseline Gemini Vision")
            return mistral_result
        print("    ✅ Tengo Gemini Vision: Mistral non migliora la baseline")
        return gemini_result

    @staticmethod
    def _score_qualita(dati: Optional[dict]) -> float:
        """
        Euristica di completezza/qualità per confrontare due estrazioni dello stesso PDF.
        Non giudica il valore economico, ma premia campi critici presenti e plausibili.
        """
        if not dati:
            return 0.0

        score = 0.0
        pesi = {
            "stato_occupazione": 3.0,
            "superficie_mq": 2.5,
            "stato_manutentivo": 2.0,
            "valore_mercato": 3.0,
            "spese_condominiali_arretrate": 1.5,
            "costi_sanatoria": 1.5,
            "quota_proprieta": 1.2,
            "categoria_catastale": 1.0,
            "qualita_posizione": 1.0,
            "anno_costruzione": 0.7,
            "classe_energetica": 0.5,
            "tipologia_immobile": 0.7,
            "rendita_catastale": 0.7,
        }
        for campo, peso in pesi.items():
            val = dati.get(campo)
            if val not in (None, "", "N/D", 0):
                score += peso

        note = dati.get("note_critiche")
        if isinstance(note, str):
            score += 0.5  # anche stringa vuota valida: il modello ha rispettato il campo

        sup = PDFAnalyzer._to_float(dati.get("superficie_mq"))
        vm = PDFAnalyzer._to_float(dati.get("valore_mercato"))
        dist = PDFAnalyzer._to_float(dati.get("distanza_stazione_km"))
        anno = PDFAnalyzer._to_float(dati.get("anno_costruzione"))
        if sup is not None and not (10 <= sup <= 1000):
            score -= 2.0
        if vm is not None and vm != 0 and not (5_000 <= vm <= 10_000_000):
            score -= 2.0
        if dist is not None and not (0 <= dist <= 100):
            score -= 1.0
        if anno is not None and not (1800 <= anno <= 2100):
            score -= 1.0

        return max(0.0, score)

    def analizza_con_gemini(self, testo: str) -> Optional[dict]:
        """
        Invia il testo a un LLM e parsifica il JSON risultante.
        Compatibilità storica: ora usa il router configurato.
        """
        return self.analizza_testo(testo)

    def analizza_testo(self, testo: str) -> Optional[dict]:
        """Analizza testo già estratto: Groq/Gemini secondo router + fallback."""
        hints = _hints_deterministici(testo)
        prompt_groq = _build_prompt(_riduci_testo_per_groq(testo), hints)
        prompt_gemini = _build_prompt(testo, hints)

        if self.text_provider == "groq":
            risultato = self._prova_groq(prompt_groq)
            if risultato is not None:
                return self._applica_hints(risultato, hints)
            return self._applica_hints(self._prova_gemini_chain(prompt_gemini), hints)

        risultato = self._prova_gemini_chain(prompt_gemini)
        if risultato is not None:
            return self._applica_hints(risultato, hints)
        return self._applica_hints(self._prova_groq(prompt_groq), hints)

    def _applica_hints(self, dati: Optional[dict], hints: dict) -> Optional[dict]:
        """Applica solo hint deterministici ad alta precisione al JSON normalizzato."""
        if not dati:
            return dati
        for campo in ("valore_mercato", "costi_sanatoria",
                      "spese_condominiali_arretrate", "spese_condominiali_annue",
                      "quota_proprieta"):
            if campo in hints:
                dati[campo] = hints[campo]
        return dati

    def _prova_groq(self, prompt: str) -> Optional[dict]:
        """Tenta Groq come backend alternativo gratuito (OpenAI-compatible API)."""
        if not GROQ_API_KEY or not GROQ_API_KEY.strip():
            print("    ⚠️ Groq non configurato — salto")
            return None

        payload = {
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 1800,
            "response_format": {
                "type": "json_schema",
                "json_schema": _JSON_SCHEMA,
            },
        }
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }

        try:
            print(f"    🔄 Groq testo [{GROQ_MODEL}]...")
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )
            if resp.status_code == 400:
                # Alcuni modelli/ambienti Groq non supportano ancora json_schema:
                # manteniamo almeno il vincolo JSON object.
                payload["response_format"] = {"type": "json_object"}
                resp = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=60,
                )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            dati = json.loads(content)
            dati = self._normalizza(dati)
            print(f"    🤖 Groq OK [{GROQ_MODEL}] — occupazione: {dati.get('stato_occupazione')}")
            return dati
        except Exception as e:
            print(f"    ❌ Groq fallito: {e}")
            return None

    def _prova_gemini_chain(self, prompt: str) -> Optional[dict]:
        """Prova Gemini primario e fallback."""
        if not self.client:
            print("    ⚠️ Gemini non configurato — salto")
            return None

        modelli_gemini = [self.model]
        if GEMINI_MODEL_FALLBACK and GEMINI_MODEL_FALLBACK != self.model:
            modelli_gemini.append(GEMINI_MODEL_FALLBACK)

        for modello in modelli_gemini:
            risultato = self._prova_modello(prompt, modello)
            if risultato is not None:
                return risultato
            print(f"    ⚠️ Modello Gemini {modello} non disponibile, provo il prossimo...")
        return None

    def _gemini_json_config(self):
        """Config Gemini per JSON strutturato, compatibile con google-genai >= 1."""
        try:
            from google.genai import types
            return types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=_JSON_SCHEMA["schema"],
            )
        except Exception:
            return None

    def _prova_modello(self, prompt: str, modello: str) -> Optional[dict]:
        """Tenta di usare un modello specifico con retry su errori temporanei."""
        for attempt in range(1, PDF_RETRY_ATTEMPTS + 1):
            try:
                response = self.client.models.generate_content(
                    model=modello,
                    contents=prompt,
                    config=self._gemini_json_config(),
                )
                risposta = response.text.strip()
                return self._parse_json_risposta(risposta, modello)

            except json.JSONDecodeError as e:
                print(f"    ⚠️ JSON non valido da {modello} (tentativo {attempt}): {e}")
                if attempt < PDF_RETRY_ATTEMPTS:
                    time.sleep(3)

            except Exception as e:
                err_str = str(e)
                # Quota esaurita: leggi il retryDelay se disponibile
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    retry_secs = self._parse_retry_delay(err_str)
                    if retry_secs and attempt < PDF_RETRY_ATTEMPTS:
                        print(f"    ⏱️  Rate limit {modello} — attendo {retry_secs}s...")
                        time.sleep(retry_secs + 2)
                    elif attempt < PDF_RETRY_ATTEMPTS:
                        time.sleep(60)
                    else:
                        return None  # quota esaurita, prova il prossimo modello
                else:
                    print(f"    ❌ Gemini errore {modello} (tentativo {attempt}): {e}")
                    if attempt < PDF_RETRY_ATTEMPTS:
                        time.sleep(5)

        return None

    def _parse_retry_delay(self, error_str: str) -> Optional[int]:
        """Estrae il retryDelay dalla stringa di errore Gemini."""
        m = re.search(r"retryDelay['\"]:\s*['\"](\d+)s", error_str)
        if m:
            return int(m.group(1))
        m = re.search(r"retry in (\d+)", error_str, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return None

    def _parse_json_risposta(self, risposta: str, modello: str) -> Optional[dict]:
        """Estrae e valida il JSON dalla risposta del modello."""
        json_str = risposta
        if "```json" in risposta:
            start = risposta.index("```json") + 7
            end = risposta.index("```", start)
            json_str = risposta[start:end].strip()
        elif "{" in risposta:
            start = risposta.index("{")
            end = risposta.rindex("}") + 1
            json_str = risposta[start:end]

        dati = json.loads(json_str)
        dati = self._normalizza(dati)
        vm = dati.get("valore_mercato")
        vm_str = f"€{vm:,.0f}" if vm else "N/D"
        print(f"    🤖 Gemini OK [{modello}] — occ: {dati.get('stato_occupazione')} | "
              f"mq: {dati.get('superficie_mq')} | val.mercato: {vm_str}")
        return dati

    def _normalizza(self, dati: dict) -> dict:
        """Normalizza e valida i valori estratti da Gemini."""
        valid_occupazione = {"LIBERO", "OCCUPATO_DEBITORE", "OCCUPATO_CON_TITOLO", "OCCUPATO_SENZA_TITOLO"}
        valid_manutentivo = {"OTTIMO", "BUONO", "MEDIOCRE", "PESSIMO", "RUDERE"}

        occ = str(dati.get("stato_occupazione", "")).upper().strip()
        dati["stato_occupazione"] = occ if occ in valid_occupazione else None

        mnut = str(dati.get("stato_manutentivo", "")).upper().strip()
        dati["stato_manutentivo"] = mnut if mnut in valid_manutentivo else None

        valid_posizione = {"OTTIMA", "BUONA", "MEDIA", "SCARSA"}
        pos = str(dati.get("qualita_posizione", "")).upper().strip()
        dati["qualita_posizione"] = pos if pos in valid_posizione else None

        # Campi numerici (euro/mq/km) — convertili a float, None se vuoti/invalidi
        for campo in ("costi_sanatoria", "superficie_mq", "valore_mercato",
                      "spese_condominiali_arretrate", "spese_condominiali_annue",
                      "spese_straordinarie_deliberate", "rendita_catastale",
                      "canone_locazione_annuo", "distanza_stazione_km"):
            dati[campo] = self._to_float(dati.get(campo))

        # valore_mercato e superficie_mq a 0 = estrazione fallita, non è uno zero
        # economicamente sensato → normalizza a None (lo scorer userà il fallback).
        for campo in ("valore_mercato", "superficie_mq"):
            if dati.get(campo) == 0:
                dati[campo] = None

        # Anno di costruzione: intero plausibile (1800-anno corrente+1)
        anno = self._to_float(dati.get("anno_costruzione"))
        dati["anno_costruzione"] = int(anno) if anno and 1800 <= anno <= 2100 else None

        # opponibilità: bool o None
        opp = dati.get("occupazione_opponibile")
        if isinstance(opp, str):
            opp = {"true": True, "false": False, "si": True, "no": False}.get(opp.lower().strip())
        dati["occupazione_opponibile"] = opp if isinstance(opp, bool) else None

        # Stringhe libere — strip, None se vuote
        for campo in ("piano_ascensore", "quota_proprieta", "categoria_catastale",
                      "classe_energetica", "tipologia_immobile", "pertinenze"):
            dati[campo] = str(dati.get(campo) or "").strip() or None

        dati["note_critiche"] = str(dati.get("note_critiche") or "").strip()

        return dati

    @staticmethod
    def _to_float(val) -> Optional[float]:
        """Converte un valore a float gestendo formati €/migliaia. None se non valido."""
        if val in (None, "", "null", "ND", "N/D"):
            return None
        if isinstance(val, (int, float)):
            return float(val)
        # stringa: rimuovi simboli, gestisci separatori italiani (1.234,56)
        s = re.sub(r"[^\d,.\-]", "", str(val))
        if not s:
            return None
        if "," in s and "." in s:        # 1.234,56 → 1234.56
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:                   # 1234,56 → 1234.56
            s = s.replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    def analizza_pdf_da_url(self, url: str, detail_page_url: Optional[str] = None) -> Optional[dict]:
        """
        Pipeline completo: download → estrai testo → analisi Gemini.

        detail_page_url: URL della pagina dettaglio asta (usato per ottenere
                         i cookie di sessione necessari a scaricare il PDF).
        """
        if not url or not url.strip():
            return None

        pdf_path = self.scarica_pdf(url)
        if not pdf_path:
            # Fallback con sessione browser
            pdf_path = self._scarica_con_playwright(url, Path(f"/tmp/asta_pdf_{int(time.time())}.pdf"),
                                                    detail_page_url)
        if not pdf_path:
            return None

        try:
            testo = self.estrai_testo(pdf_path)
            if testo:
                return self.analizza_testo(testo)

            # estrai_testo ha rilevato una scansione: scegli il metodo migliore
            # rispetto alla baseline Gemini Vision, senza degradare la qualità.
            return self.analizza_pdf_scansionato(pdf_path)
        finally:
            pdf_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────
# Test standalone
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python pdf_analyzer.py <url_pdf>")
        sys.exit(1)

    analyzer = PDFAnalyzer()
    risultato = analyzer.analizza_pdf_da_url(sys.argv[1])
    if risultato:
        print("\n✅ Risultato:")
        print(json.dumps(risultato, indent=2, ensure_ascii=False))
    else:
        print("❌ Analisi fallita")
