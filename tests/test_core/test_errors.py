"""
Tests for clio_agent.errors module.

Tests structured error types and format_error_response.
"""

from clio_agent.errors import (
    CancellationError,
    ClioError,
    ConfigError,
    ExpertError,
    MCPMissingRequiredClientCapabilityError,
    MCPUnsupportedProtocolVersionError,
    ProviderError,
    RoutingError,
    ToolError,
    format_error_response,
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

    def test_cancellation_error_type(self):
        """CancellationError should have error_type='cancelled'."""
        err = CancellationError("Turn cancelled")
        assert err.error_type == "cancelled"
        assert err.to_dict()["error"] == "cancelled"

    def test_subclass_with_details(self):
        """Subclasses should accept and store details."""
        err = ExpertError("fail", details={"expert": "data"})
        assert err.details == {"expert": "data"}

    def test_all_are_clio_errors(self):
        """All subclasses should be ClioError instances."""
        for cls in (
            ProviderError,
            RoutingError,
            ExpertError,
            ToolError,
            ConfigError,
            CancellationError,
        ):
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


class TestMCPProtocolRefusalActionableHints:
    """#1282 D2/F13: the -32021/-32022 message carries its own re-dial hint,
    built defensively (a malformed message or protocol_data must never crash
    exception construction itself -- F13)."""

    def test_missing_capability_message_names_the_required_extensions(self):
        err = MCPMissingRequiredClientCapabilityError(
            "server refused the call",
            {"requiredCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}}},
        )
        assert " Re-dial declaring the client extension(s): io.modelcontextprotocol/tasks." in str(
            err
        )

    def test_missing_capability_names_multiple_extensions_sorted(self):
        err = MCPMissingRequiredClientCapabilityError(
            "server refused the call",
            {
                "requiredCapabilities": {
                    "extensions": {"b.ext": {}, "a.ext": {}},
                }
            },
        )
        assert "a.ext, b.ext" in str(err)

    def test_unsupported_version_message_names_supported_versions(self):
        err = MCPUnsupportedProtocolVersionError(
            "server refused the version", {"supportedVersions": ["2025-11-25", "2026-07-28"]}
        )
        assert (
            " Re-dial negotiating one of the server's supported protocol version(s): "
            "2025-11-25, 2026-07-28." in str(err)
        )

    def test_no_hint_appended_when_protocol_data_is_absent(self):
        err = MCPMissingRequiredClientCapabilityError("server refused the call")
        assert str(err) == "server refused the call"

    def test_malformed_protocol_data_shapes_never_crash_construction(self):
        """#1282 F13: none of these malformed shapes may raise out of __init__ --
        each just yields no hint (the base message unchanged)."""
        for bad_protocol_data in (
            None,
            "not a dict",
            {"requiredCapabilities": "not a dict"},
            {"requiredCapabilities": {"extensions": "not a dict"}},
            {"requiredCapabilities": {"extensions": {}}},
            {"supportedVersions": "not a list"},
            {"supportedVersions": []},
        ):
            err = MCPMissingRequiredClientCapabilityError("refused", bad_protocol_data)
            assert str(err) == "refused"
            err2 = MCPUnsupportedProtocolVersionError("refused", bad_protocol_data)
            assert str(err2) == "refused"

    def test_non_str_message_is_coerced_never_raises(self):
        """#1282 F13: a non-str message (should never happen, but exception
        construction must be bulletproof) is coerced, not a crash."""
        err = MCPMissingRequiredClientCapabilityError(
            None,  # type: ignore[arg-type]
            {"requiredCapabilities": {"extensions": {"x.ext": {}}}},
        )
        assert str(err) == "None Re-dial declaring the client extension(s): x.ext."

    def test_hint_builder_exception_is_caught_and_logged_not_raised(self, monkeypatch):
        """#1282 F13: if hint-building itself raises for some unforeseen reason,
        construction still succeeds with the base message -- never propagates."""
        import clio_agent.errors as errors_mod

        def _boom(protocol_data):
            raise RuntimeError("simulated hint-builder crash")

        monkeypatch.setattr(errors_mod, "_required_extensions_hint", _boom)
        err = MCPMissingRequiredClientCapabilityError("refused", {"anything": True})
        assert str(err) == "refused"


class TestMCPCallTimeoutBackstopErrorMRO:
    """#1282 F4: MCPCallTimeoutBackstopError is BOTH a ClioError/ToolError
    (to_dict() reaches the wire) AND a TimeoutError (existing
    isinstance(exc, TimeoutError) classification survives) -- verified both
    directions, not just constructed."""

    def test_isinstance_holds_for_both_bases(self):
        from clio_agent.tools.mcp_wait_ladder import MCPCallTimeoutBackstopError

        err = MCPCallTimeoutBackstopError(
            "timed out", reason="mcp_call_timeout_backstop", details={"tool": "x"}
        )
        assert isinstance(err, TimeoutError)
        assert isinstance(err, ToolError)
        assert isinstance(err, ClioError)

    def test_to_dict_reaches_the_wire(self):
        from clio_agent.tools.mcp_wait_ladder import MCPCallTimeoutBackstopError

        err = MCPCallTimeoutBackstopError(
            "timed out", reason="mcp_call_timeout_backstop", details={"tool": "x"}
        )
        assert err.to_dict() == {
            "error": "tool_error",
            "message": "timed out",
            "details": {"tool": "x"},
        }

    def test_str_still_works(self):
        from clio_agent.tools.mcp_wait_ladder import MCPCallTimeoutBackstopError

        err = MCPCallTimeoutBackstopError(
            "timed out", reason="mcp_call_timeout_backstop", details={}
        )
        assert str(err) == "timed out"
