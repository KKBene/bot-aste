"""
Test per lo scoring v3 — modello economico ROI risk-adjusted + posizione.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from scorer import calcola_score, stima_costi, score_label, score_emoji


# ─────────────────────────────────────────────────────────────
# FIXTURE
# ─────────────────────────────────────────────────────────────

def affare_ottimo():
    """Margine ampio, libero, ben servito, conforme."""
    return {
        "prezzo_base": 130_000, "offerta_minima": 75_000, "valore_mercato": 130_000,
        "stato_occupazione": "LIBERO", "stato_manutentivo": "BUONO",
        "costi_sanatoria": 0, "spese_condominiali_arretrate": 0,
        "quota_proprieta": "1/1 piena proprietà", "superficie_mq": 90,
        "qualita_posizione": "OTTIMA", "distanza_stazione_km": 0.4,
        "note_critiche": "", "analisi_pdf": True,
    }

def trappola():
    """Sconto apparente ma margine negativo dopo i costi."""
    return {
        "prezzo_base": 47_000, "offerta_minima": 24_000, "valore_mercato": 47_000,
        "stato_occupazione": "OCCUPATO_DEBITORE", "stato_manutentivo": "MEDIOCRE",
        "costi_sanatoria": 5_000, "spese_condominiali_arretrate": 0,
        "quota_proprieta": "1/1 piena proprietà", "superficie_mq": 59,
        "qualita_posizione": "MEDIA", "distanza_stazione_km": 2.0,
        "note_critiche": "", "analisi_pdf": True,
    }


# ─────────────────────────────────────────────────────────────
# STIMA COSTI
# ─────────────────────────────────────────────────────────────

class TestStimaCosti:
    def test_costo_totale_somma_componenti(self):
        c = stima_costi(affare_ottimo())
        somma = (c["prezzo_acquisto"] + c["sanatoria"] + c["debiti_condominiali"]
                 + c["ristrutturazione"] + c["imposte"] + c["oneri_accessori"]
                 + c["liberazione"])
        assert abs(somma - c["costo_totale"]) < 1

    def test_ristrutturazione_per_stato(self):
        base = {"offerta_minima": 100_000, "superficie_mq": 100}
        c_ottimo = stima_costi({**base, "stato_manutentivo": "OTTIMO"})
        c_rudere = stima_costi({**base, "stato_manutentivo": "RUDERE"})
        assert c_ottimo["ristrutturazione"] == 0
        assert c_rudere["ristrutturazione"] > c_ottimo["ristrutturazione"]

    def test_imposte_proporzionali_al_prezzo(self):
        c = stima_costi({"offerta_minima": 100_000, "superficie_mq": 50})
        assert c["imposte"] == pytest.approx(9_000, abs=1)   # 9%

    def test_liberazione_libero_zero(self):
        c = stima_costi({"offerta_minima": 50_000, "stato_occupazione": "LIBERO"})
        assert c["liberazione"] == 0

    def test_liberazione_occupato_costa(self):
        c = stima_costi({"offerta_minima": 50_000, "stato_occupazione": "OCCUPATO_SENZA_TITOLO"})
        assert c["liberazione"] > 0

    def test_liberazione_non_opponibile_minore_di_opponibile(self):
        base = {"offerta_minima": 50_000, "stato_occupazione": "OCCUPATO_CON_TITOLO"}
        c_opp = stima_costi({**base, "occupazione_opponibile": True})
        c_no = stima_costi({**base, "occupazione_opponibile": False})
        assert c_no["liberazione"] < c_opp["liberazione"]

    def test_debiti_condominiali_inclusi(self):
        c = stima_costi({"offerta_minima": 50_000, "spese_condominiali_arretrate": 10_000})
        assert c["debiti_condominiali"] == 10_000

    def test_spese_straordinarie_incluse(self):
        c = stima_costi({"offerta_minima": 50_000, "spese_straordinarie_deliberate": 8_000})
        assert c["spese_straordinarie"] == 8_000

    def test_imu_da_rendita(self):
        """IMU stimata dalla rendita catastale (>0) entra nel costo di possesso."""
        c0 = stima_costi({"offerta_minima": 100_000, "rendita_catastale": 0})
        c1 = stima_costi({"offerta_minima": 100_000, "rendita_catastale": 500})
        assert c1["imu_annua"] > 0
        assert c1["costo_possesso"] > c0["costo_possesso"]

    def test_spese_annue_aumentano_costo_possesso(self):
        c0 = stima_costi({"offerta_minima": 100_000, "spese_condominiali_annue": 0})
        c1 = stima_costi({"offerta_minima": 100_000, "spese_condominiali_annue": 1_200})
        assert c1["costo_possesso"] > c0["costo_possesso"]

    def test_costi_possesso_riducono_margine(self):
        base = {"offerta_minima": 100_000, "valore_mercato": 160_000, "superficie_mq": 80,
                "stato_manutentivo": "OTTIMO", "stato_occupazione": "LIBERO", "analisi_pdf": True}
        s_no, _ = calcola_score(base)
        s_si, _ = calcola_score({**base, "spese_condominiali_annue": 2_000,
                                 "spese_straordinarie_deliberate": 10_000,
                                 "rendita_catastale": 1_000})
        assert s_si < s_no

    def test_rendita_lorda_locazione(self):
        _, bd = calcola_score({"offerta_minima": 100_000, "canone_locazione_annuo": 6_000,
                               "valore_mercato": 130_000, "analisi_pdf": True})
        assert bd["rendita_lorda_pct"] == 6.0


# ─────────────────────────────────────────────────────────────
# MARGINE
# ─────────────────────────────────────────────────────────────

class TestMargine:
    def test_margine_positivo_su_affare(self):
        _, bd = calcola_score(affare_ottimo())
        assert bd["margine_eur"] > 0
        assert bd["margine_pct"] > 0

    def test_margine_negativo_su_trappola(self):
        _, bd = calcola_score(trappola())
        assert bd["margine_eur"] < 0, f"margine {bd['margine_eur']}"
        assert bd["pts_margine"] == 0.0

    def test_riferimento_valore_mercato(self):
        _, bd = calcola_score(affare_ottimo())
        assert bd["margine_riferimento"] == "valore_mercato"

    def test_fallback_senza_valore_mercato(self):
        asta = {"prezzo_base": 100_000, "offerta_minima": 60_000, "superficie_mq": 80,
                "stato_manutentivo": "OTTIMO", "stato_occupazione": "LIBERO"}
        _, bd = calcola_score(asta)
        assert bd["margine_riferimento"] == "prezzo_base"

    def test_roi_calcolato(self):
        _, bd = calcola_score(affare_ottimo())
        assert bd["roi_pct"] is not None


# ─────────────────────────────────────────────────────────────
# RANGE & ORDINAMENTO
# ─────────────────────────────────────────────────────────────

class TestRangeOrdinamento:
    def test_score_in_range(self):
        for a in (affare_ottimo(), trappola(), {}):
            s, _ = calcola_score(a)
            assert 0 <= s <= 100

    def test_affare_batte_trappola(self):
        assert calcola_score(affare_ottimo())[0] > calcola_score(trappola())[0]

    def test_affare_ottimo_alto(self):
        s, _ = calcola_score(affare_ottimo())
        assert s >= 70, f"un vero affare dovrebbe essere alto, score={s}"

    def test_trappola_bassa(self):
        s, _ = calcola_score(trappola())
        assert s < 45, f"margine negativo deve dare score basso, score={s}"

    def test_margine_maggiore_score_maggiore(self):
        base = affare_ottimo()
        poco = {**base, "offerta_minima": 110_000}   # margine ridotto
        molto = {**base, "offerta_minima": 60_000}    # margine ampio
        assert calcola_score(molto)[0] > calcola_score(poco)[0]

    def test_libero_batte_occupato(self):
        base = affare_ottimo()
        occ = {**base, "stato_occupazione": "OCCUPATO_CON_TITOLO", "occupazione_opponibile": True}
        assert calcola_score(base)[0] > calcola_score(occ)[0]

    def test_posizione_ottima_batte_scarsa(self):
        base = affare_ottimo()
        scarsa = {**base, "qualita_posizione": "SCARSA", "distanza_stazione_km": 12}
        assert calcola_score(base)[0] > calcola_score(scarsa)[0]

    def test_debiti_abbassano_score(self):
        base = affare_ottimo()
        con_debiti = {**base, "spese_condominiali_arretrate": 20_000}
        assert calcola_score(base)[0] > calcola_score(con_debiti)[0]


# ─────────────────────────────────────────────────────────────
# POSIZIONE
# ─────────────────────────────────────────────────────────────

class TestPosizione:
    @pytest.mark.parametrize("q", ["OTTIMA", "BUONA", "MEDIA", "SCARSA"])
    def test_qualita_valida(self, q):
        _, bd = calcola_score({"qualita_posizione": q, "offerta_minima": 50_000})
        assert bd["qualita_posizione"] == q

    def test_stazione_vicina_piu_punti(self):
        base = {"offerta_minima": 50_000, "valore_mercato": 100_000, "qualita_posizione": "BUONA"}
        vicina, _ = calcola_score({**base, "distanza_stazione_km": 0.3})
        lontana, _ = calcola_score({**base, "distanza_stazione_km": 10})
        assert vicina > lontana

    def test_posizione_max_20(self):
        _, bd = calcola_score({"qualita_posizione": "OTTIMA", "distanza_stazione_km": 0.1,
                               "offerta_minima": 50_000})
        assert bd["pts_posizione"] <= 20.0


# ─────────────────────────────────────────────────────────────
# MOLTIPLICATORI
# ─────────────────────────────────────────────────────────────

class TestMoltiplicatori:
    def test_quota_frazionata_taglia_score(self):
        base = affare_ottimo()
        frazionata = {**base, "quota_proprieta": "1/2 piena proprietà"}
        s_intera, _ = calcola_score(base)
        s_fraz, bd = calcola_score(frazionata)
        assert bd["molt_quota"] < 1.0
        assert s_fraz < s_intera

    def test_nuda_proprieta_penalizzata(self):
        _, bd = calcola_score({**affare_ottimo(), "quota_proprieta": "nuda proprietà"})
        assert bd["molt_quota"] < 1.0

    def test_confidenza_ridotta_senza_pdf(self):
        asta = {"prezzo_base": 100_000, "offerta_minima": 60_000, "superficie_mq": 80}
        _, bd = calcola_score(asta)
        assert bd["molt_confidenza"] < 1.0

    def test_confidenza_piena_con_pdf(self):
        _, bd = calcola_score(affare_ottimo())
        assert bd["molt_confidenza"] == 1.0


# ─────────────────────────────────────────────────────────────
# AFFIDABILITÀ / NOTE
# ─────────────────────────────────────────────────────────────

class TestAffidabilita:
    def test_note_vuote_max(self):
        _, bd = calcola_score({**affare_ottimo(), "note_critiche": ""})
        assert bd["pts_affidabilita"] == 10.0

    @pytest.mark.parametrize("nota", [
        "Immobile inagibile", "amianto nei solai", "abuso non sanabile da demolire",
        "non conforme urbanisticamente",
    ])
    def test_note_gravi_zero(self, nota):
        _, bd = calcola_score({**affare_ottimo(), "note_critiche": nota})
        assert bd["pts_affidabilita"] == 0.0

    def test_note_gravi_abbassano_score(self):
        base = affare_ottimo()
        s_ok, _ = calcola_score(base)
        s_bad, _ = calcola_score({**base, "note_critiche": "Immobile inagibile"})
        assert s_ok > s_bad


# ─────────────────────────────────────────────────────────────
# EDGE CASES
# ─────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_asta_vuota_non_crasha(self):
        s, bd = calcola_score({})
        assert 0 <= s <= 100
        assert "score_totale" in bd

    def test_prezzo_zero_non_crasha(self):
        s, _ = calcola_score({"prezzo_base": 0, "offerta_minima": 0})
        assert 0 <= s <= 100

    def test_costi_stringa_non_crasha(self):
        s, _ = calcola_score({"offerta_minima": 50_000, "costi_sanatoria": "abc",
                              "spese_condominiali_arretrate": "xyz"})
        assert 0 <= s <= 100

    def test_breakdown_campi_chiave(self):
        _, bd = calcola_score(affare_ottimo())
        for campo in ["costo_totale", "margine_eur", "margine_pct", "roi_pct",
                      "pts_margine", "pts_posizione", "pts_liberabilita",
                      "pts_affidabilita", "molt_quota", "molt_confidenza", "score_totale"]:
            assert campo in bd, f"manca {campo}"


# ─────────────────────────────────────────────────────────────
# LABEL / EMOJI / STABILITÀ
# ─────────────────────────────────────────────────────────────

class TestLabels:
    @pytest.mark.parametrize("score,emoji", [
        (80, "🔥"), (70, "⭐"), (50, "👍"), (30, "📌"), (0, "📌"),
    ])
    def test_emoji(self, score, emoji):
        assert score_emoji(score) == emoji

    @pytest.mark.parametrize("score,label", [
        (80, "🔥 Eccellente"), (65, "⭐ Ottima"), (50, "👍 Buona"),
        (35, "📌 Discreta"), (10, "⚠️ Bassa"),
    ])
    def test_label(self, score, label):
        assert score_label(score) == label


class TestStabilita:
    def test_deterministico(self):
        a = affare_ottimo()
        assert calcola_score(a) == calcola_score(a)

    def test_non_modifica_input(self):
        a = affare_ottimo()
        orig = dict(a)
        calcola_score(a)
        assert a == orig


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
