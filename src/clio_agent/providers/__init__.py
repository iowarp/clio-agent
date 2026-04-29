"""LM-provider helpers that are too heavy or specialised to live in
``clio_agent.config``. Currently this is just the Argonne / ALCF
inference-endpoint auth bridge.

Imports are lazy: nothing in this package is loaded at config-time
unless the user actually selects an Argonne provider, so installs that
don't pull the optional ``globus-sdk`` dependency keep working.
"""
