"""
Report PDF settimanale — alternativa al digest Telegram testuale (troppo
lungo/confuso da leggere su mobile con 100+ comuni monitorati).

Genera due PDF, ciascuno uno "snapshot" di tutte le aste attive sopra
soglia score, con le novità della settimana (nuovi annunci + ribassi
≥5%) evidenziate in cima a ogni sezione:

  1. "Casa — Lombardia"   → categoria_localita == "citta"
  2. "Vacanza"             → sezioni Montagna + Mare

Note di design:
  - Niente emoji: i font base di reportlab (Helvetica) non hanno i
    glifi e renderizzerebbero come quadratini vuoti. La gerarchia
    visiva usa colore + tipografia invece delle icone del digest
    Telegram.
  - Ogni "card" annuncio è una Table a 2 colonne (barra colorata +
    contenuto) avvolta in KeepTogether per non spezzarsi tra due
    pagine.
"""
from pathlib import Path
from datetime import datetime
from xml.sax.saxutils import escape as _esc

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether,
)

from notifier import giorni_rimanenti

BASE_DIR = Path(__file__).parent

# ─────────────────────────────────────────────────────────────
# STILI
# ─────────────────────────────────────────────────────────────

_STYLES = getSampleStyleSheet()

TITLE = ParagraphStyle("TitoloReport", parent=_STYLES["Title"],
                        fontSize=22, textColor=colors.HexColor("#1a1a2e"))
SUBTITLE = ParagraphStyle("Sottotitolo", parent=_STYLES["Normal"],
                           fontSize=10, textColor=colors.HexColor("#6b7280"),
                           spaceAfter=4)
SEZIONE = ParagraphStyle("Sezione", parent=_STYLES["Heading2"],
                          fontSize=13, textColor=colors.white,
                          leftIndent=6, spaceBefore=0, spaceAfter=0)
NOVITA_LABEL = ParagraphStyle("NovitaLabel", parent=_STYLES["Normal"],
                               fontSize=8, leftIndent=0)
CARD_TITOLO = ParagraphStyle("CardTitolo", parent=_STYLES["Normal"],
                              fontSize=12, leading=15,
                              textColor=colors.HexColor("#1a1a2e"))
CARD_PREZZO = ParagraphStyle("CardPrezzo", parent=_STYLES["Normal"],
                              fontSize=13, leading=16,
                              textColor=colors.HexColor("#1a1a2e"))
CARD_TESTO = ParagraphStyle("CardTesto", parent=_STYLES["Normal"],
                             fontSize=9, leading=13,
                             textColor=colors.HexColor("#374151"))
CARD_AVVISO = ParagraphStyle("CardAvviso", parent=_STYLES["Normal"],
                              fontSize=9, leading=13,
                              textColor=colors.HexColor("#b91c1c"))
CARD_LINK = ParagraphStyle("CardLink", parent=_STYLES["Normal"],
                            fontSize=9, leading=13,
                            textColor=colors.HexColor("#1d4ed8"))

COLORE_SEZIONE = {
    "citta": colors.HexColor("#2563eb"),
    "montagna": colors.HexColor("#7c3aed"),
    "mare": colors.HexColor("#0891b2"),
}


def _score_color(score: float) -> colors.Color:
    if score >= 75:
        return colors.HexColor("#b91c1c")   # rosso — opportunità forte
    if score >= 60:
        return colors.HexColor("#c2820a")   # oro
    if score >= 45:
        return colors.HexColor("#2563eb")   # blu
    return colors.HexColor("#6b7280")       # grigio


def _is_frazionata(quota: str) -> bool:
    import re
    m = re.search(r"(\d+)\s*/\s*(\d+)", quota or "")
    return bool(m) and int(m.group(1)) < int(m.group(2))


def _termine_scaduto(asta: dict) -> bool:
    g = giorni_rimanenti(asta.get("termine_offerte"))
    return g is not None and g < 0


# ─────────────────────────────────────────────────────────────
# CARD SINGOLO ANNUNCIO
# ─────────────────────────────────────────────────────────────

