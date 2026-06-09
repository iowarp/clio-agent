---
name: hdf5-paper-replication
description: "replicate a paper", "reproduce paper results", "check paper conclusions", "analyze scientific paper data", "verify paper findings", replicating/reproducing scientific results from papers using HDF5 data.
version: 0.2.0
---

# Scientific Paper Replication via HDF5 Data

## Purpose

Step-by-step procedure for an AI agent to analyze a scientific paper, determine whether its underlying data is HDF5-based and publicly available, acquire the data if feasible, and attempt to replicate the paper's numeric results.

**Related skills**: `hdf5-scientific-publishing` for DOI resolution and repository APIs, `hdf5-visualization` for plotting results.

## Important: Knowing When to Stop

Some papers (or parts of papers) will exceed what can be replicated in an automated session. This is normal and expected. Watch for these warning signs:

- **Iterative debugging loops**: If you have attempted the same analysis step 3+ times with different approaches and are not converging on the paper's result, **stop trying to fix it**. Document what you tried, why it didn't work, and move on.
- **Underspecified methodology**: Papers often omit signal processing details, preprocessing steps, or parameter choices that are "obvious" to domain experts but essential for replication. If you cannot determine the correct approach from the paper text alone, document the ambiguity rather than guessing repeatedly.
- **Domain-specific tooling**: Some analyses depend on specialized software pipelines (e.g., LIGO data conditioning, climate model postprocessors) that cannot be reimplemented from a paper description. Flag this as a blocker.

**The goal is an honest, well-documented assessment of replicability — not a forced match to the paper's numbers.** A clear report stating "we could replicate X but not Y, because Z" is more valuable than an unreliable replication that required assumptions not stated in the paper.

When you hit diminishing returns, wrap up the analysis with what you have and focus on writing a clear report.

## Procedure

Work through these steps sequentially. If any step fails, **stop and report** what failed and why.

### Step 1: Parse the paper

Read the paper and extract:
- **Data sources**: Dataset names, DOIs, repository URLs, file format mentions
- **Variables of interest**: Which datasets/variables are used in the analysis
- **Methodology**: Numeric methods, statistical tests, transformations, parameters
- **Key results**: Tables, figures, numeric conclusions to replicate

If the paper does not describe its methodology in enough detail to replicate (e.g., missing parameters, vague descriptions of transformations), stop and report what is missing.

### Step 2: Identify data format

Determine whether the data is stored in HDF5 (or HDF5-based formats: NetCDF4, HDF-EOS5, BD5, NWB).

Check for: explicit format mentions, file extensions (.h5, .hdf5, .he5, .nc, .nwb), repository metadata, data documentation links.

If the data is not HDF5-based, stop and report the actual format.

### Step 3: Locate the data

Resolve data references to downloadable URLs:
- DOIs → DataCite API or repository landing page → file download links
- Named datasets → repository search (Zenodo, Earthdata, Dataverse, Figshare)
- Supplementary materials → publisher website

If the data is not publicly available (paywalled, restricted access, dead links), stop and report that.

### Step 4: Assess feasibility

Before downloading, check:
- **Total size**: Sum of all required files. If >2 GB, flag and confirm with user before proceeding.
- **File count**: If >20 files needed, flag.
- **Dependencies**: Does the analysis require software, libraries, or compute resources beyond Python + h5py + numpy + scipy + pandas?

If infeasible, stop and report why.

### Step 5: Acquire and inspect data

Download the data files. Open with h5py and report:
- File structure (groups, datasets, their shapes and dtypes)
- Whether the variables referenced in the paper exist in the file
- Any discrepancies between the paper's description and the actual data

### Step 6: Replicate the analysis

Implement the paper's methodology in Python. For each result or claim in the paper:
1. Document what the paper says to do
2. Implement it
3. Compare your result to the paper's reported result
4. If you must make an assumption (paper is ambiguous), **document the assumption**

**If a sub-analysis is not converging after 2-3 attempts**, stop, document the difficulty, and proceed to the next result. Do not loop indefinitely.

For results you cannot independently replicate, consider:
- Using the paper's stated intermediate values to verify downstream calculations
- Documenting what would be needed to replicate (specific tools, data, expertise)

### Step 7: Produce a reproduction report

Generate a **reproduction report as a PDF**. The report should follow the structure of a short scientific paper, not a bullet-point checklist. Specifically:

#### Report structure

1. **Title and metadata**: "Reproduction Report: [Original Paper Title]", with the original paper's citation.

