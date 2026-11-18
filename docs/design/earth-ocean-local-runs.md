# Ocean and weather: runs left for a local machine

The cloud qualification (ledger: [earth-ocean-ledger.md](earth-ocean-ledger.md)) ran every
row it could without personal accounts or a desktop. This page lists what is left, so it can
be run on a local CLIO after the release. Each run says what to type, what to have ready,
what a pass looks like, and what to record.

## Before you start

- Install the release and open CLIO in Desktop or Web. Do not use the old terminal UI.
- Turn on the connectors these runs use: **Ocean analysis**, **Weather**, **Copernicus
  Marine and Climate Data Store**, and, for Q16, **ParaView**.
- Accounts go in the connector cards only, never in a message, file or log. The fields:

  | Account | Connector card | Fields |
  | --- | --- | --- |
  | NASA Earthdata | Ocean analysis | NASA Earthdata Login username, password |
  | Climate Data Store | Copernicus Marine and Climate Data Store | Climate Data Store personal access token |
  | Copernicus Marine | Copernicus Marine and Climate Data Store | Copernicus Marine username, password |

  For the CDS, sign in at cds.climate.copernicus.eu once and accept the ERA5 licence on the
  dataset's page; a request for a dataset whose licence is not accepted is refused.
- Optional tools: TempestExtremes (`TEMPESTEXTREMES_BIN` set to its `bin` folder) for the
  weather object tracking, and ParaView 6 for Q16. With ParaView not on `PATH`, give the
  ParaView connector's "Command to run first" (for example `export PATH=/path/to/paraview/bin:$PATH`),
  or let the agent ask for it.
- Use a fresh conversation per run, in a workspace of its own.

## What counts as evidence

Type the prompts below as written. They name no tool, argument or step, so the result is
evidence of the agent's choices. A prompt that names a tool or the steps only verifies the
mechanism; record such a run as "mechanism verified with a tool-directed prompt; not
evidence of agent judgement". Answering the agent's own questions as yourself is fine and
does not change that.

Record each run as a row under "Qualification results" in the ledger: the run id, the
session id, a verdict (Pass, Partial or Fail) in bold, what the agent did and found against
the reference, what it said it did not do, and the evidence path (a capture or output file).
A fault found in CLIO or a server goes in a finding row with its fix commit.

## Runs that need an account

### Q8: SWOT over an eddy (NASA Earthdata)

Prompt: **"Show me what SWOT sees of the largest anticyclonic eddy in the Gulf of Mexico
this autumn, compared with the gridded sea-level product."**

- What it needs: the Earthdata login. SWOT search works without it; downloads do not.
- Pass (plan O9): the agent finds the eddy from sea level (as Q7 did), finds SWOT passes over
  its track and dates, downloads them, and shows **the swath and the gridded field on the
  same map at the pass time**. The swath should resolve structure the gridded product
  smooths (a sharper rim, smaller features), and the agent should say what the swath's
  resolution and noise allow. Wrong if the swath and the gridded field are from different
  days or the swath is drawn without its time.

### Q21: ERA5 heat waves through the CDS (CDS token)

Prompt: **"Give me hourly 2 m temperature in Lisbon for 1991 to 2020, and the heat waves in
it."**

- What it needs: the CDS token, and the ERA5 licence accepted.
- Pass (plan W3): the request is **built, validated and costed before it is submitted**
  (the cost estimate is what the cloud run could not show without the token), retrieved, and
  heat waves detected by a stated definition (variable, baseline, percentile or threshold,
  window, minimum duration). The series is shown against its climatology and threshold with
  the events marked. Reference (IPMA): early August 2003 and early August 2018 (Lisbon above
  40 °C on 4 August 2018) must be among the events. The keyless ARCO copy also holds these
  years; using it instead of the CDS is a different path, so say which one ran.

### Optional: the Copernicus Marine products (Copernicus Marine account)

Q4, Q7, Q9 and Q13 passed on public products in the cloud. With the account, rerun them on the
products the plan names, to check the connector path. Use the same prompts as the ledger's
rows (Q4: Drake Passage volume and heat transport against the published estimate; Q7: Gulf of
Mexico eddies over the last year; Q9: where the surface water at BATS came from over the last
60 days; Q13: a sampling plan for ship time out of Gulfport, with dates moved to the coming
week). References:

- Q4: DRAKE 141 Sv and cDrake 173.3 Sv; GLORYS should land between them, with the
  approximation of a regular-grid section stated.
- Q7: the Loop Current rings of the year identified and tracked west-southwest.
- Q9: backward paths spread over a few hundred kilometres, not from the Gulf Stream.
- Q13: the plan crosses the Loop Current front where the forecast puts it, shown on a map with
  the forecast field.

## Run that needs a desktop decision

### Q16: the eddy in ParaView, from a plain request

Prompt, in a workspace holding 3D model output with an eddy: **"Show me the 3D structure of
the eddy near <latitude, longitude> in my run. The output is in <folder>."**

The cloud runs used three public HYCOM GOFS 3.1 snapshots of the Gulf of Mexico (1, 8 and 15
August 2023, 0-1000 m), with a warm ring near 27°N, 89.5°W. Three plain runs drew static
figures and never offered ParaView. Named by the person ("open it in ParaView so I can rotate
it"), the whole path worked. Record:

- whether the agent offers the ParaView connector unprompted (the open product question in
  the ledger's "Needs user" list);
- if it does not, the follow-up naming ParaView, recorded as a mechanism check;
- the must-see: the 3D field in ParaView, with the eddy readable (the cloud run needed one more
  follow-up to recolour it).

## Runs that need a GPU (optional)

The AI-model rows passed on this CPU-only machine (Q19 FourCastNet, Q25 CorrDiff with
`allow_cpu`). On a CUDA machine, rerun Q19, Q20 and Q25 with the same prompts and record the
GPU, the runtime and whether the results match the CPU runs.

## Regional MOM6 with forcing (optional)

Q14's test run passed with HYCOM boundaries and no surface forcing. With a CDS token (ERA5
forcing) and a TPXO registration with its files on disk (tides), ask in the same Tasmania
workspace for a forced run: **"Add surface forcing and tides to the Tasmania test and run it
for ten days."** Pass: forcing and tides from stated products, the preflight passing, the run
stable, and the result compared with an independent field (HYCOM or satellite SST).

## After the runs

Add the rows to the ledger, move any fixed finding to its fix commit, and remove the row's
entry from the ledger's "Needs user" list.
