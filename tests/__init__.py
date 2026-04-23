"""
ClioAgent Test Suite

Tests for ClioAgent components.

Test Structure:
- test_core/: Core functionality, config, API/CLI, runtime status, optimizer plumbing
- test_arc/: ARC memory, storage, indexing, LSM, retrieval, and context compilation
- test_experts/: Data, Analysis, and Visualization expert tests
- test_tools/: File policy, gateway, HDF5, Parquet, and execution boundary tests
- test_integration/: Local filesystem and multi-expert end-to-end smoke tests

Current Test Coverage:
- Multi-provider LM configuration and local OpenAI-compatible fallbacks
- ClioAgent initialization, routing, dispatch, ARC instrumentation, and local direct-tool answers
- Real local HDF5 and Parquet FastMCP tools through the gateway
- Visualization artifact generation with file policy enforcement
- Runtime doctor and API health/status behavior

Running Tests:
    # All tests
    pytest tests/

    # Specific module
    pytest tests/test_core/

    # With coverage
    pytest tests/ -v --cov=clio_agent --cov-report=html

    # Single test
    pytest tests/test_experts/test_data_expert.py::TestDataExpert::test_capabilities -v

Note: Live external backends remain optional. Integration tests should skip
cleanly when a required runtime is unavailable.
"""
