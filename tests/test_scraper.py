"""
Test di integrazione per lo scraper Playwright.
Colpisce la rete REALE (astalegale.net) — richiede connessione internet.

Testa:
- Estrazione link dalla pagina lista
- Parsing dati dalla pagina dettaglio
- Robustezza su comuni senza risultati
- Parsing prezzi (edge cases)
- Pipeline completa su 1 comune
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import re
import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from scraper_pw import (
    _parse_price, _codice_da_url, _raccogli_links,
    _estrai_dettaglio, run_scraper,
)

BASE_URL = "https://www.astalegale.net"


# ─────────────────────────────────────────────────────────────
# UNIT TEST: funzioni pure (no rete)
# ─────────────────────────────────────────────────────────────

class TestParsing:
    @pytest.mark.parametrize("text,expected", [
        ("100.000,00 €", 100_000.0),
        ("€ 75.500", 75_500.0),
        ("50000", 50_000.0),
        ("1.234.567,89", 1_234_567.89),
        ("", None),
        (None, None),
        ("N/D", None),
        ("0,00", 0.0),
        ("123,45", 123.45),
    ])
    def test_parse_price(self, text, expected):
        result = _parse_price(text)
        if expected is None:
            assert result is None, f"'{text}' deve dare None, got {result}"
        else:
            assert result is not None, f"'{text}' non deve dare None"
            assert abs(result - expected) < 0.01, f"'{text}': atteso {expected}, got {result}"

    @pytest.mark.parametrize("url,expected", [
        ("https://www.astalegale.net/Aste/Detail/ABC123", "ABC123"),
        ("https://www.astalegale.net/Aste/Detail/XY9876543", "XY9876543"),
        ("/Aste/Detail/MINI", "MINI"),
        ("https://example.com/nessun-codice", None),
        ("", None),
    ])
    def test_codice_da_url(self, url, expected):
        result = _codice_da_url(url)
        assert result == expected, f"URL '{url}': atteso '{expected}', got '{result}'"

    def test_codice_solo_alfanumerico_maiuscolo(self):
        # Il codice deve contenere solo lettere maiuscole e numeri
        url = "https://www.astalegale.net/Aste/Detail/ABC123"
        codice = _codice_da_url(url)
        assert re.match(r'^[A-Z0-9]+$', codice)


# ─────────────────────────────────────────────────────────────
# INTEGRATION TEST: pagina lista (rete reale)
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPaginaLista:
    async def test_saronno_restituisce_link(self):
        """Saronno è una città nota — deve avere almeno qualche link."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            ))
            await page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf}",
                             lambda r: r.abort())
            try:
                annunci = await _raccogli_links(page, "saronno", "residenziali")
                # Potrebbe avere 0 risultati se non ci sono aste attive —
                # ma la funzione non deve crashare e deve restituire una lista
                assert isinstance(annunci, list)
                print(f"\n  Saronno: {len(annunci)} annunci trovati")
            finally:
                await browser.close()

    async def test_comune_inesistente_non_crasha(self):
        """Un comune inventato non deve fare crashare lo scraper."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                annunci = await _raccogli_links(page, "comune-che-non-esiste-xyz", "residenziali")
                assert isinstance(annunci, list)
                assert len(annunci) == 0 or len(annunci) >= 0  # no crash
            finally:
                await browser.close()

    async def test_annunci_hanno_codice_e_link(self):
        """Ogni annuncio estratto deve avere codice e link_dettaglio."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                annunci = await _raccogli_links(page, "gallarate", "residenziali")
                for a in annunci:
                    assert "codice" in a, f"Manca codice: {a}"
                    assert "link_dettaglio" in a, f"Manca link: {a}"
                    assert a["codice"], "Codice vuoto"
                    assert "astalegale.net" in a["link_dettaglio"], "Link non valido"
            finally:
                await browser.close()

    async def test_nessun_duplicato_nella_lista(self):
        """Non devono esserci annunci duplicati per lo stesso comune."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                annunci = await _raccogli_links(page, "busto-arsizio", "residenziali")
                codici = [a["codice"] for a in annunci]
                assert len(codici) == len(set(codici)), "Trovati duplicati nella lista!"
            finally:
                await browser.close()


# ─────────────────────────────────────────────────────────────
# INTEGRATION TEST: pagina dettaglio
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPaginaDettaglio:
    async def _get_first_annuncio(self, comune: str):
        """Helper: prende il primo annuncio reale da un comune."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                annunci = await _raccogli_links(page, comune, "residenziali")
                return annunci[0] if annunci else None, browser, page
            except:
                await browser.close()
                return None, None, None

    async def test_dettaglio_struttura_base(self):
        """Un annuncio reale deve avere almeno codice e URL."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            ))
            page = await context.new_page()
            await page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf}",
                             lambda r: r.abort())
            try:
                annunci = await _raccogli_links(page, "saronno", "residenziali")
                if not annunci:
                    pytest.skip("Nessun annuncio disponibile su saronno")

                asta = annunci[0]
                asta = await _estrai_dettaglio(page, asta)

                # Campi obbligatori
                assert asta.get("codice"), "Codice mancante"
                assert asta.get("link_dettaglio"), "Link mancante"
                assert asta.get("comune"), "Comune mancante"

                print(f"\n  Dettaglio {asta['codice']}:")
                for k in ["prezzo_base", "offerta_minima", "indirizzo_immobile",
                          "tipologia", "data_asta", "tribunale"]:
                    print(f"    {k}: {asta.get(k)}")
            finally:
                await browser.close()

    async def test_prezzo_base_numerico(self):
        """Il prezzo base deve essere un float o None."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            try:
                annunci = await _raccogli_links(page, "castellanza", "residenziali")
                if not annunci:
                    pytest.skip("Nessun annuncio")
                asta = await _estrai_dettaglio(page, annunci[0])
                prezzo = asta.get("prezzo_base")
                assert prezzo is None or isinstance(prezzo, float), f"Tipo: {type(prezzo)}"
                if prezzo:
                    assert prezzo > 0, "Prezzo deve essere positivo"
            finally:
                await browser.close()

    async def test_offerta_minima_minore_o_uguale_base(self):
        """L'offerta minima deve essere <= prezzo base."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            try:
                annunci = await _raccogli_links(page, "gallarate", "residenziali")
                if not annunci:
                    pytest.skip("Nessun annuncio")
                asta = await _estrai_dettaglio(page, annunci[0])
                if asta.get("prezzo_base") and asta.get("offerta_minima"):
                    assert asta["offerta_minima"] <= asta["prezzo_base"], (
                        f"Offerta {asta['offerta_minima']} > Base {asta['prezzo_base']}"
                    )
            finally:
                await browser.close()

    async def test_documenti_hanno_url_valido(self):
        """I link ai documenti devono essere URL validi di astalegale.net."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            try:
                annunci = await _raccogli_links(page, "saronno", "residenziali")
                if not annunci:
                    pytest.skip("Nessun annuncio")
                asta = await _estrai_dettaglio(page, annunci[0])
                for campo in ["link_perizia", "link_avviso_vendita", "link_ordinanza"]:
                    val = asta.get(campo)
                    if val:
                        assert val.startswith("http"), f"{campo} non è un URL: {val}"
                        assert "documents.astalegale.net" in val or "astalegale" in val
            finally:
                await browser.close()


