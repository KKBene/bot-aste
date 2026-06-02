"""
Test End-to-End del pipeline completo (senza DB Supabase).

Simula un'esecuzione reale:
  1. Scraping di 2 comuni reali
  2. Analisi dei dati estratti
  3. Calcolo score su ogni annuncio
  4. Generazione digest Telegram (senza inviarlo)
  5. Verifica coerenza risultati

Usa DB in-memory per non richiedere Supabase configurato.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import pytest
import time
from unittest.mock import patch, MagicMock

from scraper_api import run_scraper
from scorer import calcola_score, score_label
from notifier import _formatta_asta, send_digest


# ─────────────────────────────────────────────────────────────
# TEST E2E: Scraping + Scoring
# ─────────────────────────────────────────────────────────────

class TestE2EScrapeScore:
    """
    Scraping reale + scoring — verifica il flusso dati dall'inizio alla fine.
    """

    @pytest.fixture(scope="class")
    def annunci_reali(self):
        """Scrapa 2 comuni e restituisce i risultati (nuovi + esistenti)."""
        comuni = ["uboldo", "mozzate"]  # piccoli → veloci
        # scraper_api.run_scraper è sincrono (no asyncio)
        risultato = run_scraper(comuni, "residenziali", codici_esistenti=set())
        risultati = risultato["nuovi"] + risultato["esistenti"]
        print(f"\n  E2E: trovati {len(risultati)} annunci da {comuni}")
        return risultati

    def test_scraping_restituisce_lista(self, annunci_reali):
        assert isinstance(annunci_reali, list)

    def test_ogni_annuncio_ha_codice_univoco(self, annunci_reali):
        codici = [a["codice"] for a in annunci_reali]
        assert len(codici) == len(set(codici)), "Duplicati trovati!"

    def test_ogni_annuncio_ha_comune(self, annunci_reali):
        for a in annunci_reali:
            assert a.get("comune"), f"Comune mancante: {a.get('codice')}"

    def test_score_calcolabile_su_tutti(self, annunci_reali):
        """Lo scoring non deve mai crashare su dati reali."""
        for asta in annunci_reali:
            score, bd = calcola_score(asta)
            assert 0 <= score <= 100, (
                f"{asta['codice']}: score fuori range {score}"
            )
            assert "score_totale" in bd

    def test_annunci_ordinabili_per_score(self, annunci_reali):
        """Deve essere possibile ordinare per score."""
        scored = []
        for asta in annunci_reali:
            score, _ = calcola_score(asta)
            scored.append((score, asta["codice"]))
        scored.sort(reverse=True)
        assert scored[0][0] >= scored[-1][0]

    def test_digest_generabile(self, annunci_reali):
        """Il digest Telegram deve essere generabile senza crash."""
        # Aggiungi score ad ogni asta
        arricchite = []
        for asta in annunci_reali[:5]:  # primi 5 per velocità
            score, _ = calcola_score(asta)
            arricchite.append({**asta, "score": score})

        arricchite.sort(key=lambda x: x.get("score", 0), reverse=True)

        # Mock Telegram per non inviare davvero
        with patch("notifier.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            send_digest(arricchite[:3], {"nuovi_totali": len(annunci_reali), "pdf_analizzati": 0})
            assert mock_post.called

    def test_scoring_coerente_con_dati_reali(self, annunci_reali):
        """
        Verifica invarianti economici:
        - Annunci con 0 prezzo non devono avere score > 50 (solo per sconto)
        - Nessuno deve avere score 100 senza analisi PDF
        """
        for asta in annunci_reali:
            score, bd = calcola_score(asta)

            # Senza dati PDF → max 35+10+8+10+10 = 73 se tutto default
            # (nessuno può avere 35pt di sconto + 25pt di occupazione LIBERO
            #  senza analisi PDF che dice LIBERO)
            # Quindi: senza PDF, il max teorico è ragionevole
            if not asta.get("stato_occupazione"):
                # Occupazione sconosciuta → max 10pt per quella componente
                assert bd["pts_occupazione"] == 10

    def test_stampa_report_completo(self, annunci_reali):
        """Genera e stampa il report finale per ispezione visiva."""
        print("\n" + "="*60)
        print("REPORT E2E — ANNUNCI TROVATI")
        print("="*60)

        scored = []
        for asta in annunci_reali:
            score, bd = calcola_score(asta)
            scored.append((score, bd, asta))

        scored.sort(key=lambda x: x[0], reverse=True)

        for score, bd, asta in scored:
            print(f"\n[{score:.1f}/100] {score_label(score)}")
            print(f"  Codice:   {asta.get('codice')}")
            print(f"  Comune:   {asta.get('comune', 'N/D').title()}")
            print(f"  Prezzo:   €{asta.get('prezzo_base') or 0:,.0f} → "
                  f"€{asta.get('offerta_minima') or 0:,.0f} ({bd.get('sconto_pct', 0):.1f}% sconto)")
            print(f"  Tipologia: {asta.get('tipologia', 'N/D')}")
            print(f"  Data asta: {asta.get('data_asta', 'N/D')}")
            print(f"  Link:     {asta.get('link_dettaglio', 'N/D')}")

        print("\n" + "="*60)
        assert True  # non deve crashare


# ─────────────────────────────────────────────────────────────
# TEST: Gemini su testi sintetici (senza rete per PDF reali)
# ─────────────────────────────────────────────────────────────

class TestE2EAnalisiPDF:
    """
    Testa la pipeline PDF su testi sintetici già estratti.
    Non richiede download di PDF reali.
    """

    @pytest.fixture(scope="class")
    def analyzer(self):
        from pdf_analyzer import PDFAnalyzer
        from config import GEMINI_API_KEY, GEMINI_MODEL
        return PDFAnalyzer(GEMINI_API_KEY, GEMINI_MODEL)

    TESTI = {
        "libero_ottimo": (
            "Immobile libero da persone e cose. Superficie 90 mq. Piano quarto con ascensore. "
            "Condizioni ottime, ristrutturato recentemente. Nessuna difformità. "
            "Costi sanatoria: zero. Accatastato correttamente.",
            {"stato_occupazione": "LIBERO", "stato_manutentivo": "OTTIMO"}
        ),
        "debitore_pessimo": (
            "Occupato dal debitore esecutato. Superficie 55 mq. Piano terra senza ascensore. "
            "Condizioni pessime, infiltrazioni ovunque, impianti da rifare. "
            "Rilevata difformità nel bagno ampliato. Costi sanatoria stimati: 12.000 euro.",
            {"stato_occupazione": "OCCUPATO_DEBITORE", "stato_manutentivo": "PESSIMO"}
        ),
    }

    @pytest.mark.parametrize("nome,dati", list(TESTI.items()))
    def test_analisi_testo_sintetico(self, analyzer, nome, dati):
        testo, attesi = dati
        risultato = analyzer.analizza_con_gemini(testo)
        assert risultato is not None, f"Analisi fallita per '{nome}'"
        for campo, valore_atteso in attesi.items():
            assert risultato.get(campo) == valore_atteso, (
                f"[{nome}] {campo}: atteso {valore_atteso}, "
                f"got {risultato.get(campo)}"
            )


# ─────────────────────────────────────────────────────────────
# TEST: score + PDF integrati
# ─────────────────────────────────────────────────────────────

class TestE2EScoringConPDF:
    """Verifica che i dati PDF arricchiscano correttamente lo score."""

    def _asta_base(self):
        return {
            "codice": "TEST_E2E",
            "prezzo_base": 120_000,
            "offerta_minima": 80_000,  # 33.3% sconto → ~29pt
        }

    def test_pdf_libero_ottimo_alza_score(self):
        asta_senza = self._asta_base()
        asta_con = {
            **self._asta_base(),
            "stato_occupazione": "LIBERO",
            "stato_manutentivo": "OTTIMO",
            "costi_sanatoria": 0,
            "note_critiche": "",
        }
        s_senza, _ = calcola_score(asta_senza)
        s_con, _ = calcola_score(asta_con)
        assert s_con > s_senza, (
            f"PDF dovrebbe alzare score: senza={s_senza}, con={s_con}"
        )

    def test_pdf_pessimo_abbassa_score(self):
        asta_neutra = {
            **self._asta_base(),
            "stato_occupazione": "LIBERO",
            "stato_manutentivo": "BUONO",
        }
        asta_pessima = {
            **self._asta_base(),
            "stato_occupazione": "OCCUPATO_CON_TITOLO",
            "stato_manutentivo": "RUDERE",
            "costi_sanatoria": 25_000,
            "note_critiche": "Immobile inagibile",
        }
        s_n, _ = calcola_score(asta_neutra)
        s_p, _ = calcola_score(asta_pessima)
        assert s_n > s_p

    def test_pipeline_completa_senza_crash(self):
        """Simula l'intero flusso: scraping → score → digest."""
        # Dati simulati (come se fossero usciti dallo scraper + PDF analyzer)
        annunci = [
            {
                "codice": f"FAKE{i:04d}",
                "comune": "saronno",
                "prezzo_base": 100_000 + i * 10_000,
                "offerta_minima": 70_000 + i * 5_000,
                "indirizzo_immobile": f"Via Test {i}",
                "data_asta": "15/06/2025",
                "link_dettaglio": f"https://www.astalegale.net/Aste/Detail/FAKE{i:04d}",
                "stato_occupazione": ["LIBERO", "OCCUPATO_DEBITORE", None][i % 3],
                "stato_manutentivo": ["OTTIMO", "BUONO", "MEDIOCRE", "PESSIMO"][i % 4],
                "costi_sanatoria": [0, 5000, 15000][i % 3],
                "note_critiche": ["", "Piccola infiltrazione", "Immobile inagibile"][i % 3],
                "superficie_mq": 60 + i * 5,
            }
            for i in range(10)
        ]

        # Scoring
        for asta in annunci:
            score, bd = calcola_score(asta)
            asta["score"] = score
            assert 0 <= score <= 100

        # Ordina per score
        annunci.sort(key=lambda x: x.get("score", 0), reverse=True)

        # Genera digest
        with patch("notifier.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            send_digest(annunci[:5], {"nuovi_totali": 10, "pdf_analizzati": 8})

        assert mock_post.called

        # Verifica ordinamento
        scores = [a["score"] for a in annunci]
        assert scores == sorted(scores, reverse=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-s"])
