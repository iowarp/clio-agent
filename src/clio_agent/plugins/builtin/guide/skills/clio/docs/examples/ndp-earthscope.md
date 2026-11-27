---
title: "Analyze EarthScope data from NDP"
description: "Investigate a scientific question with documented observations, checked provenance and explicit historical coverage."
source: https://clio.iowarp.ai/docs/examples/ndp-earthscope/
---

# Analyze EarthScope data from NDP

Investigate a scientific question with documented observations, checked provenance and explicit historical coverage.

Use an ordinary scientific question to start the investigation. The agent should
find appropriate observations and analyze them, with private sign-in and normal
connection approval when needed. You do not need to specify a tool sequence or
collect samples for it.

```text
Can the available EarthScope observations help us compare ground movement around
a California reservoir over the last five winters? Show what the data supports
and explain whether it can inform an interpretation of water storage.
```

Name the reservoir in your request so CLIO can choose relevant stations. It
first checks whether the available measurements and historical coverage support
the comparison.

## Check the observations before interpreting them

The agent should establish the producing instrument or model, station identities,
measurement units, reference frame, timestamps and any processing. A station
listing is not a displacement series. A catalog publisher label or a `raw_csv`
path alone does not establish genuine instrument observations.

Preprocessed observations may be useful when their transformations and limitations
are documented. Their suitability depends on the scientific question. A smooth
plot or a large row count does not independently validate the source.

## Establish the historical coverage

CLIO should check the requested variables and time span through the actual
service or a corresponding NDP archive. It should report usable dates, gaps,
station changes and the compatibility of any sources joined for a comparison.

If the inspected stream has no retrievable history and no compatible archive is
found, the answer should say that historical data is unavailable through those
checked sources. If an error or access boundary prevents checking, the answer
should say availability could not be verified. Replaying a demonstration file
does not fill a missing historical interval.

## Review the scientific result

For data that supports the question, CLIO should produce the analysis and useful
figures itself, retaining source identities and processing with the outputs.
Displacement alone does not establish reservoir water storage: a defensible
interpretation also needs the relevant model, uncertainty and supporting
measurements. The report should distinguish measured changes from interpretations.

For reviewed exported investigations, see [scientific questions for
NDP](docs/examples/ndp-science.md). For stream and history handling, see
[NDP streams](docs/files/ndp-streams.md).
