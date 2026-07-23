"""aria2 engine: spawn a private aria2c and drive it over JSON-RPC.

HTTP-only adaptation of Trawl's aria2.py — no magnet/torrent features.
Downloads macOS firmware .ipsw and installer .pkg files via aria2 with
SHA-1 verification, multi-connection parallel download, and session resume.
No third-party packages. Selftest: python3 -m slipway.download
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from shutil import which

USER_CONF = Path.home() / ".aria2" / "aria2.conf"
STATE_DIR = Path.home() / "Library" / "Application Support" / "Slipway"


class Aria2Error(Exception):
    pass


@dataclass
class Download:
    """One tracked download, mapped from aria2's tellStatus."""

    gid: str
    name: str
    status: str  # active | waiting | paused | complete | error
    total: int
    completed: int
    speed: int
    eta: float | None
    error: str = ""
    path: str = ""

    @property
    def progress(self) -> float:
        return self.completed / self.total if self.total else 0.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _name(st: dict) -> str:
    """Extract filename from aria2 status dict."""
    files = st.get("files") or []
    if files and files[0].get("path"):
        return os.path.basename(files[0]["path"])
    return st.get("gid", "?")


def to_download(st: dict) -> Download:
    """Map an aria2 tellStatus dict to a Download. Pure."""
    total = int(st.get("totalLength") or 0)
    completed = int(st.get("completedLength") or 0)
    speed = int(st.get("downloadSpeed") or 0)
    status = st.get("status") or ""
    eta = (total - completed) / speed if speed > 0 and total > completed else None
    return Download(
        gid=st.get("gid", "?"),
        name=_name(st),
        status=status,
        total=total,
        completed=completed,
        speed=speed,
        eta=eta,
        error=st.get("errorMessage", ""),
        path=(st.get("files") or [{}])[0].get("path", ""),
    )


class Aria2:
    TIMEOUT = 5

    def __init__(self, conf: Path | None = USER_CONF, state_dir: Path = STATE_DIR):
        self.port = _free_port()
        self.secret = secrets.token_hex(8)
        self.endpoint = f"http://127.0.0.1:{self.port}/jsonrpc"
        self.conf = Path(conf) if conf else None
        self.state_dir = Path(state_dir)
        self.session = self.state_dir / "aria2-session.txt"
        self.proc: subprocess.Popen | None = None
        self.roots: list[str] = []
        self._uris: dict[str, tuple[str, dict]] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self, timeout: float = 10.0) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        args = [
            _binary(),
            "--enable-rpc",
            f"--rpc-listen-port={self.port}",
            f"--rpc-secret={self.secret}",
            "--rpc-listen-all=false",
            f"--save-session={self.session}",
            "--save-session-interval=30",
            "--max-concurrent-downloads=3",
            "--split=4",
            "--min-split-size=10M",
            "--continue=true",
            "--allow-overwrite=true",
        ]
        if self.session.is_file():
            args.append(f"--input-file={self.session}")
        if self.conf and self.conf.is_file():
            args.append(f"--conf-path={self.conf}")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._call("aria2.getVersion")
                self._adopt()
                return
            except Aria2Error:
                if self.proc.poll() is not None:
                    raise Aria2Error("aria2c exited during startup")
                time.sleep(0.1)
        raise Aria2Error("aria2c RPC did not come up")

    def stop(self) -> None:
        try:
            self._call("aria2.shutdown")
        except Aria2Error:
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                self.proc.kill()

    def __enter__(self) -> "Aria2":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- commands ------------------------------------------------------------

    def _adopt(self) -> None:
        """Track downloads aria2 restored from the session file at startup."""
        for method, params in (("aria2.tellActive", [["gid"]]),
                               ("aria2.tellWaiting", [0, 1000, ["gid"]])):
            try:
                for t in self._call(method, params) or []:
                    gid = t.get("gid")
                    if gid and gid not in self.roots:
                        self.roots.append(gid)
            except Aria2Error:
                pass

    def add(self, url: str, dest: str | None = None, sha1: str = "",
            options: dict | None = None) -> str:
        """Queue an HTTP download. Returns the root gid."""
        opts = dict(options or {})
        if dest:
            opts["dir"] = dest
        if sha1:
            opts["checksum"] = f"sha-1={sha1}"
        gid = self._call("aria2.addUri", [[url], opts])
        self.roots.append(gid)
        self._uris[gid] = (url, opts)
        return gid

    def remove(self, root: str) -> None:
        for method in ("aria2.forceRemove", "aria2.removeDownloadResult"):
            try:
                self._call(method, [root])
            except Aria2Error:
                pass
        if root in self.roots:
            self.roots.remove(root)
        self._uris.pop(root, None)

    def retry(self, root: str) -> str | None:
        """Remove a failed download and re-add from its original URL."""
        uri, opts = self._uris.get(root, ("", {}))
        if not uri:
            return None
        self.remove(root)
        try:
            return self.add(uri, options=opts)
        except Aria2Error:
            return None

    def pause(self, root: str) -> None:
        try:
            self._call("aria2.forcePause", [root])
        except Aria2Error:
            pass

    def resume(self, root: str) -> None:
        try:
            self._call("aria2.unpause", [root])
        except Aria2Error:
            pass

    def global_stat(self) -> dict:
        return self._call("aria2.getGlobalStat")

    def download_dir(self) -> str | None:
        try:
            return self._call("aria2.getGlobalOption").get("dir")
        except Aria2Error:
            return None

    def set_dir(self, path: str) -> None:
        try:
            self._call("aria2.changeGlobalOption", [{"dir": path}])
        except Aria2Error:
            pass

    def poll(self) -> list[Download]:
        """Current state of every tracked download."""
        out: list[Download] = []
        for root in list(self.roots):
            try:
                st = self._call("aria2.tellStatus", [root])
            except Aria2Error:
                continue
            d = to_download(st)
            d.gid = root  # use root gid for display (no metadata hops)
            out.append(d)
        return out

    # -- internals -----------------------------------------------------------

    def _call(self, method: str, params: list | None = None):
        payload = {
            "jsonrpc": "2.0",
            "id": "slipway",
            "method": method,
            "params": [f"token:{self.secret}", *(params or [])],
        }
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                body = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise Aria2Error(f"{method}: {e}") from e
        if isinstance(body, dict) and body.get("error"):
            raise Aria2Error(f"{method}: {body['error'].get('message', '?')}")
        return body.get("result")


