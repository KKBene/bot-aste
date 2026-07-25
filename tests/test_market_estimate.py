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
             condizione="Buono / Abitabile"):
    return {"prezzo": prezzo, "superficie": superficie, "tipologia": tipologia,
            "citta": citta, "condizione": condizione}


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
