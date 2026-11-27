"""Container assets without Docker: static Dockerfile checks, the entrypoint's user logic (run with
stubbed system commands), the healthcheck script, the deployment templates and the ignore files."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "Dockerfile"
ENTRYPOINT = REPO / "deploy" / "docker" / "entrypoint.sh"
HEALTHCHECK = REPO / "deploy" / "docker" / "healthcheck.py"
ROOT_COMPOSE = REPO / "docker-compose.yml"
UGREEN_COMPOSE = REPO / "deploy" / "ugreen" / "docker-compose.yml"
UNRAID_TEMPLATE = REPO / "deploy" / "unraid" / "schema.xml"
DEPLOYMENT_DOC = REPO / "docs" / "DEPLOYMENT.md"
ENTRYPOINT_CAPS = {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}


# --------------------------------------------------------------------------- Dockerfile


def _instructions() -> list[tuple[str, str]]:
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keyword, _, rest = line.partition(" ")
            out.append((keyword.upper(), rest.strip()))
    return out


def test_dockerfile_stages_and_runtime() -> None:
    ins = _instructions()
    froms = [rest for kw, rest in ins if kw == "FROM"]
    assert froms[0].startswith("node:${NODE_VERSION}-alpine AS frontend")
    assert froms[-1].startswith("python:${PYTHON_VERSION}-slim AS runtime")
    args = dict(rest.split("=", 1) for kw, rest in ins if kw == "ARG" and "=" in rest)
    assert args["NODE_VERSION"] == "22" and args["PYTHON_VERSION"] == "3.12"
    runs = " ".join(rest for kw, rest in ins if kw == "RUN")
    assert "npm ci" in runs and "npm run build" in runs
    for package in ("mediainfo", "tini", "gosu"):
        assert re.search(rf"apt-get install[^&]*\b{package}\b", runs), package
    assert ("EXPOSE", "8420") in ins
    assert any(kw == "HEALTHCHECK" and "schema-healthcheck" in rest for kw, rest in ins)
    assert ("ENTRYPOINT", '["/usr/bin/tini", "--", "/usr/local/bin/schema-entrypoint"]') in ins
    assert ("CMD", '["python", "-m", "schema_app"]') in ins
    env = " ".join(rest for kw, rest in ins if kw == "ENV")
    assert "SCHEMA_DATA_DIR=/config" in env and "SCHEMA_STATIC_DIR=/app/web" in env
    assert "PUID=" not in env  # must stay unset so USER_ID/GROUP_ID can apply
    assert "PYTHONDONTWRITEBYTECODE=1" in env  # no .pyc writes: the root filesystem may be read-only
    assert not any(kw == "USER" for kw, _ in ins)  # root mode stays the default for existing installs


def test_dockerfile_frontend_always_uses_the_lockfile() -> None:
    ins = _instructions()
    runs = [rest for kw, rest in ins if kw == "RUN"]
    assert not any("npm install" in run for run in runs)  # no fallback that ignores the lockfile
    assert ("COPY", "frontend/package.json frontend/package-lock.json ./") in ins


def test_dockerfile_installs_python_dependencies_before_the_code() -> None:
    """Dependencies come from a layer that only sees pyproject.toml; the package goes in with --no-deps."""
    ins = _instructions()
    backend = ins[next(i for i, (kw, rest) in enumerate(ins) if kw == "FROM" and "AS backend" in rest) :]
    backend = backend[: next(i for i, (kw, _) in enumerate(backend[1:], 1) if kw == "FROM")]

    def index(predicate) -> int:  # type: ignore[no-untyped-def]
        return next(i for i, item in enumerate(backend) if predicate(*item))

    copy_pyproject = index(lambda kw, rest: kw == "COPY" and rest.startswith("backend/pyproject.toml "))
    deps = index(lambda kw, rest: kw == "RUN" and "pip install -r" in rest)
    copy_code = index(lambda kw, rest: kw == "COPY" and rest.startswith("backend/schema_app "))
    package = index(lambda kw, rest: kw == "RUN" and "--no-deps" in rest)
    assert copy_pyproject < deps < copy_code < package
    # nothing between the pyproject copy and the dependency install brings in the source tree
    assert not any(kw in ("COPY", "ADD") for kw, _ in backend[copy_pyproject + 1 : deps])
    assert "tomllib" in backend[deps][1] and "dependencies" in backend[deps][1]
    install = backend[package][1]
    assert "--no-build-isolation" in install and "pip install --no-deps" in install


def test_dockerfile_dependency_list_matches_pyproject() -> None:
    """The deps layer reads [project].dependencies and [build-system].requires; both must exist."""
    import tomllib

    data = tomllib.loads((REPO / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] and data["build-system"]["requires"]


# --------------------------------------------------------------------------- .dockerignore


def _dockerignore_rules() -> list[tuple[bool, re.Pattern[str]]]:
    """.dockerignore patterns as regexes, following Docker's matcher (moby/patternmatcher)."""
    rules = []
    for raw in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = line[1:] if negate else line
        pattern = pattern.strip("/")
        out, i = "", 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                out, i = out + "(?:.*/)?", i + 3
            elif pattern.startswith("**", i):
                out, i = out + ".*", i + 2
            elif pattern[i] == "*":
                out, i = out + "[^/]*", i + 1
            elif pattern[i] == "?":
                out, i = out + "[^/]", i + 1
            elif pattern[i] == "[":
                end = pattern.index("]", i)
                out, i = out + pattern[i : end + 1], end + 1
            else:
                out, i = out + re.escape(pattern[i]), i + 1
        rules.append((negate, re.compile(out)))
    return rules


