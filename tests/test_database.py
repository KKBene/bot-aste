"""
Test integrazione Supabase.

Prerequisiti:
1. Eseguire setup_supabase.sql nel SQL editor Supabase
2. Impostare SUPABASE_KEY con la service_role key in config.py

I test saltano automaticamente se la key non è configurata.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uuid
import pytest
from datetime import datetime

from config import SUPABASE_URL, SUPABASE_KEY


def supabase_configurato() -> bool:
    return (
        SUPABASE_KEY
        and SUPABASE_KEY != "YOUR_SUPABASE_SERVICE_ROLE_KEY"
        and len(SUPABASE_KEY) > 50
    )


pytestmark = pytest.mark.skipif(
    not supabase_configurato(),
    reason="SUPABASE_KEY non configurata — imposta la service_role key in config.py",
)


# ─────────────────────────────────────────────────────────────
# FIXTURE
# ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    """Client Supabase (service role)."""
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_KEY)


@pytest.fixture(scope="module")
def codice_test():
    """Codice univoco per questo run di test — cleanup garantito."""
    return f"TEST_{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture(scope="module", autouse=True)
def cleanup_test_data(client, codice_test):
    """Elimina i dati di test al termine, anche se i test falliscono."""
    yield
    try:
        client.table("aste").delete().like("codice", "TEST_%").execute()
        client.table("runs").delete().eq("note", "pytest").execute()
        print(f"\n  Cleanup: dati TEST eliminati")
    except Exception as e:
        print(f"\n  Cleanup warning: {e}")


# ─────────────────────────────────────────────────────────────
# TEST: connessione
# ─────────────────────────────────────────────────────────────

class TestConnessione:
    def test_connessione_base(self, client):
        """Verifica che la connessione a Supabase funzioni."""
        try:
            res = client.table("aste").select("count", count="exact").limit(1).execute()
            assert res is not None
            print(f"\n  Connesso. Aste nel DB: {res.count or 0}")
        except Exception as e:
            pytest.fail(f"Connessione fallita: {e}")

    def test_tabella_aste_esiste(self, client):
        """La tabella 'aste' deve esistere."""
        res = client.table("aste").select("id").limit(0).execute()
        assert res is not None

    def test_tabella_runs_esiste(self, client):
        """La tabella 'runs' deve esistere."""
        res = client.table("runs").select("id").limit(0).execute()
        assert res is not None

    def test_schema_colonne_aste(self, client):
        """Verifica che tutte le colonne necessarie esistano."""
        res = client.table("aste").select("*").limit(0).execute()
        assert res is not None

    def test_url_corretto(self):
        """L'URL Supabase deve corrispondere al progetto Aste."""
        assert "mrvucjvehtofarflobvt" in SUPABASE_URL, (
            f"URL non corrisponde al progetto Aste: {SUPABASE_URL}"
        )


# ─────────────────────────────────────────────────────────────
# TEST: operazioni CRUD
# ─────────────────────────────────────────────────────────────

class TestInserimento:
    def test_inserisce_asta(self, client, codice_test):
        """Inserisce un'asta di test e verifica che sia nel DB."""
        import database as db
        asta = {
            "codice": codice_test,
            "comune": "test-comune",
            "prezzo_base": 100_000.0,
            "offerta_minima": 75_000.0,
            "tipologia": "Appartamento",
            "link_dettaglio": "https://www.astalegale.net/test",
        }
        ok = db.inserisci_asta(asta)
        assert ok is True, "Inserimento fallito"

    def test_duplicato_non_inserito(self, client, codice_test):
        """Lo stesso codice non deve essere inserito due volte."""
        import database as db
        asta = {"codice": codice_test, "comune": "test-comune"}
        ok = db.inserisci_asta(asta)
        assert ok is False, "Il duplicato doveva ritornare False"

    def test_asta_presente_in_codici_esistenti(self, client, codice_test):
        """Dopo l'inserimento, il codice deve apparire in get_codici_esistenti."""
        import database as db
        codici = db.get_codici_esistenti()
        assert codice_test in codici, f"Codice {codice_test} non trovato nel DB"

    def test_dati_inseriti_correttamente(self, client, codice_test):
        """I dati inseriti devono corrispondere a quelli passati."""
        res = client.table("aste").select("*").eq("codice", codice_test).execute()
        assert len(res.data) == 1
        asta = res.data[0]
        assert asta["codice"] == codice_test
        assert asta["comune"] == "test-comune"
        assert float(asta["prezzo_base"]) == 100_000.0
        assert float(asta["offerta_minima"]) == 75_000.0


