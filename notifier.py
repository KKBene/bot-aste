"""
Notifiche Telegram per il bot aste.
Invia il digest settimanale con le migliori opportunità ordinate per score.
"""
import html
import re
from datetime import datetime
from typing import Optional
import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GOOGLE_SHEET_ID
from scorer import score_emoji


# ─────────────────────────────────────────────────────────────
# TIMING: giorni rimanenti al termine offerte / asta
# ─────────────────────────────────────────────────────────────

def parse_data_it(s: Optional[str]) -> Optional[datetime]:
    """Parsa una data italiana 'DD/MM/YYYY' o 'DD/MM/YYYY HH:MM'. None se invalida."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def giorni_rimanenti(data_str: Optional[str], adesso: Optional[datetime] = None) -> Optional[int]:
    """Giorni interi da ora alla data indicata. Negativo se passata. None se non parsabile."""
    dt = parse_data_it(data_str)
    if not dt:
        return None
    adesso = adesso or datetime.now()
    return (dt.date() - adesso.date()).days


def _urgenza_str(giorni: Optional[int]) -> str:
    """Etichetta di urgenza in base ai giorni rimanenti al termine offerte."""
    if giorni is None:
        return ""
    if giorni < 0:
        return " ❌ <b>SCADUTO</b>"
    if giorni == 0:
        return " 🔴 <b>OGGI!</b>"
    if giorni <= 7:
        return f" 🔴 <b>tra {giorni}gg</b>"
    if giorni <= 21:
        return f" 🟡 tra {giorni}gg"
    return f" 🟢 tra {giorni}gg"


# ─────────────────────────────────────────────────────────────
# CORE: invio messaggio
# ─────────────────────────────────────────────────────────────

def _escape_html(text: str) -> str:
    """
    Escape HTML per Telegram preservando solo i tag ammessi:
    <b>, <i>, <a href="...">, <code>.
    """
    saved_links = []

    def save_link(m):
        saved_links.append(m.group(0))
        return f"__LINK_{len(saved_links) - 1}__"

    text = re.sub(r'<a href=[\'"][^\'"]+[\'"]>[^<]+</a>', save_link, text)
    text = text.replace("<b>", "__BOLD_S__").replace("</b>", "__BOLD_E__")
    text = text.replace("<i>", "__ITAL_S__").replace("</i>", "__ITAL_E__")
    text = text.replace("<code>", "__CODE_S__").replace("</code>", "__CODE_E__")

    text = html.escape(text, quote=False)

    text = text.replace("__BOLD_S__", "<b>").replace("__BOLD_E__", "</b>")
    text = text.replace("__ITAL_S__", "<i>").replace("__ITAL_E__", "</i>")
    text = text.replace("__CODE_S__", "<code>").replace("__CODE_E__", "</code>")
    for i, link in enumerate(saved_links):
        text = text.replace(f"__LINK_{i}__", link)

    return text


def send_document(file_path, caption: str = "") -> bool:
    """Invia un file (es. PDF) come documento Telegram. Ritorna True se riuscito."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": _escape_html(caption)[:1024],
                    "parse_mode": "HTML",
                },
                files={"document": (str(file_path).rsplit("/", 1)[-1], f, "application/pdf")},
                timeout=60,
            )
        if resp.status_code == 200:
            print(f"  ✅ Telegram: documento inviato ({file_path})")
            return True
        print(f"  ❌ Telegram sendDocument {resp.status_code}: {resp.text[:300]}")
        return False
    except Exception as e:
        print(f"  ❌ Telegram sendDocument exception: {e}")
        return False


