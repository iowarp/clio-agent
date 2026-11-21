# Ocean and weather: runs left for a local machine

The cloud qualification (ledger: [earth-ocean-ledger.md](earth-ocean-ledger.md)) ran every
row it could. The two below need the person's own accounts and are run on a local CLIO after
the release. Each says what to type, what to have ready, what a pass looks like, and what to
record. What could not run anywhere (rows needing a Copernicus Marine account, a GPU or a
TPXO registration) is recorded in the ledger as not run, with the reason.

## Before you start

- Install the release and open CLIO in Desktop or Web. Do not use the old terminal UI.
- Turn on the connectors these runs use: **Ocean analysis**, **Weather**, and
  **Copernicus Marine and Climate Data Store**.
- Accounts go in the connector cards only, never in a message, file or log. The fields:

  | Account | Connector card | Fields |
  | --- | --- | --- |
  | NASA Earthdata | Ocean analysis | NASA Earthdata Login username, password |
  | Climate Data Store | Copernicus Marine and Climate Data Store | Climate Data Store personal access token |

  For the CDS, sign in at cds.climate.copernicus.eu once and accept the ERA5 licence on the
  dataset's page; a request for a dataset whose licence is not accepted is refused.
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

## After the runs

Add the rows to the ledger, move any fixed finding to its fix commit, and remove the row's
entry from the ledger's "Needs user" list.
