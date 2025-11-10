"""Context retrieval logic for intelligent context loading

This module provides the ContextRetriever class for extracting relevant
conversation history and context from ARC memory based on query relevance.

Algorithm:
    1. Get conversation history from ARCMemory
    2. Extract keywords from query
    3. Score each conversation based on keyword overlap
    4. Return top N most relevant conversations
    5. Build Context object with relevant_history and key_topics

Performance:
    - Simple keyword-based matching (v0.2.0)
    - Future versions will use semantic embeddings

See docs/ARC_MEMORY_LAYER.md for architecture details.
"""

import re
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

from claudio.arc.memory import ARCMemory
from claudio.arc.schema import Context, Conversation


class ContextRetriever:
    """Retrieves relevant context for queries from ARC memory.

    Uses keyword-based relevance scoring to find the most relevant
    historical conversations for a given query. Provides context
    enrichment for agent invocations.

    Args:
        memory: ARCMemory instance for data access

    Examples:
        >>> arc = ARCMemory()
        >>> retriever = ContextRetriever(arc)
        >>>
        >>> # Retrieve context for query
        >>> context = retriever.retrieve_context_for_query(
        ...     query="How do I optimize HDF5 compression?",
        ...     session_id="session-123",
        ...     max_history=5
        ... )
        >>>
        >>> print(f"Found {len(context.retrieved_docs)} relevant docs")
        >>> print(f"Key topics: {context.learned_patterns}")
    """

    def __init__(self, memory: ARCMemory):
        """Initialize context retriever with ARC memory.

        Args:
            memory: ARCMemory instance for accessing stored data
        """
        self.memory = memory

        # Common English stop words to filter from keyword extraction
        self._stop_words = {
            "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
            "has", "he", "in", "is", "it", "its", "of", "on", "that", "the",
            "to", "was", "will", "with", "i", "do", "how", "what", "when",
            "where", "why", "can", "could", "would", "should", "my", "me",
            "you", "your", "this", "these", "those"
        }

    def retrieve_context_for_query(
        self,
        query: str,
        session_id: str,
        max_history: int = 5,
        user_id: Optional[str] = None,
    ) -> Context:
        """Retrieve relevant context for a query.

        Analyzes the query to find relevant historical conversations,
        extract key topics, and build a Context object for agent use.

        Args:
            query: User query to find context for
            session_id: Current session ID (for filtering)
            max_history: Maximum number of historical conversations to return
            user_id: Optional user ID for cross-session context (future use)

        Returns:
            Context object with relevant history and topics

        Examples:
            >>> context = retriever.retrieve_context_for_query(
            ...     query="Optimize HDF5 file compression",
            ...     session_id="session-1",
            ...     max_history=3
            ... )
            >>> print(f"Retrieved {len(context.retrieved_docs)} docs")
            >>> for pattern in context.learned_patterns:
            ...     print(f"Topic: {pattern.description}")
        """
        # Get current conversation
        current_conv = self.memory.get_conversation(session_id)

        # Get conversation history (for now, just current session)
        # In future: could retrieve from other sessions for same user
        conversations = []
        if current_conv:
            conversations = [current_conv]

        # Rank conversations by relevance
        ranked_conversations = self.rank_conversations_by_relevance(
            query=query,
            conversations=conversations
        )

        # Take top N most relevant
        relevant_history = ranked_conversations[:max_history]

        # Extract key topics from relevant conversations
        key_topics = self.extract_key_topics(relevant_history)

        # Build Context object
        # Note: Context schema is designed for domain-specific data,
        # but we use it here to return query-relevant context
        import time
        current_time = time.time()
        context = Context(
            domain=f"query_context_{session_id}",
            created_at=current_time,
            updated_at=current_time,
            retrieved_docs=[],  # Will be populated in future with RAG docs
            cached_tool_results={},  # Retrieved from memory if needed
            learned_patterns=[],  # Will contain key topics as patterns
        )

        # Add key topics as learned patterns
        from claudio.arc.schema import LearnedPattern

        for i, topic in enumerate(key_topics[:10]):  # Top 10 topics
            pattern = LearnedPattern(
                pattern_id=f"topic_{i}",
                description=topic,
                confidence=0.5,  # Default confidence for keyword-based topics
                examples_seen=1,
                learned_at=current_time,
                rule={"type": "keyword", "topic": topic}
            )
            context.learned_patterns.append(pattern)

        return context

    def extract_key_topics(self, conversations: List[Conversation]) -> List[str]:
        """Extract key topics from conversations using keyword frequency.

        Analyzes message content to identify the most frequently occurring
        meaningful words (topics).

        Args:
            conversations: List of conversations to analyze

        Returns:
            List of key topics (words), sorted by frequency (most common first)

        Examples:
            >>> conversations = [conv1, conv2, conv3]
            >>> topics = retriever.extract_key_topics(conversations)
            >>> print(topics[:5])  # Top 5 topics
            ['hdf5', 'compression', 'optimize', 'performance', 'gzip']
        """
        # Collect all message content
        all_text = []
        for conv in conversations:
            for message in conv.messages:
                all_text.append(message.content.lower())

        # Combine all text
        combined_text = " ".join(all_text)

        # Extract keywords (filter stop words, keep meaningful terms)
        keywords = self._extract_keywords(combined_text)

        # Count frequency
        word_counts = Counter(keywords)

        # Return top keywords as topics
        top_keywords = [word for word, count in word_counts.most_common(20)]

        return top_keywords

    def rank_conversations_by_relevance(
        self,
        query: str,
        conversations: List[Conversation]
    ) -> List[Conversation]:
        """Rank conversations by relevance to query.

        Uses keyword overlap scoring to determine which conversations
        are most relevant to the current query.

        Args:
            query: User query
            conversations: List of conversations to rank

        Returns:
            List of conversations sorted by relevance (most relevant first)

        Examples:
            >>> ranked = retriever.rank_conversations_by_relevance(
            ...     query="HDF5 compression optimization",
            ...     conversations=[conv1, conv2, conv3]
            ... )
            >>> # ranked[0] is the most relevant conversation
        """
        # Score each conversation
        scored_conversations = [
            (self._calculate_relevance_score(query, conv), conv)
            for conv in conversations
        ]

        # Sort by score (descending - highest score first)
        scored_conversations.sort(key=lambda x: x[0], reverse=True)

        # Return sorted conversations (without scores)
        return [conv for score, conv in scored_conversations]

    def _calculate_relevance_score(
        self,
        query: str,
        conversation: Conversation
    ) -> float:
        """Calculate relevance score (0-1) between query and conversation.

        Uses simple keyword overlap: measures how many query keywords
        appear in the conversation messages.

        Args:
            query: User query
            conversation: Conversation to score

        Returns:
            Relevance score between 0.0 (no match) and 1.0 (perfect match)

        Examples:
            >>> score = retriever._calculate_relevance_score(
            ...     query="HDF5 optimization",
            ...     conversation=conv
            ... )
            >>> if score > 0.5:
            ...     print("Highly relevant conversation")
        """
        # Extract query keywords
        query_keywords = set(self._extract_keywords(query.lower()))

        if not query_keywords:
            return 0.0

        # Extract conversation keywords
        conv_text = []
        for message in conversation.messages:
            conv_text.append(message.content.lower())

        combined_conv_text = " ".join(conv_text)
        conv_keywords = set(self._extract_keywords(combined_conv_text))

        if not conv_keywords:
            return 0.0

        # Calculate Jaccard similarity (intersection / union)
        intersection = query_keywords & conv_keywords
        union = query_keywords | conv_keywords

        if not union:
            return 0.0

        jaccard_score = len(intersection) / len(union)

        # Also calculate keyword coverage (what % of query keywords are in conversation)
        coverage_score = len(intersection) / len(query_keywords)

        # Combine scores (weighted average)
        # Jaccard: measures overall similarity
        # Coverage: ensures query keywords are present
        relevance_score = 0.4 * jaccard_score + 0.6 * coverage_score

        return min(relevance_score, 1.0)  # Clamp to [0, 1]

    def _extract_keywords(self, text: str) -> List[str]:
        """Extract meaningful keywords from text.

        Tokenizes text, removes stop words, and filters for
        meaningful terms (alphanumeric, length >= 3).

        Args:
            text: Text to extract keywords from

        Returns:
            List of keywords (lowercase, filtered)

        Examples:
            >>> keywords = retriever._extract_keywords(
            ...     "How do I optimize HDF5 file compression?"
            ... )
            >>> print(keywords)
            ['optimize', 'hdf5', 'file', 'compression']
        """
        # Tokenize: split on non-alphanumeric characters
        tokens = re.findall(r'\b[a-z0-9]+\b', text.lower())

        # Filter: remove stop words and short tokens
        keywords = [
            token for token in tokens
            if token not in self._stop_words and len(token) >= 3
        ]

        return keywords

    def get_relevant_tool_results(
        self,
        query: str,
        domain: str,
        max_results: int = 5
    ) -> List[Dict[str, Any]]:
        """Retrieve relevant cached tool results from context.

        Searches for previously cached tool results that might be
        relevant to the current query.

        Args:
            query: User query
            domain: Domain identifier (e.g., "hdf5_optimization")
            max_results: Maximum number of results to return

        Returns:
            List of cached tool results with metadata

        Examples:
            >>> results = retriever.get_relevant_tool_results(
            ...     query="analyze HDF5 file",
            ...     domain="hdf5_optimization",
            ...     max_results=3
            ... )
            >>> for result in results:
            ...     print(f"Tool: {result['tool']}, Hit count: {result['hit_count']}")
        """
        # Get context for domain
        context = self.memory.get_context(domain)

        if not context or not context.cached_tool_results:
            return []

        # Extract query keywords
        query_keywords = set(self._extract_keywords(query.lower()))

        # Score each cached tool result
        scored_results = []
        for tool_key, cached_result in context.cached_tool_results.items():
            # Score based on parameter relevance
            params_text = str(cached_result.result).lower()
            params_keywords = set(self._extract_keywords(params_text))

            if not params_keywords:
                continue

            # Calculate overlap
            intersection = query_keywords & params_keywords
            score = len(intersection) / len(query_keywords) if query_keywords else 0.0

            if score > 0:
                scored_results.append({
                    "score": score,
                    "tool": tool_key,
                    "result": cached_result.result,
                    "hit_count": cached_result.hit_count,
                    "cached_at": cached_result.cached_at,
                })

        # Sort by score (descending)
        scored_results.sort(key=lambda x: x["score"], reverse=True)

        # Return top N results (without score in output)
        return [
            {k: v for k, v in result.items() if k != "score"}
            for result in scored_results[:max_results]
        ]
