"""
nordea_parser.py — shared parser, normaliser, categoriser, and context builder.

Imported by both app.py (Streamlit dashboard) and generate_report.py (CLI cron).
Entry-point-specific constants (BASE_DIR, INBOX, PROCESSED, REPORTS, PROFILE_FILE)
remain in each entry point; pass PROCESSED explicitly to load_all_processed().
"""

import io
import json
import re
import requests
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# ── Nordea column schema ──────────────────────────────────────────────────────
NORDEA_COLS = {
    "bogføringsdato": "dato",
    "bogforingsdato": "dato",
    "beløb":          "beløb",
    "belob":          "beløb",
    "amount":         "beløb",
    "afsender":       "afsender",
    "modtager":       "modtager",
    "navn":           "navn",
    "beskrivelse":    "beskrivelse",
    "tekst":          "beskrivelse",   # old CSV format fallback
    "text":           "beskrivelse",
    "saldo":          "saldo",
    "balance":        "saldo",
    "valuta":         "valuta",
    "currency":       "valuta",
    "afstemt":        "afstemt",
}

CATEGORIES = {
    "Dagligvarer":       ["netto", "rema", "bilka", "fakta", "superbrugsen", "lidl", "aldi", "meny", "føtex", "irma", "spar", "coop", "365discount"],
    "Transport":         ["dsb", "metro", "movia", "rejsekort", "taxa", "uber", "circle k", "shell", "q8", "esso", "parking", "parkering", "7-eleven"],
    "Restaurant & Café": ["restaurant", "café", "pizza", "burger", "sushi", "mcdonalds", "kfc", "starbucks", "joe and", "bar ", "pub", "takeaway", "just eat"],
    "Abonnementer":      ["netflix", "spotify", "hbo", "disney", "apple.com", "google", "youtube", "adobe", "dropbox", "github", "openai", "flexii", "tdc", "yousee", "3 dk"],
    "Sundhed & Fitness": ["apotek", "læge", "tandlæge", "fitness", "gym", "crossfit", "thai boxing", "bjj", "zone fitness"],
    "Bolig":             ["husleje", "el ", "vand ", "varme", "internet", "bredbånd", "forsikring", "ejendom", "andels"],
    "Shopping":          ["zalando", "zara", "h&m", "asos", "amazon", "ebay", "magasin", "illum", "vinted", "roede kors", "tipster"],
    "MobilePay":         ["mobilepay", "mp "],
    "Overførsler":       ["overf", "transfer", "revolut", "saxo", "aktiesparekonto"],
    "Løn & Indkomst":    ["løn", "salary", "dagpenge", "su ", "honorar", "konsulent", "faktura"],
    "A-kasse":           ["a-kasse"],
    "Forsikringer":      ["forsikring", "gjensidige"],
    "Børn & Familie":    ["legetøj", "kids", "børn", "bleer", "sfo", "vuggestue", "dagpleje"],
    "Indretning":        ["ikea", "jysk", "ilva", "bolia", "søstrene grene", "tiger store", "imerco", "sinnerup", "inspiration"],
    "Renteindtægt":      ["rente", "interest", "rentetilskrivning"],
    "Erhvervsindkomst":  [],
    "Andet":             [],
}


# ── Core parser functions ─────────────────────────────────────────────────────

def _clean_amount(val):
    if val is None or str(val).strip() in ("", "None"):
        return None
    s = str(val).strip().replace("\xa0", "").replace(" ", "")
    # Danish format: 1.234,56 → strip thousands dot, replace comma decimal
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _override_key(row) -> str:
    """Build the stable key used for per-transaction category overrides."""
    dato = row.get("dato")
    dato_str = dato.strftime("%Y-%m-%d") if pd.notna(dato) else "NaT"
    beløb = row.get("beløb")
    # Format float consistently: always show as float to avoid -28 vs -28.0 mismatch
    beløb_str = f"{float(beløb)}" if pd.notna(beløb) and beløb is not None else ""
    label = str(row.get("label", "")) if row.get("label") is not None else ""
    return f"{dato_str}||{beløb_str}||{label}"


def categorize(row, rules: dict, overrides: dict | None = None) -> str:
    """Categorise a transaction row. Overrides take precedence over keyword rules."""
    if overrides:
        key = _override_key(row)
        if key in overrides:
            return overrides[key]
    text = " ".join(filter(None, [
        str(row.get("navn") or ""),
        str(row.get("beskrivelse") or ""),
        str(row.get("modtager") or ""),
        str(row.get("afsender") or ""),
    ])).lower()
    for cat, kws in rules.items():
        if cat == "Andet":
            continue
        if any(kw in text for kw in kws):
            return cat
    return "Andet"


def recategorize(df: pd.DataFrame, rules: dict, overrides: dict | None = None) -> pd.DataFrame:
    """Re-apply category rules and overrides to an already-parsed DataFrame."""
    df = df.copy()
    df["kategori"] = df.apply(lambda r: categorize(r, rules, overrides), axis=1)
    return df


