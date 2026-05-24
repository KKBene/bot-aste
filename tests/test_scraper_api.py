"""
Test per lo scraper basato su API (scraper_api.py).

- Parsing/normalizzazione: unit test puri (no rete).
- Integrazione: 1 test reale su un comune (deselezionabile con -k "not reale").
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from scraper_api import (
    _parse_price, _norm_data, _normalizza_item, _md_values, _link_documenti,
    cerca_comune,
)


# ─────────────────────────────────────────────────────────────
# UNIT: parsing prezzi e date
# ─────────────────────────────────────────────────────────────

class TestParsing:
    @pytest.mark.parametrize("val,exp", [
        ("€ 33.000,00", 33000.0),
        ("75.793,10", 75793.10),
        (44000, 44000.0),
        (325120.0, 325120.0),
        ("56.844,83", 56844.83),
        ("", None), (None, None), ("N/D", None),
    ])
    def test_parse_price(self, val, exp):
        r = _parse_price(val)
        if exp is None:
            assert r is None
        else:
            assert abs(r - exp) < 0.01

    @pytest.mark.parametrize("val,exp", [
        ("28/07/2026 - 11:00", "28/07/2026 11:00"),
        ("10/07/2026 13:00", "10/07/2026 13:00"),
        ("22/06/2026 -  15:00", "22/06/2026 15:00"),
        (None, None),
    ])
    def test_norm_data(self, val, exp):
        assert _norm_data(val) == exp


# ─────────────────────────────────────────────────────────────
# UNIT: normalizzazione item API
# ─────────────────────────────────────────────────────────────

class TestNormalizzaItem:
    @pytest.fixture
    def item(self):
        return {
            "id": "B2402840",
            "friendlyId": "B2402840-Via-tiro-a-segno-Gallarate",
            "titolo": "Via tiro a segno 8f",
            "comune": "Gallarate",
            "tipologia": "Abitazione di tipo civile",
            "descrizione": "Appartamento al secondo piano",
            "dataAsta": "22/06/2026 - 15:00",
            "tribunale": "Busto Arsizio",
            "proceduraNumeroAnno": "207/2025",
            "codiceLotto": "Unico",
            "prezzoNum": 75793.1,
            "offertaMinima": "€ 56.844,83",
            "urlImmaginePrincipale": "https://documents.astalegale.net/asta/0/B2402840",
        }

    def test_codice_mappato(self, item):
        a = _normalizza_item(item, "gallarate")
        assert a["codice"] == "B2402840"

    def test_link_dettaglio_costruito(self, item):
        a = _normalizza_item(item, "gallarate")
        assert a["link_dettaglio"].endswith("B2402840-Via-tiro-a-segno-Gallarate")
        assert "/Aste/Detail/" in a["link_dettaglio"]

    def test_prezzo_numerico(self, item):
        a = _normalizza_item(item, "gallarate")
        assert a["prezzo_base"] == 75793.1
        assert abs(a["offerta_minima"] - 56844.83) < 0.01

    def test_data_normalizzata(self, item):
        a = _normalizza_item(item, "gallarate")
        assert a["data_asta"] == "22/06/2026 15:00"

    def test_comune_passato(self, item):
        a = _normalizza_item(item, "gallarate")
        assert a["comune"] == "gallarate"  # usa il parametro, non item['comune']

    def test_campi_completi(self, item):
        a = _normalizza_item(item, "gallarate")
        for campo in ["codice", "comune", "link_dettaglio", "indirizzo_immobile",
                      "tipologia", "data_asta", "tribunale", "numero_procedura",
                      "prezzo_base", "offerta_minima"]:
            assert campo in a

    def test_prezzo_fallback_su_stringa(self):
        item = {"id": "X", "prezzo": "€ 100.000,00", "offertaMinima": "€ 75.000,00"}
        a = _normalizza_item(item, "x")
        assert a["prezzo_base"] == 100000.0


# ─────────────────────────────────────────────────────────────
# UNIT: parsing HTML SSR (md-value + documenti)
# ─────────────────────────────────────────────────────────────

class TestParsingHTML:
    HTML = '''
    <span md-value="Prezzo base">€ 75.793,10</span>
    <span md-value="offerta minima">€ 56.844,83</span>
    <span md-value="termine presentazione offerte">19/06/2026 13:00</span>
    <span md-value="modalità gara">Sincrona mista</span>
    <span md-value="indirizzo lotto">Via tiro a segno 8f - Gallarate</span>
    <span class="fw-semibold"><i class="fa-regular fa-file-lines pe-2"></i>Perizia (&lt; 1 Mb)</span>
      <div class="float-end"><a href="https://documents.astalegale.net/file/0/00576d21aa8b3014b370db07eb333bb5/2343869-D?cd=true">dl</a>
      <a href="https://documents.astalegale.net/file/0/00576d21aa8b3014b370db07eb333bb5/2343869-D">view</a></div>
    <span class="fw-semibold"><i class="fa-regular fa-file-lines pe-2"></i>Avviso di Vendita (&lt; 1 Mb)</span>
      <div class="float-end"><a href="https://documents.astalegale.net/file/0/bc9a56ee036e7869df43ee07a98eb549/2343870-D?cd=true">view</a></div>
    '''

    def test_md_values_estrae_chiavi(self):
        mv = _md_values(self.HTML)
        assert mv["termine presentazione offerte"] == "19/06/2026 13:00"
        assert mv["modalità gara"] == "Sincrona mista"
        assert mv["prezzo base"] == "€ 75.793,10"

    def test_link_perizia_estratto(self):
        docs = _link_documenti(self.HTML)
        assert docs["link_perizia"] == (
            "https://documents.astalegale.net/file/0/00576d21aa8b3014b370db07eb333bb5/2343869-D"
        )

    def test_link_perizia_senza_cd_true(self):
        docs = _link_documenti(self.HTML)
        assert "?cd=true" not in docs["link_perizia"]

    def test_link_avviso_estratto(self):
        docs = _link_documenti(self.HTML)
        assert docs["link_avviso_vendita"].endswith("2343870-D")

    def test_md_values_vuoto_su_html_vuoto(self):
        assert _md_values("<html></html>") == {}

    def test_perizia_nel_testo_non_confusa_con_documento(self):
        """La parola 'perizia' nel testo descrittivo non deve dare un link falso."""
        html = (
            '<p>Come da perizia, l\'immobile si trova in un terratetto. '
            'La perizia attesta la conformità.</p>'
            '<a href="https://documents.astalegale.net/file/0/abc123/9999999-D">non-doc</a>'
        )
        docs = _link_documenti(html)
        assert docs.get("link_perizia") is None


# ─────────────────────────────────────────────────────────────
# INTEGRAZIONE REALE (deselezionabile)
# ─────────────────────────────────────────────────────────────

class TestIntegrazioneReale:
    def test_cerca_comune_reale(self):
        """Su un comune noto deve restituire lotti con prezzi numerici."""
        items = cerca_comune("gallarate", "residenziali")
        assert isinstance(items, list)
        for it in items:
            assert it["codice"]
            assert "astalegale.net/Aste/Detail/" in it["link_dettaglio"]
            if it["prezzo_base"] is not None:
                assert isinstance(it["prezzo_base"], float)
                assert it["prezzo_base"] > 0
        print(f"\n  gallarate: {len(items)} lotti via API")

    def test_comune_inesistente_lista_vuota(self):
        items = cerca_comune("comune-che-non-esiste-xyz123", "residenziali")
        assert items == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-s"])