def send_message(text: str, disable_preview: bool = True) -> bool:
    """Invia un messaggio HTML a Telegram. Ritorna True se riuscito."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": _escape_html(text),
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            print("  ✅ Telegram: messaggio inviato")
            return True
        print(f"  ❌ Telegram {resp.status_code}: {resp.text[:300]}")
        return False
    except Exception as e:
        print(f"  ❌ Telegram exception: {e}")
        return False


def _split_and_send(text: str, max_len: int = 4000) -> bool:
    """Invia in blocchi se il messaggio supera il limite Telegram (4096 char)."""
    if len(text) <= max_len:
        return send_message(text)

    # Spezza sui doppi newline; se il testo non ha doppi newline, spezza a caratteri
    separatore = "\n\n" if "\n\n" in text else "\n"
    blocchi = text.split(separatore)

    # Fallback: se anche così un singolo blocco è > max_len, spezza a forza ogni max_len char
    blocchi_flat = []
    for blocco in blocchi:
        if len(blocco) > max_len:
            for i in range(0, len(blocco), max_len):
                blocchi_flat.append(blocco[i:i + max_len])
        else:
            blocchi_flat.append(blocco)

    corrente = ""
    ok = True
    sep = separatore
    for blocco in blocchi_flat:
        if len(corrente) + len(blocco) + len(sep) > max_len:
            if corrente:
                ok = send_message(corrente.strip()) and ok
            corrente = blocco
        else:
            corrente += (sep if corrente else "") + blocco
    if corrente:
        ok = send_message(corrente.strip()) and ok
    return ok


# ─────────────────────────────────────────────────────────────
# MESSAGGI STANDARD
# ─────────────────────────────────────────────────────────────

def send_start() -> bool:
    return send_message("🤖 <b>Bot Aste avviato</b> — scraping in corso, attendi...")


def send_error(msg: str) -> bool:
    return send_message(f"❌ <b>Errore Bot Aste</b>\n\n<code>{msg[:400]}</code>")


# ─────────────────────────────────────────────────────────────
# DIGEST SETTIMANALE
# ─────────────────────────────────────────────────────────────

def _formatta_asta(asta: dict, rank: int) -> str:
    score = asta.get("score") or 0
    emoji = score_emoji(score)
    # Flag ribasso (se l'asta è qui per re-notifica su calo prezzo)
    ribasso_banner = ""
    if asta.get("_ribasso_da") and asta.get("_ribasso_pct"):
        ribasso_banner = (f"💱 <b>RIBASSATO {asta['_ribasso_pct']:+.0f}%</b> "
                          f"(prima €{asta['_ribasso_da']:,.0f})\n")

    # Prezzo e sconto — vs valore di mercato del perito se disponibile, altrimenti vs base
    prezzo = asta.get("offerta_minima") or asta.get("prezzo_base") or 0
    valore_mercato = asta.get("valore_mercato")
    sconto_str = ""
    if valore_mercato and valore_mercato > 0 and prezzo > 0 and valore_mercato >= prezzo:
        pct = (valore_mercato - prezzo) / valore_mercato * 100
        sconto_str = f" <b>(-{pct:.0f}% vs mercato)</b>"
    elif asta.get("prezzo_base") and asta.get("offerta_minima") and asta["prezzo_base"] > 0:
        pct = (asta["prezzo_base"] - asta["offerta_minima"]) / asta["prezzo_base"] * 100
        if pct > 0:
            sconto_str = f" <b>(-{pct:.0f}%)</b>"

    # Prezzo al mq
    mq_str = ""
    sup = asta.get("superficie_mq")
    if sup and sup > 0 and prezzo > 0:
        mq_str = f" · <i>€{prezzo / sup:,.0f}/mq</i>"

    # Valore di mercato del perito
    mercato_line = ""
    if valore_mercato and valore_mercato > 0:
        mercato_line = f"\n    📈 Stima perito: <b>€{valore_mercato:,.0f}</b>"

    # Analisi economica: margine netto stimato + ROI (dal breakdown dello score)
    bd = asta.get("score_breakdown") or {}
    economia_line = ""
    margine_eur = bd.get("margine_eur")
    margine_pct = bd.get("margine_pct")
    costo_totale = bd.get("costo_totale")
    if margine_eur is not None and margine_pct is not None:
        seg = "🟢" if margine_pct >= 15 else ("🟡" if margine_pct >= 0 else "🔴")
        economia_line = (
            f"\n    {seg} <b>Margine: €{margine_eur:,.0f} ({margine_pct:+.0f}%)</b>"
        )
        if costo_totale:
            roi = bd.get("roi_pct")
            roi_str = f" · ROI {roi:+.0f}%" if roi is not None else ""
            economia_line += f"\n    💼 Costo tutto incluso: €{costo_totale:,.0f}{roi_str}"

    # Occupazione
    occ_map = {
        "LIBERO": "✅ Libero",
        "OCCUPATO_DEBITORE": "🔶 Occp. Debitore",
        "OCCUPATO_SENZA_TITOLO": "🔴 Occp. s/Titolo",
        "OCCUPATO_CON_TITOLO": "⛔ Occp. c/Titolo",
    }
    occ = occ_map.get(asta.get("stato_occupazione") or "", "❓ N/D")

    # Manutenzione
    mnut_map = {
        "OTTIMO": "🟢 Ottimo",
        "BUONO": "🟡 Buono",
        "MEDIOCRE": "🟠 Mediocre",
        "PESSIMO": "🔴 Pessimo",
        "RUDERE": "⚫ Rudere",
    }
    mnut = mnut_map.get(asta.get("stato_manutentivo") or "", "❓ N/D")

    # Opponibilità contratto (solo se occupato con titolo non opponibile = buona notizia)
    if asta.get("stato_occupazione") == "OCCUPATO_CON_TITOLO" and asta.get("occupazione_opponibile") is False:
        occ += " <i>(non opponibile)</i>"

    # Tipologia + catastale + anno (riga dettagli immobile)
    dettagli = []
    if asta.get("tipologia_immobile"):
        dettagli.append(str(asta["tipologia_immobile"]).capitalize())
    if asta.get("categoria_catastale"):
        dettagli.append(f"cat. {asta['categoria_catastale']}")
    if asta.get("anno_costruzione"):
        dettagli.append(f"anno {asta['anno_costruzione']}")
    if asta.get("classe_energetica"):
        dettagli.append(f"APE {asta['classe_energetica']}")
    dettagli_line = f"\n    🏷️ {' · '.join(dettagli)}" if dettagli else ""

    # Posizione: qualità zona + distanza stazione
    pos_parti = []
    if asta.get("qualita_posizione"):
        pos_parti.append(f"zona {str(asta['qualita_posizione']).capitalize()}")
    dist = asta.get("distanza_stazione_km")
    if dist is not None:
        pos_parti.append(f"stazione {dist:g} km")
    posizione_line = f"\n    📌 {' · '.join(pos_parti)}" if pos_parti else ""

    # Link mappa (da coordinate API)
    lat, lng = asta.get("posizione_lat"), asta.get("posizione_lng")
    mappa_line = ""
    if lat and lng:
        mappa_line = f'\n    🗺️ <a href="https://www.google.com/maps?q={lat},{lng}">Mappa</a>'

    # Debiti condominiali arretrati (costo nascosto — avviso)
    debiti = asta.get("spese_condominiali_arretrate")
    debiti_line = ""
    if debiti and debiti > 0:
        debiti_line = f"\n    💸 <b>Debiti condominiali: €{debiti:,.0f}</b>"

    # Quota proprietà (avviso se non intera)
    quota = asta.get("quota_proprieta") or ""
    quota_line = ""
    if quota and ("nuda" in quota.lower() or "usufrutto" in quota.lower()
                  or _is_frazionata(quota)):
        quota_line = f"\n    ⚠️ Quota: <b>{quota}</b>"

    # Note critiche (solo se presenti)
    note = asta.get("note_critiche") or ""
    note_line = f"\n    ⚠️ <i>{note[:100]}</i>" if note else ""

    link_det = asta.get("link_dettaglio") or ""
    link_line = f'\n    🔗 <a href="{link_det}">Vedi annuncio</a>' if link_det else ""

    comune = (asta.get("comune") or "N/D").title()
    indirizzo = asta.get("indirizzo_immobile") or "N/D"
    data_asta = asta.get("data_asta") or "N/D"

    # Termine presentazione offerte (deadline per agire) + urgenza in giorni
    termine = asta.get("termine_offerte")
    termine_line = ""
    if termine:
        urgenza = _urgenza_str(giorni_rimanenti(termine))
        termine_line = f"\n    ⏳ <b>Offerte entro: {termine}</b>{urgenza}"

    return (
        f"{ribasso_banner}"
        f"{emoji} <b>#{rank} — Score {score:.0f}/100</b>\n"
        f"    📍 {comune} — {indirizzo}\n"
        f"    💰 <b>€{prezzo:,.0f}</b>{sconto_str}{mq_str}"
        f"{mercato_line}"
        f"{economia_line}"
        f"{dettagli_line}"
        f"{posizione_line}\n"
        f"    🏠 {occ} · {mnut}\n"
        f"    📅 Asta: {data_asta}"
        f"{termine_line}"
        f"{debiti_line}"
        f"{quota_line}"
        f"{note_line}"
        f"{link_line}"
        f"{mappa_line}"
    )


def _is_frazionata(quota: str) -> bool:
    """True se la quota indica una frazione < 1 (es. 1/2)."""
    m = re.search(r"(\d+)\s*/\s*(\d+)", quota)
    if m:
        return int(m.group(1)) < int(m.group(2))
    return False


def _termine_scaduto(asta: dict, adesso: Optional[datetime] = None) -> bool:
    """
    True se il termine offerte è già passato (asta non più giocabile).

    Il confronto è sull'istante, non sul giorno: i termini PVP portano l'ora
    ("28/07/2026 12:00") e contando solo i giorni un'asta chiusa stamattina
    resterebbe nel report fino a mezzanotte, marcata "OGGI!".
    Quando l'ora non è indicata il termine vale fino a fine giornata.
    """
    testo = asta.get("termine_offerte")
    scadenza = parse_data_it(testo)
    if scadenza is None:
        return False
    if not re.search(r"\d{1,2}:\d{2}", str(testo)):
        scadenza = scadenza.replace(hour=23, minute=59, second=59)
    return scadenza < (adesso or datetime.now())


def aste_notificabili(aste: list) -> list:
    """Filtra le aste con termine offerte già scaduto (non più giocabili)."""
    return [a for a in aste if not _termine_scaduto(a)]


def send_digest(top_aste: list, statistiche: dict) -> bool:
    """Invia il digest settimanale con le migliori opportunità."""
    nuovi = statistiche.get("nuovi_totali", 0)
    pdf_analizzati = statistiche.get("pdf_analizzati", 0)

    # Scarta le aste il cui termine di presentazione offerte è già scaduto:
    # non sono più giocabili, inutile notificarle.
    top_aste = [a for a in top_aste if not _termine_scaduto(a)]

    if not top_aste:
        msg = (
            "✅ <b>Report Aste Settimanale</b>\n\n"
            f"📊 Nuovi annunci trovati: <b>{nuovi}</b>\n"
            "Nessuna nuova opportunità sopra soglia questa settimana."
        )
        return send_message(msg)

    header = (
        "🏠 <b>Report Aste — Venerdì</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Nuovi annunci: <b>{nuovi}</b>  |  PDF analizzati: <b>{pdf_analizzati}</b>\n"
        f"🏆 Top {len(top_aste)} opportunità per score\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    # Raggruppa per tipo di località: se sono presenti più categorie il digest
    # viene reso in sezioni (altrimenti le città monopolizzano i top score).
    SEZIONI_ORDINE = [
        ("citta",    "🏙️ <b>CITTÀ</b>"),
        ("montagna", "🏔️ <b>MONTAGNA</b>"),
        ("mare",     "🌊 <b>MARE</b>"),
    ]
    per_cat = {k: [] for k, _ in SEZIONI_ORDINE}
    altre = []
    for a in top_aste:
        c = a.get("categoria_localita")
        (per_cat[c] if c in per_cat else altre).append(a)

    categorie_attive = [(k, t) for k, t in SEZIONI_ORDINE if per_cat[k]]
    if len(categorie_attive) <= 1 and not altre:
        # tutte di un'unica categoria → render piatto (no overhead di sezioni)
        cards = [_formatta_asta(a, i) for i, a in enumerate(top_aste, 1)]
        body = "\n\n".join(cards)
    else:
        blocchi = []
        rank = 1
        for k, titolo in categorie_attive:
            lista = per_cat[k]
            blocchi.append(f"{titolo} — top {len(lista)}\n")
            for a in lista:
                blocchi.append(_formatta_asta(a, rank))
                rank += 1
        if altre:
            blocchi.append("📍 <b>ALTRE</b>\n")
            for a in altre:
                blocchi.append(_formatta_asta(a, rank))
                rank += 1
        body = "\n\n".join(blocchi)

    sheet_url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"
    footer = (
        "\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f'📊 <a href="{sheet_url}">Apri Google Sheet completo</a>'
    )

    full_msg = header + body + footer
    return _split_and_send(full_msg)
