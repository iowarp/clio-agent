---
name: statistics
title: Choose and Report a Statistical Test or Fit Correctly
description: Match a statistical test or distribution fit to the data's actual properties, and report its assumptions and limitations alongside the result.
---

Check the assumptions a method requires before applying it: normality
(or the specific distribution assumed — fatigue life data is often better
described by a lognormal or Weibull distribution than a normal one),
independence of observations, and equal variance across groups being
compared. A t-test or linear regression applied without checking these can
produce a misleading p-value or confidence interval.

State the specific test or fit and its parameters explicitly: for a
comparison, the test used and its p-value with the significance threshold
stated in advance (not chosen after seeing the result); for a distribution
fit (S-N curve, Weibull life distribution), the fitting method (least
squares, maximum likelihood) and the goodness-of-fit measure.

Distinguish statistical significance from practical/engineering
significance — a statistically significant difference with a small effect
size may not matter for the engineering decision, and this should be stated
explicitly rather than left to the reader to infer from a p-value alone.

Return the test/fit used, its checked assumptions, the result with its
confidence interval or goodness-of-fit, and whether the result is
practically significant for the decision at hand.
