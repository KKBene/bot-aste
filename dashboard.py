"""
Dashboard Streamlit per il Bot Aste Immobiliari.

Pensata per un investitore: mostra MARGINE/ROI reale (tutti i costi inclusi),
RISCHIO (occupazione, debiti, quota, note legali), POSIZIONE (mappa + zona/stazione),
URGENZA (deadline offerte) e TRACKING (storico ribassi, venduti).

Avvio:  streamlit run dashboard.py
"""
import json
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pydeck as pdk
import requests
import streamlit as st

import database as db
from notifier import giorni_rimanenti

# ─────────────────────────────────────────────────────────────
# CONFIG & STILE
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="Bot Aste — Dashboard Investitore",
                   page_icon="🏠", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
  .stApp { background: #0e1117; }
  div[data-testid="stMetric"] {
      background: #161b22; border: 1px solid #30363d; border-radius: 12px;
      padding: 14px 18px;
  }
  div[data-testid="stMetricValue"] { font-size: 1.7rem; }
  .opp-card {
      background: #161b22; border: 1px solid #30363d; border-left: 5px solid #58a6ff;
      border-radius: 10px; padding: 14px 18px; margin-bottom: 12px;
  }
  .verdict { display:inline-block; padding:2px 10px; border-radius:12px;
      font-weight:700; font-size:.8rem; }
  .v-buy   { background:rgba(63,185,80,.18);  color:#3fb950; }
  .v-ok    { background:rgba(88,166,255,.18); color:#58a6ff; }
  .v-thin  { background:rgba(210,153,34,.18); color:#d29922; }
  .v-loss  { background:rgba(248,81,73,.18);  color:#f85149; }
  .small { color:#8b949e; font-size:.85rem; }
  h1, h2, h3 { color:#e6edf3; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# DATI
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def carica_aste() -> pd.DataFrame:
    rows = db.get_client().table("aste").select("*").execute().data or []
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Estrai le metriche economiche dal breakdown dello score
    def bd(r, k):
        b = r.get("score_breakdown")
        if isinstance(b, str):
            try: b = json.loads(b)
            except Exception: b = {}
        return (b or {}).get(k)

    for col in ["margine_eur", "margine_pct", "roi_pct", "costo_totale",
                "pts_margine", "pts_posizione", "pts_liberabilita", "pts_affidabilita"]:
        df[col] = df.apply(lambda r: bd(r, col), axis=1)

    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    df["giorni_termine"] = df["termine_offerte"].apply(giorni_rimanenti)
    df["scaduto"] = df["giorni_termine"].apply(lambda g: g is not None and g < 0)
    return df


@st.cache_data(ttl=300)
def carica_storico(codice: str) -> pd.DataFrame:
    rows = db.get_storico_prezzi(codice)
    return pd.DataFrame(rows)


@st.cache_data(ttl=3600)
def galleria_immagini(codice: str, max_n: int = 10) -> list:
    """
    Costruisce la galleria foto: /asta/{i}/{codice}. L'endpoint, fuori range,
    ripete l'immagine principale → ci si ferma quando una dimensione si ripete.
    """
    base = "https://documents.astalegale.net/asta"
    urls, viste = [], set()
    for i in range(max_n):
        u = f"{base}/{i}/{codice}"
        try:
            r = requests.get(u, stream=True, timeout=6)
            if r.status_code != 200:
                break
            cl = r.headers.get("content-length")
            r.close()
            if cl and cl in viste:   # immagine ripetuta → fine galleria reale
                break
            if cl:
                viste.add(cl)
            urls.append(u)
        except Exception:
            break
    return urls


def verdetto(r) -> tuple[str, str]:
    """Verdetto sintetico investitore: (etichetta, classe css)."""
    note = (r.get("note_critiche") or "").lower()
    quota = (r.get("quota_proprieta") or "").lower()
    if any(k in note for k in ["non sanabile", "demolire", "inagibile", "amianto", "non conforme"]):
        return "⛔ Rischio legale", "v-loss"
    if "/2" in quota or "/3" in quota or "nuda" in quota:
        return "⛔ Quota parziale", "v-loss"
    m = r.get("margine_pct")
    if m is None or pd.isna(m):
        return "❔ Da analizzare", "v-ok"
    if m < 0:
        return "🚫 In perdita", "v-loss"
    if m < 10:
        return "⚠️ Margine sottile", "v-thin"
    if m < 20:
        return "👍 Interessante", "v-ok"
    return "🔥 Affare", "v-buy"


def euro(v):
    return f"€{v:,.0f}" if pd.notna(v) and v is not None else "—"


# ─────────────────────────────────────────────────────────────
# CARICAMENTO
# ─────────────────────────────────────────────────────────────
st.title("🏠 Bot Aste — Dashboard Investitore")

col_r, _ = st.columns([1, 9])
if col_r.button("🔄 Aggiorna"):
    st.cache_data.clear()
    st.rerun()

df = carica_aste()
if df.empty:
    st.warning("Nessun dato nel database. Lancia `python3 main.py` per popolarlo.")
    st.stop()

# ─────────────────────────────────────────────────────────────
# SIDEBAR — FILTRI
# ─────────────────────────────────────────────────────────────
st.sidebar.header("🔎 Filtri")

stati = st.sidebar.multiselect("Stato annuncio", sorted(df["stato_annuncio"].dropna().unique()),
                               default=["attivo"])
comuni = st.sidebar.multiselect("Comune", sorted(df["comune"].dropna().unique()))
score_min = st.sidebar.slider("Score minimo", 0, 100, 0, 5)
solo_positivo = st.sidebar.checkbox("Solo margine positivo", value=False)
escludi_scaduti = st.sidebar.checkbox("Escludi offerte scadute", value=True)
budget = st.sidebar.number_input("Budget massimo (offerta minima €)", 0, 2_000_000, 0, 10_000)
occ_sel = st.sidebar.multiselect("Occupazione",
                                 sorted(df["stato_occupazione"].dropna().unique()))

f = df.copy()
if stati:        f = f[f["stato_annuncio"].isin(stati)]
if comuni:       f = f[f["comune"].isin(comuni)]
f = f[f["score"].fillna(0) >= score_min]
if solo_positivo: f = f[f["margine_pct"].fillna(-999) > 0]
if escludi_scaduti: f = f[~f["scaduto"]]
if budget > 0:   f = f[f["offerta_minima"].fillna(0) <= budget]
if occ_sel:      f = f[f["stato_occupazione"].isin(occ_sel)]
f = f.sort_values("score", ascending=False, na_position="last")

# ─────────────────────────────────────────────────────────────
# KPI
# ─────────────────────────────────────────────────────────────
attivi = df[df["stato_annuncio"] == "attivo"]
urgenti = f[(f["giorni_termine"].notna()) & (f["giorni_termine"] >= 0) & (f["giorni_termine"] <= 7)]
positivi = f[f["margine_pct"].fillna(-999) > 0]
margine_tot = positivi["margine_eur"].fillna(0).sum()

k = st.columns(5)
k[0].metric("Opportunità (filtro)", len(f))
k[1].metric("Margine positivo", len(positivi))
k[2].metric("Score medio", f"{f['score'].mean():.0f}" if len(f) else "—")
k[3].metric("⏳ Urgenti (≤7gg)", len(urgenti))
k[4].metric("Margine totale stimato", euro(margine_tot))

st.divider()

# ─────────────────────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────────────────────
t1, t2, t3, t4 = st.tabs(["🎯 Opportunità", "🗺️ Mappa", "📊 Analisi", "🏠 Dettaglio immobile"])

# ── TAB 1: OPPORTUNITÀ ───────────────────────────────────────
with t1:
    if f.empty:
        st.info("Nessun immobile coi filtri correnti.")
    else:
        st.subheader(f"Top {min(len(f), 30)} opportunità")
        for _, r in f.head(30).iterrows():
            lab, cls = verdetto(r)
            sc = r["score"] if pd.notna(r["score"]) else 0
            emoji = "🔥" if sc >= 75 else "⭐" if sc >= 60 else "👍" if sc >= 45 else "📌"
            urg = ""
            g = r["giorni_termine"]
            if pd.notna(g):
                g = int(g)
                urg = ("❌ scaduto" if g < 0 else f"🔴 {g}gg" if g <= 7
                       else f"🟡 {g}gg" if g <= 21 else f"🟢 {g}gg")
            mq = f" · €{r['offerta_minima']/r['superficie_mq']:,.0f}/mq" if pd.notna(r.get("superficie_mq")) and r.get("superficie_mq") else ""
            st.markdown(f"""
<div class="opp-card">
  <span style="font-size:1.1rem;font-weight:700">{emoji} {sc:.0f}/100</span>
  &nbsp;<span class="verdict {cls}">{lab}</span>
  &nbsp;<span class="small">{urg}</span><br>
  <b>{(r['comune'] or '').title()}</b> — {r.get('indirizzo_immobile') or 'N/D'}<br>
  💰 <b>{euro(r['offerta_minima'])}</b>{mq}
  &nbsp;|&nbsp; 📈 Mercato {euro(r.get('valore_mercato'))}
  &nbsp;|&nbsp; 💼 Margine <b>{euro(r.get('margine_eur'))}</b>
  ({'+' if (r.get('margine_pct') or 0) >= 0 else ''}{r.get('margine_pct') if pd.notna(r.get('margine_pct')) else '?'}%)
  &nbsp;|&nbsp; ROI {r.get('roi_pct') if pd.notna(r.get('roi_pct')) else '?'}%<br>
  <span class="small">🏠 {r.get('stato_occupazione') or 'N/D'} · {r.get('stato_manutentivo') or 'N/D'}
  · zona {r.get('qualita_posizione') or 'N/D'}
  · 📅 asta {r.get('data_asta') or 'N/D'}
  · <a href="{r.get('link_dettaglio') or '#'}" target="_blank">annuncio</a></span>
</div>""", unsafe_allow_html=True)

        st.download_button("⬇️ Esporta CSV (filtrati)",
                           f.to_csv(index=False).encode("utf-8"),
                           "aste_filtrate.csv", "text/csv")

# ── TAB 2: MAPPA ─────────────────────────────────────────────
with t2:
    mappa = f.dropna(subset=["posizione_lat", "posizione_lng"]).copy()
    if mappa.empty:
        st.info("Nessuna coordinata disponibile per gli immobili filtrati.")
    else:
        mappa["lat"] = pd.to_numeric(mappa["posizione_lat"])
        mappa["lon"] = pd.to_numeric(mappa["posizione_lng"])

        def colore(s):
            s = 0 if pd.isna(s) else s
            if s >= 60: return [63, 185, 80, 200]      # verde
            if s >= 45: return [88, 166, 255, 200]      # blu
            if s >= 30: return [210, 153, 34, 200]      # giallo
            return [248, 81, 73, 200]                   # rosso
        mappa["color"] = mappa["score"].apply(colore)
        mappa["raggio"] = 120
        # Pulisci i campi numerici del tooltip (NaN romperebbe la serializzazione)
        for c in ["score", "offerta_minima", "margine_eur"]:
            mappa[c] = pd.to_numeric(mappa[c], errors="coerce").fillna(0)
        deck_df = mappa[["lat", "lon", "color", "raggio", "comune",
                         "indirizzo_immobile", "score", "offerta_minima", "margine_eur"]]

        st.caption("🟢 score ≥60 · 🔵 ≥45 · 🟡 ≥30 · 🔴 <30")
        tooltip = {"html": "<b>{comune}</b> — {indirizzo_immobile}<br>"
                           "Score {score} · Offerta €{offerta_minima}<br>Margine €{margine_eur}"}
        st.pydeck_chart(pdk.Deck(
            map_provider="carto",          # basemap gratuita, senza token Mapbox
            map_style="dark",
            initial_view_state=pdk.ViewState(
                latitude=deck_df["lat"].mean(), longitude=deck_df["lon"].mean(), zoom=9, pitch=0),
            layers=[pdk.Layer("ScatterplotLayer", data=deck_df,
                              get_position="[lon, lat]", get_fill_color="color",
                              get_radius="raggio", radius_min_pixels=6, pickable=True)],
            tooltip=tooltip,
        ))

# ── TAB 3: ANALISI ───────────────────────────────────────────
with t3:
    if f.empty:
        st.info("Nessun dato.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Distribuzione score")
            fig = px.histogram(f, x="score", nbins=20, color_discrete_sequence=["#58a6ff"])
            fig.update_layout(template="plotly_dark", height=320, margin=dict(t=10))
            st.plotly_chart(fig, width="stretch")
        with c2:
            st.subheader("Margine % vs Offerta")
            sca = f.dropna(subset=["margine_pct"]).copy()
            # size non accetta NaN: superficie mancante → dimensione di default
            sca["mq_size"] = pd.to_numeric(sca["superficie_mq"], errors="coerce").fillna(50)
            sca["score"] = pd.to_numeric(sca["score"], errors="coerce").fillna(0)
            if not sca.empty:
                fig = px.scatter(sca, x="offerta_minima", y="margine_pct", color="score",
                                 size="mq_size", hover_name="indirizzo_immobile",
                                 color_continuous_scale="RdYlGn", range_color=[0, 100])
                fig.add_hline(y=0, line_dash="dash", line_color="#f85149")
                fig.update_layout(template="plotly_dark", height=320, margin=dict(t=10))
                st.plotly_chart(fig, width="stretch")

        c3, c4 = st.columns(2)
        with c3:
            st.subheader("Score medio per comune")
            g = f.groupby("comune")["score"].mean().sort_values(ascending=False).reset_index()
            fig = px.bar(g, x="comune", y="score", color="score",
                         color_continuous_scale="RdYlGn", range_color=[0, 100])
            fig.update_layout(template="plotly_dark", height=320, margin=dict(t=10))
            st.plotly_chart(fig, width="stretch")
        with c4:
            st.subheader("Stato occupazione")
            occ = f["stato_occupazione"].fillna("N/D").value_counts().reset_index()
            occ.columns = ["stato", "n"]
            fig = px.pie(occ, names="stato", values="n", hole=0.5)
            fig.update_layout(template="plotly_dark", height=320, margin=dict(t=10))
            st.plotly_chart(fig, width="stretch")

# ── TAB 4: DETTAGLIO ─────────────────────────────────────────
with t4:
    if f.empty:
        st.info("Nessun immobile.")
    else:
        opzioni = {f"{r['codice']} — {(r['comune'] or '').title()} — {r.get('indirizzo_immobile') or ''} (score {r['score']:.0f})": r["codice"]
                   for _, r in f.iterrows()}
        sel = st.selectbox("Scegli un immobile", list(opzioni.keys()))
        r = f[f["codice"] == opzioni[sel]].iloc[0]

        lab, cls = verdetto(r)
        st.markdown(f"### {(r['comune'] or '').title()} — {r.get('indirizzo_immobile') or ''}  "
                    f"<span class='verdict {cls}'>{lab}</span>", unsafe_allow_html=True)

        m = st.columns(4)
        m[0].metric("Score", f"{r['score']:.0f}/100" if pd.notna(r['score']) else "—")
        m[1].metric("Margine", euro(r.get("margine_eur")),
                    f"{r.get('margine_pct')}%" if pd.notna(r.get("margine_pct")) else None)
        m[2].metric("ROI stimato", f"{r.get('roi_pct')}%" if pd.notna(r.get("roi_pct")) else "—")
        m[3].metric("Costo tutto incluso", euro(r.get("costo_totale")))

        # Galleria foto
        foto = galleria_immagini(r["codice"])
        if foto:
            st.subheader(f"📸 Foto ({len(foto)})")
            cols = st.columns(min(4, len(foto)))
            for i, url in enumerate(foto):
                cols[i % len(cols)].image(url, width="stretch")

        cL, cR = st.columns([3, 2])

        # Waterfall economico
        with cL:
            st.subheader("💶 Analisi economica")
            bdj = r.get("score_breakdown")
            if isinstance(bdj, str):
                try: bdj = json.loads(bdj)
                except Exception: bdj = {}
            bdj = bdj or {}
            vm = r.get("valore_mercato")
            if vm and bdj.get("costo_totale"):
                voci = [("Prezzo acquisto", -bdj.get("prezzo_acquisto", 0)),
                        ("Ristrutturazione", -bdj.get("ristrutturazione", 0)),
                        ("Imposte", -bdj.get("imposte", 0)),
                        ("Debiti condom.", -bdj.get("debiti_condominiali", 0)),
                        ("Spese straord.", -bdj.get("spese_straordinarie", 0)),
                        ("Sanatoria", -bdj.get("sanatoria", 0)),
                        ("Liberazione", -bdj.get("liberazione", 0)),
                        ("Possesso (condo+IMU)", -bdj.get("costo_possesso", 0)),
                        ("Oneri", -bdj.get("oneri_accessori", 0))]
                fig = go.Figure(go.Waterfall(
                    orientation="v",
                    measure=["absolute"] + ["relative"] * len(voci) + ["total"],
                    x=["Valore mercato"] + [v[0] for v in voci] + ["Margine netto"],
                    y=[vm] + [v[1] for v in voci] + [None],
                    connector={"line": {"color": "#30363d"}},
                    decreasing={"marker": {"color": "#f85149"}},
                    increasing={"marker": {"color": "#3fb950"}},
                    totals={"marker": {"color": "#58a6ff"}},
                ))
                fig.update_layout(template="plotly_dark", height=380, margin=dict(t=20))
                st.plotly_chart(fig, width="stretch")
            else:
                st.info("Analisi economica completa disponibile dopo l'analisi della perizia.")

            # Storico prezzi
            sto = carica_storico(r["codice"])
            if not sto.empty:
                st.subheader("📉 Storico prezzi")
                sto["rilevato_il"] = pd.to_datetime(sto["rilevato_il"])
                fig = px.line(sto, x="rilevato_il", y="prezzo_base", markers=True,
                              color_discrete_sequence=["#d29922"])
                fig.update_layout(template="plotly_dark", height=260, margin=dict(t=10))
                st.plotly_chart(fig, width="stretch")
                if pd.notna(r.get("numero_ribassi")) and r.get("numero_ribassi"):
                    st.caption(f"📉 {int(r['numero_ribassi'])} ribasso/i registrati")

        # Scheda dati + mappa
        with cR:
            st.subheader("📋 Scheda")
            def riga(label, val):
                st.markdown(f"<span class='small'>{label}</span><br><b>{val}</b>",
                            unsafe_allow_html=True)
            riga("Tipologia", r.get("tipologia_immobile") or r.get("tipologia") or "—")
            riga("Superficie", f"{r.get('superficie_mq')} mq" if pd.notna(r.get("superficie_mq")) else "—")
            riga("Occupazione", (r.get("stato_occupazione") or "—") +
                 (" (non opponibile)" if r.get("occupazione_opponibile") is False else ""))
            riga("Manutenzione", r.get("stato_manutentivo") or "—")
            riga("Quota", r.get("quota_proprieta") or "—")
            riga("Categoria cat.", r.get("categoria_catastale") or "—")
            riga("Anno", r.get("anno_costruzione") or "—")
            riga("Classe energ.", r.get("classe_energetica") or "—")
            riga("Posizione", f"{r.get('qualita_posizione') or '—'} · stazione "
                              f"{r.get('distanza_stazione_km')} km" if pd.notna(r.get("distanza_stazione_km"))
                              else (r.get("qualita_posizione") or "—"))
            if r.get("pertinenze"):
                riga("Pertinenze", r.get("pertinenze"))
            riga("Debiti condom. (arretrati)", euro(r.get("spese_condominiali_arretrate")))
            riga("Spese condom. annue", euro(r.get("spese_condominiali_annue")))
            bdj_sched = r.get("score_breakdown") or {}
            if isinstance(bdj_sched, str):
                try: bdj_sched = json.loads(bdj_sched)
                except Exception: bdj_sched = {}
            if bdj_sched.get("imu_annua"):
                riga("IMU annua stimata", euro(bdj_sched.get("imu_annua")))
            if pd.notna(r.get("canone_locazione_annuo")) and r.get("canone_locazione_annuo"):
                yl = bdj_sched.get("rendita_lorda_pct")
                riga("Canone locazione/anno", euro(r.get("canone_locazione_annuo"))
                     + (f" · resa lorda {yl}%" if yl else ""))
            riga("Rendita catastale", euro(r.get("rendita_catastale")))
            riga("Tribunale", r.get("tribunale") or "—")
            riga("Procedura", r.get("numero_procedura") or "—")
            riga("Data asta", r.get("data_asta") or "—")
            riga("Termine offerte", r.get("termine_offerte") or "—")

            if r.get("note_critiche"):
                st.warning(f"⚠️ {r['note_critiche']}")

            # Mini mappa
            if pd.notna(r.get("posizione_lat")):
                st.map(pd.DataFrame({"lat": [float(r["posizione_lat"])],
                                     "lon": [float(r["posizione_lng"])]}), zoom=13)

            # Documenti
            st.subheader("📄 Documenti")
            for nome, key in [("Perizia", "link_perizia"), ("Avviso", "link_avviso_vendita"),
                              ("Ordinanza", "link_ordinanza"), ("Planimetrie", "link_planimetrie"),
                              ("Annuncio", "link_dettaglio")]:
                if r.get(key):
                    st.markdown(f"- [{nome}]({str(r[key]).split(',')[0]})")

st.caption(f"Ultimo aggiornamento dati: {datetime.now():%d/%m/%Y %H:%M} · "
           f"{len(df)} immobili totali nel database")
