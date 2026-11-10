"""``run_code``: scripts that call the agent's tools, in a persistent confined kernel.

- :mod:`.tool` - the tool, its approval, the run loop and the model's text;
- :mod:`.kernels` - one kernel process per ``(session, react scope)``;
- :mod:`.kernel_main` - the kernel (standard library only);
- :mod:`.stubs` - the ``tools`` namespace a script calls;
- :mod:`.dispatch` - nested calls through the agent's instrumented tools;
- :mod:`.grants` - how the script's approval covers its nested calls;
- :mod:`.presentation` - how a ``run_code`` row reads.
"""
