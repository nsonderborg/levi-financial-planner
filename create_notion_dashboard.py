#!/usr/bin/env python3
"""
create_notion_dashboard.py — Build a Goldman Sachs / Morgan Stanley themed
Notion dashboard for Levi financial data.

First run  : creates the dashboard page, stores its ID in .env as
             NOTION_DASHBOARD_PAGE_ID so subsequent runs update it.
Later runs : archives all old blocks and rewrites the dashboard in-place.

Usage:
  python create_notion_dashboard.py          # create or refresh
  python create_notion_dashboard.py --force  # force-create a new page
"""

import json
import os
import sys
import argparse
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv, set_key

load_dotenv()

BASE_DIR       = Path(__file__).parent
CONFIG_DIR     = BASE_DIR / "config"
PORTFOLIO_FILE = CONFIG_DIR / "portfolio.json"
ENV_FILE       = BASE_DIR / ".env"

NOTION_KEY        = os.getenv("NOTION_INTEGRATION_KEY")
NOTION_DB_ID      = os.getenv("NOTION_DB_ID")
DASHBOARD_PAGE_ID = os.getenv("NOTION_DASHBOARD_PAGE_ID")

NOTION_VERSION = "2022-06-28"


# ── Low-level Notion HTTP helpers ──────────────────────────────────────────────

