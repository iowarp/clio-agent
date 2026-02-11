"""
Tests for clio_agent.errors module.

Tests structured error types, format_error_response, and with_degradation.
"""

import pytest

from clio_agent.errors import (
    ClioError,
    ConfigError,
    ExpertError,
    ProviderError,
    RoutingError,
    ToolError,
    format_error_response,
    with_degradation,
)


class TestClioError:
    """Test base ClioError."""

    def test_to_dict_structure(self):
        """to_dict should return error, message, and details keys."""
        err = ClioError("test msg", "test_type", {"key": "val"})
        d = err.to_dict()
        assert d == {
            "error": "test_type",
            "message": "test msg",
            "details": {"key": "val"},
        }

    def test_to_dict_empty_details(self):
        """to_dict with no details should return empty dict."""
        err = ClioError("msg", "type")
        assert err.to_dict()["details"] == {}

    def test_is_exception(self):
        """ClioError should be an Exception subclass."""
        err = ClioError("msg", "type")
        assert isinstance(err, Exception)

    def test_str_is_message(self):
        """str(ClioError) should return the message."""
        err = ClioError("hello world", "type")
        assert str(err) == "hello world"


class TestErrorSubclasses:
    """Test each ClioError subclass has correct error_type."""

    def test_provider_error_type(self):
        """ProviderError should have error_type='provider_error'."""
        err = ProviderError("LM down")
        assert err.error_type == "provider_error"
        assert err.to_dict()["error"] == "provider_error"

    def test_routing_error_type(self):
        """RoutingError should have error_type='routing_error'."""
        err = RoutingError("Router failed")
        assert err.error_type == "routing_error"
        assert err.to_dict()["error"] == "routing_error"

    def test_expert_error_type(self):
        """ExpertError should have error_type='expert_error'."""
        err = ExpertError("Expert crashed")
        assert err.error_type == "expert_error"
        assert err.to_dict()["error"] == "expert_error"

    def test_tool_error_type(self):
        """ToolError should have error_type='tool_error'."""
        err = ToolError("MCP failed")
        assert err.error_type == "tool_error"
        assert err.to_dict()["error"] == "tool_error"

    def test_config_error_type(self):
        """ConfigError should have error_type='config_error'."""
        err = ConfigError("Bad config")
        assert err.error_type == "config_error"
        assert err.to_dict()["error"] == "config_error"

    def test_subclass_with_details(self):
        """Subclasses should accept and store details."""
        err = ExpertError("fail", details={"expert": "data"})
        assert err.details == {"expert": "data"}

    def test_all_are_clio_errors(self):
        """All subclasses should be ClioError instances."""
        for cls in (ProviderError, RoutingError, ExpertError, ToolError, ConfigError):
            assert isinstance(cls("msg"), ClioError)


class TestFormatErrorResponse:
    """Test format_error_response function."""

    def test_with_clio_error(self):
        """ClioError should return its to_dict()."""
        err = ExpertError("expert fail", details={"expert": "data"})
        resp = format_error_response(err)
        assert resp["error"] == "expert_error"
        assert resp["message"] == "expert fail"
        assert resp["details"]["expert"] == "data"

    def test_with_generic_exception(self):
        """Generic Exception should return internal_error with no traceback."""
        err = RuntimeError("something broke internally")
        resp = format_error_response(err)
        assert resp["error"] == "internal_error"
        assert resp["message"] == "An internal error occurred"
        assert resp["details"] == {}
        # Must NOT contain the original error message (no traceback leak)
        assert "something broke" not in resp["message"]

    def test_with_value_error(self):
        """ValueError should also return internal_error."""
        resp = format_error_response(ValueError("bad value"))
        assert resp["error"] == "internal_error"

    def test_with_clio_subclass(self):
        """ClioError subclass should use its own to_dict."""
        err = ProviderError("timeout", details={"provider": "ollama"})
        resp = format_error_response(err)
        assert resp["error"] == "provider_error"
        assert resp["details"]["provider"] == "ollama"


class TestWithDegradation:
    """Test with_degradation function."""

    def test_primary_succeeds(self):
        """Should return primary result when it succeeds."""
        result = with_degradation(lambda: 42, lambda: 0)
        assert result == 42

    def test_fallback_on_primary_failure(self):
        """Should call fallback when primary fails."""
        def primary():
            raise RuntimeError("primary broke")

        result = with_degradation(primary, lambda: "fallback_value")
        assert result == "fallback_value"

    def test_raises_when_both_fail(self):
        """Should raise error_cls when both primary and fallback fail."""
        def primary():
            raise RuntimeError("primary")

        def fallback():
            raise RuntimeError("fallback")

        with pytest.raises(ClioError) as exc_info:
            with_degradation(primary, fallback)

        err = exc_info.value
        assert "Primary failed" in err.message
        assert "Fallback failed" in err.message
        assert err.details["primary_error"] == "primary"
        assert err.details["fallback_error"] == "fallback"

    def test_custom_error_class(self):
        """Should raise the specified error_cls when both fail."""
        def primary():
            raise RuntimeError("p")

        def fallback():
            raise RuntimeError("f")

        with pytest.raises(ProviderError):
            with_degradation(primary, fallback, error_cls=ProviderError)

    def test_fallback_not_called_on_success(self):
        """Fallback should not be called when primary succeeds."""
        fallback_called = False

        def fallback():
            nonlocal fallback_called
            fallback_called = True
            return "nope"

        with_degradation(lambda: "ok", fallback)
        assert not fallback_called
