# Levels, and the generalization test

| Level | What it covers | Example (plant phenotyping) | Example (geoscience) |
|---|---|---|---|
| **L0** capabilities | Any data, any domain | tables, stats, plotting, 3D viewport, profiling habits | same |
| **L1** domain | One science domain, any source | growth curves, dose–response, G×E, Fv/Fm, spectral indices | GNSS displacement analysis, any network |
| **L2** source | One data format / facility / platform | APPL-CORE export v6 (ORNL APPL) | EarthScope GNSS via NDP |
| **L3** instance | One experiment / dataset / run | experiment 67 (pennycress × nickel) | station P475 |

Note: a *programme* name (OPAL, NDP) isn't a level. OPAL spans several labs, and APPL is one of them. Name packs after the **source** (e.g. `appl-core`), not the programme, unless you've actually tested across the programme.

## Definition
**A level-n agent works across any instance of the level below it, within that level's contract, with zero pack changes.**
- **L3 agent:** any question about one dataset.
- **L2 agent:** any dataset that conforms to one source contract.
- **L1 agent:** any source in the domain.

**The contract** is what bounds "any". For L2 it's usually written down: format id, version range, the source's own docs. It covers **more than you've seen**. For example, a format documenting 11 sensors when your sample has 6 means the pack must handle the other 5. Test those with synthetic, spec-conforming instances.

**Out-of-contract input** (a new format version) is refused and reported. A version bump may legitimately change the pack.

**Per-instance facts** (what a treatment code means, units, which sensors are present) come in **at runtime**:
- discovered by profiling and reading the data's self-description;
- asked from the data owner;
- recorded in an experiment card stored with the data.

Each level brings in the level below's facts at runtime and never builds them in.

## Build bottom-up, extract upward
You can't design an L1 abstraction well from a single source. It ends up being the one source with the names changed. So:
- **Build the L2 agent first**, named honestly.
- **Keep the seam inside it:**
  - format mechanics stay behind the agent-written loader or a generic tool;
  - domain methods sit in skills tagged `level:L1`;
  - the L1↔L2 boundary is a **set of validated data shapes** (e.g. MIAPPE-vocabulary `design`, `observations` (trait/method/scale), `spectra`, `assets`, `events`), each with a JSON schema and a validator.
- **When a second source arrives,** extracting L1 means moving the L1-tagged skills into their own pack or plugin. It shouldn't mean untangling a rewrite.
- **Use an external standard** (e.g. MIAPPE / BrAPI for phenotyping) for the seam vocabulary, so the second source isn't forced into the first one's shape.

## Substitution tests (these are the evals that prove a level claim)
- **L3 substitution:** a new instance of the same source, with **no pack changes**. Examples: experiment 70/71 with a categorical `C`/`Ni` treatment instead of numeric doses; an unbalanced design; a missing sensor.
- **L2 substitution:** a different source, and only the source plugin changes. Needed before claiming L1.
- **Held-out honesty:** if an instance is already described somewhere the model or pack can see (another team's KB, your own notes), it isn't held out. Say so.

## Where knowledge goes
| Knowledge | Level | Home |
|---|---|---|
| "Check for sentinel-like extreme values; verify QC flags against the data" | L0 | general skill (+ bundled audit script) |
| "Projected area saturates when canopies overlap; compare 2D vs 3D" | L1 | domain skill |
| "Sensor RGB2 is a top-view camera; this facility's mm scale is hard-coded" | L2 | source skill (instrument knowledge) |
| "The export lists tables in `manifest.json`; start from `docs/`" | L2 | source skill (format habits) |
| "Plant 29127's tray weighed 0 g on 04-24" | L3 | experiment card, never the pack |

Skills describe **what can exist** ("up to N sensors; check which are present"), never **what does exist** in one instance.