def load_categories(config_dir: Path) -> dict:
    """Load category rules and overrides from JSON, seeding from CATEGORIES on first run."""
    cat_file = config_dir / "categories.json"
    if cat_file.exists():
        with open(cat_file, encoding="utf-8") as f:
            return json.load(f)
    return {"rules": dict(CATEGORIES), "overrides": {}}


def save_categories(config_dir: Path, data: dict):
    """Persist category rules and overrides to config/categories.json."""
    cat_file = config_dir / "categories.json"
    config_dir.mkdir(parents=True, exist_ok=True)
    with open(cat_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_reconciled(config_dir: Path) -> dict:
    """Load reconciliation state from config/reconciled.json.

    Keys use _override_key format: 'YYYY-MM-DD||beløb||label'.
    Values are booleans (True = transaction has been reviewed/reconciled).
    """
    rec_file = config_dir / "reconciled.json"
    if rec_file.exists():
        with open(rec_file, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_reconciled(config_dir: Path, data: dict):
    """Persist reconciliation state to config/reconciled.json."""
    rec_file = config_dir / "reconciled.json"
    config_dir.mkdir(parents=True, exist_ok=True)
    with open(rec_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _normalise(df: pd.DataFrame) -> pd.DataFrame | None:
    """Map raw Nordea columns → canonical schema, handle Reserveret rows, parse types."""
    rename = {}
    for col in df.columns:
        key = str(col).strip().lower()
        if key in NORDEA_COLS:
            rename[col] = NORDEA_COLS[key]
    df = df.rename(columns=rename)

    if "dato" not in df.columns or "beløb" not in df.columns:
        return None

    # Tag "Reserveret" rows (pending transactions — not yet booked)
    df["reserveret"] = df["dato"].astype(str).str.strip().str.lower() == "reserveret"

    # Parse dates on non-reserved rows
    date_str = df.loc[~df["reserveret"], "dato"].astype(str).str.replace("/", "-")
    df.loc[~df["reserveret"], "dato"] = pd.to_datetime(date_str, dayfirst=False, errors="coerce")
    df.loc[df["reserveret"], "dato"] = pd.NaT

    # Parse amounts
    df["beløb"] = df["beløb"].apply(_clean_amount)
    if "saldo" in df.columns:
        df["saldo"] = df["saldo"].apply(_clean_amount)

    # Drop rows without a usable amount
    df = df.dropna(subset=["beløb"])

    # Ensure all schema columns exist
    for col in ["afsender", "modtager", "navn", "beskrivelse", "valuta", "afstemt", "saldo"]:
        if col not in df.columns:
            df[col] = None

    # Direction
    df["type"] = df["beløb"].apply(lambda x: "Indkomst" if x > 0 else "Udgift")

    # Best display label: Beskrivelse > Navn > Modtager/Afsender
    def best_label(r):
        return (r.get("beskrivelse") or r.get("navn") or
                (r.get("modtager") if r["type"] == "Udgift" else r.get("afsender")) or "–")
    df["label"] = df.apply(best_label, axis=1)

    # Categorise using defaults — callers can recategorize() with custom rules afterwards
    df["kategori"] = df.apply(lambda r: categorize(r, CATEGORIES), axis=1)

    return df.sort_values("dato", ascending=False, na_position="first").reset_index(drop=True)


def parse_numbers_file(path) -> pd.DataFrame | None:
    try:
        from numbers_parser import Document
    except ImportError:
        subprocess.run(["pip", "install", "numbers-parser", "--break-system-packages", "-q"], check=True)
        from numbers_parser import Document

    doc   = Document(str(path))
    sheet = doc.sheets[0]
    table = sheet.tables[0]

    rows = [[cell.value for cell in row] for row in table.iter_rows()]
    if not rows:
        return None

    headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]
    records = [dict(zip(headers, row)) for row in rows[1:] if any(v is not None for v in row)]

    return _normalise(pd.DataFrame(records)) if records else None


def parse_csv_file(path_or_buffer) -> pd.DataFrame | None:
    """Parse a Nordea CSV. Accepts a file path (Path/str) or a file-like object (Streamlit UploadedFile)."""
    if hasattr(path_or_buffer, "read"):
        content = path_or_buffer.read()
    else:
        content = Path(path_or_buffer).read_bytes()

    text = None
    for enc in ["utf-8", "latin-1", "cp1252", "iso-8859-1"]:
        try:
            text = content.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        return None

    lines = text.strip().split("\n")
    sep   = ";" if lines and lines[0].count(";") >= lines[0].count(",") else ","
    try:
        df = pd.read_csv(io.StringIO(text), sep=sep, encoding_errors="replace")
    except pd.errors.EmptyDataError:
        return None
    df.columns = df.columns.str.strip()
    return _normalise(df)


def _save_tmp(buffer, filename) -> Path:
    tmp = Path("/tmp") / filename
    buffer.seek(0)
    tmp.write_bytes(buffer.read())
    return tmp


def parse_any(path_or_buffer, filename: str) -> pd.DataFrame | None:
    """Auto-detect file format and parse. filename is used for extension detection."""
    ext = Path(filename).suffix.lower()
    if ext == ".numbers":
        p = path_or_buffer if not hasattr(path_or_buffer, "read") else _save_tmp(path_or_buffer, filename)
        if "revolut" in filename.lower():
            return parse_revolut_numbers(p)
        return parse_numbers_file(p)
    elif ext in (".csv", ".txt"):
        return parse_csv_file(path_or_buffer)
    return None


def load_all_processed(processed_dir: Path) -> pd.DataFrame | None:
    """Load and deduplicate all processed files from processed_dir."""
    frames = []
    for pattern in ["*.csv", "*.numbers"]:
        for f in sorted(processed_dir.glob(pattern)):
            df = parse_any(f, f.name)
            if df is not None:
                df["_source_file"] = f.stem
                frames.append(df)
    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["dato", "beløb", "label"])
    return combined.sort_values("dato", ascending=False, na_position="first")


# ── AI context builder ────────────────────────────────────────────────────────

def build_context(df: pd.DataFrame, profile: dict | None = None, days: int | None = None, budgets: dict | None = None) -> str:
    """Build a financial summary string for Ollama context.

    Args:
        df:      Full transaction DataFrame (including reserveret rows).
        profile: Optional user profile dict from config/profile.json.
        days:    If set, filter to only the last N days (used by CLI reports).
        budgets: Optional monthly budget limits per category {cat: dkk}.
    """
    if days:
        cutoff = datetime.now() - timedelta(days=days)
        df = df[df["dato"].isna() | (df["dato"] >= pd.Timestamp(cutoff))]

    real     = df[~df["reserveret"]] if "reserveret" in df.columns else df
    reserved = df[df["reserveret"]]  if "reserveret" in df.columns else pd.DataFrame()

    if len(real) == 0:
        return "Ingen bogførte transaktioner i perioden."

    income  = real[real["beløb"] > 0]["beløb"].sum()
    expense = real[real["beløb"] < 0]["beløb"].sum()
    net     = income + expense
    months  = real["dato"].dropna().dt.to_period("M").nunique()

    by_cat  = real[real["beløb"] < 0].groupby("kategori")["beløb"].sum().sort_values()
    cat_str = "\n".join([f"  {c}: {abs(v):,.0f} DKK" for c, v in by_cat.items()])

    top_exp = real[real["beløb"] < 0].nsmallest(10, "beløb")
    top_str = "\n".join([
        f"  {r['dato'].strftime('%d/%m/%y') if pd.notna(r['dato']) else 'Reserv.'}"
        f" | {str(r['label'])[:38]:<38} | {r['beløb']:>10,.0f} DKK | {r['kategori']}"
        for _, r in top_exp.iterrows()
    ])

    res_str = ""
    if len(reserved) > 0:
        res_str = "\nRESERVEREDE (ikke bogført endnu):\n"
        for _, r in reserved.iterrows():
            res_str += (
                f"  {r['label']}: {r['beløb']:+,.0f} DKK\n"
                if pd.notna(r.get("beløb"))
                else f"  {r['label']}: beløb mangler\n"
            )

    ctx = f"""=== FINANSIEL DATA ===
Periode: {real['dato'].min().strftime('%d/%m/%Y')} — {real['dato'].max().strftime('%d/%m/%Y')}
Bogførte transaktioner: {len(real)} | Reserverede: {len(reserved)} | Måneder: {months}

PENGESTRØM:
  Indkomst:         {income:>12,.0f} DKK
  Udgifter:         {abs(expense):>12,.0f} DKK
  Netto:            {net:>+12,.0f} DKK
  Gns. pr. måned:   {net/max(months,1):>+12,.0f} DKK
  Opsparingsrate:   {net/income*100:.1f}%
{res_str}
UDGIFTER PR. KATEGORI:
{cat_str}

TOP 10 UDGIFTER:
{top_str}
"""
    if budgets:
        now = datetime.now()
        # Monthly budget period
        current_month = real[
            real["dato"].dt.year.eq(now.year) & real["dato"].dt.month.eq(now.month)
        ]
        by_cat_month = current_month[current_month["beløb"] < 0].groupby("kategori")["beløb"].sum().abs()

        # Quarterly budget period
        quarter = (now.month - 1) // 3
        quarter_start_month = quarter * 3 + 1
        current_quarter = real[
            real["dato"].dt.year.eq(now.year) &
            real["dato"].dt.month.ge(quarter_start_month) &
            real["dato"].dt.month.le(quarter_start_month + 2)
        ]
        by_cat_quarter = current_quarter[current_quarter["beløb"] < 0].groupby("kategori")["beløb"].sum().abs()

        budget_lines = []
        for cat, budget_val in sorted(budgets.items()):
            # Handle both old format (int) and new format (dict)
            if isinstance(budget_val, int):
                limit = budget_val
                freq = "monthly"
            else:
                limit = budget_val.get("limit", 0)
                freq = budget_val.get("frequency", "monthly")

            if limit <= 0:
                continue

            if freq == "quarterly":
                spent = by_cat_quarter.get(cat, 0)
                period_label = f"K{quarter+1} {now.year}"
            else:
                spent = by_cat_month.get(cat, 0)
                period_label = now.strftime("%B %Y")

            pct = spent / limit * 100
            budget_lines.append(f"  {cat}: {spent:,.0f} / {limit:,.0f} DKK ({pct:.0f}%) — {period_label}")

        if budget_lines:
            ctx += "\nBUDGETSTATUS:\n" + "\n".join(budget_lines) + "\n"

    if profile:
        ctx += f"""
=== BRUGERPROFIL ===
Alder: {profile.get('age','?')} | Indkomst: {profile.get('income','?')} DKK/md brutto | Formue: {profile.get('net_worth','?')} DKK
Familie: {profile.get('family','')} | Karriere: {profile.get('career','')}
Mål: {profile.get('goals','')} | Finansiel frihed: {profile.get('freedom','')}
"""
    return ctx


# ── Saxo Bank parsers ─────────────────────────────────────────────────────────

_MONTH_ABBR = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}

