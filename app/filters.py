"""Filter node parameter models and the per-video predicates they compile to."""
import re
import re._parser as sre_parse
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

import regex
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.detect import Video

MAX_REGEX_TEXT = 5000
REGEX_TIMEOUT = 0.1  # seconds per match; the `regex` package enforces it, `re` cannot

Field = Literal["title", "description", "tags"]
# A predicate returns (passed, reason).
Predicate = Callable[[Video, datetime], tuple[bool, str]]


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _field_text(video: Video, name: str) -> str:
    if name == "tags":
        return "\n".join(video.tags)
    return getattr(video, name)


class KeywordParams(Params):
    include: list[str] = []
    exclude: list[str] = []
    fields: list[Field] = ["title", "description"]
    whole_word: bool = True

    @field_validator("include", "exclude")
    @classmethod
    def _no_blank(cls, v: list[str]) -> list[str]:
        if any(not t.strip() for t in v):
            raise ValueError("terms must not be blank")
        return v

    @model_validator(mode="after")
    def _something_to_match(self):
        if not self.include and not self.exclude:
            raise ValueError("needs at least one include or exclude term")
        if not self.fields:
            raise ValueError("fields must not be empty")
        return self


def _has_nested_unbounded_repeat(items, inside: bool = False) -> bool:
    """True if an unbounded repeat (*, +, {n,}) contains another one, the classic catastrophic shape."""
    for op, av in items:
        if op in (sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT):
            unbounded = av[1] == sre_parse.MAXREPEAT
            if unbounded and inside:
                return True
            if _has_nested_unbounded_repeat(av[2], inside or unbounded):
                return True
        elif op == sre_parse.SUBPATTERN:
            if _has_nested_unbounded_repeat(av[3], inside):
                return True
        elif op == sre_parse.BRANCH:
            if any(_has_nested_unbounded_repeat(b, inside) for b in av[1]):
                return True
        elif op in (sre_parse.ASSERT, sre_parse.ASSERT_NOT):
            if _has_nested_unbounded_repeat(av[1], inside):
                return True
    return False


class RegexParams(Params):
    pattern: str
    fields: list[Field] = ["title", "description"]
    ignore_case: bool = True
    negate: bool = False  # drop on match instead of requiring one

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            regex.compile(v)
        except regex.error as e:
            raise ValueError(f"invalid regex {v!r}: {e}") from e
        try:
            nested = _has_nested_unbounded_repeat(sre_parse.parse(v))
        except (re.error, RecursionError, OverflowError):
            nested = False  # syntax only the regex package understands; the runtime timeout still applies
        if nested:
            raise ValueError(f"regex {v!r} nests an unbounded repeat inside another (catastrophic backtracking risk)")
        return v

    @field_validator("fields")
    @classmethod
    def _fields_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("fields must not be empty")
        return v


class DurationParams(Params):
    min_seconds: int | None = None
    max_seconds: int | None = None

    @model_validator(mode="after")
    def _bounds(self):
        lo, hi = self.min_seconds, self.max_seconds
        if lo is None and hi is None:
            raise ValueError("needs min_seconds and/or max_seconds")
        if (lo is not None and lo < 0) or (hi is not None and hi < 0):
            raise ValueError("bounds must be non-negative")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("min_seconds is greater than max_seconds")
        return self


class ShortsParams(Params):
    mode: Literal["exclude", "only"]


class AgeParams(Params):
    max_days: float

    @field_validator("max_days", mode="before")
    @classmethod
    def _number(cls, v):
        # strict mode rejects int for float; accept plain numbers but not bools
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("max_days must be a number")
        return float(v)

    @field_validator("max_days")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("max_days must be positive")
        return v


class LiveParams(Params):
    pass


class LlmFilterParams(Params):
    prompt: str
    model: str | None = None


class LlmRouterParams(Params):
    prompt: str
    model: str | None = None
    routes: list[str] = []


