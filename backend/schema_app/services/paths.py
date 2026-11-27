"""Path safety helpers shared by the format engine and file operations.

Contract: docs/SPEC.md §7.1.

Schema runs on Linux but usually writes to SMB/NFS shares that Windows clients browse, so every
name it produces must be legal on Windows too: no ``<>:"/\\|?*`` or control characters, no
trailing dots or spaces, no reserved device names (``CON``, ``NUL``, ``COM1`` ...), and at most
255 bytes per component.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath

from schema_app.domain import PathMapping

MAX_COMPONENT_BYTES = 255

# Characters that are illegal in Windows/SMB file names, mapped to readable substitutes.
# ":" is handled separately (configurable, default " - ").
_SUBSTITUTES: dict[str, str] = {
    "/": "-",
    "\\": "-",
    "|": "-",
    "*": "-",
    '"': "'",
    "<": "",
    ">": "",
    "?": "",
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
    | {"COM\u00b9", "COM\u00b2", "COM\u00b3", "LPT\u00b9", "LPT\u00b2", "LPT\u00b3"}
)
# One trailing extension-like segment: ".mkv", ".en", ".forced", ".pt-BR", ".sdh".
_EXT_SEGMENT_RE = re.compile(r"\.[A-Za-z0-9][A-Za-z0-9_-]{0,9}$")
_MAX_EXT_SEGMENTS = 4
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


# --------------------------------------------------------------------------- components


def sanitize_component(value: str, *, colon: str = " - ") -> str:
    """Make one value safe to embed in a single path component.

    Replaces path separators and characters illegal on Windows/SMB (<>:"/\\|?* and
    control chars) using sensible substitutes (":" -> ``colon``), collapses whitespace,
    and strips leading/trailing spaces and trailing dots. Never returns "." or "..".
    The result is NFC-normalised: an NFD title (common in macOS-made metadata) would otherwise
    create a second folder next to the NFC one that looks identical in every client.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = text.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    text = _CONTROL_RE.sub("", text)
    # "Title: Subtitle" -> "Title -  Subtitle"; the whitespace collapse below tidies the double space.
    text = text.replace(":", colon)
    for bad, good in _SUBSTITUTES.items():
        if bad in text:
            text = text.replace(bad, good)
    text = _WHITESPACE_RE.sub(" ", text)
    return _strip_edges(text)


def _strip_edges(text: str) -> str:
    """Strip spaces on both ends and dots/spaces at the end (Windows silently drops those)."""
    text = text.strip(" ")
    while text and text[-1] in ". ":
        text = text[:-1]
    text = text.lstrip(" ")
    return "" if text in {".", ".."} else text


