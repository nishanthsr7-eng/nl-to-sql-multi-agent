"""Reuse a previous question's SQL when a new question means the same thing.

The business case is the easy part: a warehouse gets asked the same dozen
questions in a hundred phrasings, and a hit costs no generation tokens and no
round-trip. The interesting part -- and the reason this file is longer than a
dict lookup -- is that a naive semantic cache is a *confidently wrong answer
generator*, which is the exact failure this project exists to prevent.

Three rules make it safe, and each of them exists because dropping it produces a
specific wrong answer:

1. **A hit never bypasses the validator.** Cached SQL goes through the same
   validation and the same execution as freshly generated SQL. The cache is an
   optimisation on *where the SQL came from*, never on whether it is allowed to
   run -- and a warehouse whose schema has since changed makes a once-valid
   cached query invalid, which only the validator can notice.

2. **Entities are part of the key, not part of the similarity.** "Revenue in
   2023" and "revenue in 2024" are lexically almost identical and embed almost
   identically -- cosine similarity around 0.98 on any model. They also have
   different answers. Every literal the planner extracted (identifiers,
   dimension values, the timeframe) must match *exactly* before similarity is
   consulted at all, so a near-hit can only ever change the phrasing, never the
   filter. This is the single rule that separates a cache from a liability.

3. **Only successful SQL is stored.** A query that failed validation or errored
   in the warehouse is not a cheaper way to fail next time; it is a way to make
   a transient failure permanent.

The similarity backend is pluggable because this repo routinely runs against a
local provider with no embeddings endpoint. With embeddings it is a genuine
semantic cache; without them it falls back to a deterministic lexical vector
over token bigrams, which catches reorderings and filler words but not synonyms.
Which one served a hit is recorded and reported, because "40% hit rate" means
two different things in the two modes and reporting one number for both would be
the sort of quiet overclaim the rest of this project is built to avoid.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from semantic_query_engine.core.logging import get_logger

logger = get_logger(__name__)

# Above this cosine similarity, two questions with identical entities are taken
# to be the same question. Deliberately high: the cost of a miss is one ordinary
# generation, and the cost of a false hit is a wrong answer delivered with full
# confidence and no model call to blame it on.
DEFAULT_THRESHOLD = 0.92

# The lexical backend is coarser than an embedding, so it is held to a stricter
# bar. It cannot recognise a synonym, which means anything it does match is
# mostly a reordering or a filler-word difference -- and those score very high.
LEXICAL_THRESHOLD = 0.97

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(question: str) -> list[str]:
    return _TOKEN.findall(question.lower())


def lexical_vector(question: str) -> dict[str, float]:
    """An L2-normalised bag of unigrams and bigrams.

    Bigrams as well as unigrams so that word order carries some weight:
    "revenue by region" and "region by revenue" share every unigram, and only
    one of them is the question that was asked.
    """
    words = _tokens(question)
    grams = words + [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
    counts = Counter(grams)
    norm = math.sqrt(sum(value * value for value in counts.values())) or 1.0
    return {gram: value / norm for gram, value in counts.items()}


def sparse_cosine(left: dict[str, float], right: dict[str, float]) -> float:
    """Cosine of two already-normalised sparse vectors."""
    if len(right) < len(left):
        left, right = right, left
    return sum(weight * right.get(gram, 0.0) for gram, weight in left.items())


def dense_cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


# Literals that change the answer and that the planner does not report as
# values: it classifies "in 2024" as the coarse label ``year_detected``, the
# same label it gives "in 2023". Relying on the planner's entities alone
# therefore left the two questions sharing a key, with nothing but cosine
# similarity between a 2023 question and a 2024 answer -- and on a real run
# those two embed at about 0.96, which is above where any useful threshold can
# sit. These are read straight out of the question text instead.
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_MONTH = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|"
    r"november|december|q[1-4])\b",
    re.IGNORECASE,
)
# "top 5" and "top 10" are different questions with the same words.
_QUANTIFIER = re.compile(r"\b(?:top|bottom|first|last|highest|lowest)\s+(\d+)\b", re.IGNORECASE)


def question_literals(question: str) -> list[str]:
    """Dates, periods and quantities in a question, normalised and sorted.

    Order-independent, so "2023 vs 2024" and "2024 vs 2023" share a key -- they
    are the same comparison. Case-folded, so "March" and "march" do too.
    """
    lower = question.lower()
    found = set(_ISO_DATE.findall(lower))
    found |= {match.group(0) for match in _YEAR.finditer(lower)}
    found |= {match.group(0) for match in _MONTH.finditer(lower)}
    found |= {f"n={match.group(1)}" for match in _QUANTIFIER.finditer(lower)}
    return sorted(found)


def entity_key(entities: dict[str, Any] | None, question: str = "") -> str:
    """A canonical, order-independent string for the literals a question names.

    Two sources, because neither is sufficient alone: the planner's extracted
    entities (dimension values and identifiers, which it does report as values)
    and the dates and quantities in the question text (which it does not).

    ``metric`` is excluded on purpose: it is the planner's coarse *family* label
    rather than a literal that appears in a filter, and including it would split
    "total revenue" from "revenue" for no gain. ``timeframe`` is excluded for
    the opposite reason -- it is coarse enough to be actively misleading, and
    the literal dates above replace it.
    """
    populated: dict[str, Any] = {
        name: str(value)
        for name, value in sorted((entities or {}).items())
        if value and name not in ("metric", "timeframe")
    }
    literals = question_literals(question)
    if literals:
        populated["_literals"] = literals
    # "" rather than "{}" when nothing survives, so a question that named no
    # literal keys the same whether the planner handed back None, an empty dict,
    # or a dict of empty slots. Three spellings of "no literals" that did not
    # compare equal would mean a stored entity-free question could never be hit.
    return json.dumps(populated, sort_keys=True) if populated else ""


@dataclass
class CacheEntry:
    question: str
    sql: str
    entity_key: str
    vector: dict[str, float]
    embedding: list[float] = field(default_factory=list)
    hits: int = 0
    # Tokens the generation that produced this SQL originally cost. Every hit
    # saves exactly this, which is what makes the saving a measurement rather
    # than an estimate.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class CacheStats:
    """What the cache did this process, for ``--explain`` and the eval report."""

    lookups: int = 0
    hits: int = 0
    entries: int = 0
    # Hits served by the embedding backend vs the lexical one. Reported
    # separately because they support different claims.
    semantic_hits: int = 0
    lexical_hits: int = 0
    # Blocked by rule 2: similar phrasing, different literals. Counted because
    # it is the number that shows the guard is doing something -- a cache with
    # zero entity blocks on a real workload is not being asked hard questions.
    entity_blocked: int = 0
    saved_prompt_tokens: int = 0
    saved_completion_tokens: int = 0
    saved_cost_usd: float = 0.0

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["hit_rate"] = self.hit_rate
        return payload


@dataclass(frozen=True)
class CacheHit:
    sql: str
    question: str
    similarity: float
    backend: str
    saved_prompt_tokens: int
    saved_completion_tokens: int
    saved_cost_usd: float


class SemanticCache:
    """Question -> validated SQL, per domain, persisted as JSON.

    One file per domain. Sharing a file would let a retail question match an
    airline one on the filler words they have in common and hand back SQL over
    tables that do not exist -- caught by the validator, but only after the
    cache had already claimed a hit and skipped the generation that would have
    been right.
    """

    def __init__(
        self,
        path: Path,
        embedder: Callable[[str], list[float]] | None = None,
        threshold: float | None = None,
    ):
        self.path = path
        self._embedder = embedder
        self._threshold = threshold
        self._entries: list[CacheEntry] = []
        self.stats = CacheStats()
        self._load()

    # --- persistence -------------------------------------------------------

    def _load(self) -> None:
        """A corrupt or unreadable cache is emptied, never fatal.

        The cache is an optimisation; refusing to start because a JSON file that
        holds no irreplaceable state went bad would trade a slow pipeline for no
        pipeline.
        """
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._entries = [CacheEntry(**entry) for entry in raw]
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Ignoring unreadable semantic cache at %s: %s", self.path, exc)
            self._entries = []
        self.stats.entries = len(self._entries)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps([asdict(entry) for entry in self._entries], indent=2),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - disk problems are not our failure
            logger.warning("Could not write the semantic cache to %s: %s", self.path, exc)

    def clear(self) -> int:
        removed = len(self._entries)
        self._entries = []
        self.stats = CacheStats()
        self.save()
        return removed

    # --- the cache itself --------------------------------------------------

    def _embed(self, question: str) -> list[float]:
        if self._embedder is None:
            return []
        try:
            return self._embedder(question)
        except Exception as exc:
            # An embedding endpoint that is down must degrade to the lexical
            # backend, not fail the query: the caller is trying to answer a
            # question, and the cache is the least important thing in the path.
            logger.warning("Cache embedding failed (%s); using the lexical backend.", exc)
            return []

    def lookup(self, question: str, entities: dict[str, Any] | None = None) -> CacheHit | None:
        """The closest usable entry, or None.

        "Usable" means: same literals (rule 2), and similar enough phrasing. The
        entity check runs first and is exact -- similarity never gets a vote on
        whether two different filters are the same question.
        """
        self.stats.lookups += 1
        if not self._entries:
            return None

        key = entity_key(entities, question)
        candidates = [entry for entry in self._entries if entry.entity_key == key]
        if not candidates:
            # Something was in the cache and the only thing ruling it out was the
            # literals. Worth counting: it is the guard earning its place.
            if self._entries:
                self.stats.entity_blocked += 1
            return None

        embedding = self._embed(question)
        vector = lexical_vector(question)

        best: CacheEntry | None = None
        best_score = 0.0
        best_backend = "lexical"
        for entry in candidates:
            if embedding and entry.embedding:
                score, backend = dense_cosine(embedding, entry.embedding), "semantic"
            else:
                score, backend = sparse_cosine(vector, entry.vector), "lexical"
            if score > best_score:
                best, best_score, best_backend = entry, score, backend

        threshold = self._threshold
        if threshold is None:
            threshold = DEFAULT_THRESHOLD if best_backend == "semantic" else LEXICAL_THRESHOLD
        if best is None or best_score < threshold:
            return None

        best.hits += 1
        self.stats.hits += 1
        if best_backend == "semantic":
            self.stats.semantic_hits += 1
        else:
            self.stats.lexical_hits += 1
        self.stats.saved_prompt_tokens += best.prompt_tokens
        self.stats.saved_completion_tokens += best.completion_tokens
        self.stats.saved_cost_usd += best.cost_usd
        return CacheHit(
            sql=best.sql,
            question=best.question,
            similarity=best_score,
            backend=best_backend,
            saved_prompt_tokens=best.prompt_tokens,
            saved_completion_tokens=best.completion_tokens,
            saved_cost_usd=best.cost_usd,
        )

    def store(
        self,
        question: str,
        sql: str,
        entities: dict[str, Any] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Record SQL that validated *and* executed. Callers must not store failures."""
        if not sql.strip():
            return
        key = entity_key(entities, question)
        for entry in self._entries:
            # Exact re-ask: refresh the cost, which may have been zero the first
            # time if that answer itself came from a fallback or a cache hit.
            if entry.question == question and entry.entity_key == key:
                entry.sql = sql
                entry.prompt_tokens = entry.prompt_tokens or prompt_tokens
                entry.completion_tokens = entry.completion_tokens or completion_tokens
                entry.cost_usd = entry.cost_usd or cost_usd
                return
        self._entries.append(
            CacheEntry(
                question=question,
                sql=sql,
                entity_key=key,
                vector=lexical_vector(question),
                embedding=self._embed(question),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
            )
        )
        self.stats.entries = len(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


def build_semantic_cache(
    path: Path | None = None, threshold: float | None = None
) -> SemanticCache:
    """The active domain's cache, wired to the embedding backend when there is one.

    Imported lazily so this module stays importable (and unit-testable) without
    touching config, domains or a provider: the cache itself is pure arithmetic
    over strings, and only this factory knows where it lives or who can embed.
    """
    from semantic_query_engine.core.config import PIPELINE, load_llm_settings
    from semantic_query_engine.core.domains import active_domain
    from semantic_query_engine.core.llm_client import build_client

    settings = load_llm_settings()
    embedder: Callable[[str], list[float]] | None = None
    if settings.is_enabled and settings.supports_embeddings:
        client = build_client(settings)

        def embedder(text: str) -> list[float]:  # noqa: F811 - the guarded definition
            response = client.embeddings.create(input=text, model=settings.embedding_model)
            return list(response.data[0].embedding)

    return SemanticCache(
        path=path or active_domain().cache_path,
        embedder=embedder,
        threshold=threshold if threshold is not None else PIPELINE.semantic_cache_threshold,
    )