def _headers():
    return {
        "Authorization": f"Bearer {NOTION_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _notion(method: str, path: str, **kwargs) -> dict:
    r = requests.request(
        method, f"https://api.notion.com/v1{path}",
        headers=_headers(), timeout=30, **kwargs
    )
    if not r.ok:
        raise RuntimeError(f"Notion {method} {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Block builder helpers ──────────────────────────────────────────────────────

def _rt(*segments) -> list:
    """
    Build a rich_text list from (text, **annotations) tuples or plain strings.
    e.g. _rt("hello"), _rt(("bold text", {"bold": True}), " normal")
    """
    out = []
    for seg in segments:
        if isinstance(seg, str):
            out.append({"type": "text", "text": {"content": seg}, "annotations": {}})
        else:
            text, ann = seg
            out.append({"type": "text", "text": {"content": text}, "annotations": ann})
    return out


def _b(btype: str, **kwargs) -> dict:
    return {"object": "block", "type": btype, btype: kwargs}


def h1(text: str, color: str = "default") -> dict:
    return _b("heading_1", rich_text=_rt(text), color=color)


def h2(text: str, color: str = "default") -> dict:
    return _b("heading_2", rich_text=_rt(text), color=color)


def h3(text: str, color: str = "default") -> dict:
    return _b("heading_3", rich_text=_rt(text), color=color)


def divider() -> dict:
    return _b("divider")


def paragraph(text: str, color: str = "default") -> dict:
    return _b("paragraph", rich_text=_rt((text, {"color": color})))


def quote(text: str) -> dict:
    return _b("quote", rich_text=_rt(text))


def bullet(text: str, bold: bool = False) -> dict:
    ann = {"bold": bold} if bold else {}
    return _b("bulleted_list_item", rich_text=_rt((text, ann)))


def callout(emoji: str, title: str, value: str, subtitle: str = "",
            color: str = "gray_background") -> dict:
    rich = [
        {"type": "text", "text": {"content": title + "\n"}, "annotations": {"bold": True, "color": "gray"}},
        {"type": "text", "text": {"content": value},        "annotations": {"bold": True, "code": True}},
    ]
    if subtitle:
        rich.append({"type": "text", "text": {"content": f"\n{subtitle}"}, "annotations": {"color": "gray", "italic": True}})
    return {
        "object": "block", "type": "callout",
        "callout": {"icon": {"type": "emoji", "emoji": emoji}, "color": color, "rich_text": rich},
    }


def columns(*cols: list) -> dict:
    """column_list wrapping each col (list of blocks) in a column block."""
    return {
        "object": "block", "type": "column_list",
        "column_list": {
            "children": [
                {"object": "block", "type": "column", "column": {"children": list(c)}}
                for c in cols
            ]
        },
    }


def table_row(cells: list) -> dict:
    return {
        "type": "table_row",
        "table_row": {"cells": [[{"type": "text", "text": {"content": str(c)}}] for c in cells]},
    }


def table(headers: list, rows: list) -> dict:
    return {
        "object": "block", "type": "table",
        "table": {
            "table_width": len(headers),
            "has_column_header": True,
            "has_row_header": False,
            "children": [table_row(headers)] + [table_row(r) for r in rows],
        },
    }


# ── Data helpers ───────────────────────────────────────────────────────────────

def _num(props, key):  return props.get(key, {}).get("number") or 0
def _sel(props, key):  return (props.get(key, {}).get("select") or {}).get("name", "")
def _date(props, key): return (props.get(key, {}).get("date") or {}).get("start", "")
def _rtt(props, key):
    return "".join(
        b.get("text", {}).get("content", "")
        for b in props.get(key, {}).get("rich_text", [])
    )


def fetch_entries(n: int = 6) -> list[dict]:
    result = _notion("POST", f"/databases/{NOTION_DB_ID}/query",
                     json={"sorts": [{"property": "Måned", "direction": "descending"}],
                           "page_size": n})
    return [r["properties"] for r in result.get("results", [])]


def parse_entry(props: dict) -> dict:
    return {
        "maaned":         _date(props, "Måned"),
        "indkomst":       _num(props, "Indkomst"),
        "udgifter":       _num(props, "Udgifter"),
        "opsparingsrate": _num(props, "Opsparingsrate"),
        "afkast":         _num(props, "Afkast YTD"),
        "portefolje":     _num(props, "Porteføljeværdi"),
        "budget_status":  _sel(props, "Budget-status"),
        "analyse":        _rtt(props, "Claude-analyse"),
        "handlinger":     _rtt(props, "Handlinger"),
    }


def load_portfolio() -> dict:
    if PORTFOLIO_FILE.exists():
        with open(PORTFOLIO_FILE) as f:
            return json.load(f)
    return {}


# ── Dashboard block builder ────────────────────────────────────────────────────

def fmt_dkk(n):  return f"{int(n):,} kr".replace(",", ".")
def fmt_pct(n):  return f"{n:+.1f} %" if n else "—"
def fmt_pct_plain(n): return f"{n:.1f} %" if n else "—"

STATUS_EMOJI = {"På sporet": "🟢", "Marginal": "🟡", "Overskred": "🔴", "Ingen budget": "⚪"}


def build_blocks(entries: list[dict], portfolio: dict) -> list[dict]:
    latest   = entries[0]
    net      = latest["indkomst"] - latest["udgifter"]
    s_emoji  = STATUS_EMOJI.get(latest["budget_status"], "⚪")
    afkast_c = "green_background" if (latest["afkast"] or 0) >= 0 else "red_background"

    blocks: list[dict] = []

    # ── PAGE HEADER ────────────────────────────────────────────────────────────
    blocks += [
        {
            "object": "block", "type": "heading_1",
            "heading_1": {
                "rich_text": [
                    {"type": "text", "text": {"content": "⚡  LEVI"},
                     "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": "   ·   Financial Command Center"},
                     "annotations": {"color": "gray"}},
                ],
                "color": "default",
            },
        },
        paragraph(
            f"Opdateret {datetime.now().strftime('%d/%m/%Y %H:%M')}   ·   "
            f"Seneste periode: {latest['maaned'] or '—'}",
            color="gray",
        ),
        divider(),
    ]

    # ── GOLDMAN SACHS ──────────────────────────────────────────────────────────
    blocks += [
        h2("Goldman Sachs  ·  Wealth Diagnostic", color="blue_background"),
        paragraph("Månedlig finansiel sundhedstjek baseret på dine reelle bogførte tal.", color="gray"),
    ]

    # Row 1 — Income / Expenses / Savings rate
    blocks.append(columns(
        [callout("💰", "INDKOMST",      fmt_dkk(latest["indkomst"]),       "Bogførte posteringer",    "blue_background")],
        [callout("📤", "UDGIFTER",      fmt_dkk(latest["udgifter"]),       "Ekskl. reservationer",    "red_background")],
        [callout("📊", "OPSPARINGSRATE", fmt_pct_plain(latest["opsparingsrate"]), "Af brutto indkomst", "green_background")],
    ))

    # Row 2 — Net savings / Budget status
    blocks.append(columns(
        [callout("🎯", "NETTO OPSPARING",  fmt_dkk(net),                    "Indkomst − udgifter",   "purple_background")],
        [callout(s_emoji, "BUDGET STATUS", latest["budget_status"] or "—",  "Aktive budgetkategorier", "yellow_background")],
    ))

    # AI analysis
    if latest["analyse"]:
        blocks += [
            h3("📋  Månedlig Analyse"),
            {
                "object": "block", "type": "callout",
                "callout": {
                    "icon": {"type": "emoji", "emoji": "🏦"},
                    "color": "gray_background",
                    "rich_text": [{"type": "text",
                                   "text": {"content": latest["analyse"][:2000]}}],
                },
            },
        ]
        # Overflow beyond 2000 chars (Notion per-segment limit)
        rest = latest["analyse"][2000:]
        while rest:
            blocks.append(paragraph(rest[:2000]))
            rest = rest[2000:]

    # Action items
    if latest["handlinger"]:
        blocks.append(h3("🎯  Prioriterede Handlinger"))
        for line in latest["handlinger"].splitlines():
            line = line.strip()
            if line:
                blocks.append(bullet(line, bold=bool(line[:2] in ("A1", "A2", "A3"))))

    blocks.append(divider())

    # ── MORGAN STANLEY ─────────────────────────────────────────────────────────
    blocks += [
        h2("Morgan Stanley  ·  Portfolio Architect", color="blue_background"),
        paragraph("Investeringsportefølje — afkast, allokering og risikoprofil.", color="gray"),
    ]

    # Portfolio KPIs
    if latest["portefolje"] or latest["afkast"]:
        blocks.append(columns(
            [callout("🏦", "PORTEFØLJEVÆRDI",
                     fmt_dkk(latest["portefolje"]) if latest["portefolje"] else "—",
                     "Aktiesparekonto (ASK)", "blue_background")],
            [callout("📈", "AFKAST YTD",
                     fmt_pct(latest["afkast"]),
                     "Year-to-date totalafkast", afkast_c)],
        ))
    else:
        blocks.append(callout("ℹ️", "PORTEFØLJE", "Ingen data endnu",
                              "Upload Saxo PDF og kør sync_to_notion.py igen", "gray_background"))

    # Holdings from portfolio.json
    if portfolio and portfolio.get("holdings"):
        blocks.append(h3("📊  Aktuel Allokering"))
        hdrs = ["Aktiv", "Vægt %", "Afkast YTD", "Kurs (EUR)"]
        rows = []
        for h in portfolio["holdings"]:
            if not h.get("isin"):  # Cash
                rows.append(["💵 Cash", f"{h.get('weight_pct', 0):.1f} %", "—", "—"])
            else:
                ret = h.get("return_pct")
                price = h.get("current_price_eur")
                rows.append([
                    h.get("name", h["isin"]),
                    f"{h.get('weight_pct', 0):.1f} %",
                    f"{ret:+.2f} %" if ret is not None else "—",
                    f"{price:.2f}" if price else "—",
                ])
        blocks.append(table(hdrs, rows))

        period = portfolio.get("period", {})
        if period.get("start"):
            blocks.append(paragraph(
                f"Periode: {period['start']} → {period.get('end', '—')}   ·   "
                f"Omkostningsprocent: {portfolio.get('cost_ratio_pct', '—')} %",
                color="gray",
            ))

        # Monthly returns chart (text-based)
        monthly = portfolio.get("monthly_returns", {})
        if monthly:
            blocks.append(h3("📅  Månedlige Afkast"))
            for month, val in sorted(monthly.items()):
                bar = "█" * int(abs(val) / 0.5)
                sign = "+" if val >= 0 else "−"
                color = "green" if val >= 0 else "red"
                label = f"{month}   {sign}{abs(val):.1f} %   {bar}"
                blocks.append(paragraph(label, color=color))

    blocks.append(divider())

    # ── HISTORICAL TABLE ───────────────────────────────────────────────────────
    blocks += [
        h2("📅  Historisk Oversigt"),
        paragraph("De seneste måneder fra Levi databasen.", color="gray"),
    ]
    if len(entries) > 1:
        hdrs = ["Måned", "Indkomst", "Udgifter", "Opsparingsrate", "Budget", "Afkast YTD"]
        rows = []
        for e in entries:
            rows.append([
                e["maaned"] or "—",
                fmt_dkk(e["indkomst"]),
                fmt_dkk(e["udgifter"]),
                fmt_pct_plain(e["opsparingsrate"]),
                e["budget_status"] or "—",
                fmt_pct(e["afkast"]),
            ])
        blocks.append(table(hdrs, rows))
    else:
        blocks.append(paragraph("Kun én post endnu — synkroniser flere måneder for en tabel.", color="gray"))

    # ── FOOTER ─────────────────────────────────────────────────────────────────
    blocks += [
        divider(),
        paragraph(
            "Genereret af Levi · Data fra lokal Nordea + Saxo parsing · "
            "Analyse via lokal Ollama · Ingen råtransaktioner forlader maskinen",
            color="gray",
        ),
    ]

    return blocks


# ── Page lifecycle helpers ─────────────────────────────────────────────────────

def _find_parent_page() -> str | None:
    """Return the ID of the first Notion page titled 'Levi', if any."""
    result = _notion("POST", "/search",
                     json={"query": "Levi", "filter": {"value": "page", "property": "object"}})
    for r in result.get("results", []):
        title_parts = (r.get("properties", {}).get("title") or {}).get("title", [])
        if title_parts and "Levi" in title_parts[0].get("text", {}).get("content", ""):
            return r["id"]
    return None


def _archive_children(page_id: str):
    """Archive all top-level blocks in a page so it can be rewritten."""
    result = _notion("GET", f"/blocks/{page_id}/children?page_size=100")
    for block in result.get("results", []):
        _notion("PATCH", f"/blocks/{block['id']}", json={"archived": True})
    # Handle pagination
    while result.get("has_more"):
        cursor = result["next_cursor"]
        result = _notion("GET", f"/blocks/{page_id}/children?page_size=100&start_cursor={cursor}")
        for block in result.get("results", []):
            _notion("PATCH", f"/blocks/{block['id']}", json={"archived": True})


def _push_blocks(page_id: str, blocks: list):
    """Append blocks to a page in batches of 100 (Notion API limit)."""
    for i in range(0, len(blocks), 100):
        _notion("PATCH", f"/blocks/{page_id}/children",
                json={"children": blocks[i:i + 100]})


def create_page(parent_id: str | None, blocks: list) -> str:
    parent = {"type": "page_id", "page_id": parent_id} if parent_id else {"type": "workspace", "workspace": True}
    page = _notion("POST", "/pages", json={
        "parent": parent,
        "properties": {
            "title": {"title": [{"text": {"content": "⚡ Levi — Financial Command Center"}}]}
        },
        "children": blocks[:100],
    })
    page_id = page["id"]
    if len(blocks) > 100:
        _push_blocks(page_id, blocks[100:])
    return page_id


def update_page(page_id: str, blocks: list):
    log("Arkiverer eksisterende blokke...")
    _archive_children(page_id)
    log("Skriver nye blokke...")
    _push_blocks(page_id, blocks)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Opret/opdater Levi Notion dashboard")
    parser.add_argument("--force", action="store_true", help="Tving oprettelse af ny side")
    args = parser.parse_args()

    if not NOTION_KEY or not NOTION_DB_ID:
        print("FEJL: Sæt NOTION_INTEGRATION_KEY og NOTION_DB_ID i .env")
        sys.exit(1)

    log("Henter data fra Notion databasen...")
    entries_raw = fetch_entries(6)
    if not entries_raw:
        print("FEJL: Ingen data i Notion DB — kør sync_to_notion.py --type monthly først")
        sys.exit(1)

    entries = [parse_entry(e) for e in entries_raw]
    portfolio = load_portfolio()
    log(f"Byggger dashboard for {len(entries)} poster, seneste: {entries[0]['maaned']}")

    blocks = build_blocks(entries, portfolio)
    log(f"{len(blocks)} top-niveau blokke genereret")

    dash_id = None if args.force else (DASHBOARD_PAGE_ID or os.getenv("NOTION_DASHBOARD_PAGE_ID"))

    if dash_id:
        log(f"Opdaterer eksisterende dashboard: {dash_id}")
        update_page(dash_id, blocks)
        page_id = dash_id
    else:
        parent_id = _find_parent_page()
        if parent_id:
            log(f"Opretter under 'Levi'-siden: {parent_id}")
        else:
            log("Ingen 'Levi'-side fundet — opretter på workspace-niveau")
        page_id = create_page(parent_id, blocks)

        # Persist so future runs update in-place
        if ENV_FILE.exists():
            set_key(str(ENV_FILE), "NOTION_DASHBOARD_PAGE_ID", page_id)
            log(f"NOTION_DASHBOARD_PAGE_ID gemt i .env")

    url = f"https://notion.so/{page_id.replace('-', '')}"
    log(f"✅ Dashboard klar: {url}")
    print(f"\n  → {url}\n")


if __name__ == "__main__":
    main()
