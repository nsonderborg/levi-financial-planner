# Levi — Personal Finance OS

A fully local personal finance system for Danish bank accounts and investment portfolios. Parses real bank exports, categorises spending, tracks budgets, runs AI analysis via a local LLM, and syncs curated summaries to Notion — with no raw transaction data ever leaving your machine.

> **Planned:** Revolut CSV support (same `_normalise()` pipeline, format profile extension only — no architectural change needed).

---

## What Levi does

```
Bank exports (CSV / .numbers / PDF)
        │
        ▼  Parse & normalise
        │  Nordea CSV · Nordea Numbers · Saxo Bank PDF · Saxo Numbers
        │
        ▼  Categorise & deduplicate
        │  Keyword rules + per-transaction overrides (persisted in config/)
        │
        ▼  Analyse
        │  Streamlit dashboard · CLI reports · Local Ollama LLM · Claude API (opt-in)
        │
        ▼  Output
           Streamlit UI · Markdown reports · Notion database row · Notion dashboard
```

---

## Inputs

| Source | Formats | How |
|---|---|---|
| Nordea bank statement | `.csv`, `.numbers` | Drop into `data/inbox/`, click Scan in sidebar |
| Saxo Bank portfolio report | `.pdf` | Upload in Tab 5 |
| Saxo Bank account statement | `.numbers` | Drop into `data/inbox/` |
| User profile | Form in Tab 5 | Age, income, goals, savings target |
| Balance sheet | Form in Tab 5 | Assets, liabilities, insurance, pension |
| Dagpenge (safety net) | Form in Tab 5 | Unemployment benefits gross/net, max weeks |
| Budget targets | Form in Tab 5 | Per-category monthly limits in DKK |
| Category rules | Inline editor in Tab 2 | Keyword rules + per-row overrides |

---

## Transformation pipeline

### 1. Parse — `nordea_parser.py: parse_any()`

- **Nordea CSV** — encoding detection (UTF-8 / latin-1), semicolon separator, Danish amount format (`1.234,56 → 1234.56`)
- **Nordea / Saxo Numbers** — `numbers_parser.Document`, Sheet 1, column mapping
- **Saxo PDF** — `pypdf`, pages 2/4/5/6/8: account metadata, total YTD return %, monthly returns, per-ETF returns, holdings (ISIN, weight %, EUR prices, EUR→DKK rate), cost ratio %. Writes `config/portfolio.json`.

### 2. Normalise — `_normalise(df)`

- Renames columns to canonical names (`dato`, `beløb`, `label`, …)
- Detects `"Reserveret"` (pending) rows → `reserveret=True`, date = NaT
- Assigns transaction type (`Indkomst` / `Udgift`)
- Runs initial keyword categorisation
- Saved to `data/processed/<timestamp>_<name>`

### 3. Merge & deduplicate — `load_all_processed()`

Merges all processed files, deduplicates on `(dato, beløb, label)`. Multiple files covering overlapping periods are safe.

### 4. Categorise — `recategorize(df, rules, overrides)`

1. **Overrides** — exact per-row assignments, keyed `YYYY-MM-DD||beløb||label`
2. **Keyword rules** — substring match across `navn`, `beskrivelse`, `modtager`, `afsender`
3. **Fallback** — `Andet`

Rules and overrides persist in `config/categories.json`.

### 5. Context building

Three functions compose the AI prompt context:

| Function | Content | Privacy |
|---|---|---|
| `build_context()` | KPIs, category totals, TOP 10 merchants, budget status | Full DKK — local only |
| `build_balance_sheet_context()` | Assets, liabilities, insurance, pension, dagpenge | Full DKK — local only |
| `build_portfolio_context()` | Holdings as % and EUR prices only | Safe for Claude API |

Merchant-level detail (`TOP 10 UDGIFTER` section) is stripped before any Claude API call via `_strip_merchant_names()`.

---

## Outputs

