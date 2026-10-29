"""The science log: a study's questions, hypotheses, experiments and what was learned.

Entries are typed and linked. Each is a ``science.entry`` semantic event in the event
log of the session that wrote it, recorded through the task layer's one writer
(``TaskHistory``), so a study travels with its sessions: an export's descendant walk
reaches every branch's entries with no extra mechanism. Study-wide reads load each
branch session's entries on demand; derived ``task_lookup`` rows find a study's
sessions. The log validates schema and references and charges a declared budget; it
never judges whether a claim is right, computes statistics, or ranks results.

* :mod:`.entries` -- the entry types, their fields and the links each needs.
* :mod:`.store` -- writing, reading, visibility, study membership and budgets.
* :mod:`.lineage` -- an Observation's sources, read from the recorded calls it cites.
* :mod:`.measurements` -- measurements as rows and as an exported data file.
* :mod:`.tools` -- the ``log_entry``, ``link``, ``set_status``, ``query_log`` and
  ``export_measurements`` tools the ``science-log`` plugin claims.
"""
