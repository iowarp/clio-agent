"""Pytest proofs that EXERCISE the S0 equivalence harness (``tests/equivalence``).

These are the acceptance tests for design §4.1 (the F3 gate): they prove the harness
(i) reproduces today's state with an EMPTY diff, (ii) fires drop-detection on a
suppressed event type, (iii) catches a one-byte projection mutation with a precise
field path, and (iv) runs on both ARC backends.
"""
