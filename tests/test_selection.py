"""Tests for core.context.selection — the gates, the merge, and the log."""

from __future__ import annotations

from core.context import selection
from core.context.selection import Thresholds
from core.graph.builder import RelatedFile

THRESHOLDS = Thresholds(min_similarity=0.20, min_similarity_ratio=0.5, min_lift=1.2, max_files=20)


def _related(path: str, score: float = 0.05, lift: float = 2.0) -> RelatedFile:
    return RelatedFile(path=path, score=score, lift=lift)


# --- the vector gate ---


def test_a_hit_above_both_floors_is_kept() -> None:
    (decision,) = selection.gate_vector([("a.py", 0.65)], THRESHOLDS)

    assert decision.kept is True
    assert decision.rule == selection.KEPT
    assert "0.650" in decision.reason


def test_a_hit_below_the_absolute_floor_is_dropped() -> None:
    (decision,) = selection.gate_vector([("a.py", 0.18)], THRESHOLDS)

    assert decision.kept is False
    assert decision.rule == selection.BELOW_MIN_SIMILARITY
    assert "below the 0.20 floor" in decision.reason


def test_a_hit_below_half_the_best_match_is_dropped() -> None:
    """The absolute floor alone can't do this: the usable similarity range
    shifts per request, so a 0.30 hit is strong next to a 0.33 best and weak
    next to a 0.69 one."""
    decisions = selection.gate_vector([("best.py", 0.69), ("tail.py", 0.30)], THRESHOLDS)

    assert decisions[0].kept is True
    assert decisions[1].kept is False
    assert decisions[1].rule == selection.BELOW_SIMILARITY_RATIO
    assert "50% of the best match" in decisions[1].reason


def test_the_same_score_survives_or_not_depending_on_the_rest_of_the_pool() -> None:
    weak_pool = selection.gate_vector([("best.py", 0.33), ("a.py", 0.30)], THRESHOLDS)
    strong_pool = selection.gate_vector([("best.py", 0.69), ("a.py", 0.30)], THRESHOLDS)

    assert weak_pool[1].kept is True
    assert strong_pool[1].kept is False


def test_no_hits_at_all_yields_no_decisions() -> None:
    assert selection.gate_vector([], THRESHOLDS) == []


# --- the graph gate ---


def test_a_graph_hit_above_the_lift_floor_is_kept() -> None:
    (decision,) = selection.gate_graph([_related("a.py", lift=2.1)], THRESHOLDS)

    assert decision.kept is True
    assert decision.lift == 2.1


def test_a_graph_hit_at_background_relevance_is_dropped_however_high_it_ranks() -> None:
    """Rank can't catch this and lift can: a repo's most connected file has
    the most pagerank mass for every request, related or not."""
    (decision,) = selection.gate_graph([_related("hub.py", score=0.9, lift=1.05)], THRESHOLDS)

    assert decision.kept is False
    assert decision.rule == selection.BELOW_MIN_LIFT
    assert "no more relevant to this request than to any other" in decision.reason


# --- merge and the file budget ---


def test_vector_survivors_come_before_graph_survivors() -> None:
    merged = selection.merge(
        selection.gate_vector([("match.py", 0.65)], THRESHOLDS),
        selection.gate_graph([_related("related.py")], THRESHOLDS),
        max_files=20,
    )

    assert [d.path for d in selection.kept(merged)] == ["match.py", "related.py"]


def test_a_file_found_by_both_keeps_its_vector_decision_and_one_slot() -> None:
    merged = selection.merge(
        selection.gate_vector([("shared.py", 0.65)], THRESHOLDS),
        selection.gate_graph([_related("shared.py")], THRESHOLDS),
        max_files=20,
    )

    kept = selection.kept(merged)
    assert [d.path for d in kept] == ["shared.py"]
    assert kept[0].source == selection.VECTOR


def test_overflow_is_demoted_with_its_own_reason_not_truncated() -> None:
    """A file cut for budget is a different fact from one that failed a floor —
    the fix for one is a bigger budget, for the other a lower floor."""
    merged = selection.merge(
        selection.gate_vector([("a.py", 0.65), ("b.py", 0.60), ("c.py", 0.55)], THRESHOLDS),
        [],
        max_files=2,
    )

    assert [d.path for d in selection.kept(merged)] == ["a.py", "b.py"]
    overflow = [d for d in merged if d.path == "c.py"][0]
    assert overflow.kept is False
    assert overflow.rule == selection.BEYOND_MAX_FILES
    assert "2-file budget" in overflow.reason


def test_every_candidate_leaves_a_decision() -> None:
    """Nothing is dropped silently — a selection nobody can interrogate is
    indistinguishable from a guess."""
    merged = selection.merge(
        selection.gate_vector([("a.py", 0.65), ("weak.py", 0.05)], THRESHOLDS),
        selection.gate_graph([_related("hub.py", lift=1.0), _related("real.py", lift=1.8)], THRESHOLDS),
        max_files=20,
    )

    assert {d.path for d in merged} == {"a.py", "weak.py", "hub.py", "real.py"}


def test_nothing_clears_the_floor_keeps_nothing() -> None:
    """The acceptance case: a request the repo has no answer for selects zero
    files rather than filling the budget with the least-bad noise."""
    merged = selection.merge(
        selection.gate_vector([("a.py", 0.18), ("b.py", 0.10)], THRESHOLDS),
        selection.gate_graph([_related("c.py", lift=1.15)], THRESHOLDS),
        max_files=20,
    )

    assert selection.kept(merged) == []
    assert len(merged) == 3


# --- the stored log ---


def test_summarize_lists_survivors_and_counts_drops_by_rule() -> None:
    merged = selection.merge(
        selection.gate_vector([("a.py", 0.65), ("weak.py", 0.05), ("tail.py", 0.30)], THRESHOLDS),
        selection.gate_graph([_related("hub.py", lift=1.0)], THRESHOLDS),
        max_files=20,
    )

    summary = selection.summarize(merged)

    assert [k["path"] for k in summary["kept"]] == ["a.py"]
    assert summary["dropped"] == {
        selection.BELOW_MIN_SIMILARITY: 1,
        selection.BELOW_SIMILARITY_RATIO: 1,
        selection.BELOW_MIN_LIFT: 1,
    }
    assert summary["candidates"] == 4


def test_summarize_reports_lift_only_where_it_exists() -> None:
    merged = selection.merge(
        selection.gate_vector([("a.py", 0.65)], THRESHOLDS),
        selection.gate_graph([_related("g.py", lift=1.8)], THRESHOLDS),
        max_files=20,
    )

    kept = {k["path"]: k for k in selection.summarize(merged)["kept"]}

    assert "lift" not in kept["a.py"]
    assert kept["g.py"]["lift"] == 1.8