# Static ISIN → human-readable name for the known Saxo ASK ETFs.
# Falls back to raw ISIN if an unexpected holding appears.
_SAXO_ISIN_NAMES = {
    "IE00B4K48X80": "iShares Core MSCI Europe",
    "IE00B4L5Y983": "iShares Core MSCI World",
    "IE00B5BMR087": "iShares Core S&P 500",
    "IE00B4L5YC18": "iShares MSCI Emerging Markets",
}

_DK_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}


def _saxo_date_str(date_str: str) -> str:
    """Convert Saxo date format '17-Apr-2026' → ISO '2026-04-17'."""
    return datetime.strptime(date_str, "%d-%b-%Y").strftime("%Y-%m-%d")


def _months_in_period(period_start: str, period_end: str) -> list[str]:
    """Return ['2026-01', '2026-02', ...] for all months in the reporting period."""
    from datetime import date
    start = datetime.strptime(period_start, "%Y-%m-%d").date().replace(day=1)
    end   = datetime.strptime(period_end,   "%Y-%m-%d").date()
    months = []
    d = start
    while d <= end:
        months.append(d.strftime("%Y-%m"))
        d = d.replace(month=d.month + 1) if d.month < 12 else d.replace(year=d.year + 1, month=1)
    return months


def parse_saxo_pdf(path, config_dir: Path | None = None) -> dict:
    """Extract portfolio snapshot from a Saxo Bank PDF report.

    Parses pages 2, 4, 5, 6, 8 for: account metadata, total return %, monthly
    returns, per-ETF returns, holdings (ISIN / weight / prices), and cost ratio.

    Args:
        path:       Path to the Saxo PDF (str or Path).
        config_dir: If provided, writes result to config_dir/portfolio.json.

    Returns:
        Portfolio dict suitable for build_portfolio_context() and JSON storage.
    """
    try:
        import pypdf
    except ImportError:
        subprocess.run(["pip", "install", "pypdf", "--break-system-packages", "-q"], check=True)
        import pypdf

    reader = pypdf.PdfReader(str(path))
    pages  = [p.extract_text() or "" for p in reader.pages]

    p2 = pages[1]   # Account summary + total return %
    p4 = pages[3]   # Monthly returns table
    p5 = pages[4]   # Per-ETF P/L + %Return
    p6 = pages[5]   # Holdings with ISINs, prices, weights
    p8 = pages[7]   # Cost summary

    # ── Account metadata (page 2) ──────────────────────────────────────────────
    m = re.search(r"Currency:(\w+)", p2)
    currency = m.group(1) if m else "DKK"

    m = re.search(r"Account\(s\):(.+?)(?:\n|Page\d)", p2)
    account = m.group(1).strip() if m else "Aktiesparekonto"

    period_start = period_end = as_of = None
    m = re.search(r"(\d{2}-[A-Za-z]{3}-\d{4})-(\d{2}-[A-Za-z]{3}-\d{4})", p2)
    if m:
        period_start = _saxo_date_str(m.group(1))
        period_end   = _saxo_date_str(m.group(2))
        as_of        = period_end

    # "To t a lreturn\n5.13%" — pypdf splits "Total" with spaces; "return" is attached to "l"
    m = re.search(r"To\s*t\s*a\s*l\s*return\s*\n?([-\d.]+)%", p2)
    total_return_pct = float(m.group(1)) if m else None

    # ── Monthly returns (page 4) ───────────────────────────────────────────────
    # Match the data row: "%Return 2.1% 1.6% -6.8% 9.2% 5.7%"
    # (requires ≥2 consecutive percentages to skip the standalone "%Return" header)
    monthly_returns: dict[str, float] = {}
    m = re.search(r"%Return\s+((?:[-\d.]+%\s*){2,})", p4)
    if m and period_start and period_end:
        vals   = re.findall(r"([-\d.]+)%", m.group(1))
        months = _months_in_period(period_start, period_end)
        for month, val in zip(months, vals):   # zip stops at shorter list (months); total % dropped
            monthly_returns[month] = float(val)

    # ── Per-ETF %Return (page 5) ───────────────────────────────────────────────
    # Each line: "{SquishedName}UCITSETF {income} {costs} {pl} {return}%"
    etf_returns = [float(v) for v in re.findall(r"UCITSETF\s+[-\d.,]+\s+[-\d.,]+\s+[-\d.,]+\s+([-\d.]+)%", p5)]

    # ── Holdings (page 6) ─────────────────────────────────────────────────────
    # Pattern: "UCITSETF(ISIN:\n{ISIN})\nEUR\n Equity {qty} {conv} {open} {current} {chg}% {unrealized_pl} {market_value} {weight}%"
    holding_re = re.compile(
        r"UCITSETF\(ISIN:\n([A-Z]{2}[A-Z0-9]{10})\)\nEUR\n Equity "
        r"(\d+) ([\d.]+) ([\d.]+) ([\d.]+) ([-\d.]+)% [-\d.,]+ [\d,.]+ ([\d.]+)%"
    )
    holdings = []
    for i, hm in enumerate(holding_re.finditer(p6)):
        isin       = hm.group(1)
        # group(2) = quantity — not stored (redacted from context)
        conv_rate  = float(hm.group(3))
        open_price = float(hm.group(4))
        curr_price = float(hm.group(5))
        price_chg  = float(hm.group(6))
        weight_pct = float(hm.group(7))
        return_pct = etf_returns[i] if i < len(etf_returns) else None
        holdings.append({
            "name":              _SAXO_ISIN_NAMES.get(isin, isin),
            "isin":              isin,
            "currency":          "EUR",
            "weight_pct":        weight_pct,
            "return_pct":        return_pct,
            "open_price_eur":    open_price,
            "current_price_eur": curr_price,
            "price_change_pct":  price_chg,
            "eur_dkk_rate":      conv_rate,
        })

    # Cash position — summary row: "Cash - {market_value} {weight}%"
    m = re.search(r"Cash\s+-\s+[\d.,]+\s+([\d.]+)%", p6)
    if m:
        holdings.append({"name": "Cash", "isin": None, "currency": "DKK",
                         "weight_pct": float(m.group(1)), "return_pct": None})

    # ── Cost ratio (page 8) ────────────────────────────────────────────────────
    m = re.search(r"Costasapercentage\s+([-\d.]+)%", p8)
    cost_ratio = float(m.group(1)) if m else None

    portfolio = {
        "account":          account,
        "currency":         currency,
        "as_of":            as_of,
        "period":           {"start": period_start, "end": period_end},
        "total_return_pct": total_return_pct,
        "monthly_returns":  monthly_returns,
        "cost_ratio_pct":   cost_ratio,
        "holdings":         holdings,
    }

    if config_dir is not None:
        config_dir = Path(config_dir)
        config_dir.mkdir(parents=True, exist_ok=True)
        with open(config_dir / "portfolio.json", "w", encoding="utf-8") as f:
            json.dump(portfolio, f, indent=2, ensure_ascii=False)

    return portfolio


