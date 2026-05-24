"""
Scoring delle aste immobiliari — modello economico risk-adjusted (v3).

Filosofia: invece di sommare punti arbitrari, si stima il MARGINE NETTO reale
dell'operazione (quanto resta rispetto al valore di mercato dopo TUTTI i costi),
poi lo si corregge per posizione, rischio occupazione e affidabilità del dato.

    costo_totale = prezzo_acquisto
                 + costi_sanatoria
                 + debiti_condominiali
                 + ristrutturazione (€/mq per stato)
                 + imposte di registro
                 + oneri accessori (notaio/voltura)
                 + costo/rischio di liberazione (occupazione)

    margine_% = (valore_mercato - costo_totale) / valore_mercato

Score 0-100 (interpretabile e stabile nel tempo):
    55  Margine economico   (mappa il margine_% atteso)
    20  Posizione           (qualità zona + distanza stazione)
    15  Liberabilità        (rischio/tempo residuo occupazione)
    10  Affidabilità        (note critiche / conformità)
    × moltiplicatore quota  (proprietà parziale/nuda → forte taglio)
    × fattore confidenza    (dati PDF mancanti → score smorzato verso il neutro)

Quando manca il valore di mercato (perizia non ancora analizzata) il margine si
stima dallo sconto offerta/base con un tetto più basso (minore confidenza).
"""
import json
from typing import Optional

from config import (
    COSTO_RISTRUTTURAZIONE_MQ, COSTO_RISTRUTTURAZIONE_DEFAULT_MQ,
    IMPOSTE_ACQUISTO_PCT, ONERI_ACCESSORI_EUR,
    COSTO_LIBERAZIONE, COSTO_LIBERAZIONE_DEFAULT, MARGINE_TARGET_PCT,
    MESI_POSSESSO_STIMA, IMU_MOLTIPLICATORE, IMU_ALIQUOTA,
)

# Punti massimi per componente (somma = 100)
PT_MARGINE = 55.0
PT_POSIZIONE = 20.0
PT_LIBERABILITA = 15.0
PT_AFFIDABILITA = 10.0

_NOTE_CRITICHE_KEYWORDS = [
    "inagibile", "amianto", "non sanabile", "non conforme", "rudere",
    "crollo", "pericolo", "sequestro", "abuso totale", "da demolire", "demolito",
]

_POSIZIONE_PTS = {"OTTIMA": 12.0, "BUONA": 9.0, "MEDIA": 5.0, "SCARSA": 1.0}
_POSIZIONE_DEFAULT = 5.0

# Liberabilità (rischio/tempo residuo, oltre al costo già conteggiato)
_LIBERABILITA_PTS = {
    "LIBERO": 15.0,
    "OCCUPATO_DEBITORE": 9.0,
    "OCCUPATO_SENZA_TITOLO": 4.0,
    "OCCUPATO_CON_TITOLO": 0.0,
}
_LIBERABILITA_NON_OPP = 8.0   # con titolo ma non opponibile → di fatto si libera
_LIBERABILITA_DEFAULT = 7.0


# ─────────────────────────────────────────────────────────────
# HELPER
# ─────────────────────────────────────────────────────────────

def _num(val) -> Optional[float]:
    try:
        return float(val) if val not in (None, "", "null") else None
    except (ValueError, TypeError):
        return None


def _quota_intera(quota: Optional[str]) -> bool:
    """True se piena proprietà su quota intera; False se frazionata/nuda/usufrutto."""
    if not quota:
        return True
    q = quota.lower()
    if "nuda" in q or "usufrutto" in q or "abitazione" in q:
        return False
    import re
    m = re.search(r"(\d+)\s*/\s*(\d+)", q)
    if m:
        return int(m.group(1)) >= int(m.group(2))
    return True


def _chiave_liberazione(occ: str, opponibile) -> str:
    if occ == "OCCUPATO_CON_TITOLO" and opponibile is False:
        return "OCCUPATO_CON_TITOLO_NON_OPP"
    return occ


# ─────────────────────────────────────────────────────────────
# STIMA DEI COSTI
# ─────────────────────────────────────────────────────────────

