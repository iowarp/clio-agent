"""
Tests for clio_agent.agent module.

Tests ClioAgent Router + ChatAgent + Expert dispatch architecture.
"""


from clio_agent.agent import ClioAgent


class TestClioAgent:
    """Test ClioAgent agent functionality."""

    def test_initialization(self):
        """Test ClioAgent agent can be initialized."""
        agent = ClioAgent()
        assert agent is not None
        assert hasattr(agent, "forward")
        agent.shutdown()

    def test_has_router(self):
        """Test ClioAgent has router component."""
        agent = ClioAgent()
        assert hasattr(agent, "router")
        agent.shutdown()

    def test_has_chat_agent(self):
        """Test ClioAgent has chat_agent component."""
        agent = ClioAgent()
        assert hasattr(agent, "chat_agent")
        agent.shutdown()

    def test_has_data_expert(self):
        """Test ClioAgent has data_expert component."""
        agent = ClioAgent()
        assert hasattr(agent, "data_expert")
        agent.shutdown()

    def test_has_arc_memory(self):
        """Test ClioAgent has ARC memory."""
        agent = ClioAgent()
        assert hasattr(agent, "arc")
        assert agent.arc is not None
        agent.shutdown()

    def test_has_lsm_tree(self):
        """Test ClioAgent has LSM tree."""
        agent = ClioAgent()
        assert hasattr(agent, "lsm")
        assert agent.lsm is not None
        agent.shutdown()

    def test_has_registry(self):
        """Test ClioAgent has agent registry."""
        agent = ClioAgent()
        assert hasattr(agent, "registry")
        agent.shutdown()

    def test_expert_registry_data(self):
        """Test that DataExpert is registered."""
        agent = ClioAgent()
        agents = agent.registry.list_agents()
        assert "data" in agents
        assert len(agents) == 1
        agent.shutdown()

    def test_expert_capabilities(self):
        """Test expert capabilities are loaded."""
        agent = ClioAgent()
        cap = agent.registry.get_capabilities("data")
        assert cap is not None
        assert cap.description is not None
        assert "hdf5" in cap.keywords
        agent.shutdown()

    def test_arc_stats(self):
        """Test ARC stats are retrievable."""
        agent = ClioAgent()
        stats = agent.get_arc_stats()
        assert "hit_rate" in stats
        assert "size" in stats
        assert "capacity" in stats
        agent.shutdown()

    def test_lsm_stats(self):
        """Test LSM stats are retrievable."""
        agent = ClioAgent()
        stats = agent.get_lsm_stats()
        assert "write_count" in stats
        agent.shutdown()

    def test_shutdown(self):
        """Test clean shutdown does not raise."""
        agent = ClioAgent()
        agent.shutdown()  # Should not raise

    def test_verbose_mode(self):
        """Test verbose mode can be enabled."""
        agent = ClioAgent(verbose=True)
        assert agent.verbose is True
        agent.shutdown()

    def test_custom_data_dir(self, tmp_path):
        """Test custom data directory for ARC/LSM storage."""
        data_dir = str(tmp_path / "clio_test")
        agent = ClioAgent(data_dir=data_dir)
        assert agent is not None
        agent.shutdown()

    def test_router_lm_configured(self):
        """Test that router LM is configured separately."""
        agent = ClioAgent()
        assert agent._router_lm is not None
        agent.shutdown()

    def test_get_session_context_empty(self):
        """Test session context retrieval with no history."""
        agent = ClioAgent()
        ctx = agent._get_session_context("test question", "empty_session")
        assert isinstance(ctx, str)
        agent.shutdown()