def parse_saxo_numbers(path) -> pd.DataFrame | None:
    """Parse a Saxo Bank .numbers account statement → normalised transaction DataFrame.

    Uses Sheet 1 ("Account Statement") which contains posting dates, event
    descriptions, and cash-flow amounts. The resulting DataFrame is compatible
    with the standard transaction pipeline (dato, beløb, label, kategori …).
    """
    try:
        from numbers_parser import Document
    except ImportError:
        subprocess.run(["pip", "install", "numbers-parser", "--break-system-packages", "-q"], check=True)
        from numbers_parser import Document

    doc = Document(str(path))
    sheet = doc.sheets[1] if len(doc.sheets) > 1 else doc.sheets[0]
    table = sheet.tables[0]
    rows  = [[cell.value for cell in row] for row in table.iter_rows()]
    if not rows:
        return None

    headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]
    records = [dict(zip(headers, r)) for r in rows[1:] if any(v is not None for v in r)]
    if not records:
        return None

    df = pd.DataFrame(records)

    col_map = {
        "Posting Date": "dato",
        "Value Date":   "value_date",
        "Event":        "beskrivelse",
        "Net Change":   "beløb",
        "Cash Balance": "saldo",
        "Comment":      "comment",
        "Account ID":   "account_id",
    }
    df = df.rename(columns={c: col_map.get(c, c) for c in df.columns})

    if "dato" in df.columns:
        df["dato"] = pd.to_datetime(df["dato"], dayfirst=True, errors="coerce")
    if "beløb" in df.columns:
        df["beløb"] = df["beløb"].apply(_clean_amount)
    if "saldo" in df.columns:
        df["saldo"] = df["saldo"].apply(_clean_amount)

    # Add standard columns so the DataFrame is pipeline-compatible
    df["navn"]       = None
    df["modtager"]   = None
    df["afsender"]   = None
    df["valuta"]     = "DKK"
    df["reserveret"] = False
    df["label"]      = df.get("beskrivelse", pd.Series(dtype=str)).astype(str)
    df["type"]       = df.get("beløb", pd.Series(dtype=float)).apply(
        lambda x: "Indkomst" if isinstance(x, (int, float)) and x > 0
                  else "Udgift" if isinstance(x, (int, float)) and x < 0
                  else "Andet"
    )
    df["kategori"] = "Overførsler"

    return df.sort_values("dato", ascending=False, na_position="first").reset_index(drop=True)


