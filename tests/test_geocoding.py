"""
Test del geocoding indirizzi. La rete non viene toccata: `_interroga` è
sostituita da uno stub, così si verifica la logica (pulizia indirizzo,
tentativi progressivi, rifiuto dei risultati fuori comune).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import geocoding as g


@pytest.fixture(autouse=True)
def _svuota_cache():
    g._interroga.cache_clear()
    yield
    g._interroga.cache_clear()


class TestPulisciIndirizzo:
    @pytest.mark.parametrize("grezzo,atteso", [
        ("Via Piave, 225, 21040 Cislago VA, Italia", "Via Piave, 225, Cislago VA"),
        ("Via Francesco Baracca - 8 - 21040 - Venegono Superiore - VA",
         "Via Francesco Baracca - 8 - - Venegono Superiore"),
        ("Corso Cavour 400", "Corso Cavour 400"),
        (None, ""),
        ("", ""),
    ])
    def test_pulizia(self, grezzo, atteso):
        assert g.pulisci_indirizzo(grezzo) == atteso

    def test_toglie_il_cap(self):
        assert "21040" not in g.pulisci_indirizzo("Via Roma 1, 21040 Cislago")


class TestGeocodifica:
    def test_usa_il_primo_risultato_coerente(self, monkeypatch):
        monkeypatch.setattr(g, "_interroga",
                            lambda q: (45.65, 8.97, "Via Piave, Cislago, Varese, Italia"))
        assert g.geocodifica("Via Piave 225", "Cislago") == (45.65, 8.97)

    def test_rifiuta_un_risultato_in_un_altro_comune(self, monkeypatch):
        """'Via Roma' esiste ovunque: senza controllo si finisce in un altro paese."""
        monkeypatch.setattr(g, "_interroga",
                            lambda q: (38.11, 13.36, "Via Roma, Palermo, Sicilia, Italia"))
        assert g.geocodifica("Via Roma 1", "Cislago") is None

    def test_ripiega_sul_centro_del_comune(self, monkeypatch):
        chiamate = []

        def finto(query):
            chiamate.append(query)
            if query.startswith("Via Inesistente"):
                return None
            return (45.65, 8.97, "Cislago, Varese, Italia")

        monkeypatch.setattr(g, "_interroga", finto)
        assert g.geocodifica("Via Inesistente 99", "Cislago") == (45.65, 8.97)
        assert len(chiamate) == 2          # prima la via, poi il comune

    def test_none_senza_comune(self, monkeypatch):
        monkeypatch.setattr(g, "_interroga", lambda q: (45.0, 9.0, "ovunque"))
        assert g.geocodifica("Via Piave 225", None) is None

    def test_none_se_nominatim_non_trova(self, monkeypatch):
        monkeypatch.setattr(g, "_interroga", lambda q: None)
        assert g.geocodifica("Via Piave 225", "Cislago") is None

    def test_funziona_anche_senza_indirizzo(self, monkeypatch):
        monkeypatch.setattr(g, "_interroga", lambda q: (45.65, 8.97, "Cislago, Varese, Italia"))
        assert g.geocodifica(None, "Cislago") == (45.65, 8.97)