2. **Introduction** (1-2 paragraphs): Briefly describe the original paper's claims and what this report attempts to replicate.

3. **Data** (1-2 paragraphs): Describe how the data was obtained, its format, and any discrepancies with the paper's description. Include file names, sizes, and source URLs.

4. **Methods and results** (the main body): For each claim or result replicated, write a short paragraph explaining what the paper does, what you did, and how the results compare. Use comparison tables where numeric results are involved. For results you could not replicate, explain what was attempted and why it failed.

5. **Discussion** (1-2 paragraphs): Summarize what was and was not replicated. Distinguish clearly between:
   - **Unambiguous results**: Fully specified methodology, exact numeric match
   - **Ambiguous results**: Methodology underspecified, assumptions required
   - **Unreplicable results**: Would require tools, data, or expertise beyond what's available

6. **Replication scripts**: Reference the Python script(s) by filename. **Do not embed source code in the report** — it creates duplication and goes stale. Instead, simply state: "The replication analysis was performed by `<script_name>.py`, located alongside this report."

#### Report style guidelines

- Write in prose paragraphs, not bullet-point lists. The report should read as a coherent document.
- Keep it concise — most reports should be 2-5 pages. A simple paper with full replication might be 2 pages; a complex paper with partial replication might be 4-5.
- Include plots where they help (e.g., reproducing a key figure from the original paper), but don't pad with unnecessary visualizations.

#### PDF generation: two approaches

There are two PDF generation approaches, each suited to different parts of the replication workflow:

**1. `markdown_to_pdf` tool — for the dummy paper PDF**

When the replication workflow requires generating a dummy/synthetic paper (the document *being* replicated), write it as a Markdown file (`.md`) with standard LaTeX math delimiters (`$...$` for inline, `$$...$$` for display) and convert it using the `markdown_to_pdf` MCP tool (or `python -m tools.markdown_to_pdf <file.md>`). This uses pandoc + pdflatex under the hood, so all LaTeX math, tables, and formatting render natively. This is the preferred approach for any document that is primarily prose with equations.

**2. matplotlib `PdfPages` — for the reproduction report PDF**

The reproduction report contains programmatic content (comparison tables with computed values, plots of replicated results, conditional pass/fail coloring) that must be generated from Python. Use matplotlib's `PdfPages` for this. Follow these rules:

**LaTeX / math rendering in matplotlib**: Do NOT use raw LaTeX delimiters like `$$...$$` in matplotlib text — they will render as literal dollar signs. Instead, use matplotlib's built-in mathtext by wrapping math expressions in single `$...$` (e.g., `$R^2 = 0.95$`). Do not enable `plt.rcParams['text.usetex'] = True` unless you have confirmed a working LaTeX installation. For plain text (units, variable names), just use regular strings without any `$` delimiters.

**Table cell text wrapping**: Long text in `ax.table()` cells will overflow and overlap adjacent cells. To prevent this:
- Wrap cell text manually using `textwrap.fill(text, width=30)` (adjust width per column) before passing to `ax.table()`.
- Set explicit column widths via `colWidths` parameter (e.g., `colWidths=[0.15, 0.35, 0.35, 0.15]`) so narrower columns get numeric data and wider columns get descriptive text.
- Use `cellDict[row, col].set_text_props(fontsize=8)` to reduce font size in dense tables.
- Increase row height for wrapped text: `cellDict[row, col].set_height(0.08)` (or more for multi-line cells).

**Section spacing**: Add explicit vertical space between the end of a paragraph and the next section header. Use `y -= 0.04` before rendering section headers. Do not place section headers immediately after the last line of a paragraph — always insert a gap.

**Accurate text height measurement**: Do NOT estimate text height with heuristic formulas like `n_lines * (fontsize / 500)` — these systematically underestimate and cause sections to overlap. Instead, use matplotlib's renderer to measure actual text extent:

```python
def _wrapped_text(ax, x, y, text, fontsize=9, width=82, **kwargs):
    wrapped = textwrap.fill(text, width=width)
    txt = ax.text(x, y, wrapped, fontsize=fontsize, va="top",
                  transform=ax.transAxes, linespacing=1.4, **kwargs)
    fig = ax.get_figure()
    fig.canvas.draw()
    bbox = txt.get_window_extent(renderer=fig.canvas.get_renderer())
    inv = ax.transAxes.inverted()
    p0 = inv.transform((0, bbox.y0))
    p1 = inv.transform((0, bbox.y1))
    return y - (p1[1] - p0[1])
```

This measures the actual rendered height in axes coordinates and returns the correct y-position for the next element.
