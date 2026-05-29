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
import traceback
from datetime import datetime

from config import (
    COMUNI_PER_LOCALITA, SCRAPA_LOCALITA,
    CATEGORIA_RESIDENZIALE,
    GEMINI_API_KEY, GEMINI_MODEL,
    SCORE_MINIMO_NOTIFICA, TOP_N_NOTIFICA,
    SYNC_TO_SHEETS,
)
import database as db
from scraper_api import run_scraper   # API JSON: molto più veloce di Playwright
from pdf_analyzer import PDFAnalyzer
from scorer import calcola_score
from notifier import send_start, send_digest, send_error

# Flag da riga di comando
SKIP_PDF = "--no-pdf" in sys.argv
SKIP_SHEET = "--no-sheet" in sys.argv
SKIP_TELEGRAM = "--no-telegram" in sys.argv
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
        step("📡 STEP 1 — Scraping astalegale.net")

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
            spariti = []
            for codice, info in attive.items():
                comune = info.get("comune")
                visti = codici_per_comune.get(comune)
                if visti and codice not in visti:
                    spariti.append(codice)
            if spariti:
                sparite_count = db.marca_sparite(spariti)
                print(f"  🗑️  Annunci non più disponibili: {sparite_count}")

        print(f"  ✅ Inseriti: {nuovi_count} | Variazioni prezzo: {variazioni_count} | "
              f"Spariti/venduti: {sparite_count}")

        # ─────────────────────────────────────────────────────
        # STEP 3: Analisi PDF
        # ─────────────────────────────────────────────────────
        step("🤖 STEP 3 — Analisi PDF con Gemini")

        if SKIP_PDF:
            print("  ⏭️  Saltato (--no-pdf)")
        elif DRY_RUN:
            print("  ⏭️  Saltato (dry-run)")
        else:
            aste_da_analizzare = db.get_aste_senza_analisi()
            print(f"  PDF da analizzare: {len(aste_da_analizzare)}")

            if aste_da_analizzare:
                # Leggi anche il link_dettaglio per passarlo al downloader PDF
                res_full = (
                    db.get_client()
                    .table("aste")
                    .select("codice, link_perizia, link_dettaglio")
                    .eq("analisi_pdf", False)
                    .not_.is_("link_perizia", "null")
                    .neq("link_perizia", "")
                    .execute()
                )
                aste_da_analizzare = res_full.data or []
                if LIMIT_PDF is not None:
                    aste_da_analizzare = aste_da_analizzare[:LIMIT_PDF]
                    print(f"  ⚙️  Limite PDF: analizzo {len(aste_da_analizzare)}")

                analyzer = PDFAnalyzer(GEMINI_API_KEY, GEMINI_MODEL)
                for asta in aste_da_analizzare:
                    codice = asta["codice"]
                    url = asta.get("link_perizia", "")
                    detail_url = asta.get("link_dettaglio")
                    print(f"\n  📄 {codice}")
                    try:
                        dati = analyzer.analizza_pdf_da_url(url, detail_url)
                        if dati:
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

            for asta in aste_da_scorare:
                try:
                    score, breakdown = calcola_score(asta)
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
        # STEP 6: Digest Telegram
        # ─────────────────────────────────────────────────────
        step("📱 STEP 6 — Invio digest Telegram")

        if SKIP_TELEGRAM or DRY_RUN:
            print("  ⏭️  Saltato")
        else:
            from notifier import aste_notificabili
            # Mai notificate + sopra soglia score
            candidate = db.get_aste_da_notificare(SCORE_MINIMO_NOTIFICA, TOP_N_NOTIFICA)
            # + Aste già notificate ma con ribasso significativo (≥5%)
            ribassi = db.get_aste_ribassate_da_notificare(soglia_pct=5.0)
            # Unione (no duplicati) preservando il flag ribasso
            visti = {a["codice"] for a in candidate}
            for r in ribassi:
                if r["codice"] not in visti:
                    candidate.append(r); visti.add(r["codice"])
            top_aste = aste_notificabili(candidate)   # esclude offerte scadute
            statistiche = {
                "nuovi_totali": nuovi_count,
                "pdf_analizzati": pdf_count,
            }
            send_digest(top_aste, statistiche)

            if top_aste:
                db.segna_notificate([a["codice"] for a in top_aste])
                print(f"  ✅ Notificate {len(top_aste)} offerte")

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
