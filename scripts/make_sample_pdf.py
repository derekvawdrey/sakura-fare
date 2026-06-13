"""Render samples/japan-trip.md into a simple PDF for testing PDF ingestion.

Usage: uv run python scripts/make_sample_pdf.py
"""
from pathlib import Path

from fpdf import FPDF

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def main() -> None:
    text = (SAMPLES / "japan-trip.md").read_text(encoding="utf-8")
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    for line in text.splitlines():
        stripped = line.lstrip("#").strip()
        if line.startswith("# "):
            pdf.set_font("Helvetica", "B", 16)
        elif line.startswith("## "):
            pdf.set_font("Helvetica", "B", 12)
        else:
            pdf.set_font("Helvetica", "", 11)
        # latin-1 only in core fonts; the sample is ASCII apart from a few dashes
        safe = stripped.encode("latin-1", errors="replace").decode("latin-1")
        pdf.multi_cell(0, 6, safe or " ", new_x="LMARGIN", new_y="NEXT")
    out = SAMPLES / "japan-trip.pdf"
    pdf.output(str(out))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
