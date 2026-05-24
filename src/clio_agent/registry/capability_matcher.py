"""Capability extraction and matching for routing"""

import re
import warnings
from typing import Any, Dict, List, Tuple


class CapabilityMatcher:
    """Matches user queries to agent capabilities using keyword extraction.

    This matcher uses simple keyword-based matching to route queries to appropriate
    agents based on their declared capabilities. The matching algorithm extracts
    keywords from the query, filters common stopwords, and calculates overlap
    scores with each agent's keyword list.

    Attributes:
        _stopwords: Set of common words to filter from queries

    Example:
        >>> matcher = CapabilityMatcher()
        >>> keywords = matcher.extract_keywords("How do I optimize HDF5 files?")
        >>> print(keywords)
        ['optimize', 'hdf5', 'files']

        >>> capabilities = {
        ...     "data_expert": {
        ...         "keywords": ["hdf5", "compression", "optimization"],
        ...         "name": "Data Expert"
        ...     }
        ... }
        >>> matches = matcher.match_query("optimize HDF5", capabilities)
        >>> print(matches)  # [(agent_id, score), ...]
        [('data_expert', 66.67)]
    """

    def __init__(self):
        """Initialize matcher with default stopwords."""
        # Common English stopwords to filter from queries
        self._stopwords = {
            "the",
            "a",
            "an",
            "in",
            "on",
            "at",
            "to",
            "for",
            "of",
            "with",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "being",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "should",
            "could",
            "can",
            "may",
            "might",
            "must",
            "shall",
            "i",
            "you",
            "he",
            "she",
            "it",
            "we",
            "they",
            "them",
            "this",
            "that",
            "these",
            "those",
            "my",
            "your",
            "his",
            "her",
            "its",
            "our",
            "their",
            "what",
            "which",
            "who",
            "when",
            "where",
            "why",
            "how",
        }

    def extract_keywords(self, query: str) -> List[str]:
        """Extract keywords from query by lowercasing and filtering stopwords.

        The extraction process:
        1. Convert to lowercase
        2. Split on non-alphanumeric characters
        3. Remove stopwords
        4. Filter empty strings

        Args:
            query: Natural language query from user

        Returns:
            List of extracted keywords (lowercase, no stopwords)

        Example:
            >>> matcher = CapabilityMatcher()
            >>> matcher.extract_keywords("How do I optimize HDF5 files?")
            ['optimize', 'hdf5', 'files']

            >>> matcher.extract_keywords("What is the best compression for ADIOS?")
            ['best', 'compression', 'adios']
        """
        # Lowercase and split on non-alphanumeric (keep numbers for hdf5, etc.)
        words = re.findall(r"\w+", query.lower())

        # Filter stopwords and empty strings
        keywords = [w for w in words if w and w not in self._stopwords]

        return keywords

    def match_query(self, query: str, capabilities: Dict[str, Any]) -> List[Tuple[str, float]]:
        """Match query to agent capabilities and return ranked list.

        Matching algorithm:
        1. Extract keywords from query
        2. For each agent, calculate keyword overlap score
        3. Score = (matching_keywords / query_keywords) * 100
        4. Return list sorted by score (descending)

        Args:
            query: User's natural language query
            capabilities: Dict mapping agent_id to capability dict with 'keywords' list

        Returns:
            List of (agent_id, score) tuples, sorted by score descending.
            Score ranges from 0.0 to 100.0.

        Example:
            >>> matcher = CapabilityMatcher()
            >>> capabilities = {
            ...     "data_expert": {
            ...         "keywords": ["hdf5", "compression", "optimization"],
            ...         "name": "Data Expert"
            ...     },
            ...     "slurm_expert": {
            ...         "keywords": ["slurm", "scheduling", "jobs"],
            ...         "name": "SLURM Expert"
            ...     }
            ... }
            >>> matches = matcher.match_query("optimize HDF5 compression", capabilities)
            >>> print(matches[0])  # Best match
            ('data_expert', 100.0)
        """
        # Extract keywords from query
        query_keywords = self.extract_keywords(query)

        # Handle empty query
        if not query_keywords:
            # BUG FIX: Warn when query contains only stopwords (silent failure)
            warnings.warn(
                f"Query '{query}' contains only stopwords. "
                "No agent matching performed. "
                "Consider rewording the query with more specific terms.",
                UserWarning,
                stacklevel=2,
            )
            return []

        # Calculate scores for each agent
        scores = []
        for agent_id, capability in capabilities.items():
            # Get agent's keywords (handle missing keywords gracefully)
            agent_keywords = capability.get("keywords", [])
            if not agent_keywords:
                continue

            # Calculate match score
            score = self._calculate_score(query_keywords, agent_keywords)

            # Only include agents with non-zero scores
            if score > 0:
                scores.append((agent_id, score))

        # Sort by score descending
        scores.sort(key=lambda x: x[1], reverse=True)

        return scores

    def _calculate_score(self, query_keywords: List[str], agent_keywords: List[str]) -> float:
        """Calculate match score between query and agent keywords.

        Score formula:
            score = (number_of_matches / total_query_keywords) * 100

        This gives a percentage-based score indicating how much of the query
        is covered by the agent's capabilities.

        Multi-word keywords like "parallel io" are expanded to individual tokens
        so they match queries containing either word.

        Args:
            query_keywords: Keywords extracted from user query
            agent_keywords: Keywords from agent's capability declaration

        Returns:
            Match score from 0.0 to 100.0

        Example:
            >>> matcher = CapabilityMatcher()
            >>> query_kw = ['optimize', 'hdf5', 'compression']
            >>> agent_kw = ['hdf5', 'compression', 'chunking', 'data']
            >>> matcher._calculate_score(query_kw, agent_kw)
            66.67  # 2 out of 3 query keywords matched
        """
        # Expand multi-word keywords by splitting them into individual tokens
        # Example: "parallel io" becomes ["parallel", "io"]
        expanded_keywords = []
        for kw in agent_keywords:
            expanded_keywords.extend(re.findall(r"\w+", kw.lower()))

        # Convert to set for O(1) lookup
        agent_kw_set = set(expanded_keywords)

        # Count how many query keywords are in agent's capabilities
        matches = sum(1 for kw in query_keywords if kw in agent_kw_set)

        # Calculate percentage score
        score = (matches / len(query_keywords)) * 100.0

        return round(score, 2)
