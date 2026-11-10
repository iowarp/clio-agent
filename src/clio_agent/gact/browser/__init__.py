"""The in-app browser: one Chromium per profile, driven by the agent from ``run_code``.

:mod:`.service` owns Chromium, profiles and pages on its own event loop thread;
:mod:`.tools` are the browser tools a script reaches through ``tools.*``; :mod:`.host_gate`
decides which hosts a page may load; :mod:`.live` streams a page to the web UI and plays the
person's input back. Nothing else in CLIO launches a browser for the agent (plan rule 3).
Playwright is imported only when the service starts (:mod:`.install`).
"""
