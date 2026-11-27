"""Download-client hook scripts in deploy/hooks (no network: HTTP is faked)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
SAB_SCRIPT = REPO / "deploy" / "hooks" / "sabnzbd" / "schema_notify.py"
GENERIC_SCRIPT = REPO / "deploy" / "hooks" / "generic" / "schema-notify.sh"


@pytest.fixture(scope="module")
def sab() -> ModuleType:
    spec = importlib.util.spec_from_file_location("schema_notify", SAB_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Opener:
    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.reply = reply if reply is not None else {"job_id": 42, "paths": ["/storage/downloads/x"]}
        self.error = error
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float) -> FakeResponse:
        self.requests.append(request)
        if self.error:
            raise self.error
        return FakeResponse(json.dumps(self.reply).encode())


ENV = {
    "SAB_COMPLETE_DIR": "/downloads/complete/tv/Show.S01E01",
    "SAB_FINAL_NAME": "Show.S01E01",
    "SAB_CAT": "tv",
    "SAB_PP_STATUS": "0",
    "SAB_NZO_ID": "SABnzbd_nzo_abc",
    "SCHEMA_URL": "http://schema.test:8420/",
    "SCHEMA_API_KEY": "k3y",
}


def test_sab_posts_json(sab: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    opener = Opener()
    assert sab.main(env=ENV, argv=["schema_notify.py"], opener=opener) == 0
    request = opener.requests[0]
    assert request.full_url == "http://schema.test:8420/api/webhooks/sabnzbd"
    assert request.get_method() == "POST" and request.get_header("X-api-key") == "k3y"
    body = json.loads(request.data)
    assert body["complete_dir"] == "/downloads/complete/tv/Show.S01E01"
    assert body["category"] == "tv" and body["status"] == "0" and body["name"] == "Show.S01E01"
    assert capsys.readouterr().out.strip() == "Schema: queued job 42 (1 path)"


def test_sab_positional_arguments(sab: ModuleType) -> None:
    argv = ["schema_notify.py", "/downloads/m", "orig.nzb", "Movie (2019)", "", "movies", "alt.binaries", "0"]
    payload = sab.build_payload({}, argv)
    assert payload["complete_dir"] == "/downloads/m" and payload["name"] == "Movie (2019)"
    assert payload["category"] == "movies" and payload["status"] == "0"


def test_sab_failed_download_not_sent(sab: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    opener = Opener()
    env = {**ENV, "SAB_PP_STATUS": "1", "SAB_FAIL_MSG": "Repair failed"}
    assert sab.main(env=env, argv=["x"], opener=opener) == 0
    assert opener.requests == []
    assert "Repair failed" in capsys.readouterr().out


def test_sab_http_errors(sab: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    denied = urllib.error.HTTPError(
        "http://schema.test", 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"bad key"}')
    )  # type: ignore[arg-type]
    assert sab.main(env=ENV, argv=["x"], opener=Opener(error=denied)) == 1
    assert "HTTP 401 (check SCHEMA_API_KEY): bad key" in capsys.readouterr().out
    down = urllib.error.URLError("Connection refused")
    assert sab.main(env=ENV, argv=["x"], opener=Opener(error=down)) == 1
    assert "could not reach http://schema.test:8420" in capsys.readouterr().out


def test_sab_requires_key_and_folder(sab: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert sab.main(env={**ENV, "SCHEMA_API_KEY": ""}, argv=["x"], opener=Opener()) == 1
    assert "SCHEMA_API_KEY" in capsys.readouterr().out
    env = {k: v for k, v in ENV.items() if k != "SAB_COMPLETE_DIR"}
    assert sab.main(env=env, argv=["x"], opener=Opener()) == 1


def test_sab_script_is_stdlib_only() -> None:
    source = SAB_SCRIPT.read_text(encoding="utf-8")
    assert "import schema_app" not in source and "import httpx" not in source and "requests" not in source
    assert source.startswith("#!/usr/bin/env python3")


def test_sab_key_comes_from_the_environment_only(sab: ModuleType, tmp_path: Path) -> None:
    assert not hasattr(sab, "SCHEMA_API_KEY")  # no key edited into the script
    plain, header = tmp_path / "plain", tmp_path / "header"
    plain.write_text("k3y\n", encoding="utf-8")
    header.write_text("X-Api-Key: k3y-2\r\n", encoding="utf-8")
    assert sab.read_api_key({"SCHEMA_API_KEY_FILE": str(plain)}) == "k3y"
    assert sab.read_api_key({"SCHEMA_API_KEY_FILE": str(header)}) == "k3y-2"
    assert sab.read_api_key({"SCHEMA_API_KEY": "env", "SCHEMA_API_KEY_FILE": str(plain)}) == "env"
    assert sab.read_api_key({"SCHEMA_API_KEY_FILE": str(tmp_path / "missing")}) == ""
    assert sab.read_api_key({}) == ""
    env = {k: v for k, v in ENV.items() if k != "SCHEMA_API_KEY"} | {"SCHEMA_API_KEY_FILE": str(header)}
    opener = Opener()
    assert sab.main(env=env, argv=["x"], opener=opener) == 0
    assert opener.requests[0].get_header("X-api-key") == "k3y-2"


def test_sab_does_not_follow_redirects(sab: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    """A redirect would make urllib resend X-Api-Key to the new host: the real opener refuses."""
    seen: list[str | None] = []

    class Target(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            seen.append(self.headers.get("X-Api-Key"))
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b'{"job_id": 1}')

        def log_message(self, *args: object) -> None:
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    location = f"http://127.0.0.1:{target.server_address[1]}/steal"

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            # Read the body first: answering with unread request data makes Windows reset the
            # connection (WinError 10053) before the client sees the 307.
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(307)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    front = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (target, front)]
    for t in threads:
        t.start()
    try:
        env = {**ENV, "SCHEMA_URL": f"http://127.0.0.1:{front.server_address[1]}"}
        assert sab.main(env=env, argv=["x"]) == 1
    finally:
        for s in (front, target):
            s.shutdown()
            s.server_close()
    assert seen == []
    assert "HTTP 307" in capsys.readouterr().out


# --------------------------------------------------------------------------- generic shell hook


def _sh() -> str | None:
    found = shutil.which("sh")
    if not found or os.name == "nt" and "System32" in found:
        return None
    return found


def _fake_curl(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    """A ``curl`` on PATH that records its arguments and its stdin."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log, stdin = tmp_path / "curl-args.txt", tmp_path / "curl-stdin.txt"
    fake = bin_dir / "curl"
    fake.write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a" >> "{log.as_posix()}"; done\n'
                    f'cat > "{stdin.as_posix()}"\nprintf \'{{"job_id":7}}\'\n', newline="\n")  # fmt: skip
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
           "SCHEMA_URL": "http://schema.test:8420/", "SCHEMA_API_KEY": "", "SCHEMA_API_KEY_FILE": ""}  # fmt: skip
    return env, log, stdin


