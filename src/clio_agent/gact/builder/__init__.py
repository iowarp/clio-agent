"""The ``builder`` plugin's server side: reading sessions for ``/capture`` and ``/optimize``.

* :mod:`.scope`: which sessions the person named, which sessions a plugin ran in, the
  scope a global ``/optimize`` reads, and its watermark and declined findings.
* :mod:`.records`: ``session_record``, which writes the full record of named sessions
  into the conversation's scratch folder for the agent to read in ``run_code``.
* :mod:`.tools`: the native tools the ``builder`` plugin claims, with their
  presentation in :mod:`.presentation`.
"""
