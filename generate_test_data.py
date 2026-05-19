#!/usr/bin/env python3
"""
generate_test_data.py — Synthetic Nordea transaction data generator

Generates a realistic CSV file matching the exact Nordea export schema:
  Bogføringsdato;Beløb;Afsender;Modtager;Navn;Beskrivelse;Saldo;Valuta;Afstemt;

Run:
  python generate_test_data.py                        # default: 60 days, ~80 transactions
  python generate_test_data.py --days 90 --count 120 # custom range
  python generate_test_data.py --seed 42              # reproducible output

Output: data/inbox/test_data.csv
"""

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path


# ── Merchant table ────────────────────────────────────────────────────────────
# (navn, kategori, typical_amount, frequency_weight)
# Negative amount = debit (money out). Positive = credit (money in).
MERCHANTS = [
    # Dagligvarer
    ("REMA 1000 AMAGER",      "Dagligvarer",       -310,   8),
    ("Netto Amager Centret",  "Dagligvarer",       -220,   6),
    ("LIDL AMAGERBROGADE",    "Dagligvarer",       -180,   4),
    ("Føtex City",            "Dagligvarer",       -420,   3),
    ("COOP EXTRA SYDHAVN",    "Dagligvarer",       -260,   3),

    # Transport
    ("DSB",                   "Transport",          -98,   5),
    ("Rejsekort A/S",         "Transport",         -200,   4),
    ("Uber",                  "Transport",         -145,   3),
    ("Circle K AMAGER",       "Transport",         -580,   2),

    # Restaurant & Café
    ("Starbucks Fisketorvet",  "Restaurant & Café",  -65,  3),
    ("Jagger Burger",          "Restaurant & Café", -185,  2),
    ("Joe and the Juice",      "Restaurant & Café",  -75,  3),
    ("Hija de Sanchez",        "Restaurant & Café", -210,  2),
    ("Wolt",                   "Restaurant & Café", -160,  3),

    # Abonnementer (fixed monthly)
    ("Netflix",               "Abonnementer",      -109,   1),
    ("Spotify",               "Abonnementer",      -109,   1),
    ("FLEXII",                "Abonnementer",      -199,   1),
    ("GitHub",                "Abonnementer",       -57,   1),
    ("OpenAI",                "Abonnementer",      -160,   1),

    # Sundhed & Fitness
    ("Zone Fitness Amager",   "Sundhed & Fitness", -299,   1),
    ("Apoteket Amager",       "Sundhed & Fitness",  -89,   2),

    # Shopping
    ("Vinted",                "Shopping",           -85,   3),
    ("ROEDE KORS BUTIK",      "Shopping",           -75,   2),
    ("TIPSTER.DK",            "Shopping",          -199,   1),
    ("Zalando",               "Shopping",          -495,   2),
    ("Amazon EU",             "Shopping",          -349,   2),

    # Bolig
    ("EL-regning RADIUS",     "Bolig",             -650,   1),
    ("HOFOR Vand",            "Bolig",             -280,   1),

    # MobilePay
    ("MobilePay Jacob",       "MobilePay",         -400,   2),
    ("MobilePay Nadia",       "MobilePay",         -250,   3),

    # Indkomst
    ("Løn Kereby Aps",        "Løn & Indkomst",  35000,    1),
    ("Dagpenge Udbetaling DK","Løn & Indkomst",  18500,    1),
]

# Fixed monthly subscriptions — billed on a consistent day
FIXED_MONTHLY = {
    "Netflix":            (1,  -109),
    "Spotify":            (1,  -109),
    "FLEXII":             (3,  -199),
    "GitHub":             (5,   -57),
    "OpenAI":             (5,  -160),
    "Zone Fitness Amager":(1,  -299),
    "EL-regning RADIUS":  (15, -650),
    "HOFOR Vand":         (15, -280),
    "Løn Kereby Aps":     (25, 35000),
    "Dagpenge Udbetaling DK": (10, 18500),
}

HUSLEJE = ("Husleje PBS - Andelsbolig Amagerbro", "Bolig", 1, -7800)


def fmt_dk(amount: float) -> str:
    """Format float as Danish number string: 1234.56 → '1.234,56'"""
    s = f"{abs(amount):,.2f}"           # '1,234.56' (en_US locale-style)
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")  # → '1.234,56'
    return f"-{s}" if amount < 0 else s


def generate(days: int, count: int, seed: int) -> list[list]:
    random.seed(seed)

    end   = date(2026, 4, 17)
    start = end - timedelta(days=days)

    # ── Build a transaction pool ──────────────────────────────────────────────
    transactions: list[tuple] = []  # (date, amount, navn, beskrivelse, afsender, modtager)

    # Fixed monthly: add one occurrence per month in the window
    months_in_range = set()
    d = start
    while d <= end:
        months_in_range.add((d.year, d.month))
        d += timedelta(days=1)

    for navn, (day_of_month, amount) in FIXED_MONTHLY.items():
        for yr, mo in sorted(months_in_range):
            try:
                tx_date = date(yr, mo, day_of_month)
            except ValueError:
                tx_date = date(yr, mo, 28)
            if start <= tx_date <= end:
                afs = "Nikolas Nogueira Sønderborg" if amount > 0 else ""
                mod = navn if amount < 0 else ""
                transactions.append((tx_date, amount, navn, navn, afs, mod))

    # Husleje
    navn_h, _, day_h, amt_h = HUSLEJE
    for yr, mo in sorted(months_in_range):
        try:
            tx_date = date(yr, mo, day_h)
        except ValueError:
            tx_date = date(yr, mo, 28)
        if start <= tx_date <= end:
            transactions.append((tx_date, amt_h, navn_h, navn_h, "", navn_h))

    # Variable transactions up to count
    variable_merchants = [m for m in MERCHANTS if m[0] not in FIXED_MONTHLY]
    weights = [m[3] for m in variable_merchants]
    target  = max(0, count - len(transactions))

    for _ in range(target):
        navn, _, base_amount, _ = random.choices(variable_merchants, weights=weights, k=1)[0]
        amount   = round(base_amount * random.uniform(0.75, 1.30), 2)
        tx_date  = start + timedelta(days=random.randint(0, days))
        afsender = "Nikolas Nogueira Sønderborg" if amount > 0 else ""
        modtager = navn if amount < 0 else ""
        transactions.append((tx_date, amount, navn, navn, afsender, modtager))

    # Sort by date descending (newest first, matching Nordea export order)
    transactions.sort(key=lambda x: x[0], reverse=True)

    # ── Compute running saldo (newest first, so we work backwards) ────────────
    # Seed balance at a realistic starting point
    saldo = round(random.uniform(38000, 55000), 2)
    rows  = []

    for tx_date, amount, navn, beskrivelse, afsender, modtager in transactions:
        rows.append([
            tx_date.strftime("%Y/%m/%d"),
            fmt_dk(amount),
            afsender,
            modtager,
            navn,
            beskrivelse,
            fmt_dk(saldo),
            "DKK",
            "",   # Afstemt — always empty in current data
            "",   # col_9 — phantom trailing column from Numbers export
        ])
        saldo = round(saldo - amount, 2)  # walk backwards

    return rows


def main():
    parser = argparse.ArgumentParser(description="Generate synthetic Nordea test data")
    parser.add_argument("--days",  type=int, default=60,  help="Number of days to cover (default: 60)")
    parser.add_argument("--count", type=int, default=80,  help="Approx. number of transactions (default: 80)")
    parser.add_argument("--seed",  type=int, default=2026, help="Random seed for reproducibility (default: 2026)")
    parser.add_argument("--out",   type=str, default="data/inbox/test_data.csv", help="Output path")
    args = parser.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    header = ["Bogføringsdato", "Beløb", "Afsender", "Modtager", "Navn", "Beskrivelse", "Saldo", "Valuta", "Afstemt", ""]

    # One Reserveret row at the top (pending transaction — no amount yet)
    reserveret_row = ["Reserveret", "", "", "", "", "FLEXII", "", "DKK", "", ""]

    rows = generate(args.days, args.count, args.seed)

    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(header)
        writer.writerow(reserveret_row)
        writer.writerows(rows)

    n_income  = sum(1 for r in rows if r[1] and not r[1].startswith("-"))
    n_expense = sum(1 for r in rows if r[1] and r[1].startswith("-"))

    print(f"Written to {out}")
    print(f"  1 Reserveret row (FLEXII, no amount)")
    print(f"  {n_income} credit transactions (indkomst)")
    print(f"  {n_expense} debit transactions (udgifter)")
    print(f"  {len(rows)} total rows")
    print(f"  Period: {rows[-1][0]} → {rows[0][0]}")
    print()
    print("Next step:")
    print("  streamlit run app.py")
    print("  → Upload data/inbox/test_data.csv, or click 'Scan inbox-mappe'")


if __name__ == "__main__":
    main()