def _parse_revolut_date(val) -> "pd.Timestamp":
    """Parse Danish date '2. jan. 2026' → pd.Timestamp, or pd.NaT on failure."""
    if val is None:
        return pd.NaT
    s = str(val).strip()
    m = re.search(r"(\d+)\.\s*(\w+)\.?\s*(\d{4})", s)
    if not m:
        return pd.NaT
    day = int(m.group(1))
    month_name = m.group(2).lower()[:3]
    year = int(m.group(3))
    month = _DK_MONTH_MAP.get(month_name)
    if not month:
        return pd.NaT
    return pd.Timestamp(year, month, day)


def _parse_revolut_amount(val) -> float | None:
    """Parse Revolut amount '1.234,56 DKK' or '12,34€ (1.234,56 DKK)' → float (DKK)."""
    if val is None:
        return None
    s = str(val).strip()
    # EUR account format: "12,34€ (1.234,56 DKK)" — extract the DKK value in parens
    paren = re.search(r"\(([^)]+DKK)\)", s)
    if paren:
        s = paren.group(1)
    # Strip trailing currency label
    s = re.sub(r"\s*(DKK|SEK|EUR|€)\s*$", "", s, flags=re.IGNORECASE).strip()
    return _clean_amount(s)


def parse_revolut_numbers(path) -> "pd.DataFrame | None":
    """Parse a Revolut consolidated .numbers statement → normalised transaction DataFrame.

    The file contains multiple embedded transaction sections (one per sub-account).
    Sections are identified by a header row where column 0 == 'Dato'.
    Each Revolut row gets a ``_revolut_account`` column with the sub-account name.
    """
    try:
        from numbers_parser import Document
    except ImportError:
        subprocess.run(["pip", "install", "numbers-parser", "--break-system-packages", "-q"], check=True)
        from numbers_parser import Document

    doc   = Document(str(path))
    sheet = doc.sheets[0]
    table = sheet.tables[0]
    rows  = [[cell.value for cell in row] for row in table.iter_rows()]
    if not rows:
        return None

    dato_indices = [i for i, row in enumerate(rows) if str(row[0] or "").strip() == "Dato"]
    if not dato_indices:
        return None

    frames = []
    for h in dato_indices:
        # Scan backwards up to 30 rows for the sub-account name.
        # Skip generic section labels that appear every section.
        _SKIP_ACCOUNT_VALS = frozenset({
            "dato", "transaktionsopgørelse", "i alt", "---------", "",
            "transaction statement (only interest receipt)",
        })
        account_name = "Revolut"
        for back_i in range(h - 1, max(0, h - 30) - 1, -1):
            cell_val = str(rows[back_i][0] or "").strip()
            if cell_val.lower() not in _SKIP_ACCOUNT_VALS and not re.match(r"^\d", cell_val):
                account_name = f"Revolut {cell_val}"
                break

        # Map column names to indices from the header row
        header_row = [str(c or "").strip() for c in rows[h]]
        col_idx: dict[str, int] = {name: i for i, name in enumerate(header_row)}

        if "Dato" not in col_idx or "Indtægtsført/udgiftsført beløb" not in col_idx:
            continue

        dato_col     = col_idx["Dato"]
        beloeb_col   = col_idx["Indtægtsført/udgiftsført beløb"]
        besk_col     = col_idx.get("Beskrivelse")
        kat_col      = col_idx.get("Kategori")
        saldo_col    = col_idx.get("Saldo")

        # Collect transaction rows until empty row or next "Dato" header
        tx_rows = []
        for r_i in range(h + 1, len(rows)):
            row = rows[r_i]
            if all(v is None for v in row):
                break
            if str(row[0] or "").strip() == "Dato":
                break
            if row[dato_col] is None:
                continue
            tx_rows.append(row)

        if not tx_rows:
            continue

        records = []
        for row in tx_rows:
            dato = _parse_revolut_date(row[dato_col])
            if pd.isna(dato):
                continue  # skip "I alt" totals rows and unparseable dates
            beloeb = _parse_revolut_amount(row[beloeb_col])
            if beloeb is None:
                continue
            records.append({
                "dato":               dato,
                "beskrivelse":        str(row[besk_col] or "").strip() if besk_col is not None else "",
                "_revolut_kategori":  str(row[kat_col]  or "").strip() if kat_col  is not None else "",
                "beløb":              beloeb,
                "saldo":              _parse_revolut_amount(row[saldo_col]) if saldo_col is not None else None,
            })

        if not records:
            continue

        sec_df = pd.DataFrame(records)
        sec_df["navn"]              = None
        sec_df["modtager"]          = None
        sec_df["afsender"]          = None
        sec_df["valuta"]            = "DKK"
        sec_df["reserveret"]        = False
        sec_df["label"]             = sec_df["beskrivelse"].astype(str)
        sec_df["type"]              = sec_df["beløb"].apply(lambda x: "Indkomst" if x > 0 else "Udgift")
        sec_df["kategori"]          = sec_df.apply(lambda r: categorize(r, CATEGORIES), axis=1)
        sec_df["_revolut_account"]  = account_name
        frames.append(sec_df)

    if not frames:
        return None

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["dato", "beløb", "label"])
    return combined.sort_values("dato", ascending=False, na_position="first").reset_index(drop=True)


