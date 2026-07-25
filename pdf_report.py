"""
Report PDF settimanale — alternativa al digest Telegram testuale (troppo
lungo/confuso da leggere su mobile con 100+ comuni monitorati).

Genera due PDF, ciascuno uno "snapshot" di tutte le aste attive sopra
soglia score, con le novità della settimana (nuovi annunci + ribassi
≥5%) evidenziate da un badge in cima alla card:

  1. "Casa — Lombardia"   → categoria_localita == "citta"
  2. "Vacanza"             → sezioni Montagna + Mare

Rendering: HTML/CSS renderizzato con Chromium via Playwright (già una
dipendenza del progetto per lo scraper legacy) invece di un builder
PDF nativo — permette foto immobile, badge a pillola, angoli
arrotondati, ombre, font custom: cose che un builder tipo reportlab
rende male o per niente. Font Inter caricato da Google Fonts al
momento del render (richiede rete, disponibile sia in locale che nel
job cloud).
"""
import re
import urllib.parse
from pathlib import Path
from datetime import datetime
from html import escape as _esc

from playwright.sync_api import sync_playwright

from notifier import giorni_rimanenti

BASE_DIR = Path(__file__).parent

BRAND_GRADIENT = "linear-gradient(135deg, #4338ca 0%, #7c3aed 100%)"

COLORE_SEZIONE = {
    "citta": "#60a5fa",
    "montagna": "#a78bfa",
    "mare": "#22d3ee",
}
NOME_SEZIONE_LABEL = {"citta": "Lombardia", "montagna": "Montagna", "mare": "Mare"}


def _score_hex(score: float) -> str:
    if score >= 75:
        return "#f43f5e"   # rose — opportunità forte
    if score >= 60:
        return "#f59e0b"   # ambra
    if score >= 45:
        return "#3b82f6"   # blu
    return "#64748b"       # slate


def _is_frazionata(quota: str) -> bool:
    m = re.search(r"(\d+)\s*/\s*(\d+)", quota or "")
    return bool(m) and int(m.group(1)) < int(m.group(2))


def _termine_scaduto(asta: dict) -> bool:
    g = giorni_rimanenti(asta.get("termine_offerte"))
    return g is not None and g < 0


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


# ─────────────────────────────────────────────────────────────
# HTML — CARD SINGOLO ANNUNCIO
# ─────────────────────────────────────────────────────────────

def _badge_novita_html(info: dict) -> str:
    if not info:
        return ""
    if info["tipo"] == "nuovo":
        return '<span class="badge badge-nuovo">Nuovo annuncio</span>'
    return (f'<span class="badge badge-ribasso">Ribassato {info["pct"]:+.0f}% '
            f'(prima €{info["da"]:,.0f})</span>')


