# Deploy in cloud (gratis) — GitHub Actions + Streamlit Cloud

Due componenti, entrambi gratuiti e senza server da gestire:

| Componente | Dove | Cosa fa |
|---|---|---|
| **Job settimanale** | GitHub Actions | venerdì: scraping → analisi → score → Telegram |
| **Dashboard** | Streamlit Community Cloud | interfaccia investitore on-demand |
| **Database** | Supabase (già attivo) | persistenza dati |

I segreti reali sono nel tuo file locale `.env` (NON è nel repo). Ti serviranno per i passi sotto.

---

## 1. Crea il repo su GitHub

```bash
cd /Users/macbook/Desktop/Code/Scraping/Aste
# il repo è già inizializzato e committato in locale
gh repo create bot-aste --private --source=. --push   # se hai gh CLI
# OPPURE: crea un repo PRIVATO vuoto su github.com e poi:
#   git remote add origin https://github.com/<tuo-utente>/bot-aste.git
#   git push -u origin main
```
> Il repo deve essere **privato**.

## 2. Aggiungi i Secrets su GitHub

Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Crea questi (i valori sono nel tuo `.env`):

- `SUPABASE_URL`
- `SUPABASE_KEY`
- `GEMINI_API_KEY`
- `GROQ_API_KEY`
- `MISTRAL_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## 3. Attiva e prova il job

Repo → tab **Actions** → abilita i workflow → apri **"Bot Aste — run settimanale"** →
**Run workflow** (avvio manuale) per testarlo subito. Parte poi ogni **venerdì ~08:00**
(cron `0 6 * * 5` UTC).

---

## 4. Dashboard su Streamlit Cloud

1. Vai su **share.streamlit.io** → accedi con GitHub → **New app**.
2. Repo `bot-aste`, branch `main`, file **`dashboard.py`**.
3. **Advanced settings → Secrets**: incolla il contenuto di
   `.streamlit/secrets.toml.example` con i valori reali del tuo `.env`.
4. Deploy. L'app si apre su un URL pubblico `*.streamlit.app` (va in sleep
   dopo inattività e si risveglia all'accesso).

---

## Note operative

- **Google Sheets**: disattivato in cloud (`SYNC_TO_SHEETS=false`) perché richiede
  `credentials.json`. Supabase + dashboard lo rendono ridondante. Per riattivarlo:
  aggiungi il JSON del service account come secret e scrivilo su file nel workflow.
- **Quota Gemini** (20/giorno): sufficiente per i pochi annunci nuovi settimanali;
  overflow automatico su Groq/Mistral.
- **Cron disabilitato dopo 60 giorni di inattività del repo** (policy GitHub):
  un commit ogni tanto, o lascia che il run settimanale lo tenga vivo.
- **Monitoraggio**: log nel tab Actions di ogni run; errori inviati su Telegram;
  ogni esecuzione registrata nella tabella `runs` di Supabase.