### Streamlit app — `streamlit run app.py`

| Tab | Content |
|---|---|
| **1 Dashboard** | Income / expenses / net / savings rate metrics; category bar chart; budget progress bars (green <80 %, yellow 80–100 %, red >100 %) |
| **2 Transaktioner** | Filterable table (category, type, date range, Afstemt); inline category reassignment; keyword rule editor; reconciliation checkboxes (auto-saved) |
| **3 AI Chat** | Local Ollama (Finansanalytiker) or Claude API (opt-in, sidebar toggle) — full conversation, context-seeded |
| **4 Livs-Roadmap** | **BlackRock** (Ollama, long-horizon plan) · **Goldman Sachs** (Claude API, wealth diagnostic) · **Morgan Stanley** (Claude API, portfolio architect) · **Wealthfront** (Claude API, real estate analyser) — Claude personas behind explicit confirmation checkboxes |
| **5 Profil & Rapporter** | Profile form; balance sheet form; budget targets; Saxo PDF uploader; local report viewer |

Sidebar shows live API key status and Ollama model selector.

### CLI reports — `generate_report.py`

```bash
python generate_report.py --type scan     # ingest data/inbox/ → data/processed/
python generate_report.py --type weekly   # Ollama weekly summary → data/reports/
python generate_report.py --type monthly  # Ollama monthly diagnostic → data/reports/
python generate_report.py --type all      # scan + weekly + monthly
python generate_report.py --model qwen2.5:7b --type monthly
```

Markdown output to `data/reports/`. Cron-ready (no interactive dependencies).

### Notion sync — `sync_to_notion.py`

```bash
python sync_to_notion.py --type monthly [--dry-run] [--model qwen2.5:7b]
python sync_to_notion.py --type weekly
```

Pushes one row per run to the Levi Notion database:

| Column | Type | Source |
|---|---|---|
| Måned | Date | local |
| Indkomst | Number | local df |
| Udgifter | Number | local df |
| Opsparingsrate | Number % | local df |
| Afkast YTD | Number % | local portfolio.json |
| Porteføljeværdi | Number | local profile.json (manual) |
| Budget-status | Select (På sporet / Marginal / Overskred) | computed locally |
| Ollama-analyse | Rich text | local Ollama output |
| Handlinger | Rich text | A1/A2/A3 lines extracted from analysis |

Also saves analysis locally to `data/reports/notion_monthly_YYYYMM.md`.

### Notion dashboard — `create_notion_dashboard.py`

```bash
python create_notion_dashboard.py          # create or refresh in-place
python create_notion_dashboard.py --force  # force new page
```

Reads the last 6 Notion DB entries + `config/portfolio.json` and writes a styled Notion page:

- **Goldman Sachs Wealth Diagnostic** — 3-column KPI cards (Indkomst / Udgifter / Opsparingsrate), 2-column row (Netto opsparing / Budget-status emoji-coded), Ollama analysis callout, A1/A2/A3 action bullets
- **Morgan Stanley Portfolio Architect** — portfolio value / YTD return cards, holdings table (name / weight % / YTD return % / EUR price), monthly returns bar chart
- **Historical table** — last 6 months, all key metrics

Page ID cached in `.env` as `NOTION_DASHBOARD_PAGE_ID` — URL stays permanent across monthly updates.

---

## Recommended monthly workflow

```bash
# 1. Export Nordea CSV and/or Saxo PDF → drop into data/inbox/

# 2. Ingest
python generate_report.py --type scan

# 3. Review in UI — recategorise, reconcile, check budgets
streamlit run app.py

# 4. Generate local report
python generate_report.py --type monthly

# 5. Push to Notion
python sync_to_notion.py --type monthly
python create_notion_dashboard.py
```

---

## Privacy model

