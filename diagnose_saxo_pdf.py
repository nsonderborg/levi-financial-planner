"""
diagnose_saxo_pdf.py — prints raw pypdf text from each relevant page.
Usage: python diagnose_saxo_pdf.py path/to/saxo_report.pdf
"""
import sys
import re
from pathlib import Path

if len(sys.argv) < 2:
    print("Usage: python diagnose_saxo_pdf.py <pdf_path>")
    sys.exit(1)

try:
    import pypdf
except ImportError:
    import subprocess
    subprocess.run(["pip", "install", "pypdf", "-q"], check=True)
    import pypdf

path = sys.argv[1]
reader = pypdf.PdfReader(path)
pages = [p.extract_text() or "" for p in reader.pages]

print(f"Total pages: {len(pages)}\n{'='*60}")

for idx in [1, 3, 4, 5, 7]:  # pages 2, 4, 5, 6, 8 (0-indexed)
    if idx < len(pages):
        print(f"\n{'='*60}")
        print(f"PAGE {idx+1} (index {idx}):")
        print(repr(pages[idx]))
    else:
        print(f"\nPage {idx+1} does not exist in this PDF (only {len(pages)} pages)")

print(f"\n{'='*60}")
print("REGEX TEST RESULTS:")

p2 = pages[1] if len(pages) > 1 else ""
p4 = pages[3] if len(pages) > 3 else ""
p5 = pages[4] if len(pages) > 4 else ""
p6 = pages[5] if len(pages) > 5 else ""
p8 = pages[7] if len(pages) > 7 else ""

m = re.search(r"ChangeinAccountValue\s+return\s+([-\d.]+)%", p2)
print(f"total_return_pct regex: {'MATCH: ' + m.group(1) if m else 'NO MATCH'}")

m = re.search(r"Currency:(\w+)", p2)
print(f"currency regex: {'MATCH: ' + m.group(1) if m else 'NO MATCH'}")

m = re.search(r"(\d{2}-[A-Za-z]{{3}}-\d{{4}})-(\d{{2}}-[A-Za-z]{{3}}-\d{{4}})", p2)
print(f"period regex: {'MATCH' if m else 'NO MATCH'}")

m = re.search(r"%Return\s+((?:[-\d.]+%\s*){{2,}})", p4)
print(f"monthly %Return regex: {'MATCH: ' + m.group(1) if m else 'NO MATCH'}")

etf_returns = re.findall(r"UCITSETF - ([-\d.]+)%", p5)
print(f"ETF returns (page 5): {etf_returns if etf_returns else 'NO MATCH'}")

holding_re = re.compile(
    r"UCITSETF\(ISIN:\n([A-Z]{{2}}[A-Z0-9]{{10}})\)\nEUR\n Equity "
    r"([\d.]+) ([\d.]+) ([\d.]+) ([-\d.]+)% ([\d.]+)%"
)
holdings = holding_re.findall(p6)
print(f"Holdings regex (page 6): {holdings if holdings else 'NO MATCH'}")

m = re.search(r"Costasapercentage\s+([-\d.]+)%", p8)
print(f"cost_ratio regex: {'MATCH: ' + m.group(1) if m else 'NO MATCH'}")

print(f"\n{'='*60}")
print("KEYWORD SEARCH IN PAGES:")
for keyword in ["ISIN", "UCITSETF", "Equity", "Weight", "Return", "ChangeinAccountValue", "Costasapercentage"]:
    for i, pg in enumerate([p2, p4, p5, p6, p8]):
        pg_num = [2, 4, 5, 6, 8][[p2, p4, p5, p6, p8].index(pg)]
        if keyword in pg:
            # Show context around the keyword
            idx = pg.index(keyword)
            snippet = repr(pg[max(0, idx-30):idx+80])
            print(f"  '{keyword}' found on page {pg_num}: ...{snippet}...")
            break
    else:
        print(f"  '{keyword}': NOT FOUND in pages 2/4/5/6/8")
