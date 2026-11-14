---
name: tariff-landed-cost
description: Compute what an imported shipment costs once cleared (customs value, duty, VAT, total) with the tariff tools, and lay out every step.
---
# Landed cost of a shipment

1. Establish the customs value: goods value plus freight and insurance to the border (CIF), in the importing country's currency.
2. Look up the duty rate for the goods' tariff line and origin with `tariff_duty_rate`. Never guess a rate.
3. Call `tariff_landed_cost` with the CIF value, the duty rate and the VAT rate of the importing country.
4. Present a table: customs value, duty, VAT base (customs value plus duty), VAT, total landed cost. Name the rates and where they came from.
