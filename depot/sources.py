"""Fetch macOS firmware & installer metadata from Apple and ipsw.me.

No third-party packages. Selftest: python3 -m depot.sources
"""

from __future__ import annotations

import plistlib
import re
import gzip
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Marketing names — macOS 11 through 26 (jumped from 15 to 26 in 2025).
# ---------------------------------------------------------------------------

MACOS_NAMES: dict[int, str] = {
    11: "Big Sur",
    12: "Monterey",
    13: "Ventura",
    14: "Sonoma",
    15: "Sequoia",
    26: "Tahoe",
}

IPSW_DEVICE_IDS = ["Macmini9,1", "MacBookAir10,1", "MacBookPro17,1", "iMac21,1"]

# Apple serves one merged catalog per seed program. The leading major in the
# chain is fixed (currently "14") and the same catalog carries every newer OS
# too — the Release chain below returns Tahoe (26) installers. Seed catalogs
# just insert a program token (14seed / 14beta / 14customerseed) after "index-".
_CATALOG_BASE = "https://swscan.apple.com/content/catalogs/others/index-"
_CHAIN = ("14-13-12-10.16-10.15-10.14-10.13-10.12-10.11-10.10-10.9-"
          "mountainlion-lion-snowleopard-leopard.merged-1.sucatalog")

SUCATALOG_URL = _CATALOG_BASE + _CHAIN

# ---------------------------------------------------------------------------
# Seed catalogs — beta / developer / customer seed programs.
# ---------------------------------------------------------------------------

_CATALOG_URLS: dict[str, str] = {
    "Release": SUCATALOG_URL,
    "Developer Seed": _CATALOG_BASE + "14seed-" + _CHAIN,
    "Public Beta": _CATALOG_BASE + "14beta-" + _CHAIN,
    "Customer Seed": _CATALOG_BASE + "14customerseed-" + _CHAIN,
}

CATALOG_NAMES = list(_CATALOG_URLS.keys())


def catalog_url(name: str) -> str:
    return _CATALOG_URLS.get(name, SUCATALOG_URL)

# ---------------------------------------------------------------------------
# Shared fetch helper
# ---------------------------------------------------------------------------


def fetch(url: str, timeout: int = 30) -> bytes:
    """Fetch a URL with timeout and one retry. Decompresses gzip if needed."""
    headers = {"User-Agent": "Depot/0.1 (macOS; +https://github.com/araidz/Depot)"}
    req = urllib.request.Request(url, headers=headers)
    last_err: Exception | None = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                # Decompress gzip if the server sent it compressed
                if data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                return data
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
    raise RuntimeError(f"fetch failed: {url} — {last_err}")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Firmware:
    name: str
    version: str
    build: str
    size: int
    url: str
    sha1: str

    @property
    def size_str(self) -> str:
        return _fmt_bytes(self.size)


@dataclass
class Installer:
    name: str
    version: str
    build: str
    date: str  # ISO date
    size: int
    url: str
    product_id: str
    catalog: str = "Release"

    @property
    def size_str(self) -> str:
        return _fmt_bytes(self.size)

    @property
    def is_beta(self) -> bool:
        return self.catalog != "Release" or "beta" in self.name.lower() or "seed" in self.build.lower()


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def _macos_name(version: str) -> str:
    """Extract marketing name from a version string like '26.5.2' or '14.0'."""
    try:
        major = int(version.split(".")[0])
        return MACOS_NAMES.get(major, f"macOS {major}")
    except (ValueError, IndexError):
        return f"macOS {version}"


# ---------------------------------------------------------------------------
# Firmwares (ipsw.me API)
# ---------------------------------------------------------------------------


def _fetch_firmware_for_device(device_id: str) -> list[dict]:
    """Return raw firmware dicts for one Apple Silicon device."""
    url = f"https://api.ipsw.me/v4/device/{device_id}?type=ipsw"
    try:
        data = fetch(url, timeout=20)
        dev = __import__("json").loads(data)
        return dev.get("firmwares", [])
    except Exception:
        return []


def firmwares() -> list[Firmware]:
    """Fetch all Apple Silicon macOS firmwares, deduplicated by build id."""
    seen_builds: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_firmware_for_device, d): d for d in IPSW_DEVICE_IDS}
        for fut in as_completed(futures):
            for fw in fut.result():
                build = fw.get("buildid", "")
                if build and build not in seen_builds:
                    seen_builds[build] = fw

    results: list[Firmware] = []
    for fw in seen_builds.values():
        version = fw.get("version", "")
        results.append(Firmware(
            name=_macos_name(version),
            version=version,
            build=fw.get("buildid", ""),
            size=int(fw.get("filesize", 0)),
            url=fw.get("url", ""),
            sha1=fw.get("sha1sum", ""),
        ))

    results.sort(key=lambda f: [int(x) for x in f.version.split(".") if x.isdigit()], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Installers (Apple sucatalog)
# ---------------------------------------------------------------------------

_IA_RE = re.compile(r"InstallAssistant\.pkg")
_VERSION_RE = re.compile(r"<key>VERSION</key>\s*<string>([^<]+)")
_BUILD_RE = re.compile(r"<key>BUILD</key>\s*<string>([^<]+)")
_TITLE_RE = re.compile(r"<title>([^<]+)</title>")
_SIZE_RE = re.compile(r"<key>Size</key>\s*<integer>(\d+)")


def _parse_dist(xml: str) -> dict[str, str]:
    """Extract VERSION, BUILD, title from a .dist XML."""
    out: dict[str, str] = {}
    m = _VERSION_RE.search(xml)
    if m:
        out["version"] = m.group(1).strip()
    m = _BUILD_RE.search(xml)
    if m:
        out["build"] = m.group(1).strip()
    m = _TITLE_RE.search(xml)
    if m:
        out["title"] = m.group(1).strip()
    return out


def _product_ia_info(product: dict) -> tuple[str, int] | None:
    """Return (url, size) for the InstallAssistant.pkg in a product, or None."""
    for pkg in product.get("Packages", []):
        url = pkg.get("URL", "")
        if _IA_RE.search(url):
            return url, int(pkg.get("Size", 0))
    return None


def installers(catalog: str = "Release") -> list[Installer]:
    """Parse an Apple sucatalog and return all macOS installers."""
    url = catalog_url(catalog)
    cat_data = fetch(url, timeout=30)
    parsed = plistlib.loads(cat_data)
    products = parsed.get("Products", {})

    # Collect product IDs that have an InstallAssistant.pkg
    ia_products: list[tuple[str, dict]] = []
    for pid, prod in products.items():
        if _product_ia_info(prod) is not None:
            ia_products.append((pid, prod))

    # Fetch .dist files concurrently
    def _fetch_one(item: tuple[str, dict]) -> Installer | None:
        pid, prod = item
        ia = _product_ia_info(prod)
        if ia is None:
            return None
        ia_url, ia_size = ia

        dist_url = prod.get("Distributions", {}).get("English", "")
        if not dist_url:
            return None

        try:
            dist_xml = fetch(dist_url, timeout=20).decode("utf-8", errors="replace")
        except Exception:
            return None

        info = _parse_dist(dist_xml)
        version = info.get("version", "")
        build = info.get("build", "")
        title = info.get("title", "macOS")

        post_date = prod.get("PostDate")
        date_str = post_date.strftime("%Y-%m-%d") if hasattr(post_date, "strftime") else str(post_date)[:10] if post_date else ""

        return Installer(
            name=title,
            version=version,
            build=build,
            date=date_str,
            size=ia_size,
            url=ia_url,
            product_id=pid,
            catalog=catalog,
        )

    results: list[Installer] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_one, item): item for item in ia_products}
        for fut in as_completed(futures):
            inst = fut.result()
            if inst is not None:
                results.append(inst)

    results.sort(key=lambda i: [int(x) for x in i.version.split(".") if x.isdigit()], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------


def selftest() -> None:
    """Assert-based check: real network, no fixtures."""
    print("firmwares()...", end=" ", flush=True)
    fws = firmwares()
    assert len(fws) > 0, "no firmwares returned"
    fw = fws[0]
    assert fw.url.startswith("https://"), f"bad url: {fw.url}"
    assert len(fw.sha1) == 40, f"bad sha1: {fw.sha1}"
    assert fw.size > 0, "zero size"
    print(f"OK — {len(fws)} firmwares, latest: {fw.name} {fw.version} ({fw.build})")

    print("installers()...", end=" ", flush=True)
    insts = installers()
    assert len(insts) > 0, "no installers returned"
    inst = insts[0]
    assert "InstallAssistant.pkg" in inst.url, f"bad url: {inst.url}"
    assert inst.version, "missing version"
    assert inst.build, "missing build"
    assert inst.name, "missing name"
    print(f"OK — {len(insts)} installers, latest: {inst.name} {inst.version} ({inst.build})")

    print("installers(catalog='Developer Seed')...", end=" ", flush=True)
    beta_insts = installers(catalog="Developer Seed")
    if beta_insts:
        bi = beta_insts[0]
        print(f"OK — {len(beta_insts)} beta installers, latest: {bi.name} {bi.version} ({bi.build})")
    else:
        print("OK — no beta installers in developer seed (may be empty)")

    print("selftest passed.")


if __name__ == "__main__":
    selftest()
