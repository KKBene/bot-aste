"""
Test per il PDF Analyzer.

Testa:
- Download PDF da URL reali
- Estrazione testo con PyMuPDF
- Parsing e normalizzazione output Gemini
- Gestione errori (PDF corrotti, URL non validi, testo vuoto)
- Qualità dell'analisi su testo sintetico
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from pdf_analyzer import PDFAnalyzer, _hints_deterministici
from config import GEMINI_API_KEY, GEMINI_MODEL

# PDF pubblici per test (piccole perizie campione)
PDF_TEST_PUBLIC = "https://www.orimi.com/pdf-test.pdf"

# Testo perizia sintetico per test Gemini
TESTO_PERIZIA_SINTETICO = """
=== PAGINA 1 ===
TRIBUNALE DI VARESE
PERIZIA DI STIMA
Procedura Esecutiva Immobiliare n. 123/2024

DESCRIZIONE IMMOBILE
Appartamento sito in Saronno (VA), Via Roma 15, piano terzo.
Superficie commerciale: 85 mq
Piano: terzo piano di edificio condominiale con ascensore.

STATO OCCUPAZIONE
L'immobile risulta LIBERO da persone e cose. Non sono presenti occupanti.
Le chiavi sono detenute dal custode giudiziario.

=== PAGINA 2 ===
STATO CONSERVATIVO
L'appartamento si presenta in buone condizioni generali.
I pavimenti sono in ceramica in buono stato.
Gli impianti elettrico e idraulico sono recenti.
Le condizioni si possono definire BUONE.

CONFORMITÀ URBANISTICA
L'immobile risulta regolarmente accatastato.
Non sono state rilevate difformità urbanistiche significative.
La perizia attesta la piena conformità catastale.
Costi di sanatoria stimati: ZERO - nessuna difformità.

=== PAGINA 3 ===
CONCLUSIONI
La valutazione finale dell'immobile è di € 120.000,00.
Si tratta di un appartamento in ottimo stato di conservazione, libero e conforme.
"""

TESTO_PERIZIA_PROBLEMI = """
=== PAGINA 1 ===
TRIBUNALE DI MILANO
PERIZIA CTU n. 456/2024

IMMOBILE: appartamento in Tradate (VA), Via Manzoni 8

STATO POSSESSO
L'immobile è OCCUPATO dal debitore esecutato e dal nucleo familiare.
L'occupazione cesserà alla pronuncia del decreto di trasferimento.

=== PAGINA 2 ===
CONDIZIONI
L'appartamento versa in PESSIME condizioni manutentive.
Presenti infiltrazioni dal tetto, impianti da rifare completamente.
Lo stato si definisce pessimo: necessita ristrutturazione totale.

CONFORMITÀ
Rilevata difformità nel locale bagno (ampliamento abusivo).
Costi stimati per sanatoria: 8.500 euro.

