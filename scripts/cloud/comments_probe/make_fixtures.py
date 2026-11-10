"""Build the Phase 0 fixtures for ``probe_embedded.py``: a chart PNG, a four-slide deck
with the chart on slide 4, and a Word report with the chart on page 2.

    uv run --no-sync python -I scripts/cloud/comments_probe/make_fixtures.py OUT_DIR
    cd OUT_DIR && soffice --headless --convert-to pdf deck.pptx && \
        soffice --headless --convert-to pdf report.docx
"""

import hashlib
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from docx import Document  # noqa: E402
from docx.shared import Inches as DocxInches  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.util import Inches  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
chart = out / "chart.png"
fig, ax = plt.subplots(figsize=(4, 3))
ax.bar([1, 2, 3], [3, 5, 2])
fig.savefig(chart, dpi=100)
print("chart.png sha256", hashlib.sha256(chart.read_bytes()).hexdigest()[:16])

deck = Presentation()
slide = None
for index in range(4):
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = f"Slide {index + 1}"
assert slide is not None
slide.shapes.add_picture(str(chart), Inches(5), Inches(2), width=Inches(4))
deck.save(out / "deck.pptx")

report = Document()
report.add_paragraph("Intro paragraph.")
report.add_page_break()
report.add_paragraph("Results:")
report.add_picture(str(chart), width=DocxInches(3))
report.add_paragraph("After.")
report.save(out / "report.docx")