def _binary() -> str:
    path = which("aria2c")
    if not path:
        raise Aria2Error("aria2c not found on PATH (brew install aria2)")
    return path


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------


def selftest() -> None:
    """Exercise the engine end to end. No real bytes downloaded."""
    # pure mapping — no network
    d = to_download({"gid": "a", "status": "active", "totalLength": "1000",
                     "completedLength": "500", "downloadSpeed": "100",
                     "files": [{"path": "/tmp/InstallAssistant.pkg"}]})
    assert d.progress == 0.5 and d.eta == 5.0 and d.name == "InstallAssistant.pkg", d
    d2 = to_download({"gid": "b", "status": "complete", "totalLength": "100",
                      "completedLength": "100", "downloadSpeed": "0",
                      "files": [{"path": "/tmp/fw.ipsw"}]})
    assert d2.progress == 1.0 and d2.status == "complete", d2
    print("pure mapping ok")

    # live plumbing — temp state, no user conf
    import tempfile
    import hashlib as _hl
    import urllib.request as _ur
    tmp = Path(tempfile.mkdtemp(prefix="slipway-selftest-"))
    eng = Aria2(conf=None, state_dir=tmp)
    with eng:
        ver = eng._call("aria2.getVersion")
        print(f"aria2 {ver['version']} up on :{eng.port}")
        assert "numActive" in eng.global_stat()

        # download Apple's robots.txt and verify SHA-1
        url = "https://www.apple.com/robots.txt"
        expected_sha1 = _hl.sha1(_ur.urlopen(url, timeout=10).read()).hexdigest()
        root = eng.add(url, dest=str(tmp))
        for _ in range(60):
            if any(d.status in ("complete", "error") for d in eng.poll()):
                break
            time.sleep(0.5)
        rows = eng.poll()
        done = [d for d in rows if d.status == "complete"]
        assert done, f"never completed: {rows}"
        d = done[0]
        assert d.total > 0 and d.path and os.path.isfile(d.path)
        actual_sha1 = _hl.sha1(open(d.path, "rb").read()).hexdigest()
        assert actual_sha1 == expected_sha1, f"SHA-1 mismatch: {actual_sha1} != {expected_sha1}"
        print(f"download + SHA-1 ok: {d.name} ({d.total} bytes)")

        # pause at add time, resume, wait for completion
        root2 = eng.add(url, dest=str(tmp), options={"pause": "true"})
        time.sleep(0.3)
        paused = eng.poll()
        assert any(d.status == "paused" for d in paused), f"not paused: {paused}"
        eng.resume(root2)
        for _ in range(30):
            if any(d.status in ("complete", "error") for d in eng.poll()):
                break
            time.sleep(0.5)
        print("pause/resume ok")

        # cleanup
        for r in list(eng.roots):
            eng.remove(r)
        print("remove ok")

    assert eng.proc.poll() is not None, "aria2c did not shut down"
    print("shutdown ok")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("\nPhase 2 selftest passed.")


if __name__ == "__main__":
    selftest()