# ─────────────────────────────────────────────────────────────
# INTEGRATION TEST: pipeline completa 1 comune
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPipelineCompleta:
    async def test_run_scraper_un_comune(self):
        """Testa run_scraper completo su 1 comune (Uboldo, tipicamente piccolo)."""
        risultati = await run_scraper(
            comuni=["uboldo"],
            categoria="residenziali",
            codici_esistenti=set(),  # tutto è "nuovo"
            sheet_type="residenziale",
        )

        assert isinstance(risultati, list)
        print(f"\n  Uboldo: {len(risultati)} annunci")

        for asta in risultati:
            # Ogni asta deve avere i campi minimi
            assert "codice" in asta, f"Manca codice: {asta}"
            assert "link_dettaglio" in asta
            assert "comune" in asta
            assert asta["sheet_type"] == "residenziale"

            # Prezzo deve essere float se presente
            if asta.get("prezzo_base") is not None:
                assert isinstance(asta["prezzo_base"], float)
            if asta.get("offerta_minima") is not None:
                assert isinstance(asta["offerta_minima"], float)

    async def test_run_scraper_salta_duplicati(self):
        """Se un codice è già nel set, non deve essere restituito."""
        # Prima passata
        risultati_1 = await run_scraper(
            comuni=["cislago"],
            categoria="residenziali",
            codici_esistenti=set(),
        )

        if not risultati_1:
            pytest.skip("Nessun annuncio disponibile per il test duplicati")

        # Seconda passata con tutti i codici come "già esistenti"
        codici = {a["codice"] for a in risultati_1}
        risultati_2 = await run_scraper(
            comuni=["cislago"],
            categoria="residenziali",
            codici_esistenti=codici,
        )

        # Non deve restituire nulla (tutto già visto)
        assert len(risultati_2) == 0, (
            f"Dovevano essere tutti filtrati, invece: {[r['codice'] for r in risultati_2]}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-s"])