def stima_costi(asta: dict) -> dict:
    """
    Stima il costo totale d'acquisizione (€) e le sue componenti.
    Ritorna un dict con il dettaglio (utile per il breakdown e il display).
    """
    prezzo = _num(asta.get("offerta_minima")) or _num(asta.get("prezzo_base")) or 0.0
    sanatoria = _num(asta.get("costi_sanatoria")) or 0.0
    debiti = _num(asta.get("spese_condominiali_arretrate")) or 0.0
    straordinarie = _num(asta.get("spese_straordinarie_deliberate")) or 0.0
    condo_annue = _num(asta.get("spese_condominiali_annue")) or 0.0
    rendita = _num(asta.get("rendita_catastale")) or 0.0
    sup = _num(asta.get("superficie_mq")) or 0.0

    manut = (asta.get("stato_manutentivo") or "").upper().strip()
    costo_mq = COSTO_RISTRUTTURAZIONE_MQ.get(manut, COSTO_RISTRUTTURAZIONE_DEFAULT_MQ)
    ristrutturazione = sup * costo_mq

    imposte = prezzo * IMPOSTE_ACQUISTO_PCT
    oneri = ONERI_ACCESSORI_EUR if prezzo > 0 else 0.0

    occ = (asta.get("stato_occupazione") or "").upper().strip()
    chiave = _chiave_liberazione(occ, asta.get("occupazione_opponibile"))
    liberazione = COSTO_LIBERAZIONE.get(chiave, COSTO_LIBERAZIONE_DEFAULT) if occ else COSTO_LIBERAZIONE_DEFAULT

    # Costi di possesso durante il flip: spese condominiali ordinarie + IMU,
    # pro-quota sui mesi medi di detenzione.
    imu_annua = rendita * IMU_MOLTIPLICATORE * IMU_ALIQUOTA
    frazione_anno = MESI_POSSESSO_STIMA / 12.0
    possesso = (condo_annue + imu_annua) * frazione_anno

    totale = (prezzo + sanatoria + debiti + straordinarie + ristrutturazione
              + imposte + oneri + liberazione + possesso)
    return {
        "prezzo_acquisto": round(prezzo, 0),
        "sanatoria": round(sanatoria, 0),
        "debiti_condominiali": round(debiti, 0),
        "spese_straordinarie": round(straordinarie, 0),
        "ristrutturazione": round(ristrutturazione, 0),
        "imposte": round(imposte, 0),
        "oneri_accessori": round(oneri, 0),
        "liberazione": round(liberazione, 0),
        "costo_possesso": round(possesso, 0),
        "imu_annua": round(imu_annua, 0),
        "costo_totale": round(totale, 0),
    }


# ─────────────────────────────────────────────────────────────
# CALCOLO SCORE
# ─────────────────────────────────────────────────────────────

