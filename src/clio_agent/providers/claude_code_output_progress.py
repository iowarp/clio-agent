"""Bound a tool-capable reply that only repeats itself without advancing the agent.

Two shapes are recognized as the reply's opening:

* repeated HTML line breaks (``<br>`` in any spelling), and
* one word repeated on its own line, such as ``_tool_calls_placeholder`` (seen when a
  model reaches for a tool it was trained with but this request does not offer, e.g.
  asked to remember something). A repeated line with words in it is an answer.

One more anywhere in the reply: a run of the same protocol word (:data:`PROTOCOL_WORD`)
on its own lines, the opening sentence notwithstanding. Seen live: "I'll start by listing
the workspace." and then ``_placeholder`` repeated for 175,000 characters and 12 minutes,
none of it shown, until the model's output limit. A run of :attr:`FormattingProgress.
_MAX_REPEATS` ends the reply like the opening shapes do.

These tokens keep the inter-token silence watchdog alive without advancing the
agent. This is a protocol recovery condition, not a deadline on accepted tasks.
Meaningful text (including a tool fence) disables this narrow guard for the reply.
While the guard is still :attr:`~FormattingProgress.undecided` the engine holds the
text back, so a reply that turns out to be non-progress never reaches the user.
"""

from __future__ import annotations

import re

#: A bare protocol-like word (``_tool_calls``, ``_check_placeholder``, ``_memory``): a
#: snake_case identifier starting with an underscore. Never prose, never a preamble.
PROTOCOL_WORD = re.compile(r"_[a-z][a-z0-9_]*")


class FormattingProgress:
    """Incrementally recognize a non-progressing prefix with constant parser state."""

    _TAGS = ("<br>", "<br/>", "<br />")
    _MAX_TAGS = 256
    #: A repeated opening word is at most this long and stops the reply at this count.
    _MAX_LINE = 80
    _MAX_REPEATS = 32

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled
        self._tags_on = enabled
        self._lines_on = enabled
        self._partial = ""
        self._tags = 0
        self._line = ""
        self._first: str | None = None
        self._repeats = 0
        #: The run of one protocol word anywhere in the reply (its own lines).
        self._runs_on = enabled
        self._run_line = ""
        self._run_word: str | None = None
        self._run_count = 0

    def cutoff(self, text: str) -> int | None:
        """Return the offset ending a non-progressing prefix, else accept the text.

        Fragmentation and case do not affect tag recognition. Neither ordinary long
        answers nor formatting in tool arguments is classified as non-progress.
        """
        if not (self._enabled or self._runs_on):
            return None
        for index, character in enumerate(text):
            if self._runs_on and self._run(character):
                return index + 1
            if not self._enabled:
                continue
            if self._tags_on and self._tag(character):
                return index + 1
            if self._lines_on and self._repeat(character):
                return index + 1
            if not (self._tags_on or self._lines_on):
                self._enabled = False
        return None

    @property
    def undecided(self) -> bool:
        """Whether the reply so far could still be non-progress (hold its text back)."""
        return self._enabled and (self._tags_on or self._lines_on)

    def only_repeats(self) -> bool:
        """At the end of a reply: it was one word repeated, at least twice, and nothing else."""
        if not (self._enabled and self._lines_on):
            return False
        if self._line.strip():
            self._repeat("\n")  # the last line has no newline after it
        return self._lines_on and self._repeats >= 2

    def _run(self, character: str) -> bool:
        """Count a protocol word repeated on its own lines; blank lines do not break a run."""
        if character not in "\r\n":
            if len(self._run_line) <= self._MAX_LINE:
                self._run_line += character
            return False
        line, self._run_line = self._run_line.strip(), ""
        if not line:
            return False
        if not PROTOCOL_WORD.fullmatch(line):
            self._run_word, self._run_count = None, 0
            return False
        if line != self._run_word:
            self._run_word, self._run_count = line, 1
            return False
        self._run_count += 1
        return self._run_count >= self._MAX_REPEATS

    def _tag(self, character: str) -> bool:
        if not self._partial and character.isspace():
            return False
        self._partial += character.lower()
        if self._partial in self._TAGS:
            self._partial = ""
            self._tags += 1
            return self._tags >= self._MAX_TAGS
        if not any(tag.startswith(self._partial) for tag in self._TAGS):
            self._tags_on = False
        return False

    def _repeat(self, character: str) -> bool:
        if character in " \t" and self._line.strip():
            self._lines_on = False  # a line with words in it is an answer
            return False
        if character not in "\r\n":
            self._line += character
            if len(self._line) > self._MAX_LINE:
                self._lines_on = False
            return False
        line, self._line = self._line.strip(), ""
        if not line:
            return False
        if any(c.isspace() for c in line) or line.lower() in self._TAGS:
            self._lines_on = False  # words, or a line break tag (counted by ``_tag``)
            return False
        if self._first is None:
            self._first, self._repeats = line, 1
            return False
        if line != self._first:
            self._lines_on = False
            return False
        self._repeats += 1
        return self._repeats >= self._MAX_REPEATS
