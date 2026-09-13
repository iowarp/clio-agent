# Qualification workspace fixtures

Copy this directory's contents into a new contained workspace before replaying the prompt corpus.
The checked-in state is the start state: example.py reports only a total, and the quality gate is
non-strict. Scenarios 1 and 14 intentionally mutate those files through review controls.

The resource HTML is an upload fixture. Attach it through the normal resource flow so the replay
receives fresh resource and processing-task identifiers; do not hard-code the historical IDs.

