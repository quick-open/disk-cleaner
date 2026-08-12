# Disk Cleaner & Info

A fast, **offline**, **100% open-source** disk cleaner & system info tool for Windows. Nothing is uploaded anywhere. Built entirely by AI with human testing and guidance, and published on [QuickOpen](https://quickopen.ai/projects/disk-cleaner).

> **100% AI-built and open source.** Apache-2.0.

## What it does

Scan and clean temporary files, browser and app caches, and the Recycle Bin (with a safe preview and size report); find the largest files and folders eating your disk; visualize disk usage by folder; and watch live CPU, memory, disk and network usage. Deletions go to the Recycle Bin by default. Runs locally, nothing uploaded.

## Install

Download **`DiskCleaner-Setup.exe`** from the [QuickOpen page](https://quickopen.ai/projects/disk-cleaner) or the [GitHub release](https://github.com/quickpod/disk-cleaner/releases/latest) and double-click it. It installs per-user, adds Desktop and Start Menu shortcuts, and can optionally trust the QuickOpen Root CA. Authenticode-signed by the QuickOpen Code Signing CA — verify at [quickopen.ai/trust](https://quickopen.ai/trust).

## Run from source

```sh
pip install -r requirements.txt
python disk_cleaner_app.py          # GUI
python -m diskkit --help    # CLI
```


## Features

- **Clean** — scans known junk locations (your `%TEMP%` folder, the Windows temp folder, Chrome / Edge / Firefox caches, the thumbnail cache, Windows Update leftovers, crash dumps and more), shows exactly how many files and how much space each holds, then reclaims what you tick. **Deletions go to the Recycle Bin by default** so anything cleaned can be restored; emptying the Recycle Bin itself is a separate, explicit action.
- **Large Files** — point it at a folder and it lists the biggest files, largest first, with a size threshold you choose. Send the ones you don't need to the Recycle Bin.
- **Disk Usage** — a folder-size breakdown that shows which sub-folders are eating your disk, with a share bar and file counts.
- **System Monitor** — live CPU (overall and per-core), memory, swap, per-disk usage, network throughput, battery and the top processes by memory, updating on a background thread.
- Safe by design: scans never delete, destructive actions always confirm first, and the whole thing runs **offline** — nothing is uploaded. Pure Python (psutil + send2trash + the standard library), with a dark mode.

## CLI examples

Everything the GUI does is available headless via `python -m diskkit` (scans are read-only; `clean` sends files to the Recycle Bin unless you pass `--no-trash`):

```sh
python -m diskkit scan                     # list cleanup targets + reclaimable size (no delete)
python -m diskkit scan --targets chrome_cache firefox_cache
python -m diskkit clean                    # clean everything (to the Recycle Bin), with a prompt
python -m diskkit clean --targets user_temp --yes      # no prompt
python -m diskkit clean --no-trash --yes               # delete permanently instead
python -m diskkit emptybin                 # permanently empty the Recycle Bin (Windows)

python -m diskkit large C:\Users\me --min 100MB --limit 20   # biggest files
python -m diskkit usage C:\Users\me --depth 2               # folder-size breakdown
python -m diskkit info                     # one system snapshot
python -m diskkit info --json              # ...as JSON
python -m diskkit watch 10 --interval 1    # live CPU/mem/net for 10 seconds
```

Any command accepts `--json` where it makes sense, and exits non-zero with a clear `error:` message (never a traceback) on failure.

## License

Apache-2.0 — see [LICENSE](LICENSE). A 100% AI-built project published on QuickOpen.