SUPERFICIE
Superficie commerciale calcolata: 62 mq (compreso loggia ponderata)
Piano: secondo piano, NO ascensore.
"""


# ─────────────────────────────────────────────────────────────
# FIXTURE
# ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def analyzer():
    return PDFAnalyzer(GEMINI_API_KEY, GEMINI_MODEL)


# ─────────────────────────────────────────────────────────────
# TEST: download PDF
# ─────────────────────────────────────────────────────────────

class TestDownloadPDF:
    def test_download_pdf_pubblico(self, analyzer):
        path = analyzer.scarica_pdf(PDF_TEST_PUBLIC)
        assert path is not None, "Download fallito per PDF pubblico"
        assert path.exists(), "File non trovato dopo download"
        assert path.stat().st_size > 0, "File vuoto"
        path.unlink(missing_ok=True)  # cleanup

    def test_url_invalido_ritorna_none(self, analyzer):
        path = analyzer.scarica_pdf("https://url-che-non-esiste-mai.xyz/fake.pdf")
        assert path is None, "URL invalido deve ritornare None"

    def test_url_vuoto_ritorna_none(self, analyzer):
        path = analyzer.scarica_pdf("")
        assert path is None

    def test_url_none_ritorna_none(self, analyzer):
        result = analyzer.analizza_pdf_da_url(None)
        assert result is None

    def test_url_stringa_vuota_ritorna_none(self, analyzer):
        result = analyzer.analizza_pdf_da_url("   ")
        assert result is None

    def test_file_pdf_valido_dopo_download(self, analyzer):
        path = analyzer.scarica_pdf(PDF_TEST_PUBLIC)
        if path is None:
            pytest.skip("Download non disponibile")
        try:
            import fitz
            doc = fitz.open(str(path))
            assert len(doc) > 0, "PDF senza pagine"
            doc.close()
        finally:
            path.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────
# TEST: estrazione testo
# ─────────────────────────────────────────────────────────────

class TestEstrazioneTesto:
    def test_estrae_testo_da_pdf_pubblico(self, analyzer):
        path = analyzer.scarica_pdf(PDF_TEST_PUBLIC)
        if path is None:
            pytest.skip("Download non disponibile")
        try:
            testo = analyzer.estrai_testo(path)
            assert testo is not None
            assert len(testo) > 10, "Testo troppo corto"
            print(f"\n  Testo estratto: {len(testo)} caratteri")
        finally:
            path.unlink(missing_ok=True)

    def test_testo_troncato_al_limite(self, analyzer):
        """Il testo non deve superare MAX_PDF_CHARS."""
        from config import MAX_PDF_CHARS
        path = analyzer.scarica_pdf(PDF_TEST_PUBLIC)
        if path is None:
            pytest.skip("Download non disponibile")
        try:
            testo = analyzer.estrai_testo(path)
            if testo:
                assert len(testo) <= MAX_PDF_CHARS + 100  # margine per il marker [troncato]
        finally:
            path.unlink(missing_ok=True)

    def test_pdf_inesistente_ritorna_none(self, analyzer):
        from pathlib import Path
        fake_path = Path("/tmp/non_esiste_questo_file.pdf")
        result = analyzer.estrai_testo(fake_path)
        assert result is None


# ─────────────────────────────────────────────────────────────
# TEST: analisi Gemini (con testo sintetico)
# ─────────────────────────────────────────────────────────────

class TestAnalisiGemini:
    def test_analisi_perizia_libera(self, analyzer):
        """Testo con immobile LIBERO deve dare stato_occupazione=LIBERO."""
        dati = analyzer.analizza_con_gemini(TESTO_PERIZIA_SINTETICO)
        assert dati is not None, "Analisi fallita"
        assert dati.get("stato_occupazione") == "LIBERO", (
            f"Atteso LIBERO, got: {dati.get('stato_occupazione')}"
        )

    def test_analisi_superficie(self, analyzer):
        """Deve estrarre i 85 mq corretti."""
        dati = analyzer.analizza_con_gemini(TESTO_PERIZIA_SINTETICO)
        assert dati is not None
        sup = dati.get("superficie_mq")
        assert sup is not None, "Superficie non estratta"
        assert isinstance(sup, (int, float))
        assert 80 <= sup <= 90, f"Superficie attesa ~85, got {sup}"

    def test_analisi_manutenzione_buono(self, analyzer):
        dati = analyzer.analizza_con_gemini(TESTO_PERIZIA_SINTETICO)
        assert dati is not None
        assert dati.get("stato_manutentivo") in ("BUONO", "OTTIMO"), (
            f"Atteso BUONO o OTTIMO, got {dati.get('stato_manutentivo')}"
        )

    def test_analisi_sanatoria_zero(self, analyzer):
        """Nessuna difformità → costi_sanatoria = 0."""
        dati = analyzer.analizza_con_gemini(TESTO_PERIZIA_SINTETICO)
        assert dati is not None
        assert dati.get("costi_sanatoria") == 0 or dati.get("costi_sanatoria") is None

    def test_analisi_note_critiche_vuote(self, analyzer):
        """Perizia senza problemi gravi → note_critiche vuote."""
        dati = analyzer.analizza_con_gemini(TESTO_PERIZIA_SINTETICO)
        assert dati is not None
        note = dati.get("note_critiche", "")
        critical = ["inagibile", "amianto", "crollo"]
        for kw in critical:
            assert kw.lower() not in (note or "").lower()

    def test_analisi_perizia_con_problemi(self, analyzer):
        """Testo con occupazione debitore e pessimo stato."""
        dati = analyzer.analizza_con_gemini(TESTO_PERIZIA_PROBLEMI)
        assert dati is not None
        assert dati.get("stato_occupazione") == "OCCUPATO_DEBITORE", (
            f"Atteso OCCUPATO_DEBITORE, got {dati.get('stato_occupazione')}"
        )

    def test_analisi_manutenzione_pessimo(self, analyzer):
        dati = analyzer.analizza_con_gemini(TESTO_PERIZIA_PROBLEMI)
        assert dati is not None
        assert dati.get("stato_manutentivo") == "PESSIMO", (
            f"Atteso PESSIMO, got {dati.get('stato_manutentivo')}"
        )

    def test_analisi_sanatoria_8500(self, analyzer):
        """Deve estrarre i 8500 euro di sanatoria."""
        dati = analyzer.analizza_con_gemini(TESTO_PERIZIA_PROBLEMI)
        assert dati is not None
        costi = dati.get("costi_sanatoria")
        assert costi is not None, "Costi sanatoria non estratti"
        assert 7000 <= float(costi) <= 10000, f"Costi attesi ~8500, got {costi}"

    def test_analisi_superficie_62_mq(self, analyzer):
        dati = analyzer.analizza_con_gemini(TESTO_PERIZIA_PROBLEMI)
        assert dati is not None
        sup = dati.get("superficie_mq")
        assert sup is not None
        assert 55 <= float(sup) <= 70, f"Superficie attesa ~62, got {sup}"

    def test_analisi_ritorna_dict(self, analyzer):
        dati = analyzer.analizza_con_gemini(TESTO_PERIZIA_SINTETICO)
        assert isinstance(dati, dict)

    def test_analisi_ha_tutti_i_campi(self, analyzer):
        dati = analyzer.analizza_con_gemini(TESTO_PERIZIA_SINTETICO)
        assert dati is not None
        required = ["stato_occupazione", "occupazione_opponibile", "costi_sanatoria",
                    "superficie_mq", "stato_manutentivo", "piano_ascensore",
                    "valore_mercato", "spese_condominiali_arretrate", "quota_proprieta",
                    "categoria_catastale", "anno_costruzione", "classe_energetica",
                    "tipologia_immobile", "note_critiche"]
        for campo in required:
            assert campo in dati, f"Campo mancante: {campo}"

    def test_analisi_valore_mercato_estratto(self, analyzer):
        """La perizia sintetica indica valore € 120.000 → deve estrarlo."""
        dati = analyzer.analizza_con_gemini(TESTO_PERIZIA_SINTETICO)
        assert dati is not None
        vm = dati.get("valore_mercato")
        assert vm is not None and 110_000 <= vm <= 130_000, f"Valore mercato: {vm}"


# ─────────────────────────────────────────────────────────────
# TEST: normalizzazione
# ─────────────────────────────────────────────────────────────

class TestNormalizzazione:
    @pytest.fixture
    def a(self):
        return PDFAnalyzer(GEMINI_API_KEY, GEMINI_MODEL)

    @pytest.mark.parametrize("stato_occ", [
        "LIBERO", "OCCUPATO_DEBITORE", "OCCUPATO_CON_TITOLO", "OCCUPATO_SENZA_TITOLO"
    ])
    def test_stato_occupazione_valido_mantenuto(self, a, stato_occ):
        dati = a._normalizza({"stato_occupazione": stato_occ, "stato_manutentivo": "BUONO"})
        assert dati["stato_occupazione"] == stato_occ

    def test_stato_occupazione_invalido_diventa_none(self, a):
        dati = a._normalizza({"stato_occupazione": "STATO_INVALIDO"})
        assert dati["stato_occupazione"] is None

    @pytest.mark.parametrize("stato_mnut", ["OTTIMO", "BUONO", "MEDIOCRE", "PESSIMO", "RUDERE"])
    def test_stato_manutentivo_valido(self, a, stato_mnut):
        dati = a._normalizza({"stato_manutentivo": stato_mnut})
        assert dati["stato_manutentivo"] == stato_mnut

    def test_superfice_stringa_a_float(self, a):
        dati = a._normalizza({"superficie_mq": "85.5"})
        assert dati["superficie_mq"] == 85.5

    def test_costi_stringa_a_float(self, a):
        dati = a._normalizza({"costi_sanatoria": "8500"})
        assert dati["costi_sanatoria"] == 8500.0

    def test_costi_none_rimane_none(self, a):
        dati = a._normalizza({"costi_sanatoria": None})
        assert dati["costi_sanatoria"] is None

    def test_stato_lowercase_normalizzato(self, a):
        dati = a._normalizza({"stato_occupazione": "libero"})
        assert dati["stato_occupazione"] == "LIBERO"

    def test_note_critiche_strip(self, a):
        dati = a._normalizza({"note_critiche": "  Problema grave  "})
        assert dati["note_critiche"] == "Problema grave"

    # ── Nuovi campi ──────────────────────────────────────────
    def test_valore_mercato_normalizzato(self, a):
        dati = a._normalizza({"valore_mercato": "120000"})
        assert dati["valore_mercato"] == 120_000.0

    def test_debiti_condominiali_normalizzati(self, a):
        dati = a._normalizza({"spese_condominiali_arretrate": "2246.74"})
        assert dati["spese_condominiali_arretrate"] == 2246.74

    def test_anno_costruzione_intero(self, a):
        dati = a._normalizza({"anno_costruzione": "1950"})
        assert dati["anno_costruzione"] == 1950
        assert isinstance(dati["anno_costruzione"], int)

    def test_anno_costruzione_implausibile_none(self, a):
        dati = a._normalizza({"anno_costruzione": "12"})
        assert dati["anno_costruzione"] is None

    @pytest.mark.parametrize("val,expected", [
        (True, True), (False, False), ("true", True), ("false", False),
        ("si", True), ("no", False), (None, None), ("forse", None),
    ])
    def test_opponibile_normalizzato(self, a, val, expected):
        dati = a._normalizza({"occupazione_opponibile": val})
        assert dati["occupazione_opponibile"] is expected

    def test_categoria_catastale_mantenuta(self, a):
        dati = a._normalizza({"categoria_catastale": "A/3"})
        assert dati["categoria_catastale"] == "A/3"

    def test_quota_proprieta_mantenuta(self, a):
        dati = a._normalizza({"quota_proprieta": "1/1 piena proprietà"})
        assert dati["quota_proprieta"] == "1/1 piena proprietà"

    def test_tipologia_immobile_mantenuta(self, a):
        dati = a._normalizza({"tipologia_immobile": "villa singola"})
        assert dati["tipologia_immobile"] == "villa singola"

    # ── Variabili economiche/possesso aggiuntive ─────────────
    def test_spese_annue_normalizzate(self, a):
        dati = a._normalizza({"spese_condominiali_annue": "353,51"})
        assert dati["spese_condominiali_annue"] == 353.51

    def test_spese_straordinarie_normalizzate(self, a):
        dati = a._normalizza({"spese_straordinarie_deliberate": "1.200,00"})
        assert dati["spese_straordinarie_deliberate"] == 1200.0

    def test_rendita_catastale_normalizzata(self, a):
        dati = a._normalizza({"rendita_catastale": "383,47"})
        assert dati["rendita_catastale"] == 383.47

    def test_canone_locazione_normalizzato(self, a):
        dati = a._normalizza({"canone_locazione_annuo": "5.400,00"})
        assert dati["canone_locazione_annuo"] == 5400.0

    def test_pertinenze_mantenute(self, a):
        dati = a._normalizza({"pertinenze": " cantina, box "})
        assert dati["pertinenze"] == "cantina, box"

    def test_pertinenze_vuote_none(self, a):
        dati = a._normalizza({"pertinenze": ""})
        assert dati["pertinenze"] is None


class TestRouterQualita:
    def test_hint_valore_mercato_preferisce_omv_finale(self):
        testo = """
        Valore superficie principale: 61,77 x 750,00 = 46.327,50
        Valore di mercato (calcolato in quota e diritto al netto degli aggiustamenti): €. 46.327,50
        Spese di regolarizzazione delle difformità (vedi cap.8): € 3.000,00
        Valore di Mercato dell'immobile nello stato di fatto e di diritto in cui si trova: € 43.327,50
        Spese condominiali scadute ed insolute alla data della perizia: € 14.855,84
        """
        hints = _hints_deterministici(testo)
        assert hints["valore_mercato"] == 43327.5
        assert hints["costi_sanatoria"] == 3000.0
        assert hints["spese_condominiali_arretrate"] == 14855.84

    def test_hint_quota_frazionata_catturata(self):
        """Quota 1/3 nel testo deve essere imposta deterministicamente."""
        testo = "appartamento della superficie di 95 mq per la quota di piena proprietà: di 1/3 di 1/3"
        hints = _hints_deterministici(testo)
        assert hints.get("quota_proprieta") == "1/3 piena proprietà"

    def test_hint_quota_intera_nessun_falso_positivo(self):
        """Quota piena (nessuna frazione <1) non deve generare hint."""
        testo = "appartamento per la quota di 1/1 di piena proprietà, valore di mercato € 100.000"
        hints = _hints_deterministici(testo)
        assert "quota_proprieta" not in hints

    def test_hint_quota_ignora_date(self):
        """Una data tipo 1/2024 non deve essere scambiata per quota."""
        testo = "atto registrato il quota immobile rep. 1/2024 presso il tribunale"
        hints = _hints_deterministici(testo)
        assert "quota_proprieta" not in hints

    def test_applica_hint_quota_sovrascrive_llm(self):
        """L'hint quota deve sovrascrivere il valore dell'LLM."""
        a = PDFAnalyzer("", "")
        dati = a._applica_hints({"quota_proprieta": "1/1 piena proprietà"},
                                {"quota_proprieta": "1/3 piena proprietà"})
        assert dati["quota_proprieta"] == "1/3 piena proprietà"

    def test_best_mode_tiene_gemini_a_parita(self):
        a = PDFAnalyzer("", "")
        mistral = {
            "stato_occupazione": "LIBERO",
            "superficie_mq": 80,
            "valore_mercato": 100000,
            "stato_manutentivo": "BUONO",
            "note_critiche": "",
        }
        gemini = dict(mistral)
        scelto = a._scegli_migliore(gemini, mistral, "Gemini Vision", "Mistral OCR+LLM")
        assert scelto is gemini


# ─────────────────────────────────────────────────────────────
# TEST: arbitraggio automatico (secondo provider su estrazione sospetta)
# ─────────────────────────────────────────────────────────────

class TestArbitraggio:
    COMPLETO = {
        "stato_occupazione": "LIBERO",
        "superficie_mq": 80,
        "valore_mercato": 100000,
        "stato_manutentivo": "BUONO",
        "note_critiche": "",
    }

    def test_estrazione_completa_non_e_sospetta(self):
        assert PDFAnalyzer._e_sospetto(self.COMPLETO, {}) is False

    def test_valore_mercato_mancante_senza_hint_e_sospetto(self):
        dati = dict(self.COMPLETO, valore_mercato=None)
        assert PDFAnalyzer._e_sospetto(dati, {}) is True

    def test_valore_mercato_mancante_ma_con_hint_non_e_sospetto(self):
        """Se l'hint deterministico l'ha già colmato, non serve un secondo parere."""
        dati = dict(self.COMPLETO, valore_mercato=None)
        assert PDFAnalyzer._e_sospetto(dati, {"valore_mercato": 100000}) is False

    def test_superficie_fuori_range_e_sospetto(self):
        dati = dict(self.COMPLETO, superficie_mq=5000)
        assert PDFAnalyzer._e_sospetto(dati, {}) is True

    def test_anno_fuori_range_e_sospetto(self):
        dati = dict(self.COMPLETO, anno_costruzione=3050)
        assert PDFAnalyzer._e_sospetto(dati, {}) is True

    def test_analizza_testo_non_chiama_arbitro_se_primario_ok(self):
        """Estrazione Groq pulita: Gemini non deve nemmeno essere chiamato (costa quota)."""
        a = PDFAnalyzer("", "")
        a.text_provider = "groq"
        with patch.object(a, "_prova_groq", return_value=dict(self.COMPLETO)) as mock_groq, \
             patch.object(a, "_prova_gemini_chain") as mock_gemini:
            risultato = a.analizza_testo("testo perizia qualunque")
        mock_groq.assert_called_once()
        mock_gemini.assert_not_called()
        assert risultato["valore_mercato"] == 100000

    def test_analizza_testo_chiama_arbitro_se_primario_sospetto(self):
        """Groq senza valore_mercato (e nessun hint): deve scattare l'arbitraggio Gemini."""
        a = PDFAnalyzer("", "")
        a.text_provider = "groq"
        groq_result = dict(self.COMPLETO, valore_mercato=None)
        gemini_result = dict(self.COMPLETO, valore_mercato=120000)
        with patch.object(a, "_prova_groq", return_value=groq_result), \
             patch.object(a, "_prova_gemini_chain", return_value=gemini_result) as mock_gemini:
            risultato = a.analizza_testo("testo perizia senza valore chiaro")
        mock_gemini.assert_called_once()
        assert risultato["valore_mercato"] == 120000  # l'arbitro ha trovato il campo mancante

    def test_analizza_testo_tiene_primario_se_arbitro_non_migliora(self):
        """A parità/peggioramento l'arbitro non deve sostituire il primario."""
        a = PDFAnalyzer("", "")
        a.text_provider = "groq"
        groq_result = dict(self.COMPLETO, valore_mercato=None)
        gemini_result = {"valore_mercato": None}  # arbitro anche peggio: quasi tutto assente
        with patch.object(a, "_prova_groq", return_value=groq_result), \
             patch.object(a, "_prova_gemini_chain", return_value=gemini_result):
            risultato = a.analizza_testo("testo perizia senza valore chiaro")
        assert risultato["stato_occupazione"] == "LIBERO"  # resta il primario, più completo

    def test_analizza_testo_primario_none_usa_arbitro(self):
        a = PDFAnalyzer("", "")
        a.text_provider = "groq"
        gemini_result = dict(self.COMPLETO)
        with patch.object(a, "_prova_groq", return_value=None), \
             patch.object(a, "_prova_gemini_chain", return_value=gemini_result) as mock_gemini:
            risultato = a.analizza_testo("testo perizia illeggibile")
        mock_gemini.assert_called_once()
        assert risultato["valore_mercato"] == 100000


