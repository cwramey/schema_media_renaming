"""Single-name analysis: one filename stem (or folder name) -> NameInfo.

Works on spans over one working string so the title can be cut precisely at the first
numbering / tag / year token (SPEC §6.1 stages 2-5). Folder context is applied by the
caller (``parse`` in ``__init__``).
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from functools import lru_cache

from ._numbering import PATTERNS as NUMBERING_PATTERNS
from ._numbering import Numbering, NumberingContext, find_numbering
from ._prefilter import Prefilter
from ._rules import B0, B1, STRONG_RULES, TAG_WORDS, WEAK_RULES, TagRule, TagState

_BRACKETS = str.maketrans({"【": "[", "】": "]", "「": "[", "」": "]", "（": "(", "）": ")", "［": "[", "］": "]"})

_WEBSITE = re.compile(
    r"^\s*(?:\[\s*)?(?:www\.[\w-]+\.[a-z]{2,6}|[\w-]+\.(?:com|net|org|tv|to|me|info|cc|io|xyz|ws|"
    r"in|co|se|mx|li|nl|ru|su|club|cx|is|lol|top|vip|pw))(?:\s*\])?\s*(?:-\s*|\s+|(?<=\])\s*)",
    re.IGNORECASE,
)
_ID_TAG = re.compile(
    r"[\[\{]\s*(?:tmdb|tvdb|imdb|anidb)(?:id)?\s*[-=]\s*[\w]+\s*[\]\}]|"
    rf"{B0}(?:tmdb|tvdb|anidb)(?:id)?-\d+{B1}",
    re.IGNORECASE,
)
_LEAD_BRACKET = re.compile(r"\s*[\[\(]([^\[\]\(\)]+)[\]\)]\s*")
_BRACKET = re.compile(r"[\[\(\{]([^\[\]\(\)\{\}]*)[\]\)\}]")
_CRC = re.compile(r"^[0-9A-Fa-f]{8}$")
_YEAR_ONLY = re.compile(r"^\s*((?:19|20)\d{2})\s*$")
_COUNTRY = re.compile(r"^(?:US|UK|AU|CA|NZ|IE|JP|KR|DE|FR)$")
_YEAR = re.compile(rf"{B0}((?:19|20)\d{{2}}){B1}")
_SAMPLE = re.compile(rf"{B0}sample{B1}", re.IGNORECASE)
_PART_STRONG = re.compile(rf"{B0}(?:cd|dis[ck])[ .\-]?(?P<n>\d{{1,2}}){B1}", re.IGNORECASE)
_PART_WEAK = re.compile(rf"{B0}(?:part|pt)[ .\-]?(?P<n>\d{{1,2}}|[ivx]{{1,4}}){B1}", re.IGNORECASE)
_PLEX_EXTRA = re.compile(
    r"-(?:trailer|behindthescenes|deleted|deletedscene|featurette|interview|scene|short|other|extra)$",
    re.IGNORECASE,
)
# anime creditless / opening / ending clips: "Title - OP2", "Title - NCED"
_OP_ED = re.compile(r"(?<=\S) +- +(?:NC)?(?:OP|ED)\d{0,2}(?:v\d)?(?= |$|[\[\(])")
_GROUP_DASH = re.compile(r"-(?P<g>[A-Za-z0-9][A-Za-z0-9.@&+!']{0,29}?)(?P<tail>(?:\s*\[[^\]]*\]|\s*\([^)]*\))*)\s*$")
_GROUP_BRACKET = re.compile(r"-\s*\[(?P<g>[^\]]{1,30})\]\s*$")
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_PACK_WORDS = re.compile(
    r"(?:(?:the|complete|full|season|series|collection|pack|box|set|boxset|all|episodes?|uncut|extras?)\s*)+",
    re.IGNORECASE,
)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}
_ALNUM = re.compile(r"[A-Za-z0-9]")
_DIGIT = re.compile(r"\d")
_SPACES = re.compile(r"\s+")
_EDGE_PUNCT = re.compile(r"^[\s\-–—.,:;~]+|[\s\-–—,:;~]+$")
_TRAILING_DOTS = re.compile(r"(?<!\.[A-Za-z])\.+$")
_WORDCHAR = re.compile(r"\w")
_LOWER = re.compile(r"[a-z]")
_ANY_BRACKET = re.compile(r"[\[\(\{][^\]\)\}]*[\]\)\}]")
_SHORT_NUMBER = re.compile(r"\d{1,4}")
_NUMBERS_ONLY = re.compile(r"[\d\s.\-]+")

# Every regex below runs only when the prefilter finds one of its literal needles in the name
# (see _prefilter). ``live`` is computed once per name from the normalized text: later steps
# only mask parts of that text with spaces, which can hide needles but never create one, so
# the set stays a safe superset for every masked working copy. Positions in this list are the
# prefilter's ids; the numbering patterns come first because _numbering reads ``live`` as
# indices into its own PATTERNS.
_MISC = (_ID_TAG, _BRACKET, _SAMPLE, _PLEX_EXTRA, _OP_ED, _PART_STRONG, _PART_WEAK, _YEAR, _GROUP_BRACKET, _GROUP_DASH)
_FILTER = Prefilter([
    *NUMBERING_PATTERNS,
    *(rule.regex for rule in STRONG_RULES),
    *(rule.regex for rule in WEAK_RULES),
    *_MISC,
])
_STRONG_AT = len(NUMBERING_PATTERNS)
_WEAK_AT = _STRONG_AT + len(STRONG_RULES)
_MISC_AT = _WEAK_AT + len(WEAK_RULES)
_RULE_BY_POS = [None] * _STRONG_AT + STRONG_RULES + WEAK_RULES
(_P_ID_TAG, _P_BRACKET, _P_SAMPLE, _P_PLEX_EXTRA, _P_OP_ED, _P_PART_STRONG, _P_PART_WEAK, _P_YEAR,
 _P_GROUP_BRACKET, _P_GROUP_DASH) = range(_MISC_AT, _MISC_AT + len(_MISC))


def _live_rules(live: AbstractSet[int]) -> tuple[list[TagRule], list[TagRule]]:
    """(strong, weak) rules the prefilter left in play, in their original order."""
    order = sorted(live)
    a, b, c = bisect_left(order, _STRONG_AT), bisect_left(order, _WEAK_AT), bisect_left(order, _MISC_AT)
    by_pos = _RULE_BY_POS
    return [by_pos[i] for i in order[a:b]], [by_pos[i] for i in order[b:c]]


@dataclass
class NameInfo:
    text: str
    title: str | None = None
    year: int | None = None
    paren_year: bool = False
    numbering: Numbering | None = None
    tags: TagState = field(default_factory=TagState)
    group: str | None = None
    leading_group: bool = False
    crc32: str | None = None
    part: int | None = None
    episode_title: str | None = None
    is_sample: bool = False
    plex_extra: bool = False
    tokens: list[str] = field(default_factory=list)
    no_digits: bool = False
    dash_split: tuple[str, str] | None = None


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    for s, e in spans:  # noqa: SIM110 - a plain loop is ~2x faster than any(); runs per candidate match
        if start < e and s < end:
            return True
    return False


def _mask(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _is_tag_text(content: str, strong_rules: list[TagRule] | None = None) -> bool:
    """True when a bracket group holds only release tags ("1080p", "BD 1080p", "HEVC x265 10bit").

    ``strong_rules``: the live strong rules of a name containing ``content`` (None: every rule).
    """
    probe = TagState()
    rest = content
    for rule in STRONG_RULES if strong_rules is None else strong_rules:
        for m in rule.regex.finditer(rest):
            rule.handler(probe, m)
            rest = rest[: m.start()] + " " * (m.end() - m.start()) + rest[m.end():]
    return not _ALNUM.search(rest)


@lru_cache(maxsize=4096)  # pure; a show's title region repeats across its episodes
def clean_title(raw: str) -> str | None:
    """De-dot and tidy a raw title region. Keeps acronyms ("S.W.A.T.") and "(US)" suffixes."""
    kept: list[str] = []

    def keep_country(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        if _COUNTRY.match(inner):
            kept.append(inner)
            return f" \x00{len(kept) - 1}\x00 "
        return " "

    t = _BRACKET.sub(keep_country, raw)
    t = t.replace("_", " ")
    stripped = t.strip(" .-")
    if " " not in stripped and "." in stripped:
        pieces = [p for p in stripped.split(".") if p != ""]
        out: list[str] = []
        run: list[str] = []
        for p in pieces + [""]:
            if len(p) == 1 and p.isalpha():
                run.append(p)
                continue
            if len(run) >= 2:
                out.append(".".join(run) + ".")
            else:
                out.extend(run)
            run = []
            if p:
                out.append(p)
        t = " ".join(out)
    t = _SPACES.sub(" ", t).strip()
    t = _EDGE_PUNCT.sub("", t)
    t = _TRAILING_DOTS.sub("", t).strip()  # trailing dots, but keep "S.W.A.T."
    for i, inner in enumerate(kept):
        t = t.replace(f"\x00{i}\x00", f"({inner})")
    t = _SPACES.sub(" ", t).strip()
    if not t or not _WORDCHAR.search(t):
        return None
    if t == t.lower() and _LOWER.search(t):
        t = " ".join(w[:1].upper() + w[1:] for w in t.split(" "))
    return t


def _clean_episode_title(raw: str) -> str | None:
    t = _ANY_BRACKET.sub(" ", raw)
    t = clean_title(t)
    return t


def analyze(stem: str, *, ctx: NumberingContext | None = None) -> NameInfo:
    """Analyse one name. ``ctx`` carries season/show context from folders."""
    ctx = ctx or NumberingContext()
    if stem.isascii():  # NFKC and the bracket table leave ASCII text unchanged
        text = stem.replace("_", " ")
    else:
        text = unicodedata.normalize("NFKC", stem).translate(_BRACKETS).replace("_", " ")
    info = NameInfo(text=text)
    info.no_digits = not _DIGIT.search(text)
    work = text
    masked: list[tuple[int, int]] = []
    title_start = 0
    live = _FILTER.live(text)
    strong_rules, weak_rules = _live_rules(live)

    m = _WEBSITE.match(work)
    if m and m.end() < len(work):
        work = _mask(work, 0, m.end())
        masked.append((0, m.end()))
        title_start = m.end()

    if _P_ID_TAG in live:
        for m in _ID_TAG.finditer(work):
            work = _mask(work, m.start(), m.end())
            masked.append((m.start(), m.end()))

    # leading bracket groups: release group, or tags/CRC that precede the title
    while True:
        m = _LEAD_BRACKET.match(work, title_start)
        if not m or m.end() >= len(work.rstrip()):
            break
        content = m.group(1).strip()
        if _CRC.match(content) and not _YEAR_ONLY.match(content):
            info.crc32 = content.upper()
            work = _mask(work, m.start(), m.end())
        elif _is_tag_text(content, strong_rules):
            pass  # handled by the tag pass; still skip it for the title
        elif info.group is None and not _YEAR_ONLY.match(content):
            info.group = content
            info.leading_group = True
            work = _mask(work, m.start(), m.end())
        else:
            break
        masked.append((m.start(), m.end()))
        title_start = m.end()

    if ctx.anime is False and (info.leading_group or info.crc32):
        ctx = NumberingContext(season=ctx.season, show_folder=ctx.show_folder, anime=True)

    # bracket groups after the title start: CRC, (Year), country codes, tag bundles
    terminators: list[tuple[int, int]] = []
    paren_years: list[tuple[int, int]] = []
    year_brackets: list[tuple[int, int]] = []
    for m in _BRACKET.finditer(work, title_start) if _P_BRACKET in live else ():
        content = m.group(1).strip()
        if _CRC.match(content):  # all-digit CRCs exist too (~2% of them)
            info.crc32 = info.crc32 or content.upper()
            work = _mask(work, m.start(), m.end())
            masked.append((m.start(), m.end()))
            terminators.append((m.start(), m.end()))
            continue
        ym = _YEAR_ONLY.match(content)
        if ym:
            paren_years.append((m.start(), int(ym.group(1))))
            year_brackets.append((m.start(), m.end()))
            terminators.append((m.start(), m.end()))
            continue
        if _COUNTRY.match(content) and m.start() > title_start:
            continue
        terminators.append((m.start(), m.end()))

    consumed: list[tuple[int, int]] = list(masked)
    tags = info.tags
    for rule in strong_rules:
        for m in rule.regex.finditer(work):
            if m.end() == m.start() or _overlaps(m.start(), m.end(), consumed):
                continue
            rule.handler(tags, m)
            consumed.append((m.start(), m.end()))
            terminators.append((m.start(), m.end()))

    for m in _SAMPLE.finditer(work, title_start) if _P_SAMPLE in live else ():
        if _overlaps(m.start(), m.end(), consumed):
            continue
        info.is_sample = True
        consumed.append((m.start(), m.end()))
        if m.start() <= title_start + 1 and work[m.end():].strip(" .-"):
            title_start = m.end()
        else:
            terminators.append((m.start(), m.end()))

    pm = _PLEX_EXTRA.search(work) if _P_PLEX_EXTRA in live else None
    if pm and pm.start() > title_start:
        info.plex_extra = True
        consumed.append((pm.start(), pm.end()))
        terminators.append((pm.start(), pm.end()))
    for m in _OP_ED.finditer(work, title_start) if _P_OP_ED in live else ():
        if not _overlaps(m.start(), m.end(), consumed):
            info.plex_extra = True
            consumed.append((m.start(), m.end()))
            terminators.append((m.start(), m.end()))
            break

    for m in _PART_STRONG.finditer(work, title_start) if _P_PART_STRONG in live else ():
        if _overlaps(m.start(), m.end(), consumed) or m.start() <= title_start:
            continue
        info.part = int(m.group("n"))
        consumed.append((m.start(), m.end()))
        terminators.append((m.start(), m.end()))
        break

    num = find_numbering(work, spans=consumed, ctx=ctx, title_start=title_start, live=live)
    info.numbering = num
    if num is not None:
        consumed.append((num.start, num.end))
        terminators.append((num.start, num.end))

    cut = min((s for s, _e in terminators if s >= title_start), default=len(work))
    if num is not None and num.kind == "leading":
        cut = num.start

    # year: the last plausible year before the cut that still leaves a title in front of it
    years = [
        (m.start(), m.end(), int(m.group(1)))
        for m in (_YEAR.finditer(work, title_start) if _P_YEAR in live else ())
        if not _overlaps(m.start(), m.end(), consumed) and m.start() < cut
    ]
    title_end = cut
    year_span: tuple[int, int] | None = None
    for start, end, value in reversed(years):
        if _ALNUM.search(work[title_start:start]):
            info.year = value
            title_end = start
            year_span = (start, end)
            break
    if info.year is None and paren_years:
        info.year = paren_years[0][1]
        info.paren_year = True
    if year_span:
        consumed.append(year_span)
        terminators.append(year_span)

    # weak "Part 2" only after the year (otherwise it belongs to the title)
    if info.part is None and year_span is not None and _P_PART_WEAK in live:
        for m in _PART_WEAK.finditer(work, year_span[1]):
            if _overlaps(m.start(), m.end(), consumed):
                continue
            raw = m.group("n").lower()
            info.part = int(raw) if raw.isdigit() else _ROMAN.get(raw)
            consumed.append((m.start(), m.end()))
            terminators.append((m.start(), m.end()))
            break

    for rule in weak_rules:
        for m in rule.regex.finditer(work, title_end):
            if m.end() == m.start() or _overlaps(m.start(), m.end(), consumed):
                continue
            rule.handler(tags, m)
            consumed.append((m.start(), m.end()))
            terminators.append((m.start(), m.end()))

    # release group: leading [Group] wins; else trailing -GROUP after the title
    if not info.leading_group:
        gm = (_GROUP_BRACKET.search(work) if _P_GROUP_BRACKET in live else None) or (
            _GROUP_DASH.search(work) if _P_GROUP_DASH in live else None
        )
        if gm and gm.start() >= title_end and gm.start() > title_start:
            g = gm.group("g").strip()
            gl = g.lower()
            if (
                g
                and gl not in TAG_WORDS
                and not _SHORT_NUMBER.fullmatch(g)
                and not _overlaps(gm.start("g"), gm.end("g"), consumed)
                and not (num is not None and num.start <= gm.start() < num.end)
            ):
                info.group = g
                consumed.append((gm.start(), gm.start("g") + len(gm.group("g"))))
                terminators.append((gm.start(), gm.end("g")))

    info.title = clean_title(text[title_start:title_end]) if title_end > title_start else None

    # episode title: text between the numbering and the next tag/bracket/group
    # ("Show S05 The Internet is Coming" has one too, but "Show S01 Complete" does not)
    ep_span: tuple[int, int] | None = None
    if num is not None:
        after = [s for s, _e in terminators if s >= num.end]
        end = min(after, default=len(work))
        ep = _clean_episode_title(text[num.end:end])
        if ep and not _NUMBERS_ONLY.fullmatch(ep) and not (num.kind == "season" and _PACK_WORDS.fullmatch(ep)):
            info.episode_title = ep
            ep_span = (num.end, end)

    if num is None and info.year is None and info.title and " - " in info.title:
        left, right = info.title.split(" - ", 1)
        if left.strip() and right.strip():
            info.dash_split = (left.strip(), right.strip())

    covered_spans = consumed + year_brackets + [(0, title_end)] + ([ep_span] if ep_span else [])
    info.tokens = _leftover_tokens(text, covered_spans)
    return info


def _leftover_tokens(text: str, spans: list[tuple[int, int]]) -> list[str]:
    """Words of ``text`` that share no character with any of ``spans`` (empty spans cover nothing).

    Same result as marking every covered character and testing each word's characters, without
    the per-character Python loops.
    """
    spans = [(s, e) for s, e in spans if s < e]
    out: list[str] = []
    for m in _WORD.finditer(text):
        start, end = m.span()
        for s, e in spans:
            if start < e and s < end:
                break
        else:
            out.append(m.group(0))
    return out


__all__ = ["NameInfo", "analyze", "clean_title"]
