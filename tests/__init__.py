"""
ClioAgent Test Suite

Tests for ClioAgent components.

Test Structure:
- test_core/: Core functionality (config, clio_agent agent)
- test_experts/: DataExpert tests
- test_tools/: MCP tool wrapper tests (placeholder)
- test_integration/: End-to-end integration tests (placeholder)

Current Test Coverage:
- ✅ Config: LM Studio configuration
- ✅ ClioAgent: Agent initialization and routing
- ✅ DataExpert: Capabilities and initialization
- 🔄 Tools: TODO
- 🔄 Integration: TODO

Running Tests:
    # All tests
    pytest tests/

    # Specific module
    pytest tests/test_core/

    # With coverage
    pytest tests/ -v --cov=clio_agent --cov-report=html

    # Single test
    pytest tests/test_experts/test_data_expert.py::TestDataExpert::test_capabilities -v

Note: This is a minimal test suite. Tests focus on core functionality.
More comprehensive testing will be added as the system expands.
"""
