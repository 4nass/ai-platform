"""The arbiter: turns retrieved candidates into a selection, with reasons.

Retrieval produces candidates; this module decides. It exists because the two
retrievers were being trusted as deciders: whatever they returned was injected,
so a request with nothing to find still shipped 20 chunks plus 20 graph files.
Measured against this repo, "What is the capital of France?" filled the context
exactly as fully as a real request did.

Two pools, two gates, deliberately not merged into one number. A cosine
similarity and a PageRank lift measure different things — "does this text read
like the request" versus "is this file unusually connected to what matched" —
and averaging them would invent a precision neither has. They are gated
separately against their own calibrated floors, then merged for the file
budget.

Every candidate that enters leaves a Decision, kept or not. A selection nobody
can interrogate is indistinguishable from a guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.graph.builder import RelatedFile

VECTOR = "vector"
GRAPH = "graph"

KEPT = "kept"
BELOW_MIN_SIMILARITY = "min_similarity"
BELOW_SIMILARITY_RATIO = "min_similarity_ratio"
BELOW_MIN_LIFT = "min_lift"
BEYOND_MAX_FILES = "max_files"


@dataclass(frozen=True)
class Thresholds:
    """The floors a candidate has to clear, and how many survive.

    Every value here was calibrated against four requests on one 69-node repo
    (see the step 3 plan). They are starting points chosen from measured
    distributions, not constants — telemetry records them per run precisely so
    they can be revisited against real usage rather than re-argued.
    """

    min_similarity: float = 0.20
    """Absolute cosine floor. The nonsense control request peaked at 0.182, so
    this eliminates it whole while every real request keeps its head."""
    min_similarity_ratio: float = 0.5
    """Relative floor, as a fraction of this request's best match. The usable
    cosine range shifts per request (0.10-0.18 for noise, 0.33-0.69 for a
    precise request), so a purely absolute floor either lets noise through or
    guts a weak-but-legitimate request."""
    min_lift: float = 1.2
    """Graph floor. 1.0 means "no more relevant to this request than to any
    other"; the control peaked at 1.15, real requests reach 1.36-2.10."""
    max_files: int = 20


@dataclass(frozen=True)
class Decision:
    path: str
    source: str
    score: float
    lift: float | None
    """None for a vector hit — lift is a property of graph expansion."""
    kept: bool
    rule: str
    """Which gate decided, as a stable key. `reason` is for people, this is for
    grouping in SQL."""
    reason: str


def gate_vector(hits: list[tuple[str, float]], thresholds: Thresholds) -> list[Decision]:
    """Gates the search results. `hits` is (path, best chunk score) — a file is
    as relevant as its strongest matching chunk.

    Runs before graph expansion, not alongside it, so the graph is seeded from
    files that actually matched rather than from whatever the search returned.
    Seeding on noise produces related noise, and it is the difference between a
    nonsense request selecting nothing and it selecting twenty files.
    """
    best = max((score for _, score in hits), default=0.0)
    ratio_floor = best * thresholds.min_similarity_ratio
    ratio_pct = round(thresholds.min_similarity_ratio * 100)

    decisions: list[Decision] = []
    for path, score in hits:
        if score < thresholds.min_similarity:
            decisions.append(
                Decision(
                    path, VECTOR, score, None, False, BELOW_MIN_SIMILARITY,
                    f"similarity {score:.3f} is below the {thresholds.min_similarity:.2f} floor",
                )
            )
        elif score < ratio_floor:
            decisions.append(
                Decision(
                    path, VECTOR, score, None, False, BELOW_SIMILARITY_RATIO,
                    f"similarity {score:.3f} is below {ratio_pct}% of the best match ({best:.3f})",
                )
            )
        else:
            decisions.append(
                Decision(path, VECTOR, score, None, True, KEPT, f"matched the request at {score:.3f}")
            )
    return decisions


def gate_graph(hits: list[RelatedFile], thresholds: Thresholds) -> list[Decision]:
    """Gates the graph expansion on lift, not on rank.

    Rank alone would keep whatever the repo's most connected files happen to
    be — measured, the same four topped nearly every request, including a
    nonsense one. Lift asks the question rank can't: is this file unusually
    relevant *here*, or just unusually well connected in general.
    """
    decisions: list[Decision] = []
    for hit in hits:
        if hit.lift < thresholds.min_lift:
            decisions.append(
                Decision(
                    hit.path, GRAPH, hit.score, hit.lift, False, BELOW_MIN_LIFT,
                    f"lift {hit.lift:.2f}x is below {thresholds.min_lift:.2f}x — no more relevant "
                    "to this request than to any other",
                )
            )
        else:
            decisions.append(
                Decision(
                    hit.path, GRAPH, hit.score, hit.lift, True, KEPT,
                    f"connected to the matched files, {hit.lift:.2f}x its usual relevance",
                )
            )
    return decisions


def merge(
    vector_decisions: list[Decision], graph_decisions: list[Decision], max_files: int
) -> list[Decision]:
    """Vector survivors first, then graph survivors, capped at max_files.

    Overflow is demoted with its own reason rather than truncated away: a file
    cut for budget is a different fact from one that failed a floor, and the
    fixes differ — raise the budget, or lower the floor.
    """
    merged: list[Decision] = []
    kept_count = 0
    seen: set[str] = set()

    for decision in [*vector_decisions, *graph_decisions]:
        if not decision.kept:
            merged.append(decision)
            continue
        # A file found by both retrievers keeps its (higher-ranked) vector
        # decision and doesn't consume a second slot.
        if decision.path in seen:
            continue
        if kept_count >= max_files:
            merged.append(
                Decision(
                    decision.path, decision.source, decision.score, decision.lift, False,
                    BEYOND_MAX_FILES,
                    f"cleared the relevance floor but ranked beyond the {max_files}-file budget",
                )
            )
            continue
        seen.add(decision.path)
        kept_count += 1
        merged.append(decision)

    return merged


def kept(decisions: list[Decision]) -> list[Decision]:
    return [d for d in decisions if d.kept]


def summarize(decisions: list[Decision]) -> dict:
    """Compact enough to store on every call row, complete enough to answer
    "why so few files?" without re-running the selection."""
    dropped: dict[str, int] = {}
    for decision in decisions:
        if not decision.kept:
            dropped[decision.rule] = dropped.get(decision.rule, 0) + 1
    return {
        "kept": [
            {
                "path": d.path,
                "source": d.source,
                "score": round(d.score, 4),
                **({"lift": round(d.lift, 2)} if d.lift is not None else {}),
            }
            for d in decisions
            if d.kept
        ],
        "dropped": dropped,
        "candidates": len(decisions),
    }
