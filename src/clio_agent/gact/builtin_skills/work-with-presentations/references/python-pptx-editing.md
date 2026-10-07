# Edit an existing PowerPoint with python-pptx

Use the prepared Python runtime. Read the source deck and render it before
editing so its text hierarchy and object types are known. Keep the source file
unchanged and save the revision to a new path.

## Preserve text formatting

`shape.text = ...` and `paragraph.text = ...` replace the text frame's runs.
They can lose explicit font sizes, colours, emphasis and hyperlinks even when
the same text fits inside the original shape. Change `run.text` when the target
text belongs to that run; its formatting remains attached to the existing run.

Inspect paragraph and run boundaries first:

```python
from pptx import Presentation

deck = Presentation(source_path)
for shape in deck.slides[-1].shapes:
    if shape.has_text_frame:
        for paragraph in shape.text_frame.paragraphs:
            print([(run.text, run.font.size, run.font.bold)
                   for run in paragraph.runs])
```

For a template whose exact target occupies one run, this bounded edit preserves
the existing run. Require exactly one match rather than silently editing the
wrong shape or assuming the replacement happened:

```python
from pptx.slide import Slide

def replace_single_run(slide: Slide, old: str, new: str) -> None:
    """Replace one exact run while retaining its formatting properties."""
    matches = [run for shape in slide.shapes if shape.has_text_frame
               for paragraph in shape.text_frame.paragraphs
               for run in paragraph.runs if run.text == old]
    if len(matches) != 1:
        raise ValueError(f"Expected one exact text run for {old!r}")
    matches[0].text = new

replace_single_run(deck.slides[-1], "Check calibration",
                   "Request calibration records")
deck.save(output_path)
```

Text spanning several differently formatted runs needs an explicit replacement
mapping that preserves those boundaries. Do not concatenate it into a new
plain paragraph. If rebuilding is required, copy paragraph and run properties
deliberately and retain links, bullets, alignment and spacing. A font property
of `None` means inherited formatting; it is not permission to choose a new size.

## Keep objects and notes intact

Keep charts as native charts and tables as editable cells. Do not recreate an
unchanged slide as a picture. Edit table-cell text through its existing runs
when preserving its formatting matters. Inspect notes first: an update must
retain supporting references and caveats outside the requested change.

Reopen the saved deck and compare slide count, object types, changed text and
notes against the source. Render the revision to PDF and inspect every overview
sheet. Check the changed slide's hierarchy, line wrapping, contrast and margins;
inspect individual slides when the overview leaves a question unresolved.
Structural preservation does not prove the new text fits. Correct the native
file and render again before publishing its PDF preview.
