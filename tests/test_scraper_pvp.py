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
        ("venegono-superiore", "venegono"),   # non "superiore": è un qualificatore
    ])
    def test_token(self, slug, atteso):
        assert s.token_distintivo(slug) == atteso

    def test_mai_un_connettore(self):
        for slug in ["la-thuile", "san-martino-di-castrozza", "corvara-in-badia"]:
            assert s.token_distintivo(slug) not in s._CONNETTORI

    @pytest.mark.parametrize("slug,atteso", [
        # i qualificatori geografici sono condivisi da molti comuni: usarli come
        # termine satura la pagina di risultati con omonimi e perde i lotti veri
        ("venegono-inferiore", "venegono"),      # NON "inferiore"
        ("santa-margherita-ligure", "margherita"),  # NON "ligure"
        ("monterosso-al-mare", "monterosso"),
        ("diano-marina", "diano"),
        ("pietra-ligure", "pietra"),
    ])
    def test_evita_qualificatori_geografici(self, slug, atteso):
        assert s.token_distintivo(slug) == atteso

    def test_mai_un_qualificatore(self):
        for slug in ["venegono-inferiore", "venegono-superiore", "diano-marina",
                     "santa-margherita-ligure", "monterosso-al-mare"]:
            assert s.token_distintivo(slug) not in s._QUALIFICATORI


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


# ─────────────────────────────────────────────────────────────
# _norm_data_asta — formato DD/MM/YYYY per _data_asta_passata
# ─────────────────────────────────────────────────────────────

class TestNormDataAsta:
    @pytest.mark.parametrize("val,atteso", [
        ("2026-10-07", "07/10/2026"),           # ISO
        ("2026-10-07T12:00", "07/10/2026"),     # ISO con ora
        ("07/10/2026", "07/10/2026"),           # già giusto
        (None, None),
        ("", None),
    ])
    def test_norm(self, val, atteso):
        assert s._norm_data_asta(val) == atteso


# ─────────────────────────────────────────────────────────────
# run_scraper — contratto drop-in per main.py
# ─────────────────────────────────────────────────────────────

class TestRunScraperContratto:
    def _setup(self, monkeypatch):
        lotti = {
            "tradate": [
                {"id": 100, "dataVendita": "2026-12-01", "categoriaLotto": "IMMOBILE_RESIDENZIALE",
                 "indirizzo": {"citta": "Tradate"}, "prezzoBaseAsta": 80000, "offertaMinima": 60000},
                {"id": 101, "dataVendita": "2026-12-01", "categoriaLotto": "IMMOBILE_RESIDENZIALE",
                 "indirizzo": {"citta": "Tradate"}, "prezzoBaseAsta": 50000, "offertaMinima": 37500},
            ],
        }
        monkeypatch.setattr(s, "ricerca_comune", lambda slug, oggi=None: lotti.get(slug, []))
        monkeypatch.setattr(s, "dettaglio", lambda idv: None)  # niente rete

    def test_split_nuovi_esistenti(self, monkeypatch):
        self._setup(monkeypatch)
        out = s.run_scraper(["tradate"], codici_esistenti={"PVP-100"},
                            categoria_localita={"tradate": "citta"}, verbose=False)
        assert [a["codice"] for a in out["nuovi"]] == ["PVP-101"]      # 100 già nel DB
        assert [e["codice"] for e in out["esistenti"]] == ["PVP-100"]
        assert out["esistenti"][0]["prezzo_base"] == 80000

    def test_codici_per_comune_keyed_by_citta(self, monkeypatch):
        self._setup(monkeypatch)
        out = s.run_scraper(["tradate"], codici_esistenti=set(),
                            categoria_localita={"tradate": "citta"}, verbose=False)
        # chiave = la citta salvata in `comune`, per far combaciare la rilevazione spariti
        assert set(out["codici_per_comune"]) == {"Tradate"}
        assert set(out["codici_per_comune"]["Tradate"]) == {"PVP-100", "PVP-101"}

    def test_categoria_localita_propagata(self, monkeypatch):
        self._setup(monkeypatch)
        out = s.run_scraper(["tradate"], codici_esistenti=set(),
                            categoria_localita={"tradate": "montagna"}, verbose=False)
        assert all(a["categoria_localita"] == "montagna" for a in out["nuovi"])


# ─────────────────────────────────────────────────────────────
# merge_deterministici — preserva i valori PVP ufficiali dall'LLM
# ─────────────────────────────────────────────────────────────