def calcola_score(asta: dict) -> tuple[float, dict]:
    """Score 0-100 risk-adjusted + breakdown economico dettagliato."""
    bd: dict = {}
    costi = stima_costi(asta)
    bd.update(costi)

    prezzo = costi["prezzo_acquisto"]
    valore_mercato = _num(asta.get("valore_mercato"))
    costo_totale = costi["costo_totale"]

    # ── 1. MARGINE ECONOMICO (0-55) ──────────────────────────
    if valore_mercato and valore_mercato > 0:
        margine_eur = valore_mercato - costo_totale
        margine_pct = margine_eur / valore_mercato
        riferimento = "valore_mercato"
        confidenza_margine = 1.0
    elif prezzo > 0:
        # Nessuna stima perito: usa lo sconto offerta/base come proxy, con
        # confidenza ridotta (il margine reale non è verificabile).
        base = _num(asta.get("prezzo_base")) or prezzo
        margine_eur = (base - costo_totale)
        margine_pct = (base - costo_totale) / base if base > 0 else 0.0
        riferimento = "prezzo_base"
        confidenza_margine = 0.6
    else:
        margine_eur, margine_pct, riferimento, confidenza_margine = 0.0, 0.0, "n/d", 0.5

    # Mappa il margine % sui punti: 0% → 0, MARGINE_TARGET → pieno (clamp 0..1)
    quota_margine = max(0.0, min(1.0, margine_pct / MARGINE_TARGET_PCT)) if MARGINE_TARGET_PCT else 0.0
    pts_margine = PT_MARGINE * quota_margine * confidenza_margine

    bd["margine_eur"] = round(margine_eur, 0)
    bd["margine_pct"] = round(margine_pct * 100, 1)
    bd["margine_riferimento"] = riferimento
    bd["roi_pct"] = round((margine_eur / costo_totale * 100), 1) if costo_totale > 0 else None
    bd["pts_margine"] = round(pts_margine, 1)

    # ── 2. POSIZIONE (0-20) ──────────────────────────────────
    qpos = (asta.get("qualita_posizione") or "").upper().strip()
    pts_zona = _POSIZIONE_PTS.get(qpos, _POSIZIONE_DEFAULT)   # 0-12
    dist = _num(asta.get("distanza_stazione_km"))
    if dist is None:
        pts_staz = 4.0   # ignota → neutro
    elif dist <= 0.5:
        pts_staz = 8.0
    elif dist <= 1.0:
        pts_staz = 6.0
    elif dist <= 2.0:
        pts_staz = 4.0
    elif dist <= 5.0:
        pts_staz = 2.0
    else:
        pts_staz = 0.0
    pts_posizione = min(PT_POSIZIONE, pts_zona + pts_staz)
    bd["qualita_posizione"] = qpos or "N/D"
    bd["distanza_stazione_km"] = dist
    bd["pts_posizione"] = round(pts_posizione, 1)

    # ── 3. LIBERABILITÀ (0-15) ───────────────────────────────
    occ = (asta.get("stato_occupazione") or "").upper().strip()
    if occ == "OCCUPATO_CON_TITOLO" and asta.get("occupazione_opponibile") is False:
        pts_liber = _LIBERABILITA_NON_OPP
    else:
        pts_liber = _LIBERABILITA_PTS.get(occ, _LIBERABILITA_DEFAULT)
    bd["stato_occupazione"] = occ or "N/D"
    bd["pts_liberabilita"] = round(pts_liber, 1)

    # ── 4. AFFIDABILITÀ / NOTE (0-10) ────────────────────────
    note = (asta.get("note_critiche") or "").lower()
    if not note:
        pts_affid = 10.0
    elif any(kw in note for kw in _NOTE_CRITICHE_KEYWORDS):
        pts_affid = 0.0
    else:
        pts_affid = 5.0
    bd["note_critiche"] = asta.get("note_critiche") or ""
    bd["pts_affidabilita"] = round(pts_affid, 1)

    # ── SOMMA + MOLTIPLICATORI ───────────────────────────────
    grezzo = pts_margine + pts_posizione + pts_liber + pts_affid

    # Quota frazionata/nuda proprietà: quasi invendibile → forte taglio
    quota = asta.get("quota_proprieta")
    molt_quota = 1.0 if (quota is None or _quota_intera(quota)) else 0.45
    bd["quota_proprieta"] = quota or "N/D"
    bd["molt_quota"] = molt_quota

    # Confidenza globale: se la perizia non è stata analizzata, smorza verso un
    # neutro prudente (evita falsi "ottimi" su dati incompleti)
    analizzata = bool(asta.get("analisi_pdf")) or valore_mercato is not None
    molt_confidenza = 1.0 if analizzata else 0.85
    bd["molt_confidenza"] = molt_confidenza

    totale = grezzo * molt_quota * molt_confidenza
    totale = round(min(100.0, max(0.0, totale)), 1)
    bd["score_totale"] = totale

    # Info display
    sup = _num(asta.get("superficie_mq"))
    if sup and sup > 0 and prezzo > 0:
        bd["prezzo_mq"] = round(prezzo / sup, 0)
    if valore_mercato and sup and sup > 0:
        bd["valore_mercato_mq"] = round(valore_mercato / sup, 0)

    # Rendimento lordo da locazione (se affittato): canone annuo / prezzo d'acquisto
    canone = _num(asta.get("canone_locazione_annuo"))
    if canone and prezzo > 0:
        bd["canone_annuo"] = round(canone, 0)
        bd["rendita_lorda_pct"] = round(canone / prezzo * 100, 1)

    return totale, bd


def score_label(score: float) -> str:
    if score >= 75:
        return "🔥 Eccellente"
    if score >= 60:
        return "⭐ Ottima"
    if score >= 45:
        return "👍 Buona"
    if score >= 30:
        return "📌 Discreta"
    return "⚠️ Bassa"


def score_emoji(score: float) -> str:
    if score >= 75:
        return "🔥"
    if score >= 60:
        return "⭐"
    if score >= 45:
        return "👍"
    return "📌"


# ─────────────────────────────────────────────────────────────
# Test standalone
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    test = {
        "prezzo_base": 100_000, "offerta_minima": 75_000, "valore_mercato": 130_000,
        "stato_occupazione": "LIBERO", "stato_manutentivo": "BUONO",
        "costi_sanatoria": 2_000, "spese_condominiali_arretrate": 0,
        "quota_proprieta": "1/1 piena proprietà", "superficie_mq": 90,
        "qualita_posizione": "BUONA", "distanza_stazione_km": 0.8,
        "note_critiche": "", "analisi_pdf": True,
    }
    s, bd = calcola_score(test)
    print(f"Score: {s}/100 — {score_label(s)}")
    print(json.dumps(bd, indent=2, ensure_ascii=False))