def _card(asta: dict, novita: dict, larghezza: float) -> KeepTogether:
    codice = asta.get("codice")
    info_novita = novita.get(codice)
    score = asta.get("score") or 0
    accent = _score_color(score)

    righe = []

    # Etichetta novità (nuovo / ribassato) sopra al titolo — testo colorato
    # (non bianco: il badge non ha uno sfondo proprio, siede sulla card chiara).
    if info_novita:
        if info_novita["tipo"] == "nuovo":
            testo, badge_hex = "● NUOVO ANNUNCIO", "#059669"
        else:
            testo = (f"● RIBASSATO {info_novita['pct']:+.0f}% "
                     f"(prima €{info_novita['da']:,.0f})")
            badge_hex = "#dc2626"
        righe.append(Paragraph(
            f'<font color="{badge_hex}"><b>{testo}</b></font>', NOVITA_LABEL))
        badge_bg = colors.HexColor(badge_hex)
    else:
        badge_bg = None

    comune = (asta.get("comune") or "N/D").title()
    indirizzo = _esc(asta.get("indirizzo_immobile") or "N/D")
    righe.append(Paragraph(
        f'<b>Score {score:.0f}/100</b> &nbsp;·&nbsp; {comune} — {indirizzo}',
        CARD_TITOLO))

    # Prezzo + sconto + mq
    prezzo = asta.get("offerta_minima") or asta.get("prezzo_base") or 0
    valore_mercato = asta.get("valore_mercato")
    sconto_str = ""
    if valore_mercato and valore_mercato > 0 and prezzo > 0 and valore_mercato >= prezzo:
        pct = (valore_mercato - prezzo) / valore_mercato * 100
        sconto_str = f' <font color="#059669"><b>(-{pct:.0f}% vs mercato)</b></font>'
    elif asta.get("prezzo_base") and asta.get("offerta_minima") and asta["prezzo_base"] > 0:
        pct = (asta["prezzo_base"] - asta["offerta_minima"]) / asta["prezzo_base"] * 100
        if pct > 0:
            sconto_str = f' <font color="#059669"><b>(-{pct:.0f}%)</b></font>'
    sup = asta.get("superficie_mq")
    mq_str = f" · €{prezzo / sup:,.0f}/mq" if sup and sup > 0 and prezzo > 0 else ""
    righe.append(Paragraph(f'€{prezzo:,.0f}{sconto_str}{mq_str}', CARD_PREZZO))

    if valore_mercato and valore_mercato > 0:
        righe.append(Paragraph(f'Stima perito: <b>€{valore_mercato:,.0f}</b>', CARD_TESTO))

    bd = asta.get("score_breakdown") or {}
    margine_eur, margine_pct = bd.get("margine_eur"), bd.get("margine_pct")
    if margine_eur is not None and margine_pct is not None:
        colore = "#059669" if margine_pct >= 15 else ("#c2820a" if margine_pct >= 0 else "#b91c1c")
        roi = bd.get("roi_pct")
        roi_str = f" · ROI {roi:+.0f}%" if roi is not None else ""
        righe.append(Paragraph(
            f'<font color="{colore}"><b>Margine: €{margine_eur:,.0f} '
            f'({margine_pct:+.0f}%)</b></font>{roi_str}', CARD_TESTO))

    occ_map = {
        "LIBERO": "Libero", "OCCUPATO_DEBITORE": "Occupato dal debitore",
        "OCCUPATO_SENZA_TITOLO": "Occupato senza titolo",
        "OCCUPATO_CON_TITOLO": "Occupato con titolo",
    }
    occ = occ_map.get(asta.get("stato_occupazione") or "", "N/D")
    if asta.get("stato_occupazione") == "OCCUPATO_CON_TITOLO" and asta.get("occupazione_opponibile") is False:
        occ += " (non opponibile)"
    mnut_map = {"OTTIMO": "Ottimo", "BUONO": "Buono", "MEDIOCRE": "Mediocre",
                "PESSIMO": "Pessimo", "RUDERE": "Rudere"}
    mnut = mnut_map.get(asta.get("stato_manutentivo") or "", "N/D")
    righe.append(Paragraph(f'Occupazione: {occ} &nbsp;·&nbsp; Manutenzione: {mnut}', CARD_TESTO))

    dettagli = []
    if asta.get("tipologia_immobile"):
        dettagli.append(str(asta["tipologia_immobile"]).capitalize())
    if asta.get("categoria_catastale"):
        dettagli.append(f"cat. {asta['categoria_catastale']}")
    if asta.get("anno_costruzione"):
        dettagli.append(f"anno {asta['anno_costruzione']}")
    if asta.get("classe_energetica"):
        dettagli.append(f"APE {asta['classe_energetica']}")
    if dettagli:
        righe.append(Paragraph(" · ".join(dettagli), CARD_TESTO))

    debiti = asta.get("spese_condominiali_arretrate")
    if debiti and debiti > 0:
        righe.append(Paragraph(f'Debiti condominiali: <b>€{debiti:,.0f}</b>', CARD_AVVISO))

    quota = asta.get("quota_proprieta") or ""
    if quota and ("nuda" in quota.lower() or "usufrutto" in quota.lower() or _is_frazionata(quota)):
        righe.append(Paragraph(f'Quota: <b>{_esc(quota)}</b>', CARD_AVVISO))

    note = asta.get("note_critiche") or ""
    if note:
        righe.append(Paragraph(f'<i>{_esc(note[:180])}</i>', CARD_AVVISO))

    termine = asta.get("termine_offerte")
    if termine:
        g = giorni_rimanenti(termine)
        urg = ""
        if g is not None:
            if g <= 7:
                urg = f' <font color="#b91c1c"><b>(tra {g}gg)</b></font>'
            elif g <= 21:
                urg = f' <font color="#c2820a">(tra {g}gg)</font>'
            else:
                urg = f' <font color="#059669">(tra {g}gg)</font>'
        righe.append(Paragraph(f'Offerte entro: <b>{_esc(str(termine))}</b>{urg}', CARD_TESTO))

    link_parts = []
    if asta.get("link_dettaglio"):
        link_parts.append(f'<a href="{_esc(asta["link_dettaglio"])}" color="#1d4ed8"><u>Vedi annuncio</u></a>')
    lat, lng = asta.get("posizione_lat"), asta.get("posizione_lng")
    if lat and lng:
        link_parts.append(f'<a href="https://www.google.com/maps?q={lat},{lng}" color="#1d4ed8"><u>Mappa</u></a>')
    if link_parts:
        righe.append(Paragraph(" &nbsp;·&nbsp; ".join(link_parts), CARD_LINK))

    contenuto_tab = Table([[r] for r in righe], colWidths=[larghezza - 10 * mm])
    contenuto_tab.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))

    barra_bg = badge_bg or accent
    card = Table([[Spacer(1, 1), contenuto_tab]],
                 colWidths=[4 * mm, larghezza - 4 * mm])
    card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), barra_bg),
        ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#f7f7f9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (1, 0), (1, 0), 8),
        ("RIGHTPADDING", (1, 0), (1, 0), 8),
        ("TOPPADDING", (1, 0), (1, 0), 6),
        ("BOTTOMPADDING", (1, 0), (1, 0), 6),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 0),
    ]))
    return KeepTogether([card, Spacer(1, 4 * mm)])