def _card_html(asta: dict, novita: dict) -> str:
    codice = asta.get("codice")
    info_novita = novita.get(codice)
    score = asta.get("score") or 0
    score_hex = _score_hex(score)

    comune = _esc((asta.get("comune") or "N/D").title())
    indirizzo = _esc(asta.get("indirizzo_immobile") or "N/D")

    prezzo = asta.get("offerta_minima") or asta.get("prezzo_base") or 0
    valore_mercato = asta.get("valore_mercato")
    sconto_html = ""
    if valore_mercato and valore_mercato > 0 and prezzo > 0 and valore_mercato >= prezzo:
        pct = (valore_mercato - prezzo) / valore_mercato * 100
        sconto_html = f'<span class="sconto">-{pct:.0f}% vs mercato</span>'
    elif asta.get("prezzo_base") and asta.get("offerta_minima") and asta["prezzo_base"] > 0:
        pct = (asta["prezzo_base"] - asta["offerta_minima"]) / asta["prezzo_base"] * 100
        if pct > 0:
            # senza stima del perito il ribasso è solo offerta-minima vs base:
            # etichettarlo, altrimenti si confonde con lo sconto sul mercato
            sconto_html = f'<span class="sconto">-{pct:.0f}% sulla base d\'asta</span>'
    sup = asta.get("superficie_mq")
    mq_html = f'<span class="mq">€{prezzo / sup:,.0f}/mq</span>' if sup and sup > 0 and prezzo > 0 else ""

    righe_extra = []

    if valore_mercato and valore_mercato > 0:
        righe_extra.append(f'<div class="riga">Stima perito: <b>€{valore_mercato:,.0f}</b></div>')

    bd = asta.get("score_breakdown") or {}
    margine_eur, margine_pct = bd.get("margine_eur"), bd.get("margine_pct")
    if margine_eur is not None and margine_pct is not None:
        colore = "#34d399" if margine_pct >= 15 else ("#fbbf24" if margine_pct >= 0 else "#f87171")
        roi = bd.get("roi_pct")
        roi_str = f" · ROI {roi:+.0f}%" if roi is not None else ""
        righe_extra.append(
            f'<div class="riga"><b style="color:{colore}">Margine €{margine_eur:,.0f} '
            f'({margine_pct:+.0f}%)</b>{roi_str}</div>')

    occ_map = {"LIBERO": "Libero", "OCCUPATO_DEBITORE": "Occupato dal debitore",
               "OCCUPATO_SENZA_TITOLO": "Occupato senza titolo",
               "OCCUPATO_CON_TITOLO": "Occupato con titolo"}
    occ = occ_map.get(asta.get("stato_occupazione") or "", "N/D")
    if asta.get("stato_occupazione") == "OCCUPATO_CON_TITOLO" and asta.get("occupazione_opponibile") is False:
        occ += " (non opponibile)"
    mnut_map = {"OTTIMO": "Ottimo", "BUONO": "Buono", "MEDIOCRE": "Mediocre",
                "PESSIMO": "Pessimo", "RUDERE": "Rudere"}
    mnut = mnut_map.get(asta.get("stato_manutentivo") or "", "N/D")
    righe_extra.append(f'<div class="riga muted">{_esc(occ)} · Manutenzione {_esc(mnut)}</div>')

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
        righe_extra.append(f'<div class="riga muted">{_esc(" · ".join(dettagli))}</div>')

    debiti = asta.get("spese_condominiali_arretrate")
    if debiti and debiti > 0:
        righe_extra.append(f'<div class="riga warn">Debiti condominiali: <b>€{debiti:,.0f}</b></div>')

    quota = asta.get("quota_proprieta") or ""
    if quota and ("nuda" in quota.lower() or "usufrutto" in quota.lower() or _is_frazionata(quota)):
        righe_extra.append(f'<div class="riga warn">Quota: <b>{_esc(quota)}</b></div>')

    note = asta.get("note_critiche") or ""
    if note:
        righe_extra.append(f'<div class="riga note">{_esc(note[:180])}</div>')

    termine = asta.get("termine_offerte")
    if termine:
        g = giorni_rimanenti(termine)
        urg_hex = "#64748b"
        if g is not None:
            urg_hex = "#f87171" if g <= 7 else ("#fbbf24" if g <= 21 else "#34d399")
        righe_extra.append(
            f'<div class="riga">Offerte entro <b>{_esc(str(termine))}</b> '
            f'<span style="color:{urg_hex};font-weight:700">'
            f'{f"(tra {g}gg)" if g is not None else ""}</span></div>')

    link_parts = []
    if asta.get("link_dettaglio"):
        link_parts.append(f'<a href="{_esc(asta["link_dettaglio"])}">Vedi annuncio →</a>')
    # Mappa: coordinate se ci sono, altrimenti ricerca per indirizzo+comune —
    # PVP fornisce le coordinate solo per una parte dei lotti e senza fallback
    # il link sparirebbe dalla maggior parte delle schede.
    lat, lng = asta.get("posizione_lat"), asta.get("posizione_lng")
    if lat and lng:
        link_parts.append(f'<a href="https://www.google.com/maps?q={lat},{lng}">Mappa →</a>')
    else:
        indirizzo = " ".join(p for p in (asta.get("indirizzo_immobile"),
                                         asta.get("comune")) if p)
        if indirizzo.strip():
            q = urllib.parse.quote_plus(indirizzo)
            link_parts.append(f'<a href="https://www.google.com/maps/search/?api=1&amp;query={q}">Mappa →</a>')

    foto_url = asta.get("immagine_url")
    foto_html = (
        f'<img class="foto" src="{_esc(foto_url)}" '
        f'onerror="this.remove(); this.parentElement.classList.add(\'no-foto\')">'
        if foto_url else ""
    )

    return f'''
    <div class="card{' no-foto' if not foto_url else ''}">
      {foto_html}
      <div class="card-body">
        <div class="card-top">
          <div class="score-ring" style="background:{score_hex}">{score:.0f}</div>
          <div class="titolo">
            {_badge_novita_html(info_novita)}
            <div class="indirizzo"><b>{comune}</b> — {indirizzo}</div>
          </div>
        </div>
        <div class="prezzo">€{prezzo:,.0f} {sconto_html} {mq_html}</div>
        {''.join(righe_extra)}
        <div class="links">{' &nbsp;·&nbsp; '.join(link_parts)}</div>
      </div>
    </div>'''