| Layer | Sees | Never sees |
|---|---|---|
| Local machine | Everything | — |
| Ollama (localhost) | Full context incl. DKK amounts | Never leaves the machine |
| Claude API (opt-in, Tab 3/4 only) | Category %, portfolio %, profile text | Absolute DKK, merchant names, account IDs |
| Notion | Curated DKK figures + Ollama text | Raw transactions, merchant names |

Claude API is strictly opt-in and requires an explicit per-call confirmation in the UI. Raw transaction data never leaves your machine.

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install and start Ollama
brew install ollama
ollama pull qwen2.5:7b
ollama serve

# 3. Configure
cp .env.example .env   # or create .env manually (see keys below)

# 4. Generate synthetic test data (optional)
python generate_test_data.py
python generate_report.py --type scan

# 5. Launch
streamlit run app.py
```

### `.env` keys

```
NOTION_INTEGRATION_KEY=ntn_xxx       # Notion internal integration token
NOTION_DB_ID=xxx                     # Levi database ID (from Notion URL)
ANTHROPIC_API_KEY=sk-ant-xxx         # Optional — Claude API personas only
NOTION_WEEKLY_DB_ID=xxx              # Optional — falls back to NOTION_DB_ID
NOTION_DASHBOARD_PAGE_ID=xxx         # Auto-set by create_notion_dashboard.py
```

---

## File structure

```
Levi-financial-planner/
├── app.py                      # Streamlit UI (5 tabs + sidebar)
├── nordea_parser.py            # Shared parser + context library
├── generate_report.py          # CLI cron: ingest + weekly/monthly reports
├── sync_to_notion.py           # CLI cron: push data + Ollama analysis to Notion DB
├── create_notion_dashboard.py  # CLI: build/refresh GS+MS Notion dashboard page
├── generate_test_data.py       # CLI: emit synthetic transactions for dev/testing
├── requirements.txt
├── pytest.ini
├── config/                     # Gitignored, created at runtime
│   ├── profile.json            # User profile, budgets, balance sheet, dagpenge
│   ├── categories.json         # {rules: {…}, overrides: {…}}
│   └── portfolio.json          # Saxo snapshot (written by parse_saxo_pdf)
├── data/
│   ├── inbox/                  # Drop zone — moved to processed/ on scan
│   ├── processed/              # Parsed DataFrames (source of truth)
│   └── reports/                # Generated Markdown + Notion reports
└── tests/
    └── test_parser.py          # 36 unit tests
```

---

## Cron setup (macOS)

```cron
# Ingest inbox daily at 09:00
0 9 * * *   /path/to/python /path/to/generate_report.py --type scan

# Weekly summary — Monday 07:30
30 7 * * 1  /path/to/python /path/to/generate_report.py --type weekly

# Monthly: report + Notion sync on 1st at 08:00 / 08:30 / 08:45
0 8 1 * *   /path/to/python /path/to/generate_report.py --type monthly
30 8 1 * *  /path/to/python /path/to/sync_to_notion.py --type monthly
45 8 1 * *  /path/to/python /path/to/create_notion_dashboard.py
```

macOS requires Full Disk Access for cron (System Settings → Privacy & Security → Full Disk Access → add `/usr/sbin/cron`).

---

## Running tests

```bash
python -m pytest tests/ -v   # 36 tests: _clean_amount, categorize, _normalise, parse_csv_file
```

---

## Exporting from Nordea

1. Log in at netbank.nordea.dk
2. Go to **Transaktioner & detaljer** for the account
3. Click the **CSV** button → save to `data/inbox/`
4. Run `python generate_report.py --type scan` or click Scan in the sidebar

Repeat for each account. Deduplication handles overlapping date ranges across multiple files.

---

## Dependencies

```
python          >= 3.11
streamlit       >= 1.32
pandas          >= 2.0
requests        >= 2.31
numbers-parser  >= 4.2
pypdf           >= 4.0
anthropic       >= 0.40
python-dotenv   >= 1.0
notion-client   >= 2.0
pytest          >= 7.0
ollama                    (local server — brew install ollama)
```