def dockerignored(rel: str) -> bool:
    """Whether Docker leaves ``rel`` (posix, relative to the repo) out of the build context."""
    parts = rel.split("/")
    candidates = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]  # a matched folder hides its files
    excluded = False
    for negate, rx in _dockerignore_rules():
        if any(rx.fullmatch(candidate) for candidate in candidates):
            excluded = not negate
    return excluded


def _tracked(prefix: str) -> list[str]:
    git = shutil.which("git")
    if git is None:
        return []
    proc = subprocess.run([git, "-C", str(REPO), "ls-files", "--", prefix],
                          capture_output=True, text=True, check=False)  # fmt: skip
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def test_dockerfile_copy_sources_exist_and_are_not_ignored() -> None:
    for kw, rest in _instructions():
        if kw != "COPY" or rest.startswith("--from"):
            continue
        *sources, _dest = rest.split()
        for source in sources:
            pattern = source.rstrip("/")
            matches = list(REPO.glob(pattern))
            assert matches, f"COPY source missing: {source}"
            for match in matches:
                rel = match.relative_to(REPO).as_posix()
                assert not dockerignored(rel), f"{rel} is excluded by .dockerignore"
                for tracked in _tracked(rel) if match.is_dir() else []:
                    assert not dockerignored(tracked), f"{tracked} is excluded by .dockerignore"


@pytest.mark.parametrize(
    "rel",
    [
        ".git/HEAD",
        ".idea/workspace.xml",
        ".tool-state/worktrees/some-branch/backend/.venv/lib/site.py",
        "appdata/schema/secret.key",
        "appdata/schema/trash/2026-11-25/120000-1/Movie (2020).mkv",
        "appdata/schema/schema.db",
        "backend/data/schema.db-wal",
        "somewhere/cache.sqlite3-shm",
        "somewhere/schema.db-journal",
        "deploy/secret.key",
        ".env",
        ".env.production",
        "frontend/.env",
        "frontend/.env.local",
        "frontend/.env.production.local",
        "frontend/node_modules/react/index.js",
        "backend/.venv/pyvenv.cfg",
    ],
)
def test_dockerignore_keeps_secrets_and_state_out(rel: str) -> None:
    assert dockerignored(rel)


@pytest.mark.parametrize(
    "rel",
    ["Dockerfile", "frontend/package-lock.json", "frontend/src/main.tsx", "backend/pyproject.toml",
     "backend/schema_app/__init__.py", "deploy/docker/entrypoint.sh", "deploy/hooks/sabnzbd/schema_notify.py"],
)  # fmt: skip
def test_dockerignore_keeps_build_inputs(rel: str) -> None:
    assert not dockerignored(rel)