def _sezione_html(chiave: str, aste: list, novita: dict) -> str:
    colore = COLORE_SEZIONE.get(chiave, "#1e293b")
    nome = NOME_SEZIONE_LABEL.get(chiave, chiave.upper())
    cards = "\n".join(_card_html(a, novita) for a in aste)
    return f'''
    <div class="section-header" style="color:{colore}">
      <span class="dot" style="background:{colore}"></span>{nome.upper()} — {len(aste)} opportunità
    </div>
    {cards}'''


# ─────────────────────────────────────────────────────────────
# DOCUMENTO
# ─────────────────────────────────────────────────────────────

_CSS = '''
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
* { box-sizing: border-box; }
html, body {
  background: #0b1120;
}
body {
  font-family: 'Inter', -apple-system, sans-serif;
  color: #e2e8f0;
  margin: 0;
  padding: 10mm 12mm 10mm 12mm;
  font-size: 12.5px;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
.banner {
  background: __GRADIENT__;
  color: white;
  border-radius: 18px;
  padding: 20px 24px;
  margin-bottom: 20px;
}
.banner h1 { margin: 0 0 4px; font-size: 25px; font-weight: 800; letter-spacing: -0.01em; }
.banner .sub { font-size: 12px; opacity: .92; margin-bottom: 12px; }
.chips { display: flex; gap: 8px; flex-wrap: wrap; }
.chip {
  background: rgba(255,255,255,.16); border: 1px solid rgba(255,255,255,.3);
  border-radius: 999px; padding: 4px 12px; font-size: 11px; font-weight: 600;
}
.section-header {
  display: flex; align-items: center; gap: 8px;
  font-weight: 800; font-size: 13px; letter-spacing: .04em; text-transform: uppercase;
  margin: 22px 0 10px;
}
.section-header .dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.card {
  display: flex; gap: 14px;
  background: #161f32; border: 1px solid #26314a; border-radius: 16px;
  padding: 12px; margin-bottom: 10px;
  box-shadow: 0 2px 6px rgba(0,0,0,.35);
  break-inside: avoid; page-break-inside: avoid;
}
.card.no-foto { padding: 14px 16px; }
.foto {
  width: 116px; height: 116px; border-radius: 12px; object-fit: cover;
  flex-shrink: 0; background: #1f2a42; border: 1px solid #2d3958;
}
.card-body { flex: 1; min-width: 0; }
.card-top { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 4px; }
.score-ring {
  width: 38px; height: 38px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  color: white; font-weight: 800; font-size: 13.5px;
}
.titolo { flex: 1; min-width: 0; }
.indirizzo { font-size: 13px; line-height: 1.35; margin-top: 2px; color: #f1f5f9; }
.badge {
  display: inline-block; padding: 2px 9px; border-radius: 999px;
  font-size: 9px; font-weight: 800; letter-spacing: .03em; text-transform: uppercase;
  color: white; margin-bottom: 3px;
}
.badge-nuovo { background: #059669; }
.badge-ribasso { background: #dc2626; }
.prezzo { font-size: 18px; font-weight: 800; margin: 6px 0 4px; color: #f8fafc; }
.prezzo .sconto { color: #34d399; font-size: 12px; font-weight: 700; margin-left: 6px; }
.prezzo .mq { color: #7c8aa5; font-size: 11.5px; font-weight: 500; margin-left: 6px; }
.riga { font-size: 11.5px; line-height: 1.55; color: #cbd5e1; }
.riga.muted { color: #8896ad; }
.riga.warn { color: #f87171; }
.riga.note { color: #fbbf24; font-style: italic; }
.links { margin-top: 5px; font-size: 11px; }
.links a { color: #60a5fa; text-decoration: none; font-weight: 600; }
.empty { color: #8896ad; font-size: 13px; padding: 20px 0; }
'''


