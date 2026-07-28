#!/usr/bin/env python3
"""
Bot Aste — Orchestratore principale.

Flusso completo:
  1. Scraping astalegale.net con Playwright
  2. Salvataggio su Supabase
  3. Analisi PDF perizie con Gemini
  4. Calcolo score opportunità (0-100)
  5. Sync opzionale su Google Sheets
  6. Digest Telegram con top offerte

Esegui manualmente:  python3 main.py
Esegui solo scraping: python3 main.py --no-pdf --no-sheet
"""
import asyncio
import json
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

from config import (
    COMUNI_PER_LOCALITA, SCRAPA_LOCALITA,
    CATEGORIA_RESIDENZIALE, FONTE_ASTE, USA_IVG,
    GEMINI_API_KEY, GEMINI_MODEL,
    SCORE_MINIMO_NOTIFICA, TOP_N_NOTIFICA,
    SYNC_TO_SHEETS,
)
import database as db
# Fonte dati selezionabile da config: PVP (fonte-madre, default) o astalegale
# (storica, dormiente). I due run_scraper condividono lo stesso contratto
# {nuovi, esistenti, codici_per_comune}.
if FONTE_ASTE == "astalegale":
    from scraper_api import run_scraper
else:
    from scraper_pvp import run_scraper, merge_deterministici, cerca_perizia_astalegale
from pdf_analyzer import PDFAnalyzer
from scorer import calcola_score
from market_estimate import stima_lotti
from geocoding import geocodifica
from scraper_ivg import run_scraper as run_scraper_ivg
from notifier import send_start, send_digest, send_error, send_document, send_message, aste_notificabili
from pdf_report import genera_report_lombardia, genera_report_vacanza, prepara_novita

# Flag da riga di comando
SKIP_PDF = "--no-pdf" in sys.argv
SKIP_SHEET = "--no-sheet" in sys.argv
SKIP_TELEGRAM = "--no-telegram" in sys.argv
SKIP_MERCATO = "--no-mercato" in sys.argv
DRY_RUN = "--dry-run" in sys.argv


