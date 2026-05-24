"""Context compilation pipeline for expert invocations.

Implements the context compilation pipeline: filter -> compact -> enrich -> assemble.

Instead of concatenating all ARC data into a single string, this compiler
selectively builds context within token budgets per tier.

Pipeline stages:
    1. FILTER: Get relevant data from ARC (conversation history, dataset profiles,
       procedural memories, routing decisions)
    2. COMPACT: Truncate/summarize each section to fit proportional token budget
    3. ENRICH: Add tool capability summaries and query keywords
    4. ASSEMBLE: Format into structured context string with section headers

Token budgets:
    - Tier 1 (Router): 2,000 tokens (~1,500 words)
    - Tier 2 (Expert): 4,000 tokens (~3,000 words)

See docs/CLIO_AGENT_ARCHITECTURE.md for context compilation rationale.
"""

from typing import Any, Dict, Optional

from clio_agent.arc.memory import ARCMemory


class ContextCompiler:
    """Compiles context for expert invocations.

    Pipeline: filter -> compact -> enrich -> assemble

    Instead of concatenating all ARC data into a single string,
    this compiler selectively builds context within token budgets.

    Args:
        arc_memory: ARCMemory instance for data access
        tier_budgets: Token budgets per tier. Defaults to tier1=2000, tier2=4000.

    Examples:
        >>> from clio_agent.arc.memory import ARCMemory
        >>> arc = ARCMemory()
        >>> compiler = ContextCompiler(arc)
        >>> context = compiler.compile("analyze HDF5 file", "session-1", tier=2)
        >>> print(context)  # Structured context within 4K token budget
    """

    # Proportional allocation of budget per section
    BUDGET_PROPORTIONS = {
        "conversation": 0.40,  # 40% for conversation history
        "profiles": 0.30,  # 30% for dataset profiles
        "procedural": 0.20,  # 20% for procedural memories
        "routing": 0.10,  # 10% for routing history
    }

    # Rough token-to-word ratio (1 token ~ 0.75 words)
    WORDS_PER_TOKEN = 0.75

    def __init__(
        self,
        arc_memory: ARCMemory,
        tier_budgets: Optional[Dict[str, int]] = None,
    ):
        """Initialize ContextCompiler.

        Args:
            arc_memory: ARCMemory instance for accessing stored data
            tier_budgets: Token budgets per tier. Keys are 'tier1' and 'tier2'.
        """
        self.arc = arc_memory
        self.tier_budgets = tier_budgets or {
            "tier1": 2000,  # Router: minimal context
            "tier2": 4000,  # Experts: moderate context
        }

    def compile(
        self,
        query: str,
        session_id: str,
        tier: int = 2,
        tool_scope: str = "all",
    ) -> str:
        """Compile context for a query within token budget.

        Steps:
        1. FILTER: Get relevant data from ARC
        2. COMPACT: Truncate/summarize to fit within budget
        3. ENRICH: Add tool capabilities and expert context
        4. ASSEMBLE: Format into structured context string

        Args:
            query: User's current query
            session_id: Session identifier
            tier: Agent tier (1 for router, 2 for expert). Determines budget.
            tool_scope: Agent/tool visibility scope for injected tool summaries.
                Use ``chat``, an expert id, ``planner``, ``all``, or ``none``.

        Returns:
            Compiled context string within token budget.

        Examples:
            >>> context = compiler.compile("analyze data.h5", "session-1", tier=2)
            >>> assert "[Session Context]" in context or "No prior context" in context
        """
        budget_key = f"tier{tier}"
        budget_tokens = self.tier_budgets.get(budget_key, 4000)

        # Stage 1: Filter
        raw_context = self._filter(query, session_id)

        # Stage 2: Compact
        compacted = self._compact(raw_context, budget_tokens)

        # Stage 3: Enrich
        enriched = self._enrich(compacted, query, tool_scope=tool_scope)

        # Stage 4: Assemble
        return self._assemble(enriched)

    def _filter(self, query: str, session_id: str) -> Dict[str, Any]:
        """Filter relevant data from ARC memory.

        Returns raw context dict with conversation history, dataset profiles,
        procedural memories, and routing decisions.

        Args:
            query: User's current query
            session_id: Session identifier

        Returns:
            Dict with 'conversation', 'profiles', 'procedural', 'routing' keys.
        """
        raw: Dict[str, Any] = {
            "conversation": [],
            "profiles": [],
            "procedural": [],
            "routing": [],
        }

        # Get conversation data from current session
        conv = self.arc.get_conversation(session_id)
        if conv:
            # Last 5 messages
            if conv.messages:
                recent_messages = conv.messages[-5:]
                raw["conversation"] = [
                    {
                        "role": m.role,
                        "content": m.content,
                        "metadata": dict(getattr(m, "metadata", {}) or {}),
                    }
                    for m in recent_messages
                ]

            # Last 3 routing decisions
            if conv.routing_decisions:
                recent_routing = conv.routing_decisions[-3:]
                raw["routing"] = [
                    {
                        "query": rd.query[:100],
                        "selected": rd.selected_agent,
                        "confidence": rd.confidence,
                    }
                    for rd in recent_routing
                ]

        # Get dataset profiles for this session
        try:
            profiles = self.arc.get_session_profiles(session_id)
            raw["profiles"] = [
                {
                    "filepath": p.filepath,
                    "format": p.file_format,
                    "schema": p.schema_info,
                    "stats": p.statistics,
                    "created_by": p.created_by,
                }
                for p in profiles
            ]
        except Exception:
            pass

        # Get procedural memories for this session
        try:
            memories = self.arc.get_procedural_memories(session_id, limit=5)
            raw["procedural"] = [
                {
                    "type": m.pattern_type,
                    "description": m.description,
                    "expert": m.expert_id,
                    "outcome": m.outcome,
                }
                for m in memories
            ]
        except Exception:
            pass

        return raw

    def _compact(self, raw_context: Dict[str, Any], budget_tokens: int) -> Dict[str, str]:
        """Compact each section to fit within proportional budget.

        Each section gets a proportional share of the total budget:
        - conversation: 40%
        - profiles: 30%
        - procedural: 20%
        - routing: 10%

        Uses word count approximation (1 token ~ 0.75 words).

        Args:
            raw_context: Dict from _filter() with raw context data
            budget_tokens: Total token budget for this tier

        Returns:
            Dict mapping section names to compacted strings.
        """
        compacted: Dict[str, str] = {}

        for section, proportion in self.BUDGET_PROPORTIONS.items():
            section_budget = int(budget_tokens * proportion)
            max_words = int(section_budget * self.WORDS_PER_TOKEN)

            data = raw_context.get(section, [])
            if not data:
                compacted[section] = ""
                continue

            # Serialize section data to text
            text = self._section_to_text(section, data)

            # Truncate to word budget
            words = text.split()
            if len(words) > max_words:
                text = " ".join(words[:max_words]) + "..."

            compacted[section] = text

        return compacted

    def _section_to_text(self, section: str, data: list) -> str:
        """Convert a section's raw data to text representation.

        Args:
            section: Section name ('conversation', 'profiles', etc.)
            data: List of dicts from _filter()

        Returns:
            Text representation of the section data.
        """
        if section == "conversation":
            lines = []
            for msg in data:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                metadata = msg.get("metadata", {}) or {}
                is_compact_summary = metadata.get("synthetic") == "compact_summary" or str(
                    content
                ).startswith("[compact summary]")
                max_chars = 1600 if is_compact_summary else 200
                if len(content) > max_chars:
                    content = self._truncate_conversation_content(
                        str(content),
                        max_chars=max_chars,
                        preserve_evidence_index=is_compact_summary,
                    )
                lines.append(f"{role}: {content}")
            return "\n".join(lines)

        elif section == "profiles":
            lines = []
            for p in data:
                filepath = p.get("filepath", "unknown")
                fmt = p.get("format", "unknown")
                schema = p.get("schema", {})
                cols = schema.get("columns", [])
                rows = schema.get("rows", "?")
                lines.append(f"{filepath} ({fmt}): {len(cols)} columns, {rows} rows")
                stats = p.get("stats", {})
                if stats:
                    stat_strs = []
                    for col, col_stats in list(stats.items())[:3]:
                        if isinstance(col_stats, dict):
                            stat_parts = [f"{k}={v}" for k, v in list(col_stats.items())[:3]]
                            stat_strs.append(f"{col}: {', '.join(stat_parts)}")
                    if stat_strs:
                        lines.append("  Stats: " + "; ".join(stat_strs))
            return "\n".join(lines)

        elif section == "procedural":
            lines = []
            for m in data:
                ptype = m.get("type", "unknown")
                desc = m.get("description", "")
                expert = m.get("expert", "unknown")
                outcome = m.get("outcome", "")
                lines.append(f"[{ptype}] ({expert}) {desc}")
                if outcome:
                    lines.append(f"  -> {outcome}")
            return "\n".join(lines)

        elif section == "routing":
            lines = []
            for r in data:
                query = r.get("query", "?")
                selected = r.get("selected", "?")
                lines.append(f"{query} -> {selected}")
            return "\n".join(lines)

        return str(data)

    @staticmethod
    def _truncate_conversation_content(
        content: str,
        *,
        max_chars: int,
        preserve_evidence_index: bool = False,
    ) -> str:
        """Truncate conversation text while preserving compact evidence tails."""
        marker = "[exact retained evidence index]"
        marker_at = content.lower().find(marker.lower())
        if (
            not preserve_evidence_index
            or marker_at < 0
            or marker_at <= max_chars // 2
            or len(content) <= max_chars
        ):
            return content[:max_chars] + "..."

        tail_budget = min(max_chars // 2, len(content) - marker_at)
        head_budget = max_chars - tail_budget
        head = content[:head_budget].rstrip()
        tail = content[marker_at : marker_at + tail_budget].lstrip()
        return f"{head}\n...[truncated compact summary; exact evidence index retained]...\n{tail}"

    def _enrich(
        self,
        compacted_context: Dict[str, str],
        query: str,
        tool_scope: str = "all",
    ) -> Dict[str, str]:
        """Enrich compacted context with tool capabilities and query keywords.

        Adds:
        - Tool capability summaries from gateway.list_capabilities()
        - Keywords extracted from query for relevance hints

        Args:
            compacted_context: Dict from _compact() with section strings
            query: User's current query
            tool_scope: Agent/tool visibility scope for injected tool summaries.

        Returns:
            Enriched context dict with 'tools' and 'keywords' keys added.
        """
        enriched = dict(compacted_context)

        # Add tool capability summaries
        try:
            from clio_agent.tools.gateway import list_capabilities

            caps = list_capabilities()
            if tool_scope and tool_scope != "all":
                if tool_scope == "none":
                    caps = []
                else:
                    from clio_agent.tools.catalog import tool_visible_to

                    caps = [c for c in caps if tool_visible_to(c["name"], tool_scope)]
            tool_lines = [f"{c['name']}: {c['description']}" for c in caps]
            enriched["tools"] = "\n".join(tool_lines)
        except Exception:
            enriched["tools"] = ""

        # Extract query keywords
        words = query.lower().split()
        keywords = [
            w
            for w in words
            if len(w) >= 3
            and w
            not in {
                "the",
                "and",
                "for",
                "with",
                "from",
                "what",
                "how",
                "can",
                "this",
                "that",
                "are",
                "was",
                "will",
                "has",
            }
        ]
        enriched["keywords"] = ", ".join(keywords) if keywords else ""

        return enriched

    def _assemble(self, enriched_context: Dict[str, str]) -> str:
        """Assemble enriched context into structured output string.

        Formats context with section headers for clear prompt injection.

        Args:
            enriched_context: Dict from _enrich() with all sections

        Returns:
            Formatted context string with section headers.
        """
        sections = []

        conversation = enriched_context.get("conversation", "")
        if conversation:
            sections.append(f"[Session Context]\n{conversation}")

        profiles = enriched_context.get("profiles", "")
        if profiles:
            sections.append(f"[Available Data]\n{profiles}")

        procedural = enriched_context.get("procedural", "")
        if procedural:
            sections.append(f"[Prior Analysis]\n{procedural}")

        routing = enriched_context.get("routing", "")
        if routing:
            sections.append(f"[Routing History]\n{routing}")

        tools = enriched_context.get("tools", "")
        if tools:
            sections.append(f"[Available Tools]\n{tools}")

        if not sections:
            return "No prior context"

        return "\n\n".join(sections)