def _documento_html(titolo: str, sottotitolo: str, sezioni: list, novita: dict) -> str:
    corpo = "".join(_sezione_html(k, aste, novita) for k, aste in sezioni if aste)
    if not corpo:
        corpo = '<div class="empty">Nessuna opportunità sopra soglia score al momento.</div>'
    chips = f'''<div class="chips">
        <span class="chip">{sum(len(a) for _, a in sezioni)} opportunità totali</span>
      </div>'''
    return f'''<!doctype html><html><head><meta charset="utf-8">
<style>{_CSS.replace("__GRADIENT__", BRAND_GRADIENT)}</style></head>
<body>
  <div class="banner">
    <h1>{_esc(titolo)}</h1>
    <div class="sub">{_esc(sottotitolo)}</div>
    {chips}
  </div>
  {corpo}
</body></html>'''


def _render_pdf(html: str, output_path: Path) -> Path:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        page.pdf(
            path=str(output_path),
            format="A4",
            print_background=True,
            # margin=0 ovunque: Chromium renderizza header/footer_template in
            # un box a parte con sfondo bianco fisso indipendente dal CSS —
            # provato, non ha funzionato in questo ambiente (nessun colore
            # passato ai template veniva applicato) e lasciava una striscia
            # bianca ai bordi di ogni pagina nel tema dark. Niente numero di
            # pagina per pagina — non essenziale per una lettura a swipe su
            # mobile, e il fondo scuro arriva così davvero a bordo pagina.
            margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
        )
        browser.close()
    return output_path


def _ordina_per_convenienza(aste: list) -> list:
    """
    Ordina per fascia di score (bande da 5 punti) e, dentro la stessa fascia,
    per prezzo crescente — budget limitato: a parità/vicinanza di punteggio
    si preferisce l'opzione più economica.
    """
    def chiave(a):
        score = a.get("score") or 0
        prezzo = a.get("offerta_minima") or a.get("prezzo_base") or float("inf")
        return (-(score // 5), prezzo)
    return sorted(aste, key=chiave)


def genera_report_lombardia(aste_citta: list, novita: dict, out_dir: Path,
                             nuovi_totali: int, ribassi_totali: int) -> Path:
    aste_citta = _ordina_per_convenienza(aste_citta)
    html = _documento_html(
        "Casa — Lombardia",
        f"Report del {datetime.now().strftime('%d/%m/%Y')} — "
        f"{nuovi_totali} nuovi annunci · {ribassi_totali} ribassati questa settimana",
        [("citta", aste_citta)], novita)
    return _render_pdf(html, out_dir / "report_lombardia.pdf")


def genera_report_vacanza(aste_montagna: list, aste_mare: list, novita: dict,
                           out_dir: Path, nuovi_totali: int, ribassi_totali: int) -> Path:
    aste_montagna = _ordina_per_convenienza(aste_montagna)
    aste_mare = _ordina_per_convenienza(aste_mare)
    html = _documento_html(
        "Vacanza — Montagna & Mare",
        f"Report del {datetime.now().strftime('%d/%m/%Y')} — "
        f"{nuovi_totali} nuovi annunci · {ribassi_totali} ribassati questa settimana",
        [("montagna", aste_montagna), ("mare", aste_mare)], novita)
    return _render_pdf(html, out_dir / "report_vacanza.pdf")