def _term_pattern(term: str, whole_word: bool) -> re.Pattern[str]:
    body = re.escape(term.strip())
    if whole_word:
        body = rf"(?<!\w){body}(?!\w)"
    return re.compile(body, re.IGNORECASE)


def _keyword(p: KeywordParams) -> Predicate:
    include = [(t, _term_pattern(t, p.whole_word)) for t in p.include]
    exclude = [(t, _term_pattern(t, p.whole_word)) for t in p.exclude]

    def find(video: Video, terms) -> tuple[str, str] | None:
        for term, rx in terms:
            for name in p.fields:
                if rx.search(_field_text(video, name)):
                    return term, name
        return None

    def run(video: Video, now: datetime) -> tuple[bool, str]:
        hit = find(video, exclude)
        if hit:
            return False, f"excluded term {hit[0]!r} in {hit[1]}"
        if include:
            hit = find(video, include)
            if not hit:
                return False, "no include term matched"
            return True, f"include term {hit[0]!r} in {hit[1]}"
        return True, "no exclude term matched"

    return run


def _regex(p: RegexParams) -> Predicate:
    rx = regex.compile(p.pattern, regex.IGNORECASE if p.ignore_case else 0)

    def run(video: Video, now: datetime) -> tuple[bool, str]:
        matched = None
        for name in p.fields:
            try:
                found = rx.search(_field_text(video, name)[:MAX_REGEX_TEXT], timeout=REGEX_TIMEOUT)
            except TimeoutError:
                # Fail closed (drop) even when negated, so a pathological title is never filed by accident.
                return False, "regex timed out"
            if found:
                matched = name
                break
        if p.negate:
            return (False, f"regex matched in {matched}") if matched else (True, "regex did not match")
        return (True, f"regex matched in {matched}") if matched else (False, "regex did not match")

    return run


def _duration(p: DurationParams) -> Predicate:
    def run(video: Video, now: datetime) -> tuple[bool, str]:
        s = video.duration_seconds
        if p.min_seconds is not None and s < p.min_seconds:
            return False, f"duration {s}s below minimum {p.min_seconds}s"
        if p.max_seconds is not None and s > p.max_seconds:
            return False, f"duration {s}s above maximum {p.max_seconds}s"
        return True, f"duration {s}s in range"

    return run


def _shorts(p: ShortsParams) -> Predicate:
    def run(video: Video, now: datetime) -> tuple[bool, str]:
        if p.mode == "exclude":
            return (False, "is a Short") if video.is_short else (True, "not a Short")
        return (True, "is a Short") if video.is_short else (False, "not a Short")

    return run


def _age(p: AgeParams) -> Predicate:
    def run(video: Video, now: datetime) -> tuple[bool, str]:
        days = (now - video.published_at).total_seconds() / 86400
        if days > p.max_days:
            return False, f"{days:.1f} days old, older than {p.max_days:g}"
        return True, f"{days:.1f} days old"

    return run


def _live(p: LiveParams) -> Predicate:
    def run(video: Video, now: datetime) -> tuple[bool, str]:
        if video.live_broadcast_content != "none":
            return False, f"live state {video.live_broadcast_content!r}"
        return True, "not live"

    return run


@dataclass(frozen=True)
class FilterSpec:
    params: type[Params]
    build: Callable[[Params], Predicate] | None  # None marks a reserved type


FILTERS: dict[str, FilterSpec] = {
    "filter.keyword": FilterSpec(KeywordParams, _keyword),
    "filter.regex": FilterSpec(RegexParams, _regex),
    "filter.duration": FilterSpec(DurationParams, _duration),
    "filter.shorts": FilterSpec(ShortsParams, _shorts),
    "filter.age": FilterSpec(AgeParams, _age),
    "filter.live": FilterSpec(LiveParams, _live),
    "filter.llm": FilterSpec(LlmFilterParams, None),
}
