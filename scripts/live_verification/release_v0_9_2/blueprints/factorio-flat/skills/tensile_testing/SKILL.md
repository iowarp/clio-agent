---
name: tensile_testing
title: Run and Interpret a Tensile Test
description: Extract baseline material behavior from a tensile test with its rate, geometry, and extensometry stated, and distinguish engineering from true stress-strain.
---

State the strain rate or crosshead speed — yield and ultimate strength are
rate-sensitive for many materials and temperatures, and a comparison across
tests needs matched rates. State specimen geometry (gauge length/diameter or
cross-section) per the governing standard (ASTM E8/E8M) so the reduction-area
and elongation results are comparable to other reported values.

Distinguish engineering stress-strain (based on original cross-section, what
the raw load-displacement data gives directly) from true stress-strain
(based on instantaneous cross-section, needed for input to a finite-element
material model past necking) — using engineering values as an FE material
input past the necking point produces an incorrect large-strain response.

Report the full property set with its extraction method: elastic modulus (its
measurement is sensitive to extensometer resolution and specimen alignment),
yield strength (0.2% offset or another stated method), ultimate strength,
elongation, and reduction of area — and flag any property extracted from a
crosshead-displacement proxy rather than a direct extensometer, since
compliance in the load train can bias modulus and yield measurements from
crosshead data alone.

Return the stress-strain curve (engineering and, if needed, true), the
extracted properties with their method, and the rate/geometry/instrumentation
the result depends on.