def fetch_gold_price_dkk(count: int) -> float | None:
    """Return total DKK value of ``count`` Centenario coins using live gold spot price.

    Uses Yahoo Finance (GC=F, gold futures USD/troy oz) + Frankfurter ECB rates.
    Both APIs are free and require no API key.
    Returns None on any network or parsing failure.
    """
    CENTENARIO_TROY_OZ = 1.20565  # pure gold content per Austrian 100-Corona coin
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF",
            params={"interval": "1d", "range": "1d"},
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        xau_usd = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        r2 = requests.get("https://api.frankfurter.app/latest?from=USD&to=DKK", timeout=5)
        usd_dkk = r2.json()["rates"]["DKK"]
        return round(count * CENTENARIO_TROY_OZ * xau_usd * usd_dkk, 0)
    except Exception:
        return None


def fetch_gold_history_dkk(count: int, period: str = "6mo") -> "pd.Series | None":
    """Return a daily pd.Series (date index → DKK value for ``count`` coins) over ``period``.

    Uses Yahoo Finance GC=F futures + Frankfurter ECB USD→DKK for the latest FX rate.
    Returns None on failure.
    """
    CENTENARIO_TROY_OZ = 1.20565
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF",
            params={"interval": "1d", "range": period},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        result = r.json()["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        r2 = requests.get("https://api.frankfurter.app/latest?from=USD&to=DKK", timeout=5)
        usd_dkk = r2.json()["rates"]["DKK"]
        dates = pd.to_datetime(timestamps, unit="s").normalize()
        values = [
            round(count * CENTENARIO_TROY_OZ * (c or 0) * usd_dkk, 0) if c else None
            for c in closes
        ]
        s = pd.Series(values, index=dates, name="DKK").dropna()
        return s if not s.empty else None
    except Exception:
        return None


def build_balance_sheet_context(profile: dict, gold_value_dkk: float | None = None) -> str:
    """Format the balance sheet section of the user profile for AI context.

    Covers assets, liabilities, insurance, pension, and unemployment benefits (dagpenge).
    Returns an empty string if none of the balance sheet fields are populated.
    """
    if not profile:
        return ""

    assets      = profile.get("assets", {})
    liabilities = profile.get("liabilities", [])
    insurance   = profile.get("insurance", {})
    pension     = profile.get("pension", {})
    dagpenge    = profile.get("dagpenge", {})

    if not any([assets, liabilities, insurance, pension, dagpenge]):
        return ""

    # ── Assets ────────────────────────────────────────────────────────────────
    liquidity    = assets.get("liquidity_dkk", 0) or 0
    investments  = assets.get("investments_dkk", 0) or 0
    pension_sav  = assets.get("pension_dkk", 0) or 0
    real_estate  = assets.get("real_estate_equity_dkk", 0) or 0
    other_assets = assets.get("other_dkk", 0) or 0
    gold_val     = gold_value_dkk or 0
    gold_count   = (profile.get("gold", {}) or {}).get("centenario_count", 0) or 0
    total_assets = liquidity + investments + pension_sav + real_estate + other_assets + gold_val

    asset_lines = []
    if liquidity:    asset_lines.append(f"  Likviditet (kontanter/opsparing): {liquidity:,.0f} DKK")
    if investments:  asset_lines.append(f"  Investeringer (aktier/fonde):     {investments:,.0f} DKK")
    if pension_sav:  asset_lines.append(f"  Pension (arbejdsmarkeds + privat):{pension_sav:,.0f} DKK")
    if real_estate:  asset_lines.append(f"  Fast ejendom (friværdi):          {real_estate:,.0f} DKK")
    if other_assets: asset_lines.append(f"  Andet:                            {other_assets:,.0f} DKK")
    if gold_val:     asset_lines.append(f"  Guld (Centenario \xd7 {gold_count}):          {gold_val:,.0f} DKK (live kurs)")

    # ── Liabilities ───────────────────────────────────────────────────────────
    total_liab = sum((d.get("balance_dkk") or 0) for d in liabilities)
    liab_lines = []
    for d in liabilities:
        bal  = d.get("balance_dkk") or 0
        if not bal:
            continue
        name = d.get("name", "Gæld")
        rate = d.get("interest_rate_pct")
        yrs  = d.get("years_remaining")
        note = ""
        if rate is not None:
            note += f" (rente {rate}%"
            if yrs:
                note += f", {yrs} år tilbage"
            note += ")"
        liab_lines.append(f"  {name}{note}: {bal:,.0f} DKK")

    net_worth = total_assets - total_liab

    ctx = f"""
=== BALANCE SHEET ===
AKTIVER:
{chr(10).join(asset_lines) if asset_lines else "  (ikke udfyldt)"}
  Total aktiver: {total_assets:,.0f} DKK

PASSIVER:
{chr(10).join(liab_lines) if liab_lines else "  (ingen registreret gæld)"}
  Total passiver: {total_liab:,.0f} DKK

NETTO FORMUE: {net_worth:,.0f} DKK
"""

    # ── Insurance ─────────────────────────────────────────────────────────────
    ins_lines = []
    life = insurance.get("life_dkk")
    if life:                                ins_lines.append(f"  Livsforsikring: {life:,.0f} DKK dækning")
    if insurance.get("critical_illness"):   ins_lines.append("  Kritisk sygdom: Ja")
    if insurance.get("home"):               ins_lines.append("  Indboforsikring: Ja")
    if ins_lines:
        ctx += "FORSIKRINGER:\n" + "\n".join(ins_lines) + "\n"

    # ── Pension ───────────────────────────────────────────────────────────────
    pen_lines = ["  ATP-bidrag: automatisk via arbejdsgiver"]
    emp_pct  = pension.get("employer_contribution_pct")
    priv_dkk = pension.get("private_contribution_dkk")
    ret_age  = pension.get("target_retirement_age")
    if emp_pct:  pen_lines.append(f"  Arbejdsgiverbidrag: {emp_pct}%/md")
    if priv_dkk: pen_lines.append(f"  Privat bidrag: {priv_dkk:,.0f} DKK/md")
    if ret_age:  pen_lines.append(f"  Pensionsalder-mål: {ret_age} år")
    if len(pen_lines) > 1 or emp_pct or priv_dkk or ret_age:
        ctx += "PENSION:\n" + "\n".join(pen_lines) + "\n"

    # ── Unemployment benefits (dagpenge) ───────────────────────────────────────
    dag_gross  = dagpenge.get("monthly_gross_dkk")
    dag_net    = dagpenge.get("monthly_net_dkk")
    dag_weeks  = dagpenge.get("max_weeks")
    if dag_gross or dag_net:
        dag_lines = []
        if dag_gross:
            dag_lines.append(f"  Dagpenge brutto: {dag_gross:,.0f} DKK/md")
        if dag_net:
            dag_lines.append(f"  Dagpenge netto (efter skat): {dag_net:,.0f} DKK/md")
        if dag_weeks:
            dag_lines.append(f"  Maks. udbetalingsperiode: {dag_weeks} uger ({dag_weeks // 4} mdr.)")
        ctx += "SIKKERHEDSNET (ARBEJDSLØSHEDSDAGPENGE):\n" + "\n".join(dag_lines) + "\n"

    return ctx


def build_portfolio_context(portfolio: dict) -> str:
    """Format a Saxo portfolio snapshot for AI context.

    Output contains only percentage figures and prices (EUR) — no absolute DKK
    amounts — making it safe to pass directly to the Claude API.
    """
    if not portfolio:
        return ""

    monthly = portfolio.get("monthly_returns", {})
    m_lines = [f"  {month}: {val:+}%" for month, val in sorted(monthly.items())]

    h_lines = []
    for h in portfolio.get("holdings", []):
        name = h.get("name", h.get("isin", "?"))
        if h.get("isin") is None:          # Cash position
            h_lines.append(f"  {name}: {h.get('weight_pct', '?')}% vægt")
            continue
        price_chg = h.get("price_change_pct")
        price_chg_str = f"{price_chg:+.2f}%" if price_chg is not None else "?"
        h_lines.append(
            f"  {name} ({h['isin']}): {h.get('weight_pct', '?')}% vægt, "
            f"{h.get('return_pct', '?')}% YTD, "
            f"kurs {h.get('current_price_eur', '?')} EUR ({price_chg_str} ændring)"
        )

    return f"""
=== INVESTERINGSPORTEFØLJE ===
Konto: {portfolio.get('account', '?')} | Valuta: {portfolio.get('currency', 'DKK')}
Pr. dato: {portfolio.get('as_of', '?')} | Periode: {portfolio.get('period', {}).get('start', '?')} — {portfolio.get('period', {}).get('end', '?')}
Samlet YTD afkast: {portfolio.get('total_return_pct', '?')}%
Omkostningsprocent: {portfolio.get('cost_ratio_pct', '?')}%

MÅNEDLIGE AFKAST:
{chr(10).join(m_lines) if m_lines else '  (ingen data)'}

BEHOLDNINGER:
{chr(10).join(h_lines) if h_lines else '  (ingen data)'}
"""
