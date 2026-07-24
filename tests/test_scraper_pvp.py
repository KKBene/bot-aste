"""
Test per lo scraper PVP (pvp.giustizia.it).

Copre gli helper puri (conversione nome comune, match tollerante, mapping ->
schema aste). Le chiamate di rete non sono testate qui: sono coperte dalla
validazione dal vivo durante lo sviluppo.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import scraper_pvp as s


# ─────────────────────────────────────────────────────────────
# slug_to_nome
# ─────────────────────────────────────────────────────────────

class TestSlugToNome:
    @pytest.mark.parametrize("slug,atteso", [
        ("tradate", "Tradate"),
        ("busto-arsizio", "Busto Arsizio"),
        ("venegono-superiore", "Venegono Superiore"),
        ("cortina-d-ampezzo", "Cortina d'Ampezzo"),
        ("san-martino-di-castrozza", "San Martino di Castrozza"),
        ("sestri-levante", "Sestri Levante"),
        ("la-thuile", "La Thuile"),
        ("corvara-in-badia", "Corvara in Badia"),
    ])
    def test_conversione(self, slug, atteso):
        assert s.slug_to_nome(slug) == atteso


# ─────────────────────────────────────────────────────────────
# token_distintivo — evita che PVP faccia esplodere il match fuzzy
# ─────────────────────────────────────────────────────────────

class TestTokenDistintivo:
    @pytest.mark.parametrize("slug,atteso", [
        ("la-thuile", "thuile"),                 # NON 'la' (matcherebbe tutto)
        ("san-martino-di-castrozza", "castrozza"),
        ("busto-arsizio", "arsizio"),
        ("tradate", "tradate"),
        ("venegono-superiore", "superiore"),
    ])
    def test_token(self, slug, atteso):
        assert s.token_distintivo(slug) == atteso

    def test_mai_un_connettore(self):
        for slug in ["la-thuile", "san-martino-di-castrozza", "corvara-in-badia"]:
            assert s.token_distintivo(slug) not in s._CONNETTORI


# ─────────────────────────────────────────────────────────────
# _comune_combacia — tollerante ai comuni fusi, senza falsi positivi
# ─────────────────────────────────────────────────────────────

class TestComuneCombacia:
    def test_esatto(self):
        assert s._comune_combacia("Tradate", "Tradate")

    def test_case_e_accenti(self):
        assert s._comune_combacia("Cortina d'Ampezzo", "CORTINA D'AMPEZZO")

    def test_comune_fuso_sottostringa(self):
        # San Martino di Castrozza è confluito in Primiero San Martino di Castrozza
        assert s._comune_combacia("San Martino di Castrozza",
                                  "Primiero San Martino di Castrozza")

    def test_no_falso_positivo_superiore_inferiore(self):
        assert not s._comune_combacia("Venegono Superiore", "Venegono Inferiore")

    def test_no_match_su_none(self):
        assert not s._comune_combacia("Tradate", None)
        assert not s._comune_combacia("", "Tradate")


# ─────────────────────────────────────────────────────────────
# to_asta — mapping verso lo schema `aste`
# ─────────────────────────────────────────────────────────────

class TestToAsta:
    SOMMARIO = {
        "id": 4582509,
        "categoriaLotto": "IMMOBILE_RESIDENZIALE",
        "prezzoBaseAsta": 93100.0,
        "offertaMinima": 69825.0,
        "dataVendita": "2026-07-21",
        "indirizzo": {"citta": "Cislago", "via": "Via Piave 225",
                      "coordinate": {"latitudine": 45.65, "longitudine": 8.97}},
        "numeroLotto": "LOTTO UNICO",
    }
    DETTAGLIO = {
        "impoBaseAsta": 93100, "impoOffertaMinima": 69825, "impoStima": 118500,
        "dataVendita": "21/07/2026", "dataTermPresOff": "20/07/2026", "oraTermPresOff": "13:00",
        "descModVendita": "Sincrona Mista", "descTipoVendita": "Senza Incanto",
        "procedura": {"numeRg": "1477", "numeAnnoRg": 2022, "descUfficio": "Corte d'Appello - Milano"},
        "lotto": {
            "codLotto": "LOTTO UNICO", "descLotto": "Trilocale al secondo piano.",
            "descTipoCategLotto": "Immobile Residenziale",
            "indirizzo": {"via": "Via Piave, 225", "descComune": "Cislago",
                          "coordinate": {"latitudine": 45.6554, "longitudine": 8.9741}},
        },
        "beni": [{
            "descrizione": "Trilocale al secondo piano con cantina e autorimessa.",
            "descTipologiaBene": "Abitazione Di Tipo Economico",
            "superficie": "104", "numeroVani": 4.5, "piano": "2",
            "disponibilita": "LIBER", "disponibilitaDesc": "Libero",
            "datiCatastali": [{"textFoglio": "9", "textParticella": "123", "textSubalterno": "7"}],
            "allegati": [
                {"descrizione": "IMMAGINE BENE", "linkAllegato": "/immagini-beni/4582509/1/foto.jpg?versionId=z"},
            ],
        }],
        "allegati": [
            {"codiceTipoAllegato": "AVEND", "linkAllegato": "/allegati/4582509/avviso.pdf?versionId=a"},
            {"codiceTipoAllegato": "PERIZ", "linkAllegato": "/allegati/4582509/perizia.pdf?versionId=b"},
        ],
    }

    def test_codice_prefisso_pvp(self):
        a = s.to_asta(self.SOMMARIO, self.DETTAGLIO, "citta")
        assert a["codice"] == "PVP-4582509"

    def test_campi_base_dal_dettaglio(self):
        a = s.to_asta(self.SOMMARIO, self.DETTAGLIO, "citta")
        assert a["prezzo_base"] == 93100
        assert a["offerta_minima"] == 69825
        assert a["comune"] == "Cislago"
        assert a["termine_offerte"] == "20/07/2026 13:00"
        assert a["categoria_localita"] == "citta"

    def test_campi_deterministici_da_pvp(self):
        """valore_mercato/superficie/occupazione presi diretti da PVP, senza LLM."""
        a = s.to_asta(self.SOMMARIO, self.DETTAGLIO, "citta")
        assert a["valore_mercato"] == 118500          # impoStima
        assert a["superficie_mq"] == 104.0            # beni.superficie
        assert a["stato_occupazione"] == "LIBERO"     # disponibilita LIBER
        assert a["tipologia_immobile"] == "Abitazione Di Tipo Economico"

    def test_numero_procedura_completo(self):
        a = s.to_asta(self.SOMMARIO, self.DETTAGLIO, "citta")
        assert a["numero_procedura"] == "1477/2022"   # numeRg + numeAnnoRg

    def test_modalita_gara_da_descModVendita(self):
        a = s.to_asta(self.SOMMARIO, self.DETTAGLIO, "citta")
        assert a["modalita_gara"] == "Sincrona Mista"  # NON "Senza Incanto"

    def test_immagine_foto_reale(self):
        a = s.to_asta(self.SOMMARIO, self.DETTAGLIO, "citta")
        assert a["immagine_url"] == (
            "https://resource-pvp.giustizia.it/immagini-beni/4582509/1/foto.jpg?versionId=z")

    def test_occupazione_occst_senza_titolo(self):
        det = dict(self.DETTAGLIO)
        det["beni"] = [dict(self.DETTAGLIO["beni"][0], disponibilita="OCCST")]
        a = s.to_asta(self.SOMMARIO, det, "citta")
        assert a["stato_occupazione"] == "OCCUPATO_SENZA_TITOLO"

    def test_occupazione_occup_generico_resta_none(self):
        """'Occupato' generico è ambiguo (debitore vs con-titolo): None, lo decide l'LLM."""
        det = dict(self.DETTAGLIO)
        det["beni"] = [dict(self.DETTAGLIO["beni"][0], disponibilita="OCCUP")]
        a = s.to_asta(self.SOMMARIO, det, "citta")
        assert a["stato_occupazione"] is None

    def test_link_perizia_assoluto(self):
        a = s.to_asta(self.SOMMARIO, self.DETTAGLIO, "citta")
        assert a["link_perizia"] == (
            "https://resource-pvp.giustizia.it/allegati/4582509/perizia.pdf?versionId=b")
        assert a["link_avviso_vendita"].endswith("avviso.pdf?versionId=a")

    def test_coordinate_dal_dettaglio(self):
        a = s.to_asta(self.SOMMARIO, self.DETTAGLIO, "citta")
        assert a["posizione_lat"] == 45.6554

    def test_coordinate_fallback_sul_sommario(self):
        det = dict(self.DETTAGLIO)
        det["lotto"] = dict(det["lotto"])
        det["lotto"]["indirizzo"] = dict(det["lotto"]["indirizzo"], coordinate={})
        a = s.to_asta(self.SOMMARIO, det, "citta")
        assert a["posizione_lat"] == 45.65  # preso dal sommario

    def test_senza_dettaglio_usa_sommario(self):
        a = s.to_asta(self.SOMMARIO, None, "montagna")
        assert a["codice"] == "PVP-4582509"
        assert a["prezzo_base"] == 93100.0
        assert a["link_perizia"] is None
        assert a["categoria_localita"] == "montagna"

    def test_sheet_type_per_categoria(self):
        assert s.to_asta(self.SOMMARIO, None, "citta")["sheet_type"] == "residenziale"
        assert s.to_asta(self.SOMMARIO, None, "mare")["sheet_type"] == "mare"


