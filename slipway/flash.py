"""Create bootable macOS USB installers via createinstallmedia.

Requires sudo. This module detects USB drives, formats them, and runs
Apple's createinstallmedia to make them bootable. Stdlib only.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


class FlashError(Exception):
    pass


@dataclass
class USBDisk:
    identifier: str  # e.g. "disk3"
    device: str  # e.g. "/dev/disk3"
    name: str
    size: int  # bytes
    mountpoint: str  # e.g. "/Volumes/MyVolume"

    @property
    def size_str(self) -> str:
        n = self.size
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(n) < 1024:
                return f"{n:.0f} {unit}" if unit != "B" else f"{n} B"
            n /= 1024
        return f"{n:.1f} PB"


def _run(cmd: list[str], timeout: int = 30, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def list_usb_disks() -> list[USBDisk]:
    """Detect external (USB/Thunderbolt) disks via diskutil."""
    result = _run(["diskutil", "list", "external", "physical"])
    if result.returncode != 0:
        # fallback: try all disks, filter by removable
        result = _run(["diskutil", "list"])
    if result.returncode != 0:
        raise FlashError(f"diskutil failed: {result.stderr.strip()}")

    disks: list[USBDisk] = []
    current_id: str | None = None
    current_name = ""
    current_size = 0
    current_device = ""
    current_mount = ""

    for line in result.stdout.splitlines():
        # Match disk line: "/dev/disk3 (external, physical):"
        m = re.match(r"^\s*/dev/(disk\d+)\s+\((?:external|USB|removable)", line)
        if m:
            current_id = m.group(1)
            current_device = f"/dev/{current_id}"
            current_name = ""
            current_size = 0
            current_mount = ""
            continue

        if current_id is None:
            continue

        # Match size line: "   499.6 GB"
        sm = re.match(r"^\s+[\d.]+\s+(B|KB|MB|GB|TB)\s*$", line)
        if sm:
            try:
                val = float(line.strip().split()[0])
                unit = sm.group(1)
                multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
                current_size = int(val * multipliers.get(unit, 1))
            except (ValueError, IndexError):
                pass

        # Match volume line: "   /Volumes/NAME"
        vm = re.match(r"^\s+(/Volumes/\S+)", line)
        if vm:
            current_mount = vm.group(1)
            current_name = current_mount.split("/")[-1]

        # If we hit the next disk, save the current one
        nm = re.match(r"^\s*/dev/(disk\d+)\s+\(", line)
        if nm and nm.group(1) != current_id:
            disks.append(USBDisk(current_id, current_device, current_name,
                                 current_size, current_mount))
            current_id = None

    # save the last one
    if current_id:
        disks.append(USBDisk(current_id, current_device, current_name,
                             current_size, current_mount))

    return disks


def _find_createinstallmedia() -> str:
    """Find the createinstallmedia binary inside installed Install macOS apps."""
    import glob as _glob
    patterns = [
        "/Applications/Install macOS *.app/Contents/Resources/createinstallmedia",
        "/Applications/Install macOS*.app/Contents/Resources/createinstallmedia",
        "/System/Volumes/Install Resources/*/createinstallmedia",
    ]
    for pat in patterns:
        matches = _glob.glob(pat)
        if matches:
            return matches[0]
    raise FlashError(
        "createinstallmedia not found.\n"
        "Download a macOS installer first (slipway → d to download, then install the .pkg)."
    )


def _find_installer_app() -> str:
    """Find the Install macOS .app in /Applications."""
    import glob as _glob
    patterns = [
        "/Applications/Install macOS *.app",
        "/Applications/Install macOS*.app",
    ]
    for pat in patterns:
        matches = sorted(_glob.glob(pat), reverse=True)
        if matches:
            return matches[0]
    raise FlashError(
        "No Install macOS app found in /Applications.\n"
        "Download a macOS installer first (slipway → d to download, then install the .pkg)."
    )


def format_and_flash(disk: USBDisk, installer_app: str, volume_name: str = "MyVolume",
                     status_cb=None) -> None:
    """Format a disk and create a bootable macOS installer.

    Args:
        disk: the USBDisk to flash
        installer_app: path to the Install macOS .app
        volume_name: name for the formatted volume
        status_cb: optional callback(msg: str) for progress updates
    """
    def _status(msg: str):
        if status_cb:
            status_cb(msg)

    # Step 1: Format the disk as Mac OS Extended (Journaled)
    _status(f"Formatting {disk.identifier} as Mac OS Extended…")
    r = _run(["diskutil", "eraseDisk", "JHFS+", volume_name, "GPT", disk.device], timeout=120)
    if r.returncode != 0:
        raise FlashError(f"Format failed: {r.stderr.strip()}")

    # Step 2: Find the volume mountpoint after format
    r2 = _run(["diskutil", "info", disk.identifier])
    mount = ""
    for line in r2.stdout.splitlines():
        if "Mount Point:" in line:
            mount = line.split(":", 1)[1].strip()
            break
    if not mount:
        raise FlashError("Could not find mount point after formatting")

    # Step 3: Run createinstallmedia
    createinstallmedia = _find_createinstallmedia()
    _status(f"Running createinstallmedia on {mount}…")
    proc = subprocess.Popen(
        [createinstallmedia, "--volume", mount, "--nointeraction"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for line in proc.stdout or []:
        line = line.strip()
        if line:
            _status(line)
    proc.wait(timeout=600)
    if proc.returncode != 0:
        raise FlashError(f"createinstallmedia failed (exit {proc.returncode})")

    _status(f"Done! {disk.name} is now a bootable macOS installer.")


def install_pkg(pkg_path: str, status_cb=None) -> None:
    """Install an InstallAssistant.pkg to /Applications (requires sudo)."""
    def _status(msg: str):
        if status_cb:
            status_cb(msg)

    _status(f"Installing {pkg_path} to /Applications…")
    r = _run(
        ["sudo", "-A", "installer", "-pkg", pkg_path, "-target", "/"],
        timeout=1200,
    )
    if r.returncode != 0:
        raise FlashError(f"pkg install failed: {r.stderr.strip()}")
    _status("Installer app installed to /Applications.")


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------


def selftest() -> None:
    """Test USB detection and createinstallmedia discovery (no destructive ops)."""
    print("list_usb_disks()...", end=" ", flush=True)
    try:
        disks = list_usb_disks()
        if disks:
            for d in disks:
                print(f"  {d.identifier}: {d.name} ({d.size_str}) @ {d.mountpoint}")
        else:
            print("OK — no external disks detected (normal if no USB connected)")
    except FlashError as e:
        print(f"OK — diskutil error (may need permissions): {e}")

    print("_find_createinstallmedia()...", end=" ", flush=True)
    try:
        path = _find_createinstallmedia()
        print(f"OK — {path}")
    except FlashError as e:
        print(f"OK — not found (expected if no installer downloaded): {e}")

    print("selftest passed.")


if __name__ == "__main__":
    selftest()
