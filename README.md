# Depot

```
▄▀▄ █▄▀ █   █▀▄ ▀▄▀
█▀█ █ █ █▄▄ █▀▄  █ 
```

![macOS](https://img.shields.io/badge/macOS-000?logo=apple&logoColor=white)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-success)
![license](https://img.shields.io/badge/license-MIT-blue)

A lightweight, terminal-native macOS firmware & installer downloader. Browse all
available macOS firmwares (`.ipsw`) and installers (`InstallAssistant.pkg`) in
a fast keyboard-driven TUI, pick one, and **aria2** downloads it with
multi-connection parallel download, resume, and SHA-1 verification.

A CLI/TUI reimplementation of [Mist](https://github.com/ninxsoft/Mist) — same
data sources, zero Xcode, one `brew install`.

## Preview

```
▄▀▄ █▄▀ █   █▀▄ ▀▄▀
█▀█ █ █ █▄▄ █▀▄  █   macOS installer & firmware downloader
────────────────────────────────────────────────────────────────────
❯ Firmwares (73)    Name                    Version Build   Size
  Installers (24)   ─────────────────────────────────────────────
  Downloads (0)   ❯ Tahoe                    26.5.2 25F84   18.4 GB
                      Tahoe                    26.5.1 25F80   18.4 GB
                      Tahoe                    26.5   25F71   18.4 GB
                      Sequoia                  15.5   25A354  13.0 GB
                      Sonoma                   14.7   24A335  12.0 GB

  ↑↓ move · Enter details · d download · S sort · e export · / filter
```

## Features

- **Browse firmwares & installers** — lists all available macOS versions from
  Apple Silicon firmwares (`.ipsw`) to ready-to-run installers
  (`InstallAssistant.pkg`), showing name, version, build, date, and size.
- **Beta catalogs** — cycle through Release, Developer Seed, Public Beta, and
  Customer Seed catalogs with `c` (Installers pane).
- **Flash to USB** — format a USB drive and create a bootable macOS installer
  with `f` (Installers pane). Supports any external USB drive.
- **Download with aria2** — multi-connection parallel download, automatic resume
  across sessions, SHA-1 checksum verification for firmwares.
- **Filter & sort** — type to filter, `S` to cycle sort by version/date/size,
  `g`/`G` to jump to top/bottom.
- **Export** — export the current list to CSV, JSON, or plist (`e`).
- **Keyboard-driven** — every action is a keystroke. No mouse, no menus, no
  distractions.
- **Single file** — ships as one stdlib zipapp executable on your `PATH`.

## Requirements

- **macOS** (uses `open`, `pbcopy`/`pbpaste`, `osascript`)
- **[aria2](https://aria2.github.io/)** (Homebrew installs it for you)
- **Python 3.10+**

## Install

### Homebrew

```sh
brew tap araidz/depot https://github.com/araidz/Depot
brew install depot
```

`brew` pulls in `aria2` and Python automatically. Update later with `brew upgrade depot`.

### From source

```sh
git clone https://github.com/araidz/Depot.git && cd Depot
sh build.sh                                       # -> dist/depot (one self-contained file)
ln -sf "$PWD/dist/depot" /opt/homebrew/bin/depot  # or anywhere on your PATH
```

Needs `aria2` (`brew install aria2`). Or run without building: `python3 -m depot`.

## Usage

Depot opens with the Firmwares list. Browse with `↑↓`, press Enter for details,
press `d` to download. Switch panes with `Tab`.

### Keyboard Shortcuts

**List panes** (Firmwares / Installers)

| Key | Action |
| --- | --- |
| `↑` / `↓` / `j` / `k` | move selection |
| `g` / `G` | jump to top / bottom |
| `Tab` | switch pane → |
| `Shift-Tab` | ← switch pane |
| `Enter` | view item details |
| `d` | download selected |
| `y` | copy download URL |
| `S` | cycle sort (version / date / size) |
| `c` | cycle catalog (Installers only: Release/Seed/Beta) |
| `f` | flash bootable installer to USB (Installers only) |
| `type` | filter list (backspace to delete) |
| `/` | clear filter |
| `e` | export list (csv / json / plist) |
| `?` | help |
| `q` | quit |

**Downloads pane**

| Key | Action |
| --- | --- |
| `↑` / `↓` / `j` / `k` | move selection |
| `d` | download selected (from other panes) |
| `p` | pause / resume |
| `x` | cancel download |
| `r` | retry failed download |
| `o` | reveal in Finder |

## Examples

```sh
# Start the TUI browser
depot

# Show version
depot --version
```

## How it works

Depot fetches firmware metadata from the [IPSW Downloads API](https://ipswdownloads.docs.apiary.io)
and installer metadata from Apple's Software Update Catalogs (`.sucatalog`).
Both Release and beta catalogs (Developer Seed, Public Beta, Customer Seed) are
supported — press `c` in the Installers pane to cycle between them.

Downloads are handled by a private `aria2c` instance driven over JSON-RPC — the
same engine and pattern used by [Trawl](https://github.com/araidz/Trawl).
aria2 handles parallel connections, resume, and session persistence.

The `f` key in the Installers pane starts the USB flash flow: it detects
external USB drives via `diskutil`, erases the selected drive as Mac OS Extended
(Journaled), copies the installer using Apple's `createinstallmedia`, and
validates the result.

State lives in `~/Library/Application Support/Depot/`:
`aria2-session.txt` (private session for resume).

## Privacy

Your files stay on your disk; nothing routes through a central server. Depot
only talks to Apple's public APIs (ipsw.me, swscan.apple.com) and downloads
via aria2.

## Credits

- [Mist](https://github.com/ninxsoft/Mist) — the original macOS Installer
  Super Tool this was inspired by
- [aria2](https://aria2.github.io/) — the download engine
- [Trawl](https://github.com/araidz/Trawl) — the TUI architecture this grew
  from
- [IPSW Downloads API](https://ipswdownloads.docs.apiary.io) — firmware
  metadata

No third-party Python packages are used; Depot is an independent stdlib-only
implementation.

## License

[MIT](LICENSE)
