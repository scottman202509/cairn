"""Per-project stall detection for the dispatcher loop.

The scheduler invokes :func:`detect_stall` before dispatching a new reason or
explore task. When a project's intent stream is going in circles (e.g. every
explore concludes "still unreachable" against an infrastructure block) the
detector returns a :class:`StallVerdict` so the loop can auto-abandon the
project instead of burning more tokens. The reason LLM should usually catch
this first via the prompt-level abandon path; the detector is the structural
safety net for when it doesn't.

Two independent checks, both configurable via ``runtime.stall`` in
``dispatch.yaml`` and disabled by setting a threshold to ``0``:

* **intent quota** -- absolute cap on total intents per project.
* **repeat window** -- average pairwise Jaccard token overlap over the last
  ``repeat_window`` concluded fact descriptions. High overlap means the same
  negative finding is being re-derived.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cairn.dispatcher.config import StallConfig
from cairn.server.models import Fact, ProjectDetail

# Synthetic facts written by the server/dispatcher that aren't real
# exploration output and must not be considered by the repeat check.
_SYSTEM_FACT_IDS: frozenset[str] = frozenset({"origin", "goal"})
_ABANDONED_PREFIX = "[ABANDONED]"

# Strip punctuation and very short tokens so we compare meaning, not glue
# words. Tokens shorter than this are dropped before Jaccard.
_MIN_TOKEN_LEN = 3
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


@dataclass(slots=True, frozen=True)
class StallVerdict:
    """Result of a stall check.

    Attributes:
        reason: Human-readable description of why we declared the project
            stalled. Surfaced verbatim into the abandon fact, so write it
            with the operator audience in mind.
        evidence_fact_ids: Facts the dispatcher will cite as the abandon
            ``from`` source list.
    """

    reason: str
    evidence_fact_ids: list[str]


def detect_stall(project: ProjectDetail, config: StallConfig) -> StallVerdict | None:
    """Return a :class:`StallVerdict` if the project is stuck, else None.

    The intent-quota check runs first because it's the cheapest signal and
    catches runaway projects even when their facts genuinely vary. The
    repeat-window check runs second and catches the more common "every
    explore returns the same negative finding" pattern.
    """
    if not config.enabled:
        return None

    quota_verdict = _check_intent_quota(project, config)
    if quota_verdict is not None:
        return quota_verdict

    return _check_repeat_window(project, config)


def _check_intent_quota(project: ProjectDetail, config: StallConfig) -> StallVerdict | None:
    if config.max_intents_per_project <= 0:
        return None
    total = len(project.intents)
    if total < config.max_intents_per_project:
        return None
    evidence = _latest_exploration_fact_ids(project.facts, count=1) or ["origin"]
    return StallVerdict(
        reason=(
            f"dispatcher.stall: intent quota exceeded "
            f"({total} >= {config.max_intents_per_project}). The intent stream "
            f"is not converging on Goal. Inject an operator hint with a new "
            f"constraint or capability before reactivating."
        ),
        evidence_fact_ids=evidence,
    )


def _check_repeat_window(project: ProjectDetail, config: StallConfig) -> StallVerdict | None:
    if config.repeat_window <= 0:
        return None

    recent = _recent_exploration_facts(project.facts, config.repeat_window)
    if len(recent) < max(2, config.repeat_min_facts):
        return None

    token_sets = [(fact.id, _tokenize(fact.description)) for fact in recent]
    token_sets = [(fid, tokens) for fid, tokens in token_sets if len(tokens) >= 2]
    if len(token_sets) < max(2, config.repeat_min_facts):
        return None

    similarities: list[float] = []
    for i in range(len(token_sets)):
        for j in range(i + 1, len(token_sets)):
            similarities.append(_jaccard(token_sets[i][1], token_sets[j][1]))
    if not similarities:
        return None

    avg_similarity = sum(similarities) / len(similarities)
    if avg_similarity < config.repeat_jaccard_threshold:
        return None

    evidence = [fid for fid, _ in token_sets[-3:]] or ["origin"]
    return StallVerdict(
        reason=(
            f"dispatcher.stall: the last {len(token_sets)} concluded facts are "
            f"semantically repetitive (avg Jaccard {avg_similarity:.2f} >= "
            f"threshold {config.repeat_jaccard_threshold:.2f}). Exploration is "
            f"re-deriving the same negative finding. Inject an operator hint "
            f"with a new constraint or capability before reactivating."
        ),
        evidence_fact_ids=evidence,
    )


def _recent_exploration_facts(facts: list[Fact], window: int) -> list[Fact]:
    if window <= 0:
        return []
    eligible = [
        fact
        for fact in facts
        if fact.id not in _SYSTEM_FACT_IDS and not fact.description.startswith(_ABANDONED_PREFIX)
    ]
    eligible.sort(key=_fact_sort_key)
    return eligible[-window:]


def _latest_exploration_fact_ids(facts: list[Fact], count: int) -> list[str]:
    recent = _recent_exploration_facts(facts, count)
    return [fact.id for fact in recent]


def _fact_sort_key(fact: Fact) -> tuple[int, str]:
    # Fact ids are emitted as f001, f002, ... so a numeric extraction sorts
    # them in creation order. Fall back to the raw id for anything that
    # doesn't match the convention.
    match = re.match(r"f(\d+)", fact.id)
    if match is None:
        return (10**9, fact.id)
    return (int(match.group(1)), fact.id)


def _tokenize(text: str) -> set[str]:
    return {token for token in _TOKEN_SPLIT_RE.split(text.lower()) if len(token) >= _MIN_TOKEN_LEN}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)