# --------------------------------------------------------------------------- .gitignore


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize(
    ("rel", "ignored"),
    [
        ("appdata/schema/secret.key", True),
        ("appdata/schema/schema.db", True),
        ("appdata/schema/trash/2026-11-25/a.mkv", True),
        ("backend/data/schema.db-wal", True),
        ("somewhere/schema.db-wal", True),
        ("somewhere/schema.db-shm", True),
        ("somewhere/cache.sqlite3-wal", True),
        ("somewhere/secret.key", True),
        (".env", True),
        ("backend/.env", True),
        ("frontend/.env.local", True),
        ("frontend/.env.production.local", True),
        (".env.example", False),
        ("frontend/.env.example", False),
        ("backend/schema_app/main.py", False),
        ("deploy/docker/entrypoint.sh", False),
    ],
)
def test_gitignore(rel: str, ignored: bool) -> None:
    proc = subprocess.run([shutil.which("git") or "git", "-C", str(REPO), "check-ignore", "-q", "--no-index", rel],
                          capture_output=True, text=True, check=False)  # fmt: skip
    assert proc.returncode in (0, 1), proc.stderr
    assert (proc.returncode == 0) is ignored


# --------------------------------------------------------------------------- entrypoint


def _sh() -> str | None:
    found = shutil.which("sh")
    if not found or (os.name == "nt" and "system32" in found.lower()):
        return None
    return found


def _gnu_find() -> str | None:
    sh = _sh()
    if sh is None:
        return None
    if os.name == "nt":  # Git for Windows ships GNU find next to sh; System32's find.exe is something else
        candidate = Path(sh).parent / "find.exe"
        return str(candidate) if candidate.is_file() else None
    return shutil.which("find")


SETUP_STUB = '[ -z "${FAIL_SETUP:-}" ] || exit 10; echo "%s $*" >> "$LOG"'
STUBS = {
    "id": """case "$1" in
  -u) echo "${FAKE_UID:-0}" ;;
  -g) echo "${FAKE_GID:-${FAKE_UID:-0}}" ;;
  -G) echo "${FAKE_GIDS:-${FAKE_GID:-${FAKE_UID:-0}}}" ;;
  *) echo "uid=${FAKE_UID:-0}" ;;
esac""",
    "getent": 'echo "getent $*" >> "$LOG"; exit 2',
    "groupadd": SETUP_STUB % "groupadd",
    "useradd": SETUP_STUB % "useradd",
    "usermod": SETUP_STUB % "usermod",
    # REAL_FIND runs GNU find for real (it then calls the chown stub below)
    "find": 'if [ -n "${REAL_FIND:-}" ]; then exec "$REAL_FIND" "$@"; fi; echo "find $*" >> "$LOG"',
    "chown": 'echo "chown $*" >> "$LOG"',
    "gosu": 'if [ "$2" = "test" ]; then exit 0; fi; echo "gosu $*" >> "$LOG"; echo "ran as $1: $2 $3 $4"',
}


def run_entrypoint(
    tmp_path: Path, env: dict[str, str], *cmd: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n", newline="\n")
        stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    full_env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "LOG": log.as_posix(),
        "SCHEMA_DATA_DIR": (tmp_path / "config").as_posix(),
        **env,
    }
    if os.name == "nt":
        full_env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "C:\\Windows")
    proc = subprocess.run([_sh(), ENTRYPOINT.as_posix(), *(cmd or ("python", "-m", "schema_app"))],  # type: ignore[list-item]
                          capture_output=True, text=True, env=full_env, timeout=30, check=False)  # fmt: skip
    return proc, log.read_text().splitlines()


needs_sh = pytest.mark.skipif(_sh() is None, reason="needs a POSIX sh")
SETUP_COMMANDS = ("groupadd", "useradd", "usermod", "find", "gosu")


def _setup_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if c.split(" ", 1)[0] in SETUP_COMMANDS]


# ----- root mode


@needs_sh
def test_entrypoint_defaults_to_1000(tmp_path: Path) -> None:
    proc, calls = run_entrypoint(tmp_path, {})
    assert proc.returncode == 0, proc.stderr
    assert "groupadd -o -g 1000 schema" in calls
    assert any(c.startswith("useradd -o -u 1000 -g 1000") for c in calls)
    assert calls[-1] == "gosu schema python -m schema_app"
    assert (tmp_path / "config").is_dir()


@needs_sh
def test_entrypoint_accepts_jlesage_aliases(tmp_path: Path) -> None:
    _, calls = run_entrypoint(tmp_path, {"USER_ID": "99", "GROUP_ID": "100"})
    assert "groupadd -o -g 100 schema" in calls and any("useradd -o -u 99 -g 100" in c for c in calls)
    _, calls = run_entrypoint(tmp_path, {"USER_ID": "99", "GROUP_ID": "100", "PUID": "1001", "PGID": "1002"})
    assert any("useradd -o -u 1001 -g 1002" in c for c in calls)


