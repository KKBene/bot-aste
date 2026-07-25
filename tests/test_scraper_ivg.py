"""
Test dello scraper IVG (piattaforma astagiudiziaria.com).

La rete non viene toccata: le chiamate a Typesense e alla pagina di dettaglio
sono sostituite da stub, così si verificano mapping, filtro residenziale e
dedup verso le altre fonti.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import scraper_ivg as ivg


DOC = {
    "id": 1356166,
    "city": "BUSTO ARSIZIO",
    "address": "Via Palestro 18, ",
    "price": 245760,
    "minimumOffer": 184320,
    "descrizione": "Appartamento su due piani",
    "title": "APPARTAMENTO",
    "category": "IMMOBILE RESIDENZIALE",
    "subcategory": ["APPARTAMENTO"],
    "sellStartDate": "27/10/2026 15:30",
    "sellType": "SENZA INCANTO",
    "tribunal": "Tribunale di BUSTO ARSIZIO",
    "numero_procedura": "ESECUZIONE IMMOBILIARE Nr. 50/2025",
    "lotto_code": 1,
    "permalink": "inserzioni/appartamento-1356166",
    "gallery": ["https://imgr.astagiudiziaria.com/512/foto.jpg"],
}


class TestSlugToCitta:
    @pytest.mark.parametrize("slug,atteso", [
        ("busto-arsizio", "BUSTO ARSIZIO"),
        ("tradate", "TRADATE"),
        ("venegono-superiore", "VENEGONO SUPERIORE"),
    ])
    def test_conversione(self, slug, atteso):
        assert ivg.slug_to_citta(slug) == atteso


class TestToAsta:
    def test_codice_prefisso_ivg(self):
        assert ivg.to_asta(DOC, {}, "citta")["codice"] == "IVG-1356166"

    def test_campi_principali(self):
        a = ivg.to_asta(DOC, {}, "citta")
        assert a["comune"] == "Busto Arsizio"          # non tutto maiuscolo
        assert a["prezzo_base"] == 245760
        assert a["offerta_minima"] == 184320
        assert a["indirizzo_immobile"] == "Via Palestro 18"   # virgola finale tolta
        assert a["data_asta"] == "27/10/2026 15:30"
        assert a["tipologia_immobile"] == "APPARTAMENTO"
        assert a["categoria_localita"] == "citta"

    def test_documenti_e_foto(self):
        a = ivg.to_asta(DOC, {"perizia": "http://x/p.pdf", "avviso": "http://x/a.pdf"}, "mare")
        assert a["link_perizia"] == "http://x/p.pdf"
        assert a["link_avviso_vendita"] == "http://x/a.pdf"
        assert a["immagine_url"].endswith("foto.jpg")
        assert a["sheet_type"] == "mare"

    def test_link_dettaglio_su_dominio_universale(self):
        a = ivg.to_asta(DOC, {}, "citta")
        assert a["link_dettaglio"] == f"{ivg.HOST_DETTAGLIO}/inserzioni/appartamento-1356166"

    def test_data_non_valida_diventa_none(self):
        a = ivg.to_asta(dict(DOC, sellStartDate="da destinarsi"), {}, "citta")
        assert a["data_asta"] is None


class TestPossibileDuplicato:
    """Una parte dei lotti IVG sta anche su PVP: non devono entrare due volte."""

    def test_stesso_comune_e_prezzo_base(self):
        esistenti = [{"comune": "Busto Arsizio", "prezzo_base": 245760, "offerta_minima": None}]
        assert ivg.possibile_duplicato(DOC, esistenti) is True

    def test_match_anche_su_offerta_minima(self):
        esistenti = [{"comune": "Busto Arsizio", "prezzo_base": 184320, "offerta_minima": None}]
        assert ivg.possibile_duplicato(DOC, esistenti) is True

    def test_stesso_prezzo_ma_altro_comune(self):
        esistenti = [{"comune": "Genova", "prezzo_base": 245760, "offerta_minima": None}]
        assert ivg.possibile_duplicato(DOC, esistenti) is False

    def test_stesso_comune_prezzo_diverso(self):
        esistenti = [{"comune": "Busto Arsizio", "prezzo_base": 99000, "offerta_minima": 74250}]
        assert ivg.possibile_duplicato(DOC, esistenti) is False

    def test_nessun_esistente(self):
        assert ivg.possibile_duplicato(DOC, []) is False


class TestQualitaMetadati:
    """La classificazione IVG è meno affidabile di PVP: serve un controllo sul testo."""

    def test_titolo_smentisce_la_categoria_residenziale(self):
        doc = dict(DOC, title="Deposito Fabbricato costruito per esigenze commerciali")
        assert ivg.titolo_non_residenziale(doc) is True

    def test_titolo_residenziale_passa(self):
        assert ivg.titolo_non_residenziale(DOC) is False

    @pytest.mark.parametrize("titolo", [
        "Terreno agricolo con annesso", "Box auto al piano interrato",
        "Capannone industriale", "Negozio con vetrina",
    ])
    def test_altre_tipologie_escluse(self, titolo):
        assert ivg.titolo_non_residenziale(dict(DOC, title=titolo, descrizione="")) is True

    def test_comune_incoerente_col_titolo(self):
        """city dice Caronno Pertusella ma il testo colloca l'immobile in Calabria."""
        doc = dict(DOC, city="Caronno Pertusella",
                   title="LOTTO 2: Appartamento sito a Cassano Jonio, in Piazzetta Laura Serra")
        assert ivg.comune_incoerente(doc) is True

    def test_comune_coerente(self):
        doc = dict(DOC, city="BUSTO ARSIZIO",
                   title="Appartamento sito a Busto Arsizio, via Palestro")
        assert ivg.comune_incoerente(doc) is False

    def test_nessuna_indicazione_nel_testo_non_e_incoerenza(self):
        assert ivg.comune_incoerente(dict(DOC, title="Appartamento su due livelli")) is False

    def test_city_vuota_e_incoerente(self):
        assert ivg.comune_incoerente(dict(DOC, city="")) is True


