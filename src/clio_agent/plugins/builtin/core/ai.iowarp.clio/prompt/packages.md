---
id: core.packages
section: capabilities
when: {tools: [shell]}
---
CLIO brings its own uv and Python: every shell and every run_code kernel has
`$CLIO_UV` and `$CLIO_PYTHON`, and uv keeps its cache in this session's own scratch
space. A Python package a script needs is something you add, not a reason to stop or
to rewrite the work without it:

- One script: `"$CLIO_UV" run --no-project --python "$CLIO_PYTHON" --with <package>
  --with <package> script.py`. uv installs the packages into a cached environment the
  first time and reuses it.
- Longer work: an environment in the workspace, `"$CLIO_UV" venv .venv --python
  "$CLIO_PYTHON"`, then `"$CLIO_UV" pip install --python .venv <packages>`, and run its
  own `python`.
- A package with no build for CLIO's Python: name a version (`--python 3.12`) and uv
  fetches that Python.

The run_code kernel cannot import these packages. A run_code script, background or
not, runs such a script as a subprocess and reads what it prints.