# ─────────────────────────────────────────────────────────────
# ricerca_comune — filtro residenziale + solo attivi
# ─────────────────────────────────────────────────────────────

class TestRicercaFiltro:
    def _fake_page(self, lotti):
        return {"content": lotti, "last": True}

    def test_filtra_non_residenziale_e_comune(self, monkeypatch):
        lotti = [
            {"id": 1, "dataVendita": "2026-12-01", "categoriaLotto": "IMMOBILE_RESIDENZIALE",
             "indirizzo": {"citta": "Tradate"}},
            {"id": 2, "dataVendita": "2026-12-01", "categoriaLotto": "IMMOBILE_COMMERCIALE",
             "indirizzo": {"citta": "Tradate"}},          # scartato: non residenziale
            {"id": 3, "dataVendita": "2026-12-01", "categoriaLotto": "IMMOBILE_RESIDENZIALE",
             "indirizzo": {"citta": "Venegono"}},          # scartato: comune diverso
        ]
        monkeypatch.setattr(s, "_post_ricerca", lambda t, p: self._fake_page(lotti))
        out = s.ricerca_comune("tradate", oggi="2026-07-25")
        assert [c["id"] for c in out] == [1]

    def test_scarta_scadute(self, monkeypatch):
        lotti = [
            {"id": 1, "dataVendita": "2026-12-01", "categoriaLotto": "IMMOBILE_RESIDENZIALE",
             "indirizzo": {"citta": "Tradate"}},
            {"id": 2, "dataVendita": "2026-06-01", "categoriaLotto": "IMMOBILE_RESIDENZIALE",
             "indirizzo": {"citta": "Tradate"}},           # scaduta
        ]
        monkeypatch.setattr(s, "_post_ricerca", lambda t, p: self._fake_page(lotti))
        out = s.ricerca_comune("tradate", oggi="2026-07-25")
        assert [c["id"] for c in out] == [1]
