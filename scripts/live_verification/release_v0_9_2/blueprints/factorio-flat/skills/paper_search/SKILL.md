---
name: paper_search
title: Search Scientific Literature for Relevant Sources
description: Decompose a literature question into search lanes, fetch and read actual sources, and return only what was verified from the fetched text.
---

Define the question, the decision it informs, and the terms and synonyms a
relevant paper might use — a single search term misses adjacent literatures
that use different vocabulary for the same concept. Split a broad question
into independent lanes (mechanism, measurement method, competing approach,
material system) that can be searched without duplicating each other.

Fetch and read the actual source before citing it; a search-result snippet or
title is not evidence of what a paper found. Record, per source: what was
read (full text vs. abstract only), publication venue and date, and the
specific claim it supports or contradicts.

Follow consequential references forward and backward — a cited method or a
paper that cites the one just found often matters more than the next search
result. Stop when additional searches stop changing the claim map, not on a
fixed count.

Return the fetched sources with what was actually read from each, the claims
each supports, and hand the set to `literature_review` for synthesis.