class TestCercaComune:
    def _stub(self, monkeypatch, documenti):
        class R:
            status_code = 200
            @staticmethod
            def json():
                return {"hits": [{"document": d} for d in documenti]}
        monkeypatch.setattr(ivg.requests, "get", lambda *a, **k: R())

    def test_tiene_solo_i_residenziali(self, monkeypatch):
        self._stub(monkeypatch, [
            DOC,
            dict(DOC, id=2, category="IMMOBILE COMMERCIALE"),
            dict(DOC, id=3, category="TERRENO"),
        ])
        assert [d["id"] for d in ivg.cerca_comune("busto-arsizio")] == [1356166]

    def test_scarta_senza_data_di_vendita(self, monkeypatch):
        self._stub(monkeypatch, [DOC, dict(DOC, id=2, sellStartDate=None)])
        assert [d["id"] for d in ivg.cerca_comune("busto-arsizio")] == [1356166]

    def test_scarta_titolo_non_residenziale(self, monkeypatch):
        self._stub(monkeypatch, [DOC, dict(DOC, id=2, title="Deposito commerciale")])
        assert [d["id"] for d in ivg.cerca_comune("busto-arsizio")] == [1356166]

    def test_scarta_comune_incoerente(self, monkeypatch):
        self._stub(monkeypatch, [DOC, dict(DOC, id=2, city="Caronno Pertusella",
                                           title="Appartamento sito a Cassano Jonio")])
        assert [d["id"] for d in ivg.cerca_comune("busto-arsizio")] == [1356166]

    def test_lista_vuota_su_errore(self, monkeypatch):
        def esplode(*a, **k):
            raise RuntimeError("rete giù")
        monkeypatch.setattr(ivg.requests, "get", esplode)
        assert ivg.cerca_comune("busto-arsizio") == []


class TestRunScraper:
    def _stub(self, monkeypatch, lotti):
        monkeypatch.setattr(ivg, "cerca_comune", lambda slug, categorie=None: lotti)
        monkeypatch.setattr(ivg, "documenti_lotto", lambda p: {})
        monkeypatch.setattr(ivg.time, "sleep", lambda *_: None)

    def test_contratto_nuovi_esistenti(self, monkeypatch):
        self._stub(monkeypatch, [DOC, dict(DOC, id=999, price=50000, minimumOffer=37500)])
        out = ivg.run_scraper(["busto-arsizio"], codici_esistenti={"IVG-1356166"},
                              categoria_localita={"busto-arsizio": "citta"}, verbose=False)
        assert [a["codice"] for a in out["nuovi"]] == ["IVG-999"]
        assert [e["codice"] for e in out["esistenti"]] == ["IVG-1356166"]

    def test_scarta_i_duplicati_di_altre_fonti(self, monkeypatch):
        self._stub(monkeypatch, [DOC])
        out = ivg.run_scraper(["busto-arsizio"], codici_esistenti=set(),
                              aste_esistenti=[{"comune": "Busto Arsizio",
                                               "prezzo_base": 245760, "offerta_minima": None}],
                              verbose=False)
        assert out["nuovi"] == []

    def test_codici_per_comune_keyed_by_citta(self, monkeypatch):
        self._stub(monkeypatch, [DOC])
        out = ivg.run_scraper(["busto-arsizio"], codici_esistenti=set(), verbose=False)
        assert set(out["codici_per_comune"]) == {"Busto Arsizio"}


class TestDocumentiLotto:
    def test_estrae_perizia_e_avviso(self, monkeypatch):
        html = ('<a alt="Avviso di vendita" href="https://library.astagiudiziaria.com/pdf/aaa111.pdf">'
                '<a alt="Perizia" href="https://library.astagiudiziaria.com/pdf/bbb222.pdf">')
        class R:
            status_code = 200
            text = html
        monkeypatch.setattr(ivg.requests, "get", lambda *a, **k: R())
        d = ivg.documenti_lotto("inserzioni/x-1")
        assert d["perizia"].endswith("bbb222.pdf")
        assert d["avviso"].endswith("aaa111.pdf")

    def test_dizionario_vuoto_senza_permalink(self):
        assert ivg.documenti_lotto(None) == {}
