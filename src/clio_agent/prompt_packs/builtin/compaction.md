Create an evidence-preserving compact memory for the following CLIO conversation transcript. This becomes the next model-context checkpoint, so preserve concrete scientific evidence, not just a high-level story.

Rules:
- Keep exact file paths, dataset names, column names, variable names, units, dimensions, counts, statistics, artifact paths, and error messages when they appear in the transcript.
- Preserve which findings came from which source, grouped by file/provider or workflow stage.
- Preserve unresolved gaps, failed inspections, missing dependencies, and next checks.
- If evidence is missing or a source was not inspected, say that explicitly. Do not fill gaps with plausible details.
- Do not invent dataset names, columns, statistics, compression settings, or readiness conclusions that are not supported by the transcript.
- Prefer concise structured bullets over prose. Keep the summary compact, but do not omit identifiers needed for a later expert to continue the work.{focus}{files}

--- transcript ---
{transcript}
--- end ---
