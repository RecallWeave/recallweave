# Cold Trails: guided discovery design

> Status: **v1 shipped in Atlas** (deterministic selection engine + guided tour UI).
> This document defines the product contract; canonical notes remain untouched.

## Purpose

Cold Trails turns a graph a person *can* explore into a short, evidence-led
walk through connections they were unlikely to search for. A trail is useful
only when it helps someone reopen source thinking, form a question, or dismiss
a weak connection with confidence.

It is not a vault summary, tutorial, recommendation engine, or claim generator.
Atlas may present graph structure as fact. It must present inferred meaning as a
question.

Local success goals for evaluating tours (not transmitted telemetry):

- at least half of shown trails earn **Save** or **Open source**;
- at least 60 percent involve a note the user has not already selected;
- every candidate is labeled `CANDIDATE - NOT A FACT`;
- weak graphs produce an honest short tour or refusal;
- selection completes in browser memory in under one second at supported graph
  limits.

No success measurement is transmitted.

## Release gates and schema prerequisites

v1 shipped after these gates cleared:

1. Atlas privacy, accessibility, import-integrity, reset, and evidence gates
   passed independently;
2. clickable source citations or a safe copy-path fallback;
3. deterministic unit fixtures for scoring, selection, diversification, and
   refusal;
4. a separately reviewed, now **frozen** `recallweave.viewer.v2` schema.

The **`recallweave.viewer.v2` schema is frozen** in `docs/json-output.md`
(section “export-viewer and recallweave.viewer.v2”). Required additions (now
emitted by `export-viewer` and consumed by Atlas):

- `created_at` and `modified_at` per node (nullable when unknown);
- `content_hash` per node plus graph-level `export_history`;
- optional `vault_name` (label only, never a filesystem path);
- optional `policy_config_sha256` (never policy path or contents);
- distinct evidence `signals` (`lexical_terms`, `shared_tags`,
  `mutual_neighbor_ids`).

When an export omits those fields (legacy `viewer.v1` or incomplete v2), Atlas
must not claim dormancy, rediscovery, drift over time, or a direct
source-opening capability. Contradiction, causality, semantic similarity without
shared vocabulary, and importance remain out of scope even for `viewer.v2`; they
require a model or human review and an explicit trust design.

## Deterministically supportable trail types

The first implementation supported five types. Timestamp-backed trails now bring
the supported set to eight:

| Type | Eligibility | Claim class |
| --- | --- | --- |
| Unwritten link | Candidate pair has no authored path within three hops | Structural fact plus lexical prompt |
| Distant neighbors | Candidate crosses domains with at most one other inter-domain edge | Structural fact plus lexical prompt |
| Bridge | A note has authored edges into at least two weakly connected domains | Structural fact |
| Island | A note has degree at most one and at least two candidate edges | Structural fact |
| Reinforced | Candidate has at least two independent signals | Structural fact plus weighted prompt |
| Dormant | A note with valid `modified_at` at least 180 days old that still has candidate edges | Structural fact naming the observed timestamp |
| Parallel invention | Candidate pair across domains with `created_at` within 14 days and at least two surprise terms | Structural timestamps plus lexical prompt |
| Drift | A note whose `modified_at` is at least 90 days after `created_at`, still touched by candidates; mentions `export_history` change counts when present | Structural fact naming observed timestamps |

All timestamp and history trail types must state exactly which observed fields
triggered them.

## Surprise qualification

A high score is not necessarily surprising. A pair qualifies only when at least
two of these conditions hold:

1. there is no authored path within three hops;
2. it crosses a rarely connected domain boundary;
3. the shared language is not already obvious from titles, tags, or domains;
4. the notes are structurally distant but lexically close.

Compute:

```text
surprise_terms =
  shared_terms
  - tokens(source title)
  - tokens(target title)
  - source tags
  - target tags
  - source domain
  - target domain
```

Candidate trails require at least two `surprise_terms`. Rank only the qualified
set; never rank all edges and call the top results surprising.

## Scoring

For the same graph and feedback state, scoring and selection must be identical:

```text
score =
    0.30 * novelty
  + 0.25 * distance
  + 0.25 * evidence
  + 0.10 * centrality
  + 0.10 * structure
  - penalties
```

Age-relative trails (Dormant) and age bonuses use a single reference instant:
validated graph `generated_at`, or an explicit `nowMs` override. Invalid or
absent `generated_at` does not fall back to node timestamps; Dormant trails are
omitted instead.
- `novelty`: inverse authored-path proximity; `1.0` when no authored path
  exists.
- `distance`: `0.0` for one domain, `0.6` for different routinely connected
  domains, and `1.0` for different domains with at most one existing crossing.
- `evidence`: `0.4 * min(shared_terms / 6, 1)`, plus `0.3` when both endpoint
  passages are cited, `0.2` for shared tags, and
  `0.1 * min(mutual_neighbors / 3, 1)`.
- `centrality`: the larger endpoint degree divided by the 90th percentile,
  capped at `1.0`. This is connectedness, not importance.
- `structure`: a type-specific bridge-domain or island-candidate bonus.

Penalties:

- `-0.30` when an endpoint has already appeared in the current tour;
- `-0.20` for a previously dismissed pair;
- `-0.15` when surprise terms are redundant with an earlier selected trail;
- hard exclusion when evidence is below `0.25`.

When `viewer.v2` adds timestamps, age can contribute at most `0.15` before
renormalization:

```text
age_factor = min(days_since_modified / 365, 1)
age_bonus = 0.15 * age_factor
score = weighted_total + age_bonus
```

Age is applied to the older endpoint and is never described as importance.
It is added after the weighted terms so it is not multiplied by the structure
weight.

## Evidence floor

A candidate trail is eligible only when:

- both endpoints exist;
- it has at least three shared terms, bilateral cited passages, or shared tags;
- both endpoint citations match `path:line` or `path:line-line` and their
  respective node paths;
- evidence score is at least `0.25`;
- it has at least two surprise terms.

Bridge and Island trails may waive lexical requirements because they state only
topological facts. There is no "best available" exception.

## Diversified selection

Select greedily and rescore after every choice:

- six trails by default;
- at most two of any trail type;
- at most two touching one domain;
- at least three domains when three or more domains are represented by eligible trails;
- at least one Bridge or Island structural trail;
- no node appears twice;
- nodes above the 95th degree percentile are ineligible except for Bridge.

Shorten the tour rather than relax a constraint. Sequence the stops:

1. one unarguable Bridge or Island;
2. the two strongest qualified Unwritten link or Distant neighbors
   (pairs that also qualify as Parallel invention are held for step 3);
3. a different type;
4. a trail from domains not yet emphasized;
5. the strongest Reinforced candidate.

This establishes factual structure before asking for judgment and ends with the
best-evidenced candidate.

## Trail card contract

Each card contains, in order:

1. position, such as `Trail 3 of 6`;
2. trail-type badge;
3. `AUTHORED LINK` or `CANDIDATE - NOT A FACT`;
4. a fixed-template headline;
5. both note titles and domains with equal visual weight;
6. the surprise terms;
7. bilateral cited passages when available;
8. plain structural facts;
9. the candidate caveat;
10. controls;
11. a collapsed, exact score breakdown.

Candidate cards use the dashed treatment already used for candidate edges.
Their headline must never assert what the connection means. A mixed trail uses
candidate styling because the weaker trust class wins.

## Controls and feedback boundary

- **Back**, **Next**, and **Skip** navigate without judgment.
- **Save** adds a citation-rich item to a local session list.
- **Dismiss** affects future ranking only.
- **Explain** reveals all signals and the exact score.
- **Open source** copies the relative note path — always available, the
  permanent safe floor, and the only navigation the export itself supports. When
  (and only when) the viewer operator has locally configured an Obsidian vault
  name, an additional opt-in **Open in Obsidian** affordance appears; see "Vault
  navigation" below. Copy-path never depends on that configuration.
- **Show me another** deterministically excludes already shown trails.
- **Show on map** frames both endpoints on the canvas.
- **End tour** offers a local Markdown export of saved trails.

Feedback never writes to the vault, index, or graph file. If persisted, it is
stored locally under a graph fingerprint and records pair hashes rather than
paths. It may change penalties only; it may not create, delete, promote, or
verify an edge. Clearing history is one explicit action.

## Vault navigation (opt-in, local presentation only)

Copy-relative-path is the permanent, always-available way to reach a source and
the only navigation the export itself supports. Atlas may additionally offer a
single opt-in **Open in Obsidian** action, bound by these rules:

- **Obsidian only.** The sole supported deep link is `obsidian://open`. Atlas
  never runs commands, never offers arbitrary or configurable URI schemes, and
  never invokes a generic external handler.
- **Local presentation state only.** The Obsidian vault name is configured in
  the viewer's own local state at view time (browser storage). It is NOT read
  from the export, NOT written to the export, and NEVER affects export bytes,
  the export schema, export hashes, provenance, the index, deterministic
  findings, task contracts, or the Steward Truth plane. The export describes
  evidence; the local viewer decides how to navigate to it.
- **`vault_name` is not navigation.** The frozen `recallweave.viewer.v2`
  `vault_name` field stays provenance-only and is never used to build a link.
  The navigation vault name is a separate, independently validated local value.
- **Assembled at click time.** No actionable Obsidian URI is ever stored in the
  export or pre-rendered. When the user explicitly clicks Open in Obsidian,
  Atlas builds `obsidian://open?vault=<configured>&file=<relative note path>`
  from the locally configured vault name and the note's already-validated
  relative path, URI-encoding both components. No absolute path may enter the
  URI; a path that is absolute, drive-qualified, or contains a `..` segment
  fails closed and no link is offered.
- **Hidden when unconfigured.** With no valid local vault configured, the Open
  in Obsidian affordance is absent — not shown disabled, and never backed by a
  fabricated vault name. Copy-path remains.
- **Hostile input fails closed.** A configured vault name that is empty, a path,
  drive- or URL-fragment shaped, or otherwise invalid is rejected and not
  stored.

## Accessibility

The tour is DOM-based, not canvas-only:

- a focus-trapped dialog with a visible close control;
- `aria-live="polite"` for the new trail announcement;
- visible focus on every action;
- keyboard map: Right or Space for next, Left for back, `S` save, `D` dismiss,
  `E` explain, `O` open source, and Escape to end;
- reduced-motion treatment with no essential animated state;
- all information conveyed by text as well as color and line style.

## Refusal behavior

Cold Trails should say no when evidence does not support a useful tour:

| Condition | Response |
| --- | --- |
| Fewer than eight nodes | Graph is small enough to explore directly |
| Fewer than three candidates | Not enough discovery candidates |
| No passage text | Candidates may still qualify from citations and signals; notice the limitation |
| Fewer than three eligible trails | Show the shorter count and explain why |
| No candidates pass surprise terms | Say the overlap mirrors existing labels |
| One domain | Omit cross-domain types and state the limitation |

## Agent boundary

Cold Trails is for human review. An assistant receives a separate bounded,
cited subgraph, not the whole Atlas export. The vault remains canonical;
RecallWeave remains disposable and rebuildable; candidates remain prompts;
canonical changes remain behind proposal, review, approval, and re-indexing.
Atlas may later render an agent retrieval trace, but it must never expand the
agent's access boundary or convert a candidate into a claim.