# ─────────────────────────────────────────────────────────────
# TEST: _to_float (conversione robusta numeri italiani)
# ─────────────────────────────────────────────────────────────

class TestToFloat:
    @pytest.mark.parametrize("val,expected", [
        (1000, 1000.0),
        (1234.56, 1234.56),
        ("1000", 1000.0),
        ("1.234,56", 1234.56),       # formato italiano
        ("1234,56", 1234.56),
        ("€ 82.147,20", 82147.20),
        ("120000", 120000.0),
        (None, None),
        ("", None),
        ("null", None),
        ("ND", None),
        ("abc", None),
    ])
    def test_to_float(self, val, expected):
        result = PDFAnalyzer._to_float(val)
        if expected is None:
            assert result is None, f"'{val}' → atteso None, got {result}"
        else:
            assert result is not None and abs(result - expected) < 0.01, (
                f"'{val}' → atteso {expected}, got {result}"
            )


# ─────────────────────────────────────────────────────────────
# TEST: rilevamento PDF scansionati (watermark-only)
# ─────────────────────────────────────────────────────────────

class TestScansione:
    @pytest.fixture
    def a(self):
        return PDFAnalyzer(GEMINI_API_KEY, GEMINI_MODEL)

    def _crea_pdf(self, testo_per_pagina: list) -> Path:
        """Crea un PDF di test con il testo specificato per pagina."""
        import fitz
        doc = fitz.open()
        for testo in testo_per_pagina:
            page = doc.new_page()
            page.insert_text((72, 72), testo, fontsize=11)
        path = Path(f"/tmp/test_scan_{os.getpid()}_{id(testo_per_pagina)}.pdf")
        doc.save(str(path))
        doc.close()
        return path

    def test_pdf_solo_watermark_rilevato_come_scansione(self, a):
        """PDF con solo il watermark astalegale.net → None (scansione)."""
        wm = "Astalegale.net - E' vietata la stampa e la riproduzione dei documenti"
        path = self._crea_pdf([wm] * 10)
        try:
            testo = a.estrai_testo(path)
            assert testo is None, "Un PDF di soli watermark dev'essere rilevato come scansione"
        finally:
            path.unlink(missing_ok=True)

    def test_pdf_con_testo_reale_estratto(self, a):
        """PDF con testo reale abbondante → estratto normalmente."""
        contenuto = (
            "TRIBUNALE DI VARESE PERIZIA CTU. L'immobile e' un appartamento di 85 mq "
            "sito al terzo piano, libero da persone, in buono stato di manutenzione, "
            "valore di mercato stimato in euro 120000, conforme catastalmente."
        )
        path = self._crea_pdf([contenuto] * 5)
        try:
            testo = a.estrai_testo(path)
            assert testo is not None, "Un PDF con testo reale non deve essere scartato"
            assert "TRIBUNALE" in testo
        finally:
            path.unlink(missing_ok=True)

    def test_watermark_rimosso_dal_testo(self, a):
        """Il watermark non deve inquinare il testo estratto."""
        wm = "Astalegale.net - E' vietata la stampa"
        contenuto = (
            "Perizia immobiliare dettagliata. Appartamento di ampia metratura "
            "situato in zona centrale con tutti i servizi. Stato manutentivo buono. "
            "Superficie commerciale complessiva pari a 95 metri quadri."
        )
        path = self._crea_pdf([f"{contenuto}\n{wm}"] * 5)
        try:
            testo = a.estrai_testo(path)
            assert testo is not None
            assert "Astalegale.net" not in testo, "Il watermark non è stato rimosso"
        finally:
            path.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────
# TEST: pipeline completa su URL reale astalegale.net
# (commentato di default — decommenta per test manuale)
# ─────────────────────────────────────────────────────────────

# Questi test richiedono un URL reale di perizia da astalegale.net.
# Trovane uno dal Google Sheet e incollalo qui.
# class TestPipelineRealePDF:
#     PERIZIA_URL = "https://documents.astalegale.net/..."
#
#     def test_pipeline_completa(self, analyzer):
#         dati = analyzer.analizza_pdf_da_url(self.PERIZIA_URL)
#         assert dati is not None
#         assert "stato_occupazione" in dati
#         print(json.dumps(dati, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-s"])
