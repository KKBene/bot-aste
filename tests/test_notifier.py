"""
Test per il modulo notifier: formattazione messaggi Telegram, escape HTML,
split messaggi lunghi.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
from notifier import (
    _escape_html, _split_and_send, send_message, _formatta_asta, send_digest,
    parse_data_it, giorni_rimanenti, _termine_scaduto,
)


# ─────────────────────────────────────────────────────────────
# TEST: escape HTML
# ─────────────────────────────────────────────────────────────

class TestEscapeHTML:
    def test_ampersand_escaped(self):
        result = _escape_html("costo & valore")
        assert "&amp;" in result

    def test_less_than_escaped(self):
        result = _escape_html("prezzo < 100000")
        assert "&lt;" in result

    def test_greater_than_escaped(self):
        result = _escape_html("score > 75")
        assert "&gt;" in result

    def test_bold_tag_preservato(self):
        result = _escape_html("<b>ciao</b>")
        assert "<b>ciao</b>" in result

    def test_italic_tag_preservato(self):
        result = _escape_html("<i>nota</i>")
        assert "<i>nota</i>" in result

    def test_link_preservato(self):
        link = '<a href="https://example.com">Clicca qui</a>'
        result = _escape_html(link)
        assert 'href="https://example.com"' in result
        assert "Clicca qui" in result

    def test_link_con_caratteri_speciali_nel_testo(self):
        link = '<a href="https://example.com">Costo & valore < 100</a>'
        result = _escape_html(link)
        # Il link deve essere preservato
        assert 'href="https://example.com"' in result

    def test_testo_vuoto(self):
        assert _escape_html("") == ""

    def test_nessun_tag_html(self):
        result = _escape_html("Testo normale senza tag")
        assert result == "Testo normale senza tag"

    def test_caratteri_speciali_italiani(self):
        result = _escape_html("L'immobile è al 3° piano")
        # apostrofo e accenti non devono essere escaped
        assert "L'immobile" in result
        assert "è" in result

    def test_doppio_escape_non_applicato(self):
        # Non deve fare double-escape
        result = _escape_html("<b>costo & valore</b>")
        assert "<b>" in result
        assert "</b>" in result
        assert "&amp;" in result
        # Non deve avere &amp;amp;
        assert "&amp;amp;" not in result

    def test_codice_tag_preservato(self):
        result = _escape_html("<code>codice</code>")
        assert "<code>codice</code>" in result

    def test_tag_non_supportati_escapati(self):
        # <script> non è supportato, deve essere escaped
        result = _escape_html("<script>alert('xss')</script>")
        assert "<script>" not in result


# ─────────────────────────────────────────────────────────────
# TEST: formattazione singola asta
# ─────────────────────────────────────────────────────────────

class TestFormattaAsta:
    def get_asta_completa(self):
        return {
            "codice": "ABC123",
            "comune": "saronno",
            "prezzo_base": 120_000,
            "offerta_minima": 78_000,
            "indirizzo_immobile": "Via Roma 1",
            "stato_occupazione": "LIBERO",
            "stato_manutentivo": "BUONO",
            "note_critiche": "",
            "superficie_mq": 85.0,
            "data_asta": "15/01/2026",
            "link_dettaglio": "https://www.astalegale.net/Aste/Detail/ABC123",
            "score": 72.5,
        }

    def test_contiene_score(self):
        card = _formatta_asta(self.get_asta_completa(), 1)
        assert "72" in card

    def test_contiene_comune(self):
        card = _formatta_asta(self.get_asta_completa(), 1)
        assert "Saronno" in card  # title() applicato

    def test_contiene_prezzo(self):
        card = _formatta_asta(self.get_asta_completa(), 1)
        assert "78" in card  # parte del prezzo 78.000

    def test_contiene_sconto(self):
        card = _formatta_asta(self.get_asta_completa(), 1)
        # (120000-78000)/120000 = 35%
        assert "35%" in card

    def test_contiene_prezzo_mq(self):
        card = _formatta_asta(self.get_asta_completa(), 1)
        # 78000/85 ≈ 918 €/mq
        assert "918" in card or "917" in card or "919" in card

    def test_contiene_link(self):
        card = _formatta_asta(self.get_asta_completa(), 1)
        assert "astalegale.net" in card

    def test_libero_verde(self):
        card = _formatta_asta(self.get_asta_completa(), 1)
        assert "✅" in card

    def test_occupato_titolo_rosso(self):
        asta = {**self.get_asta_completa(), "stato_occupazione": "OCCUPATO_CON_TITOLO"}
        card = _formatta_asta(asta, 1)
        assert "⛔" in card

    def test_score_alto_emoji_fuoco(self):
        asta = {**self.get_asta_completa(), "score": 80}
        card = _formatta_asta(asta, 1)
        assert "🔥" in card

    def test_score_medio_stella(self):
        asta = {**self.get_asta_completa(), "score": 65}
        card = _formatta_asta(asta, 1)
        assert "⭐" in card

    def test_note_critiche_mostrate(self):
        asta = {**self.get_asta_completa(), "note_critiche": "Presenza amianto"}
        card = _formatta_asta(asta, 1)
        assert "Presenza amianto" in card

    def test_note_vuote_non_mostrate(self):
        asta = {**self.get_asta_completa(), "note_critiche": ""}
        card = _formatta_asta(asta, 1)
        assert "⚠️" not in card  # nessun warning per note vuote

    def test_asta_senza_dati_non_crasha(self):
        card = _formatta_asta({"codice": "X", "score": 50}, 1)
        assert isinstance(card, str) and len(card) > 0

    def test_rank_nel_testo(self):
        card = _formatta_asta(self.get_asta_completa(), 3)
        assert "#3" in card


# ─────────────────────────────────────────────────────────────
# TEST: invio messaggi (mockato)
# ─────────────────────────────────────────────────────────────

class TestSendMessage:
    @patch("notifier.requests.post")
    def test_invio_riuscito(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp
        assert send_message("Test") is True

    @patch("notifier.requests.post")
    def test_invio_fallito_400(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_post.return_value = mock_resp
        assert send_message("Test") is False

    @patch("notifier.requests.post")
    def test_timeout_gestito(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.Timeout()
        assert send_message("Test") is False

    @patch("notifier.requests.post")
    def test_messaggio_lungo_splittato(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        lungo = "A" * 5000  # > 4000 char
        _split_and_send(lungo)
        # Deve aver inviato più di un messaggio
        assert mock_post.call_count >= 2


# ─────────────────────────────────────────────────────────────
# TEST: digest completo
# ─────────────────────────────────────────────────────────────

class TestSendDigest:
    def get_aste_esempio(self):
        return [
            {
                "codice": f"TEST{i:03d}",
                "comune": "saronno",
                "prezzo_base": 100_000 + i * 5_000,
                "offerta_minima": 70_000,
                "stato_occupazione": "LIBERO",
                "stato_manutentivo": "BUONO",
                "note_critiche": "",
                "superficie_mq": 80.0,
                "data_asta": "20/06/2025",
                "link_dettaglio": f"https://www.astalegale.net/Aste/Detail/TEST{i:03d}",
                "score": 80 - i * 5,
            }
            for i in range(5)
        ]

    @patch("notifier.requests.post")
    def test_digest_vuoto(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        send_digest([], {"nuovi_totali": 0, "pdf_analizzati": 0})
        assert mock_post.called

    @patch("notifier.requests.post")
    def test_digest_con_aste(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        aste = self.get_aste_esempio()
        send_digest(aste, {"nuovi_totali": 10, "pdf_analizzati": 5})
        assert mock_post.called

        # Verifica il contenuto del primo messaggio
        call_args = mock_post.call_args[1]["json"]
        text = call_args["text"]
        assert "Report Aste" in text
        assert "TEST000" in text or "Saronno" in text

    @patch("notifier.requests.post")
    def test_digest_mostra_statistiche(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        send_digest([], {"nuovi_totali": 42, "pdf_analizzati": 15})
        call_args = mock_post.call_args[1]["json"]
        text = call_args["text"]
        assert "42" in text


# ─────────────────────────────────────────────────────────────
# TEST: timing (giorni al termine offerte)
# ─────────────────────────────────────────────────────────────

class TestTiming:
    def test_parse_data_con_ora(self):
        dt = parse_data_it("25/05/2026 09:00")
        assert dt == datetime(2026, 5, 25, 9, 0)

    def test_parse_data_senza_ora(self):
        dt = parse_data_it("25/05/2026")
        assert dt == datetime(2026, 5, 25)

    def test_parse_data_invalida(self):
        assert parse_data_it("non una data") is None
        assert parse_data_it(None) is None
        assert parse_data_it("") is None

    def test_giorni_rimanenti_futuro(self):
        adesso = datetime(2026, 5, 1, 12, 0)
        assert giorni_rimanenti("11/05/2026 13:00", adesso) == 10

    def test_giorni_rimanenti_passato(self):
        adesso = datetime(2026, 5, 23, 12, 0)
        assert giorni_rimanenti("22/05/2026 13:00", adesso) == -1

    def test_giorni_rimanenti_oggi(self):
        adesso = datetime(2026, 5, 23, 8, 0)
        assert giorni_rimanenti("23/05/2026 13:00", adesso) == 0

    def test_giorni_rimanenti_non_parsabile(self):
        assert giorni_rimanenti(None) is None

    def test_termine_scaduto_true(self):
        adesso_patch = datetime(2026, 5, 23)
        with patch("notifier.datetime") as mock_dt:
            mock_dt.now.return_value = adesso_patch
            mock_dt.strptime = datetime.strptime
            assert _termine_scaduto({"termine_offerte": "20/05/2026 13:00"}) is True

    def test_termine_non_scaduto(self):
        with patch("notifier.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 23)
            mock_dt.strptime = datetime.strptime
            assert _termine_scaduto({"termine_offerte": "30/06/2026 13:00"}) is False

    def test_termine_assente_non_scaduto(self):
        """Senza termine non possiamo dire che è scaduto → non filtrare."""
        assert _termine_scaduto({"codice": "X"}) is False

    def test_scaduto_stamattina_non_e_piu_giocabile(self):
        """Contando solo i giorni restava nel report fino a mezzanotte, come 'OGGI!'."""
        asta = {"termine_offerte": "28/07/2026 12:00"}
        assert _termine_scaduto(asta, datetime(2026, 7, 28, 18, 0)) is True

    def test_stesso_giorno_ma_ora_non_ancora_passata(self):
        asta = {"termine_offerte": "28/07/2026 12:00"}
        assert _termine_scaduto(asta, datetime(2026, 7, 28, 9, 0)) is False

    def test_senza_orario_vale_fino_a_fine_giornata(self):
        asta = {"termine_offerte": "28/07/2026"}
        assert _termine_scaduto(asta, datetime(2026, 7, 28, 18, 0)) is False
        assert _termine_scaduto(asta, datetime(2026, 7, 29, 0, 1)) is True


class TestDigestFiltraScaduti:
    @patch("notifier.requests.post")
    def test_digest_scarta_offerte_scadute(self, mock_post):
        mock_resp = MagicMock(); mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        with patch("notifier.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 23)
            mock_dt.strptime = datetime.strptime
            aste = [
                {"codice": "SCADUTA", "comune": "uboldo", "score": 80,
                 "indirizzo_immobile": "Via Scaduta 1", "prezzo_base": 100000,
                 "termine_offerte": "20/05/2026 13:00"},
                {"codice": "VALIDA", "comune": "uboldo", "score": 70,
                 "indirizzo_immobile": "Via Valida 2", "prezzo_base": 100000,
                 "termine_offerte": "30/06/2026 13:00"},
            ]
            send_digest(aste, {"nuovi_totali": 2, "pdf_analizzati": 0})

        text = mock_post.call_args[1]["json"]["text"]
        assert "Via Valida 2" in text
        assert "Via Scaduta 1" not in text

    @patch("notifier.requests.post")
    def test_digest_urgenza_mostrata(self, mock_post):
        mock_resp = MagicMock(); mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        with patch("notifier.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 23)
            mock_dt.strptime = datetime.strptime
            aste = [{"codice": "URGENTE", "comune": "uboldo", "score": 80,
                     "prezzo_base": 100000, "termine_offerte": "27/05/2026 13:00"}]
            send_digest(aste, {"nuovi_totali": 1, "pdf_analizzati": 0})

        text = mock_post.call_args[1]["json"]["text"]
        assert "tra 4gg" in text


class TestRibassoBanner:
    def test_card_mostra_banner_ribasso(self):
        asta = {"codice": "X", "comune": "uboldo", "score": 70,
                "indirizzo_immobile": "Via Test 1", "prezzo_base": 60_000,
                "offerta_minima": 45_000, "_ribasso_da": 80_000, "_ribasso_pct": 25.0}
        out = _formatta_asta(asta, 1)
        assert "RIBASSATO" in out
        assert "25%" in out
        assert "80,000" in out

    def test_card_senza_ribasso_non_mostra_banner(self):
        asta = {"codice": "X", "comune": "uboldo", "score": 70,
                "indirizzo_immobile": "Via Test 1", "prezzo_base": 60_000,
                "offerta_minima": 45_000}
        out = _formatta_asta(asta, 1)
        assert "RIBASSATO" not in out


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
