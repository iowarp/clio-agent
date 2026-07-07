"""Mutation-runner shim (#773): re-collects the real module so mutmut's
focused tests_dir stays a plain directory (no symlinks — Windows-safe)."""

from tests.test_arc.test_segment_index import *  # noqa: F401,F403
