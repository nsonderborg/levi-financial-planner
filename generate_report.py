#!/usr/bin/env python3
"""
Nordea Financial Report Generator — cron-ready CLI

Cron examples:
  Daily inbox scan at 09:00:
    0 9 * * *   /path/to/venv/bin/python /path/to/generate_report.py --type scan

  Weekly summary every Monday at 07:30:
    30 7 * * 1  /path/to/venv/bin/python /path/to/generate_report.py --type weekly

  Monthly report on the 1st at 08:00:
    0 8 1 * *   /path/to/venv/bin/python /path/to/generate_report.py --type monthly

  All-in-one (scan + weekly + monthly) on the 1st:
    0 8 1 * *   /path/to/venv/bin/python /path/to/generate_report.py --type all
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import requests

from nordea_parser import parse_any, load_all_processed, build_context, load_categories, recategorize

BASE_DIR     = Path(__file__).parent
INBOX        = BASE_DIR / "data/inbox"
PROCESSED    = BASE_DIR / "data/processed"
REPORTS      = BASE_DIR / "data/reports"
PROFILE_FILE = BASE_DIR / "config/profile.json"
OLLAMA_URL   = "http://localhost:11434/api/chat"

for d in [INBOX, PROCESSED, REPORTS, BASE_DIR / "config"]:
    d.mkdir(parents=True, exist_ok=True)


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ── Ollama ────────────────────────────────────────────────────────────────────

def ask_ollama(model, prompt, system=None):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    try:
        r = requests.post(OLLAMA_URL, json={"model": model, "messages": msgs, "stream": False}, timeout=300)
        if r.status_code == 200:
            return r.json()["message"]["content"]
        return f"Ollama fejl {r.status_code}"
    except Exception as e:
        return f"Fejl: {e}"


def get_default_model():
    try:
        r      = requests.get("http://localhost:11434/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        for pref in ["qwen2.5:7b", "llama3.1", "llama3.2", "mistral"]:
            for m in models:
                if pref in m:
                    return m
        return models[0] if models else "llama3.1"
    except Exception:
        return "llama3.1"


# ── Report functions ──────────────────────────────────────────────────────────

def scan_inbox():
    moved = 0
    for pattern in ["*.csv", "*.numbers"]:
        for f in INBOX.glob(pattern):
            df = parse_any(f, f.name)
            if df is not None:
                n_res  = df["reserveret"].sum() if "reserveret" in df.columns else 0
                n_real = len(df) - n_res
                dest   = PROCESSED / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{f.name}"
                shutil.move(str(f), str(dest))
                log(f"✓ {f.name} → {dest.name} ({n_real} bogførte + {n_res} reserverede)")
                moved += 1
            else:
                log(f"⚠ Kunne ikke parse {f.name} — springer over")
    if moved == 0:
        log("Ingen nye filer i inbox")
    return moved


def run_monthly(model, df, profile):
    log("Genererer månedlig rapport...")
    ctx = build_context(df, profile, days=35)

    prompt = f"""Lav en struktureret månedlig finansiel rapport baseret på disse data:

{ctx}

FORMAT:
## Månedlig Finansiel Rapport — {datetime.now().strftime('%B %Y')}

### Resumé
(3 præcise bullet points om måneden)

### Pengestrøm
(Indkomst, udgifter, netto — sammenlign med hvad der er normalt)

### Kategorianalyse
(Hvad stikker ud? Hvad er bekymrende? Hvad er godt?)

### Opsparingsstatus
(Nuværende opsparingsrate — er den på rette vej?)

### Abonnementscheck
(List alle abonnementer med beløb — er der noget at skære?)

### Reserverede posteringer
(Kommenter på eventuelle reserverede posteringer der endnu ikke er bogført)

### 3 handlinger til næste måned
(Konkrete, målbare — med estimeret DKK-impact)

### På-sporet score: X/10
(Begrundelse + hvad der skal til for at komme til 10)"""

    system = "Du er en præcis finansanalytiker. Konkret, direkte, brug tal. Svar på dansk."
    report = ask_ollama(model, prompt, system)

    fname = REPORTS / f"rapport_{datetime.now().strftime('%Y%m')}.md"
    fname.write_text(
        f"# Månedlig Finansiel Rapport\nGenereret: {datetime.now().strftime('%d/%m/%Y %H:%M')}\nModel: {model}\n\n{report}",
        encoding="utf-8"
    )
    log(f"Rapport gemt: {fname}")
    return fname


def run_weekly(model, df, profile):
    log("Genererer ugentlig sammenfatning...")
    ctx = build_context(df, profile, days=8)

    prompt = f"""Lav en kort ugentlig finansiel sammenfatning.

{ctx}

FORMAT:
## Ugentlig Oversigt — uge {datetime.now().isocalendar()[1]}, {datetime.now().year}

### Ugens 3 nøgletal
### Hvad gik godt?
### Hvad skal jeg være opmærksom på?
### Ét konkret fokuspunkt denne uge

Hold det kort — max 300 ord."""

    system = "Du er en personlig finanscoach. Kort, præcis, handlingsorienteret. Svar på dansk."
    report = ask_ollama(model, prompt, system)

    fname = REPORTS / f"uge_{datetime.now().strftime('%Y_W%V')}.md"
    fname.write_text(
        f"# Ugentlig Oversigt\nGenereret: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n{report}",
        encoding="utf-8"
    )
    log(f"Ugentlig rapport gemt: {fname}")
    return fname


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nordea Financial Report Generator")
    parser.add_argument("--type",  choices=["monthly", "weekly", "scan", "all"], default="monthly")
    parser.add_argument("--model", default=None, help="Ollama model (auto-detect hvis ikke angivet)")
    args = parser.parse_args()

    model   = args.model or get_default_model()
    profile = json.load(open(PROFILE_FILE, encoding="utf-8")) if PROFILE_FILE.exists() else {}
    log(f"Model: {model}")

    if args.type in ["scan", "all"]:
        scan_inbox()

    df = load_all_processed(PROCESSED)

    if df is not None:
        cat_data = load_categories(BASE_DIR / "config")
        df = recategorize(df, cat_data["rules"], cat_data.get("overrides", {}))

    if df is None or len(df) == 0:
        if args.type == "scan":
            log("Scan færdig. Ingen data at rapportere på.")
            sys.exit(0)
        log("FEJL: Ingen behandlede data. Upload CSV- eller .numbers-filer til data/inbox/ først.")
        sys.exit(1)

    real = df[~df["reserveret"]] if "reserveret" in df.columns else df
    log(f"Data: {len(real)} bogførte posteringer fra {real['dato'].min().strftime('%d/%m/%Y')} til {real['dato'].max().strftime('%d/%m/%Y')}")

    if args.type in ["monthly", "all"]:
        run_monthly(model, df, profile)

    if args.type in ["weekly", "all"]:
        run_weekly(model, df, profile)

    log("Færdig.")


if __name__ == "__main__":
    main()