@needs_sh
def test_entrypoint_supplementary_groups(tmp_path: Path) -> None:
    proc, calls = run_entrypoint(tmp_path, {"SUP_GROUP_IDS": "2001, 2002"})
    assert proc.returncode == 0, proc.stderr
    assert "groupadd -o -g 2001 schema-2001" in calls and "usermod -a -G schema-2002 schema" in calls


@needs_sh
def test_entrypoint_never_chowns_media(tmp_path: Path) -> None:
    _, calls = run_entrypoint(tmp_path, {"PUID": "1000", "PGID": "1000"})
    finds = [c for c in calls if c.startswith("find ")]
    assert len(finds) == 1 and (tmp_path / "config").as_posix() in finds[0]
    assert not any("/storage" in c or "/watch" in c or "/output" in c for c in calls)


@needs_sh
def test_entrypoint_chown_never_follows_symlinks(tmp_path: Path) -> None:
    _, calls = run_entrypoint(tmp_path, {"PUID": "1000", "PGID": "1000"})
    (find,) = [c for c in calls if c.startswith("find ")]
    assert "-xdev ! -type l" in find  # symlinks are skipped ...
    assert "-exec chown -h 1000:1000" in find  # ... and chown would act on a link, not its target


@needs_sh
@pytest.mark.skipif(_gnu_find() is None, reason="needs GNU find")
def test_entrypoint_chown_skips_a_planted_symlink_with_real_find(tmp_path: Path) -> None:
    """A symlink in /config pointing at the entrypoint must not reach chown (which runs as root)."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "schema.db").write_text("x")
    target = tmp_path / "schema-entrypoint"
    target.write_text("#!/bin/sh\n")
    with contextlib.suppress(OSError):  # e.g. Windows without developer mode: still run the real find
        (config / "planted").symlink_to(target)
    proc, calls = run_entrypoint(tmp_path, {"PUID": "4242", "PGID": "4243", "REAL_FIND": _gnu_find() or ""})
    assert proc.returncode == 0, proc.stderr
    chowns = [c for c in calls if c.startswith("chown ")]
    assert chowns and all(c.startswith("chown -h 4242:4243 ") for c in chowns)
    chowned = " ".join(chowns)
    assert "schema.db" in chowned
    assert "planted" not in chowned and target.as_posix() not in chowned
    if not (config / "planted").is_symlink():
        pytest.skip("checked chown -h with real find, but can't create a symlink here")


@needs_sh
@pytest.mark.parametrize(
    "env",
    [
        {"PUID": "0"},
        {"PGID": "0"},
        {"PUID": "0", "PGID": "0"},
        {"PUID": "000"},  # leading zeros are still root
        {"USER_ID": "0", "GROUP_ID": "100"},
    ],
)
def test_entrypoint_refuses_root(tmp_path: Path, env: dict[str, str]) -> None:
    proc, calls = run_entrypoint(tmp_path, env)
    assert proc.returncode == 64
    assert "refusing to run Schema as root" in proc.stderr and "SCHEMA_ALLOW_ROOT=1" in proc.stderr
    assert _setup_calls(calls) == []


@needs_sh
def test_entrypoint_root_only_with_explicit_opt_out(tmp_path: Path) -> None:
    proc, calls = run_entrypoint(
        tmp_path, {"PUID": "0", "PGID": "0", "SCHEMA_ALLOW_ROOT": "1"}, "echo", "as-root"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "as-root" and _setup_calls(calls) == []
    assert "warning: running as root" in proc.stderr
    # a root group with a normal user is allowed too, then the user is set up as usual
    proc, calls = run_entrypoint(tmp_path, {"PUID": "1000", "PGID": "0", "SCHEMA_ALLOW_ROOT": "1"})
    assert proc.returncode == 0, proc.stderr
    assert any(c.startswith("useradd -o -u 1000 -g 0") for c in calls)


@needs_sh
@pytest.mark.parametrize(("sup", "message"), [("0", "root group"), ("2001,0", "root group"), ("00", "root group"),
                                              ("2001,abc", "group ids"), ("2001;2002", "group ids")])  # fmt: skip
def test_entrypoint_validates_supplementary_groups(tmp_path: Path, sup: str, message: str) -> None:
    proc, calls = run_entrypoint(tmp_path, {"SUP_GROUP_IDS": sup})
    assert proc.returncode == 64 and message in proc.stderr
    assert _setup_calls(calls) == []  # validated before anything is created


@needs_sh
def test_entrypoint_rejects_bad_ids(tmp_path: Path) -> None:
    proc, calls = run_entrypoint(tmp_path, {"PUID": "abc"})
    assert proc.returncode == 64 and "must be numbers" in proc.stderr and _setup_calls(calls) == []
    proc, _ = run_entrypoint(tmp_path, {"PGID": "-1"})
    assert proc.returncode == 64


@needs_sh
def test_entrypoint_explains_a_read_only_root_filesystem(tmp_path: Path) -> None:
    proc, _ = run_entrypoint(tmp_path, {"FAIL_SETUP": "1"})
    assert proc.returncode == 71
    assert "read-only root filesystem" in proc.stderr and 'user: "1000:1000"' in proc.stderr


# ----- UMASK (both modes)


@needs_sh
@pytest.mark.parametrize("umask", ["abc", "08", "0x22", "-022", "02222", "22 ", "u=rwx"])
def test_entrypoint_rejects_invalid_umask(tmp_path: Path, umask: str) -> None:
    proc, calls = run_entrypoint(tmp_path, {"UMASK": umask})
    assert proc.returncode == 64 and "UMASK must be an octal mask" in proc.stderr
    assert _setup_calls(calls) == []


@needs_sh
def test_entrypoint_applies_umask(tmp_path: Path) -> None:
    proc, _ = run_entrypoint(tmp_path, {"FAKE_UID": "1000", "UMASK": "002"}, "sh", "-c", "umask")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() in ("0002", "002") and "warning" not in proc.stderr


@needs_sh
@pytest.mark.parametrize("umask", ["000", "0", "004", "0001"])
def test_entrypoint_warns_about_world_writable_umask(tmp_path: Path, umask: str) -> None:
    proc, _ = run_entrypoint(tmp_path, {"FAKE_UID": "1000", "UMASK": umask}, "true")
    assert proc.returncode == 0 and "writable by everyone" in proc.stderr


# ----- non-root mode (user: "PUID:PGID", read-only root filesystem)


@needs_sh
def test_entrypoint_non_root_mode_writes_nothing_and_just_execs(tmp_path: Path) -> None:
    proc, calls = run_entrypoint(tmp_path, {"FAKE_UID": "1000"}, "sh", "-c", 'echo "$HOME"')
    assert proc.returncode == 0, proc.stderr
    assert calls == []  # no groupadd/useradd/chown/gosu: nothing outside /config is touched
    assert proc.stdout.strip().endswith("config")  # HOME points into /config, not the read-only /


@needs_sh
def test_entrypoint_non_root_mode_refuses_the_root_group(tmp_path: Path) -> None:
    proc, _ = run_entrypoint(tmp_path, {"FAKE_UID": "1000", "FAKE_GIDS": "1000 0"}, "true")
    assert proc.returncode == 64 and "root group" in proc.stderr
    proc, _ = run_entrypoint(tmp_path, {"FAKE_UID": "1000", "FAKE_GID": "0"}, "true")
    assert proc.returncode == 64
    proc, _ = run_entrypoint(
        tmp_path, {"FAKE_UID": "1000", "FAKE_GIDS": "1000 0", "SCHEMA_ALLOW_ROOT": "1"}, "true"
    )
    assert proc.returncode == 0, proc.stderr


@needs_sh
def test_entrypoint_non_root_mode_notes_an_ignored_puid(tmp_path: Path) -> None:
    proc, _ = run_entrypoint(tmp_path, {"FAKE_UID": "1000", "PUID": "1001"}, "true")
    assert proc.returncode == 0 and "PUID=1001 is ignored" in proc.stderr
    proc, _ = run_entrypoint(tmp_path, {"FAKE_UID": "1000", "PUID": "1000"}, "true")
    assert "ignored" not in proc.stderr


@needs_sh
def test_entrypoint_non_root_mode_needs_a_writable_config(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    proc, _ = run_entrypoint(
        tmp_path, {"FAKE_UID": "1000", "SCHEMA_DATA_DIR": f"{blocker.as_posix()}/config"}, "true"
    )
    assert (
        proc.returncode == 73
        and "not writable by uid 1000" in proc.stderr
        and "chown -R 1000:" in proc.stderr
    )


# --------------------------------------------------------------------------- healthcheck


def _load_healthcheck():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("schema_healthcheck", HEALTHCHECK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Resp(io.BytesIO):
    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _answer(monkeypatch: pytest.MonkeyPatch, hc, body: bytes) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(hc.urllib.request, "urlopen", lambda url, timeout: _Resp(body))


def test_healthcheck(monkeypatch: pytest.MonkeyPatch) -> None:
    hc = _load_healthcheck()
    seen: list[str] = []

    def ok(url: str, timeout: float) -> _Resp:
        seen.append(url)
        return _Resp(json.dumps({"status": "ok", "version": "0.1.0", "commit": "abc"}).encode())

    monkeypatch.setattr(hc.urllib.request, "urlopen", ok)
    monkeypatch.setenv("SCHEMA_HOST", "0.0.0.0")
    monkeypatch.setenv("SCHEMA_PORT", "8420")
    assert hc.main() == 0 and seen == ["http://127.0.0.1:8420/api/health"]

    _answer(monkeypatch, hc, b'{"status":"error"}')
    assert hc.main() == 1

    def down(url: str, timeout: float) -> _Resp:
        raise OSError("connection refused")

    monkeypatch.setattr(hc.urllib.request, "urlopen", down)
    assert hc.main() == 1


@pytest.mark.parametrize(
    "body",
    [
        {"status": "ok"},  # anonymous answer without version/commit
        {"status": "ok", "db": "ok"},
        {"status": "OK"},
    ],
)
def test_healthcheck_relies_only_on_status_ok(monkeypatch: pytest.MonkeyPatch, body: dict[str, str]) -> None:
    hc = _load_healthcheck()
    _answer(monkeypatch, hc, json.dumps(body).encode())
    assert hc.main() == 0


@pytest.mark.parametrize(
    "body",
    [b"{}", b'{"version": "0.1.0"}', b'{"status": "degraded"}', b'{"status": "unhealthy", "db": "error"}',
     b'{"status": null}', b'["ok"]', b'"ok"', b"", b"<html>proxy error</html>"],
)  # fmt: skip
def test_healthcheck_anything_but_status_ok_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    hc = _load_healthcheck()
    _answer(monkeypatch, hc, body)
    assert hc.main() == 1


def test_healthcheck_http_error_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    hc = _load_healthcheck()

    def unavailable(url: str, timeout: float) -> _Resp:
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, io.BytesIO(b'{"status":"error"}'))  # type: ignore[arg-type]

    monkeypatch.setattr(hc.urllib.request, "urlopen", unavailable)
    assert hc.main() == 1


def test_healthcheck_url(monkeypatch: pytest.MonkeyPatch) -> None:
    hc = _load_healthcheck()
    monkeypatch.setenv("SCHEMA_HOST", "::1")
    monkeypatch.setenv("SCHEMA_PORT", "9000")
    assert hc.health_url() == "http://[::1]:9000/api/health"
    monkeypatch.setenv("SCHEMA_HOST", "::")
    assert hc.health_url() == "http://127.0.0.1:9000/api/health"


# --------------------------------------------------------------------------- deployment templates


def _active_yaml(path: Path) -> str:
    """The compose file without comments (commented-out examples don't count)."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(lines)


def _service_volumes(text: str) -> list[tuple[str, str]]:
    block = re.search(r"^    volumes:\n((?:      - .*\n?)+)", text, re.M)
    assert block, "no volumes"
    out = []
    for line in block.group(1).splitlines():
        host, container, *_ = line.strip()[2:].strip("\"'").split(":")
        out.append((host, container))
    return out


@pytest.mark.parametrize("path", [ROOT_COMPOSE, UGREEN_COMPOSE])
def test_compose_hardening_and_limits(path: Path) -> None:
    text = _active_yaml(path)
    assert "no-new-privileges:true" in text and "cap_drop: [ALL]" in text
    assert re.search(r"^    pids_limit: \d+", text, re.M)
    assert re.search(r"^    mem_limit: 1536m$", text, re.M) and re.search(r"^    cpus: 2\.0$", text, re.M)
    assert re.search(r"^    stop_grace_period: 2m$", text, re.M)  # Watchtower/docker stop mid-copy
    assert re.search(r"^      SCHEMA_ALLOWED_HOSTS: ", text, re.M)
    caps = re.search(r"^    cap_add: \[(.*)\]$", text, re.M)
    if caps:
        assert {c.strip() for c in caps.group(1).split(",")} <= ENTRYPOINT_CAPS
    for key in ("TMDB_API_KEY", "TVDB_API_KEY", "TVDB_PIN", "TRAKT_CLIENT_ID"):
        assert key not in text  # provider keys belong in Settings, not in `docker inspect`


@pytest.mark.parametrize(
    ("path", "targets"),
    [(ROOT_COMPOSE, {"/config", "/storage"}), (UGREEN_COMPOSE, {"/config", "/media"})],
)
def test_compose_mounts_one_data_folder_without_appdata(path: Path, targets: set[str]) -> None:
    volumes = _service_volumes(_active_yaml(path))
    assert {container for _, container in volumes} == targets
    config_host = next(host for host, container in volumes if container == "/config")
    (data_host,) = [host for host, container in volumes if container != "/config"]
    assert data_host.rstrip("/") not in ("/mnt/user", "/volume1", "/mnt", "/")  # not the whole array
    assert "appdata" not in data_host and "docker" not in data_host
    assert not config_host.startswith(data_host.rstrip("/") + "/")  # /config never inside the data mount


def test_root_compose_keeps_root_mode_with_minimal_caps() -> None:
    text = _active_yaml(ROOT_COMPOSE)
    caps = re.search(r"^    cap_add: \[(.*)\]$", text, re.M)
    assert caps and {c.strip() for c in caps.group(1).split(",")} == ENTRYPOINT_CAPS
    assert re.search(r'^      PUID: "\d+"', text, re.M) and 'PUID: "0"' not in text


def test_ugreen_compose_runs_non_root_and_read_only() -> None:
    text = _active_yaml(UGREEN_COMPOSE)
    user = re.search(r'^    user: "(\d+):(\d+)"$', text, re.M)
    assert user and "0" not in (user.group(1), user.group(2))
    assert re.search(r"^    read_only: true$", text, re.M)
    assert re.search(r"^    tmpfs:\n      - /tmp(:\S*)?$", text, re.M)
    assert "cap_add" not in text  # no capabilities at all
    assert "PUID" not in text and "PGID" not in text  # ignored in non-root mode
    raw = UGREEN_COMPOSE.read_text(encoding="utf-8")
    assert "# cap_add: [CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID]" in raw  # documented root-mode fallback


def test_unraid_template() -> None:
    root = ET.parse(UNRAID_TEMPLATE).getroot()
    extra = (root.findtext("ExtraParams") or "").split()
    for flag in ("--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--pids-limit", "--memory", "1536m",
                 "--cpus", "2", "--stop-timeout"):  # fmt: skip
        assert flag in extra, flag
    caps = {extra[i + 1] for i, word in enumerate(extra) if word == "--cap-add"}
    assert caps == ENTRYPOINT_CAPS
    configs = {c.get("Target"): c for c in root.findall("Config")}
    assert configs["UMASK"].get("Default") == "022" and configs["UMASK"].text == "022"
    storage = configs["/storage"].get("Default") or ""
    assert storage not in ("/mnt/user", "/mnt/user/", "/mnt") and "appdata" not in storage
    assert not (configs["/config"].get("Default") or "").startswith(storage.rstrip("/") + "/")
    assert "SCHEMA_ALLOWED_HOSTS" in configs
    for key in ("PUID", "PGID"):
        assert configs[key].get("Default") != "0"


@pytest.mark.parametrize("path", [ROOT_COMPOSE, UGREEN_COMPOSE, UNRAID_TEMPLATE, DEPLOYMENT_DOC, DOCKERFILE])
def test_no_template_mounts_the_whole_array(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "/mnt/user:/storage" not in text and "/volume1:/" not in text
    assert "UMASK=000" not in text and 'UMASK: "000"' not in text


def test_deployment_doc_covers_the_hardening() -> None:
    doc = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    for needle in ("EXDEV", "read_only: true", "cap_drop: [ALL]", "group_add", "SCHEMA_ALLOW_ROOT",
                   "SCHEMA_ALLOWED_HOSTS", "Tailscale Serve", "127.0.0.1:8420:8420", "docker inspect",
                   "--stop-timeout 120s", "WATCHTOWER_TIMEOUT", "Cookies ignore ports"):  # fmt: skip
        assert needle in doc, needle