def fold_name(name: str) -> str:
    """Comparison key for names that SMB clients, macOS and Jellyfin treat as one: case-folded and
    NFC-normalised ("Doctor Who" ~ "doctor who", NFD "Amélie" ~ NFC "Amélie")."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def is_reserved_name(name: str) -> bool:
    """True for Windows device names, with or without an extension ("CON", "nul.txt", "COM1.mkv")."""
    base = name.split(".", 1)[0].rstrip(" ").upper()
    return base in _RESERVED_NAMES


def _unreserve(name: str) -> str:
    if not is_reserved_name(name):
        return name
    head, dot, tail = name.partition(".")
    return f"{head.rstrip(' ')}_{dot}{tail}"


def split_extension(name: str, *, max_segments: int = _MAX_EXT_SEGMENTS) -> tuple[str, str]:
    """Split ``name`` into (stem, suffix) where suffix is up to ``max_segments`` short trailing
    dot segments: "Show - S01E01.eng.forced.ass" -> ("Show - S01E01", ".eng.forced.ass").

    Segments containing spaces are never treated as extensions ("Mr. Robot" stays whole).
    """
    stem, suffix = name, ""
    for _ in range(max_segments):
        match = _EXT_SEGMENT_RE.search(stem)
        if not match or match.start() == 0:
            break
        suffix = match.group(0) + suffix
        stem = stem[: match.start()]
    return stem, suffix


def _truncate_utf8(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def fit_component(name: str, *, max_bytes: int = MAX_COMPONENT_BYTES, reserve: int = 0) -> str:
    """Trim ``name`` to ``max_bytes - reserve`` UTF-8 bytes, cutting the stem and keeping the
    extension and sidecar suffixes ("...Title.en.forced.srt") intact."""
    limit = max_bytes - reserve
    if len(name.encode("utf-8")) <= limit:
        return name
    stem, suffix = split_extension(name)
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix and suffix_bytes < limit // 2:
        stem = _truncate_utf8(stem, limit - suffix_bytes)
        stem = _strip_edges(stem) or "_"
        return stem + suffix
    return _strip_edges(_truncate_utf8(name, limit)) or "_"


def safe_filename(name: str, *, colon: str = " - ", max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    """Sanitize one complete file or folder name: illegal characters, reserved device names and
    the byte limit. Raises ValueError when nothing usable is left."""
    cleaned = _unreserve(sanitize_component(name, colon=colon))
    cleaned = fit_component(cleaned, max_bytes=max_bytes)
    if not cleaned:
        raise ValueError(f"name {name!r} is empty after sanitization")
    return cleaned


# --------------------------------------------------------------------------- relative paths


def sanitize_relative_path(path: str) -> str:
    """Sanitize a rendered relative path ("A/B/C.mkv") component by component.

    Drops empty components, rejects absolute paths and ".." traversal (raise ValueError),
    trims each component to 255 bytes UTF-8 while preserving the extension.

    Both "/" and "\\" separate folders (format literals may use either). Each component is
    also cleaned like :func:`sanitize_component` (literal text in a format can contain ":"),
    and Windows device names get a trailing underscore ("CON" -> "CON_"). The result uses "/"
    separators. Raises ValueError when nothing is left (an empty destination is never valid).
    """
    if path is None:
        raise ValueError("path is empty")
    text = str(path).strip()
    if not text:
        raise ValueError("path is empty")
    if text.startswith(("/", "\\")) or _DRIVE_RE.match(text):
        raise ValueError(f"absolute paths are not allowed here: {text!r}")
    parts: list[str] = []
    for raw in re.split(r"[\\/]", text):
        stripped = raw.strip()
        if stripped == "..":
            raise ValueError(f"'..' is not allowed in a destination path: {text!r}")
        if stripped in {"", "."}:
            continue
        cleaned = _unreserve(sanitize_component(stripped))
        cleaned = fit_component(cleaned)
        if cleaned:
            parts.append(cleaned)
    if not parts:
        raise ValueError(f"path {text!r} is empty after sanitization")
    return "/".join(parts)


# --------------------------------------------------------------------------- boundaries


def _norm(path: str | os.PathLike[str]) -> str:
    """Absolute, symlink-resolved, case-normalized (on Windows) form used for containment checks."""
    resolved = os.path.realpath(os.path.abspath(os.fspath(path)))
    return os.path.normcase(resolved)


def _contains(root: str, candidate: str) -> bool:
    if candidate == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return candidate.startswith(prefix)


# Schema's own data folder (``/config``: schema.db, secret.key, the trash) is off limits to every file
# operation, the file browser and every root, whatever ``allowed_roots`` says: with it reachable, a
# session could copy secret.key into the library and forge cookies. ``_EXEMPT`` holds sub-folders that
# stay usable (the generated demo library in demo mode). Both are _norm() forms; set by protect().
_PROTECTED: tuple[str, ...] = ()
_EXEMPT: tuple[str, ...] = ()


def protect(paths: Iterable[str | os.PathLike[str]], *, exempt: Iterable[str | os.PathLike[str]] = ()) -> None:
    """Make ``paths`` (and everything below them, symlinks resolved) unreachable through
    :func:`is_within`, :func:`is_within_nofollow` and :func:`root_problem`, except below ``exempt``.
    Replaces any earlier call (one process has one data folder; tests build several)."""
    global _PROTECTED, _EXEMPT
    _PROTECTED = tuple(dict.fromkeys(_norm(p) for p in paths if p and str(p).strip()))
    _EXEMPT = tuple(dict.fromkeys(_norm(p) for p in exempt if p and str(p).strip()))


def protected_paths() -> tuple[str, ...]:
    """The resolved folders :func:`protect` made unreachable."""
    return _PROTECTED


def _in_protected(candidate: str) -> bool:
    return any(_contains(p, candidate) for p in _PROTECTED) and not any(_contains(e, candidate) for e in _EXEMPT)


def is_protected_path(path: str | Path, *, follow: bool = True) -> bool:
    """True when ``path`` is (or, resolved, lies inside) a folder given to :func:`protect`.
    ``follow=False`` doesn't resolve a symlink in the last component (it is the entry itself)."""
    if not _PROTECTED or path is None or str(path) == "":
        return False
    try:
        if follow:
            return _in_protected(_norm(path))
        p = os.path.abspath(os.fspath(path))
        parent, name = os.path.split(p)
        candidate = os.path.normcase(os.path.join(os.path.realpath(parent), name)) if name else _norm(p)
        return _in_protected(candidate)
    except (OSError, ValueError):
        return True  # can't tell: refuse


def root_problem(root: str | Path) -> str | None:
    """Why ``root`` can't serve as an allowed / library / watch / destination root, or None.

    Refused: a filesystem root ("/", "C:\\"), and any folder that is, holds or sits inside Schema's
    data folder (see :func:`protect`; demo-mode exemptions apply to folders inside it)."""
    try:
        candidate = _norm(root)
    except (OSError, ValueError):
        return "it can't be resolved"
    if os.path.dirname(candidate) == candidate:
        return "a whole filesystem can't be a root; pick the media folders"
    for data in _PROTECTED:
        if _contains(candidate, data):
            return f"it contains Schema's data folder {data}"
    if _in_protected(candidate):
        return "it is inside Schema's data folder"
    return None


def is_within(path: str | Path, roots: list[str]) -> bool:
    """True if the resolved ``path`` is inside one of ``roots`` (resolved, case-insensitive on Windows)
    and not inside Schema's data folder (:func:`protect`), whatever the roots say."""
    if not roots or path is None or str(path) == "":
        return False
    try:
        candidate = _norm(path)
    except (OSError, ValueError):
        return False
    if _in_protected(candidate):
        return False
    for root in roots:
        if not root or not str(root).strip():
            continue
        try:
            if _contains(_norm(root), candidate):
                return True
        except (OSError, ValueError):
            continue
    return False


def is_within_nofollow(path: str | Path, roots: list[str]) -> bool:
    """Like :func:`is_within` but does not follow a symlink in the *last* component.

    Used for destinations: the entry itself (which may be a symlink about to be replaced, or not
    exist yet) lives where its parent directory is.
    """
    p = Path(os.path.abspath(os.fspath(path)))
    if p.parent == p:  # filesystem root
        return is_within(p, roots)
    parent_ok = is_within(p.parent, roots)
    if not parent_ok:
        return False
    if is_protected_path(p, follow=False):  # the data folder itself, seen from its parent
        return False
    # The parent is inside; the entry is inside unless it *is* a root boundary edge case.
    return p.name not in {"", ".", ".."}


def containing_root(path: str | Path, roots: list[str]) -> str | None:
    """The (resolved) root that contains ``path``, preferring the deepest one."""
    try:
        candidate = _norm(path)
    except (OSError, ValueError):
        return None
    best: str | None = None
    for root in roots:
        if not root or not str(root).strip():
            continue
        try:
            resolved = os.path.realpath(os.path.abspath(root))
        except (OSError, ValueError):
            continue
        if _contains(os.path.normcase(resolved), candidate) and (best is None or len(resolved) > len(best)):
            best = resolved
    return best


# --------------------------------------------------------------------------- path mapping


def _looks_windows(path: str) -> bool:
    return bool(_DRIVE_RE.match(path)) or path.startswith("\\\\") or ("\\" in path and "/" not in path)


def _split_parts(path: str) -> tuple[list[str], bool]:
    """Split into comparable parts; returns (parts, windows_style)."""
    windows = _looks_windows(path)
    pure = PureWindowsPath(path) if windows else PurePosixPath(path.replace("\\", "/"))
    return [p for p in pure.parts], windows


def map_path(path: str, mappings: Iterable[PathMapping], *, reverse: bool = False) -> str:
    """Translate ``path`` with the longest matching ``source`` prefix of ``mappings`` (component-wise,
    case-insensitive for Windows-style paths). ``reverse=True`` maps target -> source.

    The remainder keeps its components and adopts the target's separator style, so
    "/downloads/tv/x.mkv" with {"/downloads" -> "D:\\Downloads"} becomes "D:\\Downloads\\tv\\x.mkv".
    Returns ``path`` unchanged when nothing matches.
    """
    if not path:
        return path
    parts, windows = _split_parts(path)
    best: tuple[int, list[str], str] | None = None
    for mapping in mappings:
        src = mapping.target if reverse else mapping.source
        dst = mapping.target if not reverse else mapping.source
        if not src or not dst:
            continue
        src_parts, src_windows = _split_parts(src.rstrip("/\\") or src)
        if len(src_parts) > len(parts):
            continue
        fold = windows or src_windows
        head = parts[: len(src_parts)]
        same = (
            [p.lower().rstrip("\\/") for p in head] == [p.lower().rstrip("\\/") for p in src_parts]
            if fold
            else [p.rstrip("/") for p in head] == [p.rstrip("/") for p in src_parts]
        )
        if same and (best is None or len(src_parts) > best[0]):
            best = (len(src_parts), parts[len(src_parts) :], dst)
    if best is None:
        return path
    _, rest, dst = best
    if not rest:
        return dst
    if _looks_windows(dst):
        return str(PureWindowsPath(dst, *rest))
    return str(PurePosixPath(dst.replace("\\", "/"), *rest))