def _sezione_header(nome: str, n: int, larghezza: float) -> Table:
    colore = COLORE_SEZIONE.get(nome.lower(), colors.HexColor("#1a1a2e"))
    tab = Table([[Paragraph(f'{nome.upper()} — {n} opportunità', SEZIONE)]],
                colWidths=[larghezza])
    tab.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colore),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return tab


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.HexColor("#9ca3af"))
    canvas.drawString(15 * mm, 10 * mm,
                       f"Bot Aste — generato {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Pagina {doc.page}")
    canvas.restoreState()


# ─────────────────────────────────────────────────────────────
# COSTRUZIONE DOCUMENTO
# ─────────────────────────────────────────────────────────────

def _costruisci(output_path: Path, titolo: str, sezioni: list, novita: dict,
                 nuovi_totali: int, ribassi_totali: int) -> Path:
    """sezioni: list di (nome, lista_aste) già filtrate/ordinate per score."""
    doc = SimpleDocTemplate(str(output_path), pagesize=A4,
                             leftMargin=15 * mm, rightMargin=15 * mm,
                             topMargin=15 * mm, bottomMargin=18 * mm)
    larghezza = A4[0] - 30 * mm
    story = [
        Paragraph(titolo, TITLE),
        Paragraph(
            f"Report del {datetime.now().strftime('%d/%m/%Y')} — "
            f"{nuovi_totali} nuovi annunci · {ribassi_totali} ribassati questa settimana",
            SUBTITLE),
        Spacer(1, 4 * mm),
    ]

    totale = sum(len(a) for _, a in sezioni)
    if totale == 0:
        story.append(Paragraph(
            "Nessuna opportunità sopra soglia score al momento.", CARD_TESTO))

    for i, (nome, aste) in enumerate(sezioni):
        if not aste:
            continue
        if i > 0:
            story.append(Spacer(1, 3 * mm))
        story.append(_sezione_header(nome, len(aste), larghezza))
        story.append(Spacer(1, 3 * mm))
        for asta in aste:
            story.append(_card(asta, novita, larghezza))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return output_path


def prepara_novita(nuovi_codici: set, ribassi: list) -> dict:
    """Mappa codice -> {tipo, pct, da} per evidenziare le novità della settimana."""
    novita = {c: {"tipo": "nuovo"} for c in nuovi_codici}
    for r in ribassi:
        novita[r["codice"]] = {
            "tipo": "ribasso",
            "pct": r.get("_ribasso_pct", 0),
            "da": r.get("_ribasso_da", 0),
        }
    return novita


def genera_report_lombardia(aste_citta: list, novita: dict, out_dir: Path,
                             nuovi_totali: int, ribassi_totali: int) -> Path:
    aste_citta = sorted(aste_citta, key=lambda a: a.get("score") or 0, reverse=True)
    path = out_dir / "report_lombardia.pdf"
    return _costruisci(path, "Casa — Lombardia", [("Lombardia", aste_citta)],
                        novita, nuovi_totali, ribassi_totali)


def genera_report_vacanza(aste_montagna: list, aste_mare: list, novita: dict,
                           out_dir: Path, nuovi_totali: int, ribassi_totali: int) -> Path:
    aste_montagna = sorted(aste_montagna, key=lambda a: a.get("score") or 0, reverse=True)
    aste_mare = sorted(aste_mare, key=lambda a: a.get("score") or 0, reverse=True)
    path = out_dir / "report_vacanza.pdf"
    return _costruisci(path, "Vacanza — Montagna & Mare",
                        [("Montagna", aste_montagna), ("Mare", aste_mare)],
                        novita, nuovi_totali, ribassi_totali)