class TestMergeDeterministici:
    def test_preserva_superficie_e_occupazione(self):
        llm = {"superficie_mq": 99, "stato_occupazione": "OCCUPATO_DEBITORE", "note_critiche": "x"}
        riga = {"superficie_mq": 104, "stato_occupazione": "LIBERO",
                "valore_mercato": None, "prezzo_base": 80000}
        out = s.merge_deterministici(llm, riga)
        assert out["superficie_mq"] == 104            # PVP vince
        assert out["stato_occupazione"] == "LIBERO"   # PVP vince
        assert out["note_critiche"] == "x"            # campo LLM intatto

    def test_valore_preservato_se_diverso_da_base(self):
        llm = {"valore_mercato": 120000}
        riga = {"valore_mercato": 118500, "prezzo_base": 93100,
                "superficie_mq": None, "stato_occupazione": None}
        out = s.merge_deterministici(llm, riga)
        assert out["valore_mercato"] == 118500        # stima PVP vera → vince

    def test_valore_llm_vince_se_stima_uguale_a_base(self):
        """impoStima==base è un placeholder: lì la perizia (LLM) è più affidabile."""
        llm = {"valore_mercato": 79500}
        riga = {"valore_mercato": 80000, "prezzo_base": 80000,
                "link_perizia": "http://x/perizia.pdf",   # il valore LLM viene dalla perizia
                "superficie_mq": None, "stato_occupazione": None}
        out = s.merge_deterministici(llm, riga)
        assert out["valore_mercato"] == 79500         # LLM vince

    def test_noop_se_db_vuoto(self):
        """Fonte senza deterministici (tutti None): l'LLM resta intatto."""
        llm = {"superficie_mq": 70, "stato_occupazione": "LIBERO", "valore_mercato": 100000}
        riga = {"superficie_mq": None, "stato_occupazione": None,
                "valore_mercato": None, "prezzo_base": 50000}
        out = s.merge_deterministici(dict(llm), riga)
        assert out == llm

    def test_scarta_stima_implausibile(self):
        """Stima 15k su un immobile a 335k: errore di estrazione → meglio None."""
        llm = {"valore_mercato": 15198}
        riga = {"prezzo_base": 335000, "valore_mercato": None,
                "superficie_mq": None, "stato_occupazione": None}
        out = s.merge_deterministici(llm, riga)
        assert out["valore_mercato"] is None


class TestDescrizioneDichiaraIntero:
    @pytest.mark.parametrize("desc", [
        "piena proprietà per 1/1 di appartamento ad uso abitazione",
        "per la piena ed intera proprietà: appartamento (monolocale)",
        "piena ed intera proprietà di appartamento a Vernazza",
        "per l'intero abitazione residenziale su due piani",
        "piena proprietà per la quota di 1000/1000 delle seguenti unità",
    ])
    def test_riconosce_intero(self, desc):
        assert s.descrizione_dichiara_intero(desc) is True

    @pytest.mark.parametrize("desc", [
        "piena proprietà per la quota di 1/2 di appartamento",   # frazione vera
        "quota di 4/27 di villetta unifamiliare",
        "appartamento al piano primo, composto da tre locali",   # non dichiara nulla
        "",
        None,
    ])
    def test_non_dichiara_intero(self, desc):
        assert s.descrizione_dichiara_intero(desc) is False


class TestQuotaMergeArbitro:
    def test_descrizione_intera_corregge_falso_positivo(self):
        """Regex dice 1/2 ma la descrizione ufficiale dichiara 1/1: vince la descrizione."""
        llm = {"quota_proprieta": "1/2 piena proprietà"}
        riga = {"descrizione": "piena proprietà per 1/1 di appartamento ad uso abitazione",
                "prezzo_base": 100000, "valore_mercato": None,
                "superficie_mq": None, "stato_occupazione": None}
        out = s.merge_deterministici(llm, riga)
        assert out["quota_proprieta"] == "1/1 piena proprietà"

    def test_quota_frazionata_reale_resta(self):
        llm = {"quota_proprieta": "1/2 piena proprietà"}
        riga = {"descrizione": "piena proprietà per la quota di 1/2 di appartamento",
                "prezzo_base": 100000, "valore_mercato": None,
                "superficie_mq": None, "stato_occupazione": None}
        out = s.merge_deterministici(llm, riga)
        assert out["quota_proprieta"] == "1/2 piena proprietà"


class TestValoreMercatoPlausibile:
    @pytest.mark.parametrize("vm,pb,atteso", [
        (130000, 100000, True),    # stima sopra il prezzo: caso normale
        (100000, 120000, True),    # prezzo poco sopra la stima: plausibile
        (73968, 131990, True),     # 1.8x: borderline ma realistico
        (15198, 335000, False),    # 22x: errore di estrazione
        (15198, 102000, False),    # 6.7x: errore di estrazione
        (None, 100000, False),     # nessuna stima
        (0, 100000, False),        # zero non è una stima
        (50000, None, True),       # senza prezzo base non possiamo giudicare
    ])
    def test_plausibilita(self, vm, pb, atteso):
        assert s.valore_mercato_plausibile(vm, pb) is atteso

    def test_da_avviso_stima_uguale_al_base_e_inaffidabile(self):
        """L'avviso non riporta la stima: vm==base è il prezzo base riletto."""
        assert s.valore_mercato_plausibile(22500, 22500, da_avviso=True) is False
        # con la perizia invece può essere un primo incanto legittimo
        assert s.valore_mercato_plausibile(22500, 22500, da_avviso=False) is True

    def test_merge_scarta_stima_da_avviso(self):
        llm = {"valore_mercato": 22500}
        riga = {"prezzo_base": 22500, "valore_mercato": None, "link_perizia": None,
                "superficie_mq": None, "stato_occupazione": None, "descrizione": ""}
        assert s.merge_deterministici(llm, riga)["valore_mercato"] is None
