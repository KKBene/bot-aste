-- ================================================================
-- SCHEMA SUPABASE per Bot Aste Immobiliari
-- Esegui questo SQL nel query editor di Supabase:
-- https://supabase.com/dashboard/project/mrvucjvehtofarflobvt/sql
-- ================================================================

-- Abilita UUID
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ──────────────────────────────────────────────────────────────
-- TABELLA PRINCIPALE: aste
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS aste (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    codice          TEXT UNIQUE NOT NULL,       -- codice univoco astalegale.net

    -- Dati base (da scraping)
    comune          TEXT,
    prezzo_base     NUMERIC(12,2),
    offerta_minima  NUMERIC(12,2),
    indirizzo_immobile TEXT,
    indirizzo_asta  TEXT,
    tipologia       TEXT,
    data_asta       TEXT,                       -- stringa per flessibilità formati
    termine_offerte TEXT,                       -- scadenza presentazione offerte
    modalita_gara   TEXT,                       -- es. "Sincrona mista"
    descrizione     TEXT,                       -- descrizione testuale dell'immobile
    tribunale       TEXT,
    numero_procedura TEXT,
    lotto           TEXT,
    link_dettaglio  TEXT,
    link_avviso_vendita TEXT,
    link_perizia    TEXT,
    link_ordinanza  TEXT,
    link_planimetrie TEXT,
    posizione_lat   NUMERIC(10,7),              -- coordinate (da API)
    posizione_lng   NUMERIC(10,7),
    sheet_type      TEXT DEFAULT 'residenziale', -- 'residenziale' | 'montagna'
    scraping_date   TIMESTAMPTZ,

    -- Analisi PDF (da Gemini)
    stato_occupazione   TEXT,                  -- LIBERO | OCCUPATO_*
    occupazione_opponibile BOOLEAN,            -- contratto opponibile all'acquirente?
    costi_sanatoria     NUMERIC(12,2),
    superficie_mq       NUMERIC(8,2),
    stato_manutentivo   TEXT,                  -- OTTIMO | BUONO | MEDIOCRE | PESSIMO | RUDERE
    piano_ascensore     TEXT,
    distanza_stazione_km NUMERIC(6,2),         -- distanza dalla stazione ferroviaria
    qualita_posizione   TEXT,                  -- OTTIMA | BUONA | MEDIA | SCARSA
    valore_mercato      NUMERIC(12,2),         -- OMV stimato dal perito
    spese_condominiali_arretrate NUMERIC(12,2),-- debiti art. 568 cpc a carico acquirente
    quota_proprieta     TEXT,                  -- es. "1/1 piena proprietà"
    categoria_catastale TEXT,                  -- es. A/2, A/3, A/7
    anno_costruzione    INTEGER,
    classe_energetica   TEXT,                  -- APE A4..G
    tipologia_immobile  TEXT,                  -- appartamento, villa singola...
    note_critiche       TEXT,
    analisi_pdf         BOOLEAN DEFAULT FALSE,
    data_analisi        TIMESTAMPTZ,

    -- Scoring
    score               NUMERIC(5,1),          -- 0-100
    score_breakdown     JSONB,                 -- dettaglio componenti score

    -- Notifiche
    notificato          BOOLEAN DEFAULT FALSE,

    -- Ciclo di vita annuncio
    stato_annuncio      TEXT DEFAULT 'attivo',     -- attivo | sparito | venduto
    prima_vista         TIMESTAMPTZ,               -- prima volta che è stato visto
    ultima_vista        TIMESTAMPTZ,               -- ultima volta visto in uno scrape
    prezzo_base_iniziale NUMERIC(12,2),            -- primo prezzo base osservato
    numero_ribassi      INTEGER DEFAULT 0,         -- quante volte il prezzo è calato

    -- Metadata
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────────
-- STORICO PREZZI: una riga per ogni variazione osservata
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prezzi_storico (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    codice          TEXT NOT NULL REFERENCES aste(codice) ON DELETE CASCADE,
    prezzo_base     NUMERIC(12,2),
    offerta_minima  NUMERIC(12,2),
    rilevato_il     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_storico_codice ON prezzi_storico(codice);
CREATE INDEX IF NOT EXISTS idx_aste_stato     ON aste(stato_annuncio);

-- Indici per query frequenti
CREATE INDEX IF NOT EXISTS idx_aste_codice         ON aste(codice);
CREATE INDEX IF NOT EXISTS idx_aste_score          ON aste(score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_aste_notificato     ON aste(notificato);
CREATE INDEX IF NOT EXISTS idx_aste_analisi_pdf    ON aste(analisi_pdf);
CREATE INDEX IF NOT EXISTS idx_aste_comune         ON aste(comune);
CREATE INDEX IF NOT EXISTS idx_aste_sheet_type     ON aste(sheet_type);
CREATE INDEX IF NOT EXISTS idx_aste_data_asta      ON aste(data_asta);

-- Trigger updated_at automatico
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS aste_updated_at ON aste;
CREATE TRIGGER aste_updated_at
    BEFORE UPDATE ON aste
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ──────────────────────────────────────────────────────────────
-- TABELLA LOG ESECUZIONI: runs
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    status          TEXT DEFAULT 'running',    -- running | success | error | interrupted
    nuovi_annunci   INTEGER DEFAULT 0,
    pdf_analizzati  INTEGER DEFAULT 0,
    errori          INTEGER DEFAULT 0,
    note            TEXT
);

-- ──────────────────────────────────────────────────────────────
-- ROW LEVEL SECURITY (disabilitato per service role)
-- ──────────────────────────────────────────────────────────────
ALTER TABLE aste DISABLE ROW LEVEL SECURITY;
ALTER TABLE runs DISABLE ROW LEVEL SECURITY;

-- ──────────────────────────────────────────────────────────────
-- VISTE UTILI
-- ──────────────────────────────────────────────────────────────

-- Top offerte con score alto e libere
CREATE OR REPLACE VIEW v_top_offerte AS
SELECT
    codice,
    comune,
    offerta_minima,
    prezzo_base,
    ROUND(((prezzo_base - offerta_minima) / NULLIF(prezzo_base, 0)) * 100, 1) AS sconto_pct,
    superficie_mq,
    ROUND(offerta_minima / NULLIF(superficie_mq, 0), 0)                       AS prezzo_mq,
    stato_occupazione,
    stato_manutentivo,
    score,
    data_asta,
    link_dettaglio,
    link_perizia
FROM aste
WHERE score IS NOT NULL
ORDER BY score DESC;

-- Statistiche per comune
CREATE OR REPLACE VIEW v_stats_comune AS
SELECT
    comune,
    COUNT(*)                                        AS totale,
    COUNT(*) FILTER (WHERE analisi_pdf)             AS con_analisi,
    ROUND(AVG(score) FILTER (WHERE score IS NOT NULL), 1) AS score_medio,
    ROUND(AVG(offerta_minima), 0)                   AS prezzo_medio,
    COUNT(*) FILTER (WHERE stato_occupazione = 'LIBERO') AS liberi
FROM aste
GROUP BY comune
ORDER BY totale DESC;