@pytest.mark.skipif(_sh() is None, reason="needs a POSIX sh")
def test_generic_hook_uses_curl_with_encoded_fields(tmp_path: Path) -> None:
    env, log, stdin = _fake_curl(tmp_path)
    proc = subprocess.run(
        [_sh(), GENERIC_SCRIPT.as_posix(), "-k", "movie", "-c", "movies", "-l", "Batch & Co",
         "/downloads/A b.mkv", "/downloads/c.mkv"],
        capture_output=True, text=True, env={**env, "SCHEMA_API_KEY": "k3y_-Ab9"}, timeout=30, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert 'queued: {"job_id":7}' in proc.stdout
    args = log.read_text().splitlines()
    assert "http://schema.test:8420/api/webhooks/generic" in args
    assert "kind=movie" in args and "category=movies" in args and "label=Batch & Co" in args
    assert args[args.index("paths=/downloads/A b.mkv") + 1] == "/downloads/c.mkv"  # newline-joined
    # The key travels on stdin as a curl config line, never on the command line.
    assert not any("k3y" in a for a in args)
    assert args[args.index("--config") + 1] == "-"
    assert stdin.read_text().strip() == 'header = "X-Api-Key: k3y_-Ab9"'


@pytest.mark.skipif(_sh() is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("content", ["X-Api-Key: fil3key\n", "fil3key", "  fil3key \r\n"])
def test_generic_hook_reads_the_key_file(tmp_path: Path, content: str) -> None:
    env, log, stdin = _fake_curl(tmp_path)
    key_file = tmp_path / "schema-api-key"
    key_file.write_bytes(content.encode())
    proc = subprocess.run([_sh(), GENERIC_SCRIPT.as_posix(), "/downloads/x.mkv"], capture_output=True, text=True,
                          env={**env, "SCHEMA_API_KEY_FILE": key_file.as_posix()}, timeout=30, check=False)  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert stdin.read_text().strip() == 'header = "X-Api-Key: fil3key"'
    assert not any("fil3key" in a for a in log.read_text().splitlines())


def _path_without_curl(tmp_path: Path, bin_dir: Path) -> str:
    """PATH holding ``bin_dir`` plus the tools the hook needs, but no curl."""
    sh_dir = Path(_sh() or "").parent
    if not any((sh_dir / name).exists() for name in ("curl", "curl.exe", "wget", "wget.exe")):
        return f"{bin_dir}{os.pathsep}{sh_dir}"  # Git for Windows: usr/bin has no curl
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("cat", "dirname", "grep", "head", "mktemp", "od", "rm", "sed", "tr"):
        found = shutil.which(tool)
        if not found:
            pytest.skip(f"needs {tool}")
        try:
            os.symlink(found, tools / tool)
        except OSError:
            pytest.skip("cannot build a PATH without curl here")
    return f"{bin_dir}{os.pathsep}{tools}"


def _fake_wget(tmp_path: Path, version: str) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    out = tmp_path / "wget"
    out.mkdir()
    fake = bin_dir / "wget"
    fake.write_text(
        '#!/bin/sh\n'
        f'if [ "$1" = "--version" ]; then echo "{version}"; exit 0; fi\n'
        f'for a in "$@"; do printf "%s\\n" "$a" >> "{(out / "args").as_posix()}"; done\n'
        f'cat "$WGETRC" > "{(out / "rc").as_posix()}"\n'
        f'printf "%s" "$WGETRC" > "{(out / "rc-path").as_posix()}"\n'
        "printf '{\"job_id\":8}'\n",
        newline="\n",
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": _path_without_curl(tmp_path, bin_dir), "SCHEMA_URL": "http://schema.test:8420",
           "SCHEMA_API_KEY": "k3y", "SCHEMA_API_KEY_FILE": "", "TMPDIR": tmp_path.as_posix()}  # fmt: skip
    return env, out


@pytest.mark.skipif(_sh() is None, reason="needs a POSIX sh")
def test_generic_hook_gnu_wget_reads_the_key_from_a_private_wgetrc(tmp_path: Path) -> None:
    env, out = _fake_wget(tmp_path, "GNU Wget 1.21.4 built on linux-gnu.")
    proc = subprocess.run([_sh(), GENERIC_SCRIPT.as_posix(), "-k", "movie", "/downloads/A b.mkv"],
                          capture_output=True, text=True, env=env, timeout=30, check=False)  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert 'queued: {"job_id":8}' in proc.stdout
    args = (out / "args").read_text().splitlines()
    assert not any("k3y" in a for a in args)
    assert "--post-data=paths=%2f%64%6f%77%6e%6c%6f%61%64%73%2f%41%20%62%2e%6d%6b%76&kind=%6d%6f%76%69%65" in args
    assert (out / "rc").read_text().splitlines() == ["header = X-Api-Key: k3y", "max_redirect = 0"]
    rc_path = (out / "rc-path").read_text()
    assert rc_path and not Path(rc_path).exists()  # removed on exit


@pytest.mark.skipif(_sh() is None, reason="needs a POSIX sh")
def test_generic_hook_refuses_busybox_wget(tmp_path: Path) -> None:
    env, out = _fake_wget(tmp_path, "BusyBox v1.36.1")
    proc = subprocess.run([_sh(), GENERIC_SCRIPT.as_posix(), "/downloads/x.mkv"], capture_output=True, text=True,
                          env=env, timeout=30, check=False)  # fmt: skip
    assert proc.returncode == 1 and "GNU wget" in proc.stderr
    assert not (out / "args").exists()


@pytest.mark.skipif(_sh() is None, reason="needs a POSIX sh")
def test_generic_hook_refuses_odd_keys_and_line_breaks(tmp_path: Path) -> None:
    env, log, _stdin = _fake_curl(tmp_path)
    odd = subprocess.run([_sh(), GENERIC_SCRIPT.as_posix(), "/x.mkv"], capture_output=True, text=True,
                         env={**env, "SCHEMA_API_KEY": 'k"\nheader = "Evil: 1'}, timeout=30, check=False)  # fmt: skip
    assert odd.returncode == 1 and "unexpected characters" in odd.stderr
    if os.name != "nt":  # MSYS sh on Windows cuts a command-line argument at its first newline
        broken = subprocess.run([_sh(), GENERIC_SCRIPT.as_posix(), "/downloads/a\n/storage"], capture_output=True,
                                text=True, env={**env, "SCHEMA_API_KEY": "k3y"}, timeout=30, check=False)  # fmt: skip
        assert broken.returncode == 1 and "line break" in broken.stderr
    missing = subprocess.run([_sh(), GENERIC_SCRIPT.as_posix(), "/x.mkv"], capture_output=True, text=True,
                             env={**env, "SCHEMA_API_KEY_FILE": (tmp_path / "nope").as_posix()}, timeout=30,
                             check=False)  # fmt: skip
    assert missing.returncode == 1 and "SCHEMA_API_KEY_FILE" in missing.stderr
    assert not log.exists()  # curl never ran


@pytest.mark.skipif(_sh() is None, reason="needs a POSIX sh")
def test_generic_hook_requires_key(tmp_path: Path) -> None:
    env = {**os.environ, "SCHEMA_API_KEY": ""}
    proc = subprocess.run([_sh(), GENERIC_SCRIPT.as_posix(), "/x.mkv"], capture_output=True, text=True, env=env,
                          timeout=30, check=False)  # fmt: skip
    assert proc.returncode == 1 and "SCHEMA_API_KEY" in proc.stderr
    usage = subprocess.run([_sh(), GENERIC_SCRIPT.as_posix()], capture_output=True, text=True, env=env,
                           timeout=30, check=False)  # fmt: skip
    assert usage.returncode == 2