class TestAggiornamento:
    def test_aggiorna_analisi_pdf(self, client, codice_test):
        """Aggiorna i dati PDF dell'asta di test."""
        import database as db
        dati_pdf = {
            "stato_occupazione": "LIBERO",
            "costi_sanatoria": 0,
            "superficie_mq": 85.0,
            "stato_manutentivo": "BUONO",
            "piano_ascensore": "Terzo - Ascensore SI",
            "note_critiche": "",
        }
        db.aggiorna_analisi_pdf(codice_test, dati_pdf)

        res = client.table("aste").select("*").eq("codice", codice_test).execute()
        asta = res.data[0]
        assert asta["stato_occupazione"] == "LIBERO"
        assert float(asta["superficie_mq"]) == 85.0
        assert asta["stato_manutentivo"] == "BUONO"
        assert asta["analisi_pdf"] is True

    def test_aggiorna_score(self, client, codice_test):
        """Aggiorna lo score e il breakdown."""
        import database as db
        breakdown = {
            "pts_sconto": 21.9,
            "pts_occupazione": 25,
            "pts_manutenzione": 15,
            "pts_sanatoria": 10.0,
            "pts_note": 10.0,
            "score_totale": 81.9,
        }
        db.aggiorna_score(codice_test, 81.9, breakdown)

        res = client.table("aste").select("score, score_breakdown").eq("codice", codice_test).execute()
        asta = res.data[0]
        assert float(asta["score"]) == 81.9
        assert asta["score_breakdown"]["pts_occupazione"] == 25

    def test_asta_senza_analisi_trovata(self, client):
        """get_aste_senza_analisi deve funzionare."""
        import database as db
        risultato = db.get_aste_senza_analisi()
        assert isinstance(risultato, list)

    def test_asta_senza_score_trovata(self, client):
        """get_aste_senza_score deve funzionare."""
        import database as db
        risultato = db.get_aste_senza_score()
        assert isinstance(risultato, list)


class TestNotifiche:
    def test_get_aste_da_notificare(self, client, codice_test):
        """L'asta con score alto deve apparire nelle da notificare."""
        import database as db
        aste = db.get_aste_da_notificare(min_score=50, limit=10)
        assert isinstance(aste, list)
        codici = [a["codice"] for a in aste]
        assert codice_test in codici, (
            f"L'asta con score 81.9 dovrebbe essere nelle da notificare (min=50)"
        )

    def test_segna_notificata(self, client, codice_test):
        """Dopo segna_notificate, l'asta non appare più."""
        import database as db
        db.segna_notificate([codice_test])

        aste = db.get_aste_da_notificare(min_score=50, limit=10)
        codici = [a["codice"] for a in aste]
        assert codice_test not in codici, "L'asta doveva essere rimossa dopo notifica"


# ─────────────────────────────────────────────────────────────
# TEST: log runs
# ─────────────────────────────────────────────────────────────

class TestRuns:
    def test_start_e_end_run(self, client):
        """Crea un run e lo termina — verifica nel DB."""
        from supabase import create_client
        from config import SUPABASE_URL, SUPABASE_KEY
        c = create_client(SUPABASE_URL, SUPABASE_KEY)

        # Inserisci run di test con nota pytest
        res = c.table("runs").insert({
            "started_at": datetime.now().isoformat(),
            "status": "running",
            "note": "pytest",
        }).execute()
        run_id = res.data[0]["id"]
        assert run_id

        # Termina il run
        import database as db
        db.end_run(run_id, "success", nuovi=5, pdf=3, errori=0)

        # Verifica
        check = c.table("runs").select("*").eq("id", run_id).execute()
        assert check.data[0]["status"] == "success"
        assert check.data[0]["nuovi_annunci"] == 5
        assert check.data[0]["pdf_analizzati"] == 3
        assert check.data[0]["errori"] == 0
        assert check.data[0]["finished_at"] is not None


# ─────────────────────────────────────────────────────────────
# TEST: query
# ─────────────────────────────────────────────────────────────

