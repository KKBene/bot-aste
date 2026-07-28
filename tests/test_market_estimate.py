"""
Test per la stima di mercato da annunci comparabili.

La rete non viene toccata: gli annunci si passano già pronti a
prezzo_mq_zona/stima_da_comparabili.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import market_estimate as me


def annuncio(prezzo, superficie, tipologia="Trilocale", citta="Gallarate",
             condizione="Buono / Abitabile", lat=None, lng=None):
    return {"prezzo": prezzo, "superficie": superficie, "tipologia": tipologia,
            "citta": citta, "condizione": condizione, "lat": lat, "lng": lng}


class TestComuneToSlug:
    @pytest.mark.parametrize("nome,atteso", [
        ("Gallarate", "gallarate"),
        ("Venegono Superiore", "venegono-superiore"),
        ("Cortina d'Ampezzo", "cortina-d-ampezzo"),
        ("La Spezia", "la-spezia"),
        (None, ""),
    ])
    def test_slug(self, nome, atteso):
        assert me.comune_to_slug(nome) == atteso


class TestSuperficie:
    @pytest.mark.parametrize("raw,atteso", [
        ("85 m²", 85.0), ("1.200 m²", 1200.0), ("316 m²", 316.0),
        (None, None), ("", None),
    ])
    def test_parse(self, raw, atteso):
        assert me._superficie(raw) == atteso


class TestPrezzoMqZona:
    def test_mediana_non_media(self):
        """Un fuoriscala non deve spostare il riferimento: si usa la mediana."""
        annunci = [annuncio(100_000, 100) for _ in range(5)]      # 1000 €/mq
        annunci.append(annuncio(1_400_000, 100))                   # 14000 €/mq, lusso
        zona = me.prezzo_mq_zona("Gallarate", None, annunci)
        assert zona["prezzo_mq_mediano"] == 1000
        assert zona["campione"] == 6

    def test_none_se_campione_troppo_piccolo(self):
        assert me.prezzo_mq_zona("Gallarate", None, [annuncio(100_000, 100)]) is None

    def test_esclude_tipologie_non_residenziali(self):
        annunci = [annuncio(100_000, 100) for _ in range(5)]
        annunci += [annuncio(5_500, 6, tipologia="Progetto"),
                    annuncio(20_000, 15, tipologia="Garage/Posto auto"),
                    annuncio(30_000, 500, tipologia="Terreno")]
        zona = me.prezzo_mq_zona("Gallarate", None, annunci)
        assert zona["campione"] == 5          # solo i residenziali

    def test_esclude_prezzo_mq_fuori_scala(self):
        annunci = [annuncio(100_000, 100) for _ in range(5)]
        annunci += [annuncio(1_000, 100),        # 10 €/mq: annuncio malformato
                    annuncio(10_000_000, 100)]   # 100k €/mq
        assert me.prezzo_mq_zona("Gallarate", None, annunci)["campione"] == 5

    def test_esclude_altri_comuni(self):
        annunci = [annuncio(100_000, 100) for _ in range(5)]
        annunci.append(annuncio(900_000, 100, citta="Milano"))
        assert me.prezzo_mq_zona("Gallarate", None, annunci)["campione"] == 5

    def test_filtra_per_fascia_superficie(self):
        annunci = [annuncio(100_000, 100) for _ in range(5)]   # in fascia per target 100
        annunci += [annuncio(900_000, 300) for _ in range(5)]  # fuori fascia (+200%)
        zona = me.prezzo_mq_zona("Gallarate", 100, annunci)
        assert zona["campione"] == 5
        assert zona["prezzo_mq_mediano"] == 1000

    def test_allarga_la_fascia_se_i_comparabili_non_bastano(self):
        """Meglio una stima di zona che nessuna stima."""
        annunci = [annuncio(100_000, 300) for _ in range(6)]   # tutti fuori fascia per 50mq
        zona = me.prezzo_mq_zona("Gallarate", 50, annunci)
        assert zona is not None and zona["campione"] == 6


class TestFiltroCondizione:
    """Un immobile da ristrutturare non va confrontato con dei ristrutturati."""

    def _mercato(self):
        # ristrutturati cari + da ristrutturare economici, stessa superficie
        return ([annuncio(200_000, 100, condizione="Ottimo / Ristrutturato") for _ in range(6)] +
                [annuncio(80_000, 100, condizione="Da ristrutturare") for _ in range(6)])

    def test_rudere_confrontato_coi_da_ristrutturare(self):
        zona = me.prezzo_mq_zona("Gallarate", 100, self._mercato(), stato="RUDERE")
        assert zona["prezzo_mq_mediano"] == 800          # non 2000 dei ristrutturati
        assert zona["base_confronto"] == "stato e superficie simili"

    def test_ottimo_confrontato_coi_ristrutturati(self):
        zona = me.prezzo_mq_zona("Gallarate", 100, self._mercato(), stato="OTTIMO")
        assert zona["prezzo_mq_mediano"] == 2000

    def test_senza_stato_usa_tutto_il_mercato(self):
        zona = me.prezzo_mq_zona("Gallarate", 100, self._mercato(), stato=None)
        assert zona["campione"] == 12

    def test_fallback_progressivo_riporta_la_base(self):
        """Se lo stato non trova abbastanza comparabili si allarga, ma lo si dichiara."""
        annunci = [annuncio(100_000, 100, condizione="Ottimo / Ristrutturato") for _ in range(6)]
        zona = me.prezzo_mq_zona("Gallarate", 100, annunci, stato="RUDERE")
        assert zona is not None
        assert zona["base_confronto"] == "superficie simile"   # stato mollato

    def test_annuncio_senza_condizione_escluso_se_filtriamo_per_stato(self):
        annunci = ([annuncio(80_000, 100, condizione="Da ristrutturare") for _ in range(5)] +
                   [annuncio(500_000, 100, condizione="") for _ in range(5)])
        zona = me.prezzo_mq_zona("Gallarate", 100, annunci, stato="PESSIMO")
        assert zona["campione"] == 5
        assert zona["prezzo_mq_mediano"] == 800


class TestFiltroVicinato:
    """Su una città grande la mediana comunale mescola quartieri incomparabili."""

    # Genova: Albaro (caro) vs Certosa (economico), ~6 km di distanza
    ALBARO = (44.3900, 8.9700)
    CERTOSA = (44.4300, 8.8900)

    def _mercato(self):
        return ([annuncio(400_000, 100, citta="Genova", lat=44.3905, lng=8.9705) for _ in range(6)] +
                [annuncio(120_000, 100, citta="Genova", lat=44.4305, lng=8.8905) for _ in range(6)])

    def test_usa_solo_il_vicinato_del_lotto(self):
        zona = me.prezzo_mq_zona("Genova", None, self._mercato(), coord=self.ALBARO)
        assert zona["prezzo_mq_mediano"] == 4000        # non la mediana mista
        assert zona["campione"] == 6
        assert zona["base_confronto"].startswith("in zona")

    def test_quartiere_economico(self):
        zona = me.prezzo_mq_zona("Genova", None, self._mercato(), coord=self.CERTOSA)
        assert zona["prezzo_mq_mediano"] == 1200

    def test_senza_coordinate_usa_tutto_il_comune(self):
        zona = me.prezzo_mq_zona("Genova", None, self._mercato(), coord=None)
        assert zona["campione"] == 12
        assert not zona["base_confronto"].startswith("in zona")

    def test_allarga_al_comune_se_il_vicinato_e_vuoto(self):
        """Nessun annuncio vicino: meglio la media comunale che nessuna stima."""
        lontani = [annuncio(200_000, 100, citta="Genova", lat=44.50, lng=9.20) for _ in range(6)]
        zona = me.prezzo_mq_zona("Genova", None, lontani, coord=self.ALBARO)
        assert zona is not None
        assert not zona["base_confronto"].startswith("in zona")


class TestPaginazione:
    """Senza più pagine, in città grandi il vicinato non trova mai comparabili."""

    def _finta_pagina(self, ids):
        return [{"realEstate": {"id": i, "price": {"value": 100_000},
                                "properties": [{"surface": "80 m²"}]}} for i in ids]

    def test_unisce_le_pagine(self, monkeypatch):
        pagine = {1: self._finta_pagina([1, 2]), 2: self._finta_pagina([3, 4])}
        monkeypatch.setattr(me, "_scarica_pagina", lambda s, p, t: pagine.get(p, []))
        monkeypatch.setattr(me.time, "sleep", lambda *_: None)
        assert len(me.scarica_annunci("genova", pagine=3)) == 4

    def test_si_ferma_su_pagina_vuota(self, monkeypatch):
        chiamate = []

        def finta(slug, pagina, timeout):
            chiamate.append(pagina)
            return self._finta_pagina([pagina]) if pagina == 1 else []

        monkeypatch.setattr(me, "_scarica_pagina", finta)
        monkeypatch.setattr(me.time, "sleep", lambda *_: None)
        me.scarica_annunci("comunepiccolo", pagine=4)
        assert chiamate == [1, 2]          # non insiste dopo il vuoto

    def test_scarta_i_duplicati_e_si_ferma(self, monkeypatch):
        """Se il sito ripete la stessa pagina non va contata due volte."""
        monkeypatch.setattr(me, "_scarica_pagina",
                            lambda s, p, t: self._finta_pagina([1, 2]))
        monkeypatch.setattr(me.time, "sleep", lambda *_: None)
        assert len(me.scarica_annunci("genova", pagine=4)) == 2


class TestStimaLottiLazy:
    """Le pagine extra si scaricano solo dove il vicinato resta scoperto."""

    def _annunci(self, n, lat=45.0, lng=9.0):
        return [annuncio(100_000, 100, citta="X", lat=lat, lng=lng) for _ in range(n)]

    def _spia(self, monkeypatch, per_chiamata):
        chiamate = []

        def finto(comune, timeout=25, pagine=me.PAGINE_DA_SCARICARE):
            chiamate.append(pagine)
            return per_chiamata(pagine)

        monkeypatch.setattr(me, "scarica_annunci", finto)
        monkeypatch.setattr(me.time, "sleep", lambda *_: None)
        return chiamate

    def test_paese_una_sola_passata(self, monkeypatch):
        """Poche inserzioni: la prima passata basta, niente pagine extra."""
        chiamate = self._spia(monkeypatch, lambda pagine: self._annunci(8))
        lotti = [{"codice": "A", "comune": "X", "superficie_mq": 100,
                  "posizione_lat": 45.0, "posizione_lng": 9.0}]
        me.stima_lotti(lotti, verbose=False)
        assert chiamate == [me.PAGINE_INIZIALI]

    def test_citta_scarica_altre_pagine(self, monkeypatch):
        """Prima passata piena e lotto lontano da tutto: si insiste."""
        def per_chiamata(pagine):
            # annunci tutti lontani dal lotto -> nessun confronto "in zona"
            return self._annunci(pagine * me.ANNUNCI_PER_PAGINA, lat=46.5, lng=11.0)

        chiamate = self._spia(monkeypatch, per_chiamata)
        lotti = [{"codice": "A", "comune": "X", "superficie_mq": 100,
                  "posizione_lat": 45.0, "posizione_lng": 9.0}]
        me.stima_lotti(lotti, verbose=False)
        assert chiamate == [me.PAGINE_INIZIALI, me.PAGINE_DA_SCARICARE]

    def test_niente_pagine_extra_se_il_vicinato_e_gia_coperto(self, monkeypatch):
        chiamate = self._spia(monkeypatch,
                              lambda pagine: self._annunci(pagine * me.ANNUNCI_PER_PAGINA))
        lotti = [{"codice": "A", "comune": "X", "superficie_mq": 100,
                  "posizione_lat": 45.0, "posizione_lng": 9.0}]
        stime = me.stima_lotti(lotti, verbose=False)
        assert chiamate == [me.PAGINE_INIZIALI]
        assert stime["A"]["base_confronto"].startswith("in zona")

    def test_lotto_senza_coordinate_non_scatena_pagine_extra(self, monkeypatch):
        """Senza coordinate il vicinato non è applicabile: inutile insistere."""
        chiamate = self._spia(monkeypatch,
                              lambda pagine: self._annunci(pagine * me.ANNUNCI_PER_PAGINA))
        lotti = [{"codice": "A", "comune": "X", "superficie_mq": 100}]
        me.stima_lotti(lotti, verbose=False)
        assert chiamate == [me.PAGINE_INIZIALI]


class TestDistanzaKm:
    def test_distanza_nota(self):
        # Albaro -> Certosa, circa 6-7 km
        d = me.distanza_km(44.3900, 8.9700, 44.4300, 8.8900)
        assert 5.0 < d < 9.0

    def test_stesso_punto(self):
        assert me.distanza_km(44.39, 8.97, 44.39, 8.97) == pytest.approx(0, abs=0.01)

    def test_none_se_mancano_coordinate(self):
        assert me.distanza_km(44.39, 8.97, None, 8.97) is None


class TestStimaDaComparabili:
    def test_valore_stimato(self):
        annunci = [annuncio(100_000, 100) for _ in range(6)]    # 1000 €/mq
        stima = me.stima_da_comparabili("Gallarate", 80, annunci)
        assert stima["valore_stimato"] == 80_000
        assert stima["prezzo_mq_mediano"] == 1000

    def test_none_senza_superficie(self):
        annunci = [annuncio(100_000, 100) for _ in range(6)]
        assert me.stima_da_comparabili("Gallarate", None, annunci) is None
        assert me.stima_da_comparabili("Gallarate", 0, annunci) is None

    def test_none_se_zona_non_valutabile(self):
        assert me.stima_da_comparabili("Gallarate", 80, [annuncio(100_000, 100)]) is None

    def test_sconto_prudenziale_se_stato_non_filtrabile(self):
        """Nessun 'da ristrutturare' in zona: il valore va corretto al ribasso."""
        annunci = [annuncio(100_000, 100, condizione="Ottimo / Ristrutturato") for _ in range(6)]
        stima = me.stima_da_comparabili("Gallarate", 100, annunci, stato="RUDERE")
        assert stima["sconto_stato"] == 0.55
        assert stima["valore_stimato"] == 55_000        # 100.000 × 0.55

    def test_nessuno_sconto_se_lo_stato_e_stato_filtrato(self):
        annunci = [annuncio(80_000, 100, condizione="Da ristrutturare") for _ in range(6)]
        stima = me.stima_da_comparabili("Gallarate", 100, annunci, stato="RUDERE")
        assert "sconto_stato" not in stima
        assert stima["valore_stimato"] == 80_000

    def test_nessuno_sconto_per_immobili_in_buono_stato(self):
        annunci = [annuncio(100_000, 100, condizione="Nuovo / In costruzione") for _ in range(6)]
        stima = me.stima_da_comparabili("Gallarate", 100, annunci, stato="BUONO")
        assert "sconto_stato" not in stima