def _arg_value(flag: str) -> str | None:
    """Legge il valore di un flag --x VALORE da sys.argv."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


# --comuni a,b,c  → scrapa solo questi comuni (utile per test)
COMUNI_OVERRIDE = _arg_value("--comuni")
# --limit-pdf N   → analizza al massimo N PDF (rispetta la quota Gemini)
_lim = _arg_value("--limit-pdf")
LIMIT_PDF = int(_lim) if _lim and _lim.isdigit() else None


def step(label: str):
    print(f"\n{'─'*55}")
    print(f"  {label}")
    print(f"{'─'*55}")


def main():
    start_time = datetime.now()
    print(f"\n{'='*55}")
    print(f"  🏠 BOT ASTE — {start_time.strftime('%d/%m/%Y %H:%M')}")
    print(f"{'='*55}")

    if DRY_RUN:
        print("  ⚠️  DRY RUN — nessun dato sarà scritto")

    run_id = None
    nuovi_count = 0
    pdf_count = 0
    errori_count = 0
    variazioni_count = 0
    sparite_count = 0

    try:
        if not DRY_RUN:
            run_id = db.start_run()

        if not SKIP_TELEGRAM and not DRY_RUN:
            send_start()

        # ─────────────────────────────────────────────────────
        # STEP 1: Scraping
        # ─────────────────────────────────────────────────────
        step(f"📡 STEP 1 — Scraping fonte: {FONTE_ASTE}")

        codici_esistenti = db.get_codici_esistenti() if not DRY_RUN else set()
        attive = db.get_aste_attive() if not DRY_RUN else {}
        print(f"  Annunci nel DB: {len(codici_esistenti)} (attivi: {len(attive)})")

        # mappa {comune: categoria_localita} ricostruita dalla config
        comune_to_cat = {com: cat for cat, lista in COMUNI_PER_LOCALITA.items() for com in lista}

        if COMUNI_OVERRIDE:
            comuni = [c.strip() for c in COMUNI_OVERRIDE.split(",") if c.strip()]
            print(f"  ⚙️  Comuni override: {comuni}")
        else:
            categorie_attive = [cat for cat, on in SCRAPA_LOCALITA.items() if on]
            comuni = [c for cat in categorie_attive for c in COMUNI_PER_LOCALITA[cat]]
            riassunto = ", ".join(
                f"{cat}={len(COMUNI_PER_LOCALITA[cat])}" for cat in categorie_attive
            )
            print(f"  📍 Categorie attive: {riassunto} (totale {len(comuni)} comuni)")

        # scraper_api.run_scraper è sincrono (requests); supporta anche
        # eventuali implementazioni async (es. scraper_pw) come fallback.
        risultato = run_scraper(
            comuni, CATEGORIA_RESIDENZIALE, codici_esistenti,
            categoria_localita={c: comune_to_cat.get(c, "citta") for c in comuni},
        )
        if asyncio.iscoroutine(risultato):
            risultato = asyncio.run(risultato)
        nuovi_annunci = risultato["nuovi"]
        esistenti_annunci = risultato["esistenti"]
        codici_per_comune = risultato["codici_per_comune"]
        print(f"\n  ✅ Nuovi: {len(nuovi_annunci)} | Ri-controllati: {len(esistenti_annunci)}")

        # Fonte aggiuntiva IVG: porta i nuovi esperimenti di vendita che su PVP
        # risultano ancora fermi a un'asta passata. Scarta ciò che PVP ha già
        # portato, confrontando comune e importi.
        if USA_IVG and FONTE_ASTE != "astalegale":
            try:
                gia_viste = list(attive.values()) + [
                    {"comune": a.get("comune"), "prezzo_base": a.get("prezzo_base"),
                     "offerta_minima": a.get("offerta_minima")} for a in nuovi_annunci]
                ivg = run_scraper_ivg(comuni, codici_esistenti=codici_esistenti,
                                      categoria_localita={c: comune_to_cat.get(c, "citta")
                                                          for c in comuni},
                                      aste_esistenti=gia_viste, verbose=False)
                nuovi_annunci += ivg["nuovi"]
                esistenti_annunci += ivg["esistenti"]
                for comune, codici in ivg["codici_per_comune"].items():
                    codici_per_comune.setdefault(comune, []).extend(codici)
                print(f"  ➕ IVG: {len(ivg['nuovi'])} nuovi | {len(ivg['esistenti'])} già noti")
            except Exception as e:
                print(f"  ⚠️ Fonte IVG non disponibile: {str(e)[:120]}")

        # ─────────────────────────────────────────────────────
        # STEP 2: Salvataggio + tracking prezzi + annunci spariti
        # ─────────────────────────────────────────────────────
        step("💾 STEP 2 — Salvataggio, prezzi, disponibilità")

        if not DRY_RUN:
            # 2a. Nuovi annunci → insert
            for asta in nuovi_annunci:
                try:
                    if db.inserisci_asta(asta):
                        nuovi_count += 1
                except Exception as e:
                    print(f"  ❌ Errore inserimento {asta.get('codice')}: {e}")
                    errori_count += 1

            # 2b. Annunci esistenti → tracking variazioni di prezzo
            for asta in esistenti_annunci:
                codice = asta["codice"]
                prec = attive.get(codice, {})
                try:
                    if db.sincronizza_esistente(
                        codice, asta.get("prezzo_base"), asta.get("offerta_minima"),
                        prec.get("prezzo_base"), prec.get("offerta_minima"),
                    ):
                        variazioni_count += 1
                        print(f"  💱 Variazione prezzo: {codice} "
                              f"{prec.get('prezzo_base')} → {asta.get('prezzo_base')}")
                except Exception as e:
                    print(f"  ❌ Errore sync {codice}: {e}")
                    errori_count += 1

            # 2c. Annunci spariti: attivi nel DB del comune ma non più visti.
            # Marca solo nei comuni che hanno restituito ALMENO un risultato:
            # un comune con lista vuota può essere un fallimento transitorio di
            # rete (cerca_comune ritorna []), non un comune realmente svuotato →
            # evita di marcare per errore tutti gli annunci come "venduti".
            # Con più fonti attive il confronto va fatto DENTRO la stessa
            # fonte: se PVP fallisce su un comune mentre IVG risponde, i suoi
            # lotti verrebbero altrimenti marcati venduti per errore.
            spariti = []
            for codice, info in attive.items():
                visti = codici_per_comune.get(info.get("comune"))
                if not visti:
                    continue
                fonte = codice.split("-")[0] if "-" in codice else codice[:1]
                visti_stessa_fonte = [c for c in visti if c.startswith(fonte)]
                if visti_stessa_fonte and codice not in visti_stessa_fonte:
                    spariti.append(codice)
            if spariti:
                sparite_count = db.marca_sparite(spariti)
                print(f"  🗑️  Annunci non più disponibili: {sparite_count}")

            # Rete di sicurezza: marca 'venduto' le aste con data già passata
            # (non più offerte valide), anche se ancora viste nello scrape.
            scadute = db.marca_scadute()
            if scadute:
                print(f"  📅 Aste scadute marcate come vendute: {scadute}")

        print(f"  ✅ Inseriti: {nuovi_count} | Variazioni prezzo: {variazioni_count} | "
              f"Spariti/venduti: {sparite_count}")

        # ─────────────────────────────────────────────────────
        # STEP 3: Analisi PDF
        # ─────────────────────────────────────────────────────
        step("🤖 STEP 3 — Analisi PDF (perizia, con fallback avviso)")

        if SKIP_PDF:
            print("  ⏭️  Saltato (--no-pdf)")
        elif DRY_RUN:
            print("  ⏭️  Saltato (dry-run)")
        else:
            # Lotti con un documento analizzabile: perizia se c'è, altrimenti
            # avviso di vendita (fallback verificato — contiene occupazione,
            # superficie, valore, catasto).
            aste_da_analizzare = db.get_aste_da_analizzare()
            if LIMIT_PDF is not None:
                aste_da_analizzare = aste_da_analizzare[:LIMIT_PDF]
                print(f"  ⚙️  Limite PDF: analizzo {len(aste_da_analizzare)}")
            print(f"  PDF da analizzare: {len(aste_da_analizzare)}")

            analyzer = PDFAnalyzer(GEMINI_API_KEY, GEMINI_MODEL)
            for asta in aste_da_analizzare:
                codice = asta["codice"]
                # PVP non allega sempre la perizia: senza quella il valore di
                # mercato resta ignoto (l'avviso riporta solo il prezzo base).
                # Prima di ripiegare sull'avviso, prova a recuperarla da
                # astalegale, che ripubblica gli stessi lotti coi documenti.
                if not asta.get("link_perizia") and FONTE_ASTE != "astalegale":
                    recuperata = cerca_perizia_astalegale(
                        asta.get("comune"), asta.get("indirizzo_immobile"),
                        asta.get("prezzo_base"))
                    if recuperata:
                        asta["link_perizia"] = recuperata
                        db.aggiorna_link_perizia(codice, recuperata)
                        print(f"    🔗 {codice}: perizia recuperata da astalegale")

                url = asta.get("link_perizia") or asta.get("link_avviso_vendita")
                fonte_doc = "perizia" if asta.get("link_perizia") else "avviso"
                detail_url = asta.get("link_dettaglio")
                print(f"\n  📄 {codice} ({fonte_doc})")
                try:
                    dati = analyzer.analizza_pdf_da_url(url, detail_url)
                    if dati:
                        # Preserva i campi deterministici ufficiali PVP (valore/
                        # superficie/occupazione): l'LLM riempie solo i buchi.
                        if FONTE_ASTE != "astalegale":
                            dati = merge_deterministici(dati, asta)
                        db.aggiorna_analisi_pdf(codice, dati)
                        pdf_count += 1
                    else:
                        print("    ⚠️ Analisi non riuscita")
                except Exception as e:
                    print(f"    ❌ Errore: {e}")
                    errori_count += 1

        # ─────────────────────────────────────────────────────
        # STEP 4: Scoring
        # ─────────────────────────────────────────────────────
        step("📊 STEP 4 — Calcolo score opportunità")

        if not DRY_RUN:
            # Ricalcola lo score di TUTTE le aste attive: è deterministico e
            # istantaneo, così riflette sempre prezzi e dati PDF aggiornati
            # (nessuna possibilità di score "stale"). da_riscorare è incluso.
            aste_da_scorare = db.get_aste_attive_complete()
            print(f"  Aste attive da (ri)scorare: {len(aste_da_scorare)}")

            # Secondo riferimento di valore, indipendente dalla perizia: il €/m²
            # realmente richiesto oggi in quel comune. La stima del CTU è spesso
            # conservativa e datata, quindi da sola sottostima il margine.
            stime_mercato = {}
            if not SKIP_MERCATO:
                # Senza coordinate la stima non può restringersi al vicinato e
                # nelle città grandi confronta quartieri incomparabili: PVP le
                # dà solo per una parte dei lotti, il resto si geocodifica.
                da_geocodificare = [a for a in aste_da_scorare
                                    if a.get("superficie_mq") and not a.get("posizione_lat")]
                geocodificate = 0
                for asta in da_geocodificare:
                    coord = geocodifica(asta.get("indirizzo_immobile"), asta.get("comune"))
                    if coord:
                        asta["posizione_lat"], asta["posizione_lng"] = coord
                        db.aggiorna_coordinate(asta["codice"], *coord)
                        geocodificate += 1
                if geocodificate:
                    print(f"  📍 Coordinate ricavate per geocoding: {geocodificate}")

                try:
                    stime_mercato = stima_lotti(
                        [a for a in aste_da_scorare if a.get("superficie_mq")],
                        verbose=False)
                    print(f"  🏘️  Stime di mercato da comparabili: {len(stime_mercato)}")
                except Exception as e:
                    print(f"  ⚠️ Stima di mercato non disponibile: {str(e)[:100]}")

            for asta in aste_da_scorare:
                try:
                    score, breakdown = calcola_score(asta)
                    # Lo scoring riscrive il breakdown da zero: senza questo, un
                    # run con --no-mercato cancellerebbe le stime già calcolate.
                    mercato = (stime_mercato.get(asta["codice"])
                               or (asta.get("score_breakdown") or {}).get("mercato"))
                    if mercato:
                        breakdown["mercato"] = mercato
                    db.aggiorna_score(asta["codice"], score, breakdown)
                except Exception as e:
                    print(f"  ❌ Errore scoring {asta.get('codice')}: {e}")
                    errori_count += 1

            print(f"  ✅ Score (ri)calcolati per {len(aste_da_scorare)} aste")

        # ─────────────────────────────────────────────────────
        # STEP 5: Google Sheets sync
        # ─────────────────────────────────────────────────────
        step("📋 STEP 5 — Sync Google Sheets")

        if SKIP_SHEET or not SYNC_TO_SHEETS:
            print("  ⏭️  Saltato")
        elif DRY_RUN:
            print("  ⏭️  Saltato (dry-run)")
        else:
            try:
                from sheets import sync_to_sheets
                sync_to_sheets()
            except Exception as e:
                print(f"  ❌ Errore Google Sheets: {e}")
                errori_count += 1

        # ─────────────────────────────────────────────────────
        # STEP 6: Report PDF settimanale (Lombardia + Vacanza) via Telegram
        # ─────────────────────────────────────────────────────
        step("📱 STEP 6 — Report PDF settimanale")

        if SKIP_TELEGRAM or DRY_RUN:
            print("  ⏭️  Saltato")
        else:
            try:
                # Novità della settimana: mai notificate + sopra soglia score
                # (nessun cap: lo snapshot PDF non ha i limiti di lunghezza di
                # un messaggio Telegram, quindi mostriamo tutte le novità).
                candidate = db.get_aste_da_notificare(SCORE_MINIMO_NOTIFICA, 500)
                # + Aste già notificate ma con ribasso significativo (≥5%)
                ribassi = db.get_aste_ribassate_da_notificare(soglia_pct=5.0)

                visti = {a["codice"] for a in candidate}
                da_marcare = list(candidate)
                for r in ribassi:
                    if r["codice"] not in visti:
                        da_marcare.append(r); visti.add(r["codice"])
                da_marcare = aste_notificabili(da_marcare)   # esclude offerte scadute
                codici_da_marcare = {a["codice"] for a in da_marcare}

                nuovi_set = {a["codice"] for a in candidate} & codici_da_marcare
                ribassi_giocabili = [r for r in ribassi
                                      if r["codice"] in codici_da_marcare and r["codice"] not in nuovi_set]
                novita = prepara_novita(nuovi_set, ribassi_giocabili)

                # Snapshot completo: tutte le aste attive sopra soglia, non scadute
                snapshot = [a for a in db.get_aste_attive_complete()
                            if (a.get("score") or 0) >= SCORE_MINIMO_NOTIFICA]
                snapshot = aste_notificabili(snapshot)

                per_cat = {"citta": [], "montagna": [], "mare": []}
                for a in snapshot:
                    per_cat.get(a.get("categoria_localita") or "citta", per_cat["citta"]).append(a)

                nuovi_settimana = len(nuovi_set)
                ribassi_settimana = len(ribassi_giocabili)

                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    pdf_lom = genera_report_lombardia(
                        per_cat["citta"], novita, tmp_path, nuovi_settimana, ribassi_settimana)
                    pdf_vac = genera_report_vacanza(
                        per_cat["montagna"], per_cat["mare"], novita, tmp_path,
                        nuovi_settimana, ribassi_settimana)

                    send_message(
                        f"Report Aste — {datetime.now().strftime('%d/%m/%Y')}\n"
                        f"{nuovi_settimana} nuovi annunci, {ribassi_settimana} ribassati questa settimana.\n"
                        f"Due PDF in arrivo: Lombardia e Vacanza."
                    )
                    send_document(pdf_lom, caption=f"Casa — Lombardia ({len(per_cat['citta'])} opportunità)")
                    send_document(pdf_vac, caption=(
                        f"Vacanza — Montagna & Mare "
                        f"({len(per_cat['montagna']) + len(per_cat['mare'])} opportunità)"))

                if da_marcare:
                    db.segna_notificate([a["codice"] for a in da_marcare])
                    print(f"  ✅ Notificate {len(da_marcare)} offerte (report PDF)")

            except Exception as e:
                print(f"  ❌ Errore report PDF: {e} — fallback al digest testuale")
                traceback.print_exc()
                candidate = db.get_aste_da_notificare(SCORE_MINIMO_NOTIFICA, TOP_N_NOTIFICA)
                ribassi = db.get_aste_ribassate_da_notificare(soglia_pct=5.0)
                visti = {a["codice"] for a in candidate}
                for r in ribassi:
                    if r["codice"] not in visti:
                        candidate.append(r); visti.add(r["codice"])
                top_aste = aste_notificabili(candidate)
                send_digest(top_aste, {"nuovi_totali": nuovi_count, "pdf_analizzati": pdf_count})
                if top_aste:
                    db.segna_notificate([a["codice"] for a in top_aste])
                    print(f"  ✅ Notificate {len(top_aste)} offerte (fallback testuale)")

        # ─────────────────────────────────────────────────────
        # FINE
        # ─────────────────────────────────────────────────────
        elapsed = (datetime.now() - start_time).seconds
        print(f"\n{'='*55}")
        print(f"  ✅ COMPLETATO in {elapsed}s")
        print(f"  Nuovi: {nuovi_count} | PDF: {pdf_count} | "
              f"Variazioni: {variazioni_count} | Spariti: {sparite_count} | Errori: {errori_count}")
        print(f"{'='*55}\n")

        if run_id:
            db.end_run(run_id, "success", nuovi_count, pdf_count, errori_count)

    except KeyboardInterrupt:
        print("\n\n⚠️ Interrotto dall'utente")
        if run_id:
            db.end_run(run_id, "interrupted", nuovi_count, pdf_count, errori_count)

    except Exception as e:
        print(f"\n❌ ERRORE CRITICO: {e}")
        traceback.print_exc()
        if run_id:
            db.end_run(run_id, "error", nuovi_count, pdf_count, errori_count + 1)
        if not SKIP_TELEGRAM and not DRY_RUN:
            send_error(str(e))


if __name__ == "__main__":
    main()