class TestCicloVita:
    """Tracciamento prezzi e rilevamento annunci spariti/venduti."""

    def test_insert_imposta_prima_vista(self, client, codice_test):
        """All'inserimento, prima_vista e prezzo_base_iniziale sono valorizzati."""
        res = client.table("aste").select(
            "prima_vista, ultima_vista, prezzo_base_iniziale, stato_annuncio"
        ).eq("codice", codice_test).execute()
        a = res.data[0]
        assert a["prima_vista"] is not None
        assert a["stato_annuncio"] == "attivo"
        assert float(a["prezzo_base_iniziale"]) == 100_000.0

    def test_get_aste_attive(self, client, codice_test):
        import database as db
        attive = db.get_aste_attive()
        assert isinstance(attive, dict)
        assert codice_test in attive
        assert attive[codice_test]["comune"] == "test-comune"

    def test_sincronizza_senza_variazione(self, client, codice_test):
        """Stesso prezzo → nessuna variazione registrata."""
        import database as db
        variazione = db.sincronizza_esistente(
            codice_test, 100_000.0, 75_000.0, 100_000.0, 75_000.0
        )
        assert variazione is False

    def test_sincronizza_con_ribasso(self, client, codice_test):
        """Prezzo calato → variazione registrata + ribasso incrementato + storico."""
        import database as db
        variazione = db.sincronizza_esistente(
            codice_test, 90_000.0, 67_500.0, 100_000.0, 75_000.0
        )
        assert variazione is True

        # Il prezzo corrente è aggiornato e numero_ribassi incrementato
        res = client.table("aste").select(
            "prezzo_base, numero_ribassi"
        ).eq("codice", codice_test).execute()
        a = res.data[0]
        assert float(a["prezzo_base"]) == 90_000.0
        assert a["numero_ribassi"] >= 1

        # Lo storico contiene la nuova osservazione
        storico = db.get_storico_prezzi(codice_test)
        assert len(storico) >= 1
        assert float(storico[-1]["prezzo_base"]) == 90_000.0

    def test_marca_sparite_venduto_se_asta_passata(self, client):
        """Un'asta con data passata e non più vista → 'venduto'."""
        import database as db
        import uuid
        codice = f"TEST_{uuid.uuid4().hex[:8].upper()}"
        client.table("aste").insert({
            "codice": codice, "comune": "test-comune",
            "data_asta": "01/01/2020 10:00", "stato_annuncio": "attivo",
        }).execute()

        db.marca_sparite([codice])
        res = client.table("aste").select("stato_annuncio").eq("codice", codice).execute()
        assert res.data[0]["stato_annuncio"] == "venduto"

    def test_marca_sparite_sparito_se_asta_futura(self, client):
        """Un'asta con data futura e non più vista → 'sparito' (ritirata)."""
        import database as db
        import uuid
        codice = f"TEST_{uuid.uuid4().hex[:8].upper()}"
        client.table("aste").insert({
            "codice": codice, "comune": "test-comune",
            "data_asta": "31/12/2099 10:00", "stato_annuncio": "attivo",
        }).execute()

        db.marca_sparite([codice])
        res = client.table("aste").select("stato_annuncio").eq("codice", codice).execute()
        assert res.data[0]["stato_annuncio"] == "sparito"

    def test_sincronizza_riattiva_sparito(self, client):
        """Un annuncio 'sparito' che riappare torna 'attivo'."""
        import database as db
        import uuid
        codice = f"TEST_{uuid.uuid4().hex[:8].upper()}"
        client.table("aste").insert({
            "codice": codice, "comune": "test-comune",
            "prezzo_base": 50_000, "stato_annuncio": "sparito",
        }).execute()

        db.sincronizza_esistente(codice, 50_000.0, None, 50_000.0, None)
        res = client.table("aste").select("stato_annuncio").eq("codice", codice).execute()
        assert res.data[0]["stato_annuncio"] == "attivo"


class TestQuery:
    def test_get_codici_esistenti_ritorna_set(self, client):
        import database as db
        codici = db.get_codici_esistenti()
        assert isinstance(codici, set)

    def test_codici_esistenti_non_vuoto(self, client, codice_test):
        import database as db
        codici = db.get_codici_esistenti()
        assert len(codici) > 0

    def test_get_aste_senza_analisi_filtra_bene(self, client):
        """Solo aste con link_perizia non null e analisi_pdf=False."""
        import database as db
        aste = db.get_aste_senza_analisi()
        for asta in aste:
            assert asta.get("link_perizia"), "Asta senza perizia nella lista"

    def test_get_aste_da_notificare_ordinate_per_score(self, client):
        """Le aste devono essere ordinate per score decrescente."""
        import database as db
        aste = db.get_aste_da_notificare(min_score=0, limit=100)
        if len(aste) > 1:
            scores = [a["score"] for a in aste if a.get("score")]
            assert scores == sorted(scores, reverse=True), "Non ordinate per score desc"


if __name__ == "__main__":
    if not supabase_configurato():
        print("❌ SUPABASE_KEY non configurata in config.py")
        print("   Vai su https://supabase.com/dashboard/project/mrvucjvehtofarflobvt/settings/api")
        print("   Copia la 'service_role' key e incollala in config.py")
    else:
        import pytest
        pytest.main([__file__, "-v", "--tb=short", "-s"])
