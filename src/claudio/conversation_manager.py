 #!/usr/bin/env -S uv run
 # /// script
 # requires-python = ">=3.11"
 # dependencies = [
 #   "dspy-ai>=2.6.0",
 # ]
 # ///

"""
ClaudIO Conversation Manager Module

Manages conversation history, context summarization, and multi-turn flow.
Uses DSPy for intelligent history processing and memory buffering.
"""

import dspy
from typing import List, Dict, Any, Optional
import sys
from pathlib import Path

# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent  # src/claudio/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))


class ConversationManagerSignature(dspy.Signature):
    """Manage conversation history for context-aware responses.

    Input:
        - history: Full conversation history
        - current_question: Current user question

    Output:
        - summary: Concise summary of context
        - key_topics: Main topics discussed
        - context_for_response: Relevant context
    """
    history: dspy.History = dspy.InputField(desc="Conversation history")
    current_question: str = dspy.InputField(desc="Current question")
    summary: str = dspy.OutputField(desc="Summary of context")
    key_topics: List[str] = dspy.OutputField(desc="Key topics")
    context_for_response: str = dspy.OutputField(desc="Relevant context")


class ConversationManager(dspy.Module):
    """DSPy module for managing conversation history and context.

    Buffers history in memory, summarizes for efficiency, and provides context.
    """

    def __init__(self, max_history_length: int = 10):
        super().__init__()
        self.max_history_length = max_history_length
        self.history_buffer: List[Dict[str, Any]] = []
        self.summarizer = dspy.ChainOfThought(ConversationManagerSignature)

    def add_message(self, role: str, content: str):
        """Add a message to the history buffer."""
        self.history_buffer.append({"role": role, "content": content})
        if len(self.history_buffer) > self.max_history_length:
            # Keep only recent messages
            self.history_buffer = self.history_buffer[-self.max_history_length:]

    def get_history(self) -> dspy.History:
        """Get current history as dspy.History."""
        return dspy.History(messages=self.history_buffer)

    def summarize_context(self, current_question: str) -> dspy.Prediction:
        """Summarize history for context."""
        history = self.get_history()
        return self.summarizer(history=history, current_question=current_question)

    def forward(self, current_question: str) -> dspy.Prediction:
        """Process current question with history context."""
        return self.summarize_context(current_question)