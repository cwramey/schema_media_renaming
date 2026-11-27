"""Filename-parser benchmark (not collected by pytest: run it by hand).

    cd backend
    python tests/metadata/bench_parser.py            # timings
    python tests/metadata/bench_parser.py --profile  # plus a cProfile top-20

Scenarios (microseconds per parse, best of ``--repeat`` runs):
  mixed-cold   every corpus path once, parser caches cleared first (worst case: no folder reuse)
  mixed-warm   the same paths again with caches warm
  big-import   a 10k-file import laid out like a real library (shows / seasons / movie folders),
               the shape the matcher sees for large jobs
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import parser_corpus  # noqa: E402

from schema_app.core import parser as parser_mod  # noqa: E402


def _clear() -> None:
    clear = getattr(parser_mod, "clear_caches", None)
    if clear is not None:
        clear()


def _time(paths: list[str], *, repeat: int, cold: bool) -> float:
    best = float("inf")
    parse = parser_mod.parse
    for _ in range(repeat):
        if cold:
            _clear()
        start = time.perf_counter()
        for p in paths:
            parse(p)
        best = min(best, time.perf_counter() - start)
    return best * 1e6 / len(paths)


def big_import(count: int = 10_000) -> list[str]:
    """Library-shaped paths: many files per season / movie folder, like one big import job."""
    names = parser_corpus.generated(seed=99, count=count)
    out: list[str] = []
    for i, name in enumerate(names):
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        show = parser_corpus.TITLES[i // 120 % len(parser_corpus.TITLES)]
        if i % 3 == 0:
            out.append(f"/data/movies/{show} ({1990 + i % 30})/{base}")
        else:
            out.append(f"/data/tv/{show}/Season {i // 40 % 9 + 1}/{base}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=5000, help="generated paths added to the golden set")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()

    mixed = parser_corpus.bench_corpus(args.count)
    library = big_import()
    for p in mixed[:200]:  # import-time warm-up (regex compile caches etc.)
        parser_mod.parse(p)

    print(f"python {sys.version.split()[0]}  mixed={len(mixed)} paths  big-import={len(library)} paths")
    print(f"  mixed-cold  {_time(mixed, repeat=args.repeat, cold=True):8.1f} us/parse")
    print(f"  mixed-warm  {_time(mixed, repeat=args.repeat, cold=False):8.1f} us/parse")
    print(f"  big-import  {_time(library, repeat=args.repeat, cold=True):8.1f} us/parse")

    if args.profile:
        _clear()
        prof = cProfile.Profile()
        prof.enable()
        for p in mixed:
            parser_mod.parse(p)
        prof.disable()
        out = io.StringIO()
        pstats.Stats(prof, stream=out).sort_stats("tottime").print_stats(20)
        print(out.getvalue())


if __name__ == "__main__":
    main()
