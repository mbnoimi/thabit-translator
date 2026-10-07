# Thabit Translator

A CPU-only subtitle automation tool: download, extract, translate and transcribe
subtitles from any video file.

It ships in **two** forms:

1. **CLI** — `core/`, the pipeline itself. Install it with
   [pipx](https://pipx.pypa.io/) from PyPI, or run it straight from a checkout.
2. **Jellyfin 10.11+ plugin** — `jellyfin-plugin/`, a .NET assembly that runs that same
   pipeline as a subprocess inside a derived `jellyfin/jellyfin:10.11` image.

All commands below run from the repository root (`git clone … && cd thabit-translator`).

## Contents

- [Features](#features)
- [Installation](#installation)
  - [CLI with pipx](#cli-with-pipx)
  - [Jellyfin plugin](#jellyfin-plugin)
  - [From source](#from-source)
- [Usage](#usage)
- [Configuration](#configuration)
- [Supported languages](#supported-languages)
- [Output files](#output-files)
- [Jellyfin plugin: build, run, test](#jellyfin-plugin-build-run-test)
- [Releasing](#releasing)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [License](#license)
- [Support](#support)

## Features

- **Download** subtitles (OpenSubtitles, SubDL, SubSource) with file-hash / structured
  episode search and a relevance guard
- **Translate** subtitles between 16+ languages using Argos Translate
- **Extract** embedded subtitle tracks from video files (FFmpeg)
- **Transcribe** speech to text using Vosk (opt-in — it is hours of CPU)
- **Auto mode** — one command runs the whole chain (download → extract →
  translate → STT) on a single file or a whole folder
- **Jellyfin integration** — a subtitle provider in the per-item download menu, a
  scheduled task that fills in missing subtitles, a configuration page and
  `GET /ThabitTranslator/status`

Everything is CPU-only: the first run installs a CPU-only PyTorch build
(~1.6 GB total, once).

## Installation

### CLI with pipx

Requirements: Ubuntu/Debian-family Linux, `python3` (3.9+), `python3-venv`, `ffmpeg`.

```bash
pipx install thabit-translator

thabit-translator --help
```

Notes:

- The **first run** downloads the ML stack (Argos, Vosk, CPU-only torch,
  ~1.6 GB) into a per-app virtual environment — this is the same bootstrap the
  source checkout and the Jellyfin plugin use, and it repairs itself on later runs.
- On first run the config is created at
  `~/.config/thabit/thabit_translator.conf` — add your provider API keys there
  (see [Configuration](#configuration)).
- `pipx` never touches your system Python; upgrade with
  `pipx upgrade thabit-translator`.

### Jellyfin plugin

Three ways to install, from most to least complete:

**A. This project's image (recommended)** — build the plugin, the image and start
Jellyfin in one go:

```bash
cd jellyfin-plugin

UID=$(id -u) GID=$(id -g) docker compose run --rm plugin-build        # build the plugin zip
UID=$(id -u) GID=$(id -g) docker compose build jellyfin               # runtime image
UID=$(id -u) GID=$(id -g) MEDIA_ROOT=/path/to/shows \
    docker compose up -d jellyfin                                    # http://localhost:8096
```

or just `./test.sh` (build + install + verify) and `./test-stop.sh` (graceful
shutdown; state kept in `jf-config/`). The image ships python3, ffmpeg and the
plugin; `entrypoint.sh` installs the plugin into `/config/plugins/` on every
container start.

**B. As a Jellyfin plugin repository (GitHub route)** — add
`https://raw.githubusercontent.com/mbnoimi/thabit-translator/main/manifest.json`
as a repository (Dashboard → Plugins → Repositories), install *Thabit
Translator* from the Catalog and restart. The manifest points at the release zip
(`sourceUrl`) and carries its MD5 (`checksum`); both are produced by
[`deploy.sh`](#releasing) for the tagged release.

**C. Manual copy** — unzip the release zip into the server's plugin directory and
restart:

```bash
mkdir -p  <config>/plugins/Jellyfin.Plugin.ThabitTranslator
unzip thabit-translator-plugin_<version>.zip -d <config>/plugins/Jellyfin.Plugin.ThabitTranslator
```

`<config>` is `/config` for the official container image,
`/var/lib/jellyfin/plugins` for the deb/rpm packages. Jellyfin discovers plugins
from *directories* containing the assembly — a loose `.zip` is not picked up.

Runtime requirements for B and C (the official image misses all of these):

| Requirement | Notes |
|---|---|
| Jellyfin ≥ 10.11 | `targetAbi 10.11.0.0` |
| `python3` + `python3-venv` | `python3 -m venv` must work (Debian: install `python3-venv`) |
| `ffmpeg`/`ffprobe` on `PATH` | the official image keeps them only under `/usr/lib/jellyfin-ffmpeg/` |
| Writable plugin data folder | `library/`, `.venv_thabit/`, `thabit_translator.conf`, `staging/`, `home/` live there |
| Ubuntu/Debian-family `/etc/os-release` | the library's own system check insists on it |

The DLL carries the whole Python library with it (extracted into the plugin data
folder on load), and the venv bootstrap creates everything else on first run —
or up front via the **Prepare runtime** button on the config page.

### From source

```bash
git clone https://github.com/mbnoimi/thabit-translator.git
cd thabit-translator

python3 core/thabit_translator/__main__.py --help     # venv bootstraps itself on first run
# equivalently, from core/:
python3 -m thabit_translator --help
```

The shared venv lives at `.venv_thabit` in the repository root: every run
verifies it and installs whatever is missing, so a partial venv repairs itself
instead of crashing with `ModuleNotFoundError`. Delete `.venv_thabit` to rebuild
from scratch.

## Usage

Modes: `auto`, `translate`, `extract`, `download`, `stt` (or no argument for the
interactive menu). The examples below use the pipx-installed command; from a
checkout, replace `thabit-translator` with `python3 -m thabit_translator`
(run from `core/`).

| Mode | Purpose | Input |
|------|---------|-------|
| `auto` | Download → extract → translate → STT fallback | file **or folder** |
| `translate` | Convert an existing SRT to another language | `.srt` file |
| `extract` | List / extract embedded subtitle streams | single video |
| `download` | Fetch subtitles from providers | single video |
| `stt` | Generate subtitles from speech | single video |

### Auto mode (recommended)

```bash
# Any video, or an existing subtitle file (subtitle in = just translate it)
thabit-translator auto "movie.mp4"
thabit-translator auto "movie.srt" -t fr

# Custom target language (default comes from config)
thabit-translator auto "movie.mp4" -t ar

# A folder - season, whole show, or a movies folder (recursive batch)
thabit-translator auto "/path/to/Show/Season 01"
thabit-translator auto "/path/to/Show"             # every season
thabit-translator auto "/path/to/Show" --force     # redo files that already have one
```

**Auto mode flow:**

1. Try downloading a subtitle in the target language (hash → query, all providers)
2. Try extracting embedded subtitles from the video
3. Try downloading an English subtitle as fallback → translate
4. Use Vosk STT as last resort → translate

**Input resolution:** if the given path does not exist, the tool matches the real
file in the same folder and works with that instead (`Monk - S01E3.mp4` →
`Monk - S01E03.mp4`). Applies to all five modes.

**Output naming:** the subtitle name always mirrors the source file exactly —
`Monk - S01E03.mp4` produces `Monk - S01E03.ar.srt`, never a name derived from a
mistyped input path.

**Folder mode (batch):** every video in the folder is processed in sorted order
and each subtitle is written next to its video:

- Videos that already have a valid `<name>.<lang>.srt` are **skipped**, so an
  interrupted run can simply be re-run and continues where it stopped
- `--force` re-processes files that already have a subtitle
- Hidden files and files whose name contains `sample` are ignored
- One failing video never aborts the batch — it is reported in the summary
- Ends with a summary: `[AUTO] Folder done: 12 ok, 1 skipped, 0 failed`
- `extract`, `download` and `stt` remain single-file only

**Never blocks on input:** no provider call can stall a batch. When several
candidates match, `auto` takes option 1 instead of prompting — only the
`download` command asks you to pick, and only when stdin is a terminal.

**STT asks first:** when the providers have nothing (or are blocked), the file
has no embedded track and the English download failed too, `auto` stops before
transcribing, because STT is by far the slowest step:

```
Start STT? [y]es  [n]o/skip  [a]lways for this run:
```

`--stt ask` (default) prompts on a terminal, `--stt yes` never asks (the right
choice for a subprocess caller such as the Jellyfin plugin), `--stt no` never
runs STT. With no terminal to ask, the video is skipped with a message instead
of hanging, and folder mode counts those as `STT-declined`:

```
[AUTO] Folder done: 12 ok, 3 skipped, 1 STT-declined, 0 failed
```

### Episode search is structured, not textual

`Monk - S01E12` is not a title any provider indexes. `auto` resolves the series
once (`query=Monk, type=episode` → `parent_imdb_id`, cached per show name, so a
124-video folder pays for one lookup) and asks for that exact season/episode;
when the API knows no such episode it widens to the whole season and lets the
name filter pick the release labelled `S01E12` — OpenSubtitles files some
episodes off by one (its S01E13 entry is the file released as `Monk.S01E12...`).
SubDL gets `type=tv` with `season_number`/`episode_number` instead of a
movie-name search, which is what its "can't find movie or tv" error was about.

**Wrong-title protection:** a result must really be your episode. Names are
parsed with **guessit** (what subliminal/bazarr use), so `S01E12`, `1x12`,
`Season 1 Episode 12` and scene releases all mean the same episode, and plain
movie names carry no code at all. A candidate is rejected when its own name
carries a different code, when the API's season/episode metadata says something
else and the name adds nothing, or when nothing identifies it and it did not
come from an exact file-hash match. Rejections are logged as `dropped N
result(s) for a different episode`, and a provider whose search comes back
empty is queried once instead of repeating the same search for every candidate
index.

### Provider limits and quota handling

**OpenSubtitles** allows ~20 downloads per day per account. When the quota runs
out the workflow falls back to Vosk STT for **every remaining video, folder mode
included** — correct but very slow on long videos, so prefer running one season
at a time. Once OpenSubtitles answers with HTTP 406 (`remaining: 0`) it is
skipped until that response's `reset_time` (usually 23:59 UTC) and then
**re-enabled automatically**, so a batch crossing the reset keeps working
without a restart. Every mention of it names the cause — your *account's* limit,
not "no subtitles":

```
[WARN] OpenSubtitles: your account hit its daily download limit (20/day), reset Sun 02:59 +03
[AUTO] OpenSubtitles is over your account's daily download limit until Sun 02:59 +03 - it may already have this subtitle
[INFO] OpenSubtitles skipped: your account's daily download limit until Sun 02:59 +03 - trying: subdl, subsource
```

**SubDL**'s free plan allows 2,000 searches/day but only **50 downloads/day**
(a download link without `api_key` is instead capped at 300/day per IP), so a
SubDL 429 is nearly always the download quota, not a missing subtitle. The tool
names which one it was, drops the provider for the rest of the run, and picks it
up again on its own when the limit's own short window expires:

```
[WARN] SubDL: your account's daily download limit is spent (50 downloads/day on the free plan)
[INFO] subdl skipped: your account's daily download limit is spent (50 downloads/day on the free plan) - trying: subsource
```

A short throttle (`Retry-After` up to 30s) is waited out and retried once instead
of being given up on, and expired windows are re-checked. Missing or rejected
credentials are reported once as a setup problem rather than retried for every
candidate index of every video, and API keys are masked in logged download URLs.
Each reason is printed **once per run**, so a season where all three providers
are exhausted stays readable.

Only OpenSubtitles supports a file-hash lookup, so SubDL and SubSource are asked
**once** per video rather than twice (the "retry by query" pass used to repeat
the exact same request). SubSource has no Monk TV episodes at all and returns
irrelevant results — these are dropped by the relevance guard.

### Other modes

```bash
# Translate: the one mode where you name the output yourself (-s defaults to en, -t to ar)
thabit-translator translate input.srt output.ar.srt -s en -t ar
thabit-translator translate movie.srt french.srt -t fr

# Extract embedded subtitles (list first, then pick the stream index)
thabit-translator extract "movie.mp4" -l
thabit-translator extract "movie.mp4"          # first stream
thabit-translator extract "movie.mp4" -s 2     # specific stream
thabit-translator extract "movie.mp4" --src en # language hint for untagged streams

# Download from providers
thabit-translator download "movie.mp4" -t ar
thabit-translator download "movie.mp4" -t ar -q "Movie Title 2024"   # custom query
thabit-translator download "movie.mp4" -t ar -p opensubtitles         # one provider
thabit-translator download "movie.mp4" -t ar --select 0               # pick by index

# Speech to text
thabit-translator stt "movie.mp4" -s en
thabit-translator stt "movie.mp4" -s en -t fr
thabit-translator stt "movie.mp4" -s en -m vosk-model-small-en-us-0.15 -t ar --max-duration 5.0 --max-chars 60
```

- `download` searches by file hash first, then falls back to a filename query;
  `--no-hash` skips the hash lookup, `--no-auto` disables auto-pick (use with
  `--select N`)
- The Vosk model for STT is downloaded on first use (~40–70 MB) and cached in
  `~/.cache/thabit_translator/vosk_models/`

Running with **no arguments** opens an interactive prompt (`thabit>`): type the
same commands as above but without `thabit-translator`, `?` reprints the menu,
`q` quits.

## Configuration

Provider API keys and defaults live in one INI file. Lookup order:

1. `$THABIT_CONFIG` (explicit path — also what any `-c/--config` argument does)
2. `core/thabit_translator.conf` (source checkout, next to the package)
3. `~/.config/thabit/thabit_translator.conf`
4. `~/.thabit_translator.conf`

The first file that exists wins. If **none** exists (fresh pipx install), the
shipped template is copied to `~/.config/thabit/thabit_translator.conf` on first
run — that is the file to edit. Create it manually from
`core/thabit_translator/thabit_translator.conf.template` when working from
a checkout:

```ini
[opensubtitles]
api_key = YOUR_API_KEY
username = your_username
password = your_password
default_language = ar
user_agent = ThabitTranslator v1.0

[subdl]
api_key = YOUR_SUBDL_KEY

[subsource]
api_key = YOUR_SUBSOURCE_KEY

[settings]
default_source_lang = "en"
default_target_lang = "ar"
verbose = true
cache_dir = "~/.cache/thabit_translator"

[providers]
enabled = opensubtitles,subdl,subsource
```

Get API keys from:

- **OpenSubtitles**: https://www.opensubtitles.com/ (free account)
- **SubDL**: https://subdl.com/
- **SubSource**: https://subsource.net/

Without keys the pipeline falls back to embedded tracks (and STT in auto mode).

### Jellyfin plugin settings

Dashboard → Plugins → Thabit Translator, or directly
`http://localhost:8096/web/ConfigurationPage?name=Thabit%20Translator` (admin
session cookie or `X-Emby-Token` required — the page name is the plugin name,
*not* the C# namespace).

| Setting | Default | Notes |
|---|---|---|
| OpenSubtitles / SubDL / SubSource keys | empty | rendered into `thabit_translator.conf`; without them the pipeline falls back to embedded tracks |
| Target languages | `ar` | comma separated ISO-639-1, shown in the download menu |
| Source language | `en` | language the produced subtitle is in |
| Enabled providers | all three | |
| STT policy | `ask` | `no` skips, `yes` runs Vosk, `ask` only asks when a terminal exists — a scheduled run never has one, so it skips (see `core/auto_mode.md`) |
| Auto task | enabled, max 0 (unlimited) | languages can be narrowed per library by that library's own "Download subtitles in these languages" |

Saving the settings regenerates `thabit_translator.conf` immediately; every run
re-writes it too, so a change never needs a restart. Status for debugging:

```sh
curl -H "X-Emby-Token: $TOKEN" http://localhost:8096/ThabitTranslator/status
 # {"Loaded":true,"DataFolder":…,"TargetLanguages":"ar","PythonPath":"python3",
 #  "PythonAvailable":true,"PythonDetail":"Python 3.13.5 at python3","VenvReady":true,
 #  "LibraryPath":…,"LibraryExtracted":true,"IsBusy":false,…}
```

## Supported languages

### Translation (Argos Translate)

| Code | Language | Code | Language |
|------|---------|------|---------|
| en | English | ar | Arabic |
| fr | French | de | German |
| es | Spanish | it | Italian |
| pt | Portuguese | ru | Russian |
| zh | Chinese | ja | Japanese |
| ko | Korean | tr | Turkish |

### Speech recognition (Vosk)

| Code | Model | Size |
|------|-------|------|
| en, en_US, en_GB | vosk-model-small-en-us-0.15 | ~40MB |
| en_IN | vosk-model-small-en-in-0.4 | ~45MB |
| ar, ar_SA | vosk-model-small-ar-0.22 | ~60MB |
| fr, fr_FR | vosk-model-small-fr-0.22 | ~55MB |
| de, de_DE | vosk-model-small-de-0.15 | ~40MB |
| es, es_ES | vosk-model-small-es-0.22 | ~55MB |
| ru, ru_RU | vosk-model-small-ru-0.4 | ~70MB |
| pt, pt_BR | vosk-model-small-pt-0.3 | ~60MB |
| tr, tr_TR | vosk-model-small-tr-0.3 | ~55MB |
| zh, zh_CN | vosk-model-small-cn-0.3 | ~80MB |
| ja, ja_JP | vosk-model-small-ja-0.22 | ~55MB |
| ko, ko_KR | vosk-model-small-ko-0.22 | ~55MB |
| it, it_IT | vosk-model-small-it-0.4 | ~70MB |
| nl, nl_NL | vosk-model-small-nl-0.22 | ~55MB |
| pl, pl_PL | vosk-model-small-pl-0.4 | ~70MB |
| uk, uk_UA | vosk-model-small-uk-0.4 | ~70MB |

## Output files

Subtitles are saved next to the input file, named after it:

```
movie.mp4           → movie.ar.srt     (Arabic subtitle)
video.mkv           → video.en.srt     (English subtitle)
subtitle.srt        → subtitle.fr.srt  (French translation)
```

The base name always matches the source file exactly, including zero padding:
`Monk - S01E03.mp4` produces `Monk - S01E03.ar.srt`.

## Jellyfin plugin: build, run, test

```sh
cd jellyfin-plugin

# One shot - build the zip, build the image, start Jellyfin, install and verify
./test.sh
./test-stop.sh     # graceful shutdown (SIGTERM + 30s; state kept in jf-config/)

# The same steps by hand (bash's UID is readonly, so it is injected with env(1))
env UID=$(id -u) GID=$(id -g) docker compose run --rm plugin-build   # zip -> artifacts/
env UID=$(id -u) GID=$(id -g) docker compose build jellyfin          # runtime image
env UID=$(id -u) GID=$(id -g) MEDIA_ROOT=/path/to/shows docker compose up -d jellyfin
```

`MEDIA_ROOT` is mounted read-write at `/media` and defaults to the dev media
folder; point it at something that exists (for the e2e use
`MEDIA_ROOT=$PWD/media-test`).

**The development loop is build → image → up (or `./test.sh`).**
`entrypoint.sh` copies `/opt/thabit/plugin/*` (baked into the image) over
`./config/plugins/…` on *every* container start, so a hand-copied DLL is silently
replaced the next time the container restarts. Always rebuild the image.

### How the plugin runs the pipeline

The plugin is a **launcher for the library's CLI**: the .NET assembly embeds
`core/**/*.py` (only `*.py` — never `thabit_translator.conf`), extracts it
into the plugin data folder when the DLL changes, checks for a Python interpreter
at install time, and then spawns

```
python3 <data>/library/thabit_translator/__main__.py auto <video path> -t <lang> …
```

The CLI bootstraps its own venv (`<data>/.venv_thabit`, CPU-only torch first) on
first run — or up front via the **Prepare runtime** button on the config page.

```
Jellyfin 10.11 (jellyfin/jellyfin:10.11 + python3)
└── /config/plugins/Jellyfin.Plugin.ThabitTranslator/
    ├── Jellyfin.Plugin.ThabitTranslator.dll     installed by entrypoint.sh on start
    ├── library/                                *.py extracted from the DLL (versioned marker)
    │   └── thabit_translator/                   the package
    │       ├── __main__.py                      the entry the plugin spawns
    │       ├── app.py                           venv bootstrap + system check
    │       ├── cli.py  core/  providers/
    ├── .venv_thabit/                            created by the CLI's bootstrap on first run
    ├── thabit_translator.conf                   regenerated from plugin settings
    ├── staging/<itemId>_<lang>/                 symlink sandbox for manual downloads
    └── home/                                   HOME/XDG for the child process (Argos/vosk cache)
```

A manual download goes through a **staging sandbox**: the runner creates
`staging/<itemId>_<lang>/`, symlinks the video into it and hands the **symlink
path** to the CLI, so extraction, intermediate `.srt` files and the final
translation all land in staging — nothing is ever written to (or deleted from)
the media folder by the library. The bytes are returned to Jellyfin through
`DownloadSubtitles`, and Jellyfin writes them itself to
`<video stem>.<lang>.srt` next to the video. Staging is removed on every exit
path.

Because the provider hides a language that already exists on disk, a second
download of the same language is impossible — that is what keeps
`.0.srt`/`.1.srt` duplicates from ever appearing. The scheduled task
(*Fill in missing subtitles (Thabit Translator)*) uses the same runner but skips
items whose subtitle already exists, items whose file is missing, and libraries
where the provider is disabled as a subtitle fetcher.

### Credential handling

`core/thabit_translator.conf` holds real API credentials and must never
enter an image layer — and it cannot: `core/` is not part of any Docker
build context (`Dockerfile.dockerignore` allow-lists only `jellyfin-plugin/**`,
with explicit denies as belt-and-braces), and the DLL embeds only `*.py` (a
build guard in the csproj fails the build if anything else would be packed).
Runtime credentials live in the plugin settings XML and the per-plugin
`thabit_translator.conf`, both under `jf-config/` (never committed).

### Tests

```sh
cd jellyfin-plugin

# start the stack against the fixtures (the e2e asserts on media-test/)
env UID=$(id -u) GID=$(id -g) MEDIA_ROOT=$PWD/media-test \
    docker compose up -d --force-recreate jellyfin

env UID=$(id -u) GID=$(id -g) docker compose run --rm plugin-test   # xunit (60 tests)
python3 tests/e2e_smoke.py                                         # full HTTP round trip
```

`tests/e2e_smoke.py` is stdlib-only: it runs the startup wizard, logs in,
creates a library over `media-test/`, searches for subtitles, downloads one
(which runs the Python pipeline inside the container), and asserts that
`<video stem>.ar.srt` exists with non-empty Arabic content, no duplicate was
created, the provider hides a language it already produced, and the staging
folder is empty afterwards. It is re-runnable: it never deletes anything, so
delete the produced `.srt` first if you want the pipeline to run again.

## Releasing

Release metadata (version, maintainer, plugin catalog fields, PyPI description)
lives in [`metadata.conf`](https://github.com/mbnoimi/thabit-translator/blob/main/metadata.conf) —
**the one file you edit before a release**. `./deploy.sh` then prepares
everything else:

1. stamps `jellyfin-plugin/build.yaml` and `core/pyproject.toml` from it
   (idempotent — a re-run never dirties git),
2. builds the plugin zip + `manifest.json` into `<repo>/dist/`,
3. builds the pipx sdist + wheel in Docker and runs `twine check` on them.

```bash
./deploy.sh          # both channels -> dist/
```

| Artifact | Purpose |
|---|---|
| `thabit-translator-plugin_<version>.zip` | manual plugin install (route C above) |
| `manifest.json` | catalog file for a **GitHub plugin repository**, with the zip's MD5 baked in — copy it to the repo root |
| `thabit_translator-<version>.whl` + `.tar.gz` | the PyPI release (`twine upload dist/thabit_translator-*`) |

The actual `twine upload` is the one manual step; after it,
`pipx install thabit-translator` is the whole CLI distribution.

`deploy.sh` prints the MD5 of each artifact and refuses to package anything that
would leak `core/thabit_translator.conf`, the file that holds the real
provider keys.

## Development

```bash
# Python package: run the checks you need from core/
cd core && python3 -m thabit_translator --help

# Plugin: xunit in Docker (see Tests above)
cd jellyfin-plugin && env UID=$(id -u) GID=$(id -g) docker compose run --rm plugin-test

# Packaging test: builds the wheel and installs it with pipx - inside Docker only,
# your host pipx/venv is never touched
./core/test-pipx.sh
```

- Contributor notes, architecture and hard-won constraints:
  `ai/AGENTS.md` (local file in the project, not published to this repository)
- Plugin design notes: `core/auto_mode.md`

### Internal architecture

```
core/
├── thabit_translator/          the Python package (PyPI: thabit-translator)
│   ├── __main__.py             entry for `python3 -m` and the plugin spawn
│   ├── app.py                  venv bootstrap + system check (CLI or lib)
│   ├── cli.py                  subcommands + interactive menu
│   ├── core/
│   │   ├── config.py           config loading (+ first-run template copy)
│   │   ├── extract.py          FFmpeg subtitle extraction
│   │   ├── translate.py        Argos Translate wrapper
│   │   ├── speech_to_text.py   Vosk STT wrapper
│   │   ├── paths.py            input resolution (S01E3 -> S01E3 ...)
│   │   ├── match.py            episode/title guard (guessit)
│   │   └── auto_workflow.py    auto mode orchestration (file + folder batch)
│   ├── providers/
│   │   ├── __init__.py         provider dispatcher (NO_MATCH stops empty retries)
│   │   ├── limits.py           shared quota/rate-limit state
│   │   ├── opensubtitles.py    OpenSubtitles.com API (quota, structured search)
│   │   ├── subdl.py            SubDL.com API (movie + tv search)
│   │   └── subsource.py        SubSource.net API
│   └── thabit_translator.conf.template   copied to ~/.config on first run
├── pyproject.toml              PyPI package (version/description stamped by deploy.sh)
├── test-pipx.sh                packaging test - Docker-only
├── Dockerfile.pipx-test{,.dockerignore}  its build image + credential allow-list
├── thabit_translator.conf      local provider keys (gitignored)
└── auto_mode.md                plugin design notes
```

**CPU enforcement:** `ARGOS_DEVICE_TYPE=cpu`, `CUDA_VISIBLE_DEVICES` cleared,
CPU-only torch from `https://download.pytorch.org/whl/cpu`, `imageio-ffmpeg` as
extraction fallback.

**Auto-environment:** every run bootstraps its dependencies before anything
else — create the venv on first run, repair it later (install only what is
missing), install CPU-only torch first (the default PyPI wheel drags in several
GB of CUDA libraries; PyPI is only the fallback), then re-execute inside it.
Where the venv lives depends on how you run it:

- source checkout → `.venv_thabit` in the repository root (sibling of `core/`)
- Jellyfin plugin → `<plugin data>/.venv_thabit` (sibling of the extracted `library/`)
- pipx → the pipx venv itself is repaired in place, no extra venv

Models cache in `~/.cache/thabit_translator/`. If the venv is beyond repair,
delete it and rerun — it is recreated from scratch:

```bash
rm -rf .venv_thabit
cd core && python3 -m thabit_translator --help
```

## Troubleshooting

### CLI

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named '...'` | the venv was skipped/incomplete — the bootstrap fixes it on the next run; if not, `rm -rf .venv_thabit` and rerun |
| Missing essential system tools | `sudo apt install ffmpeg mkvtoolnix` (mkvtoolnix recommended for MKV) |
| Download fails or returns empty file | the tool retries by query, walks several results, validates downloads, then falls back to embedded tracks or STT |
| Folder run slows to a crawl after ~20 files | OpenSubtitles' ~20/day account quota ran out; every remaining video falls back to STT (minutes–hours each). Stop (Ctrl+C) and re-run later — videos with a valid `<name>.<lang>.srt` are skipped, so the batch resumes |
| STT is slow / first run slow | first run downloads the Vosk model (~40–70 MB, cached afterwards in `~/.cache/thabit_translator/vosk_models/`) or, on a fresh install, the ~1.6 GB ML stack |
| Extraction fails | install mkvtoolnix for better MKV support |
| PyPI install cannot reach the CPU torch index | the bootstrap falls back to PyPI automatically (larger download) |

### Jellyfin plugin

| Symptom | Cause / fix |
|---|---|
| A hand-copied DLL "reverts" after a restart | `entrypoint.sh` reinstalls the image's plugin on every start — rebuild (build → image → up) |
| `ConfigurationPage` → 404 | wrong `name=`: use `Thabit Translator` (the page name), not the C# namespace |
| 401 on API calls | `Authorization: MediaBrowser Token="<token>", Client=…, DeviceId=…, Version=…` — a *bare* `Token=` header is rejected |
| "no API key configured" warnings | settings are empty; paste the keys on the config page |
| Red banner "Python was not found" | install `python3` + `python3-venv` (or set the interpreter path) — the status API's `PythonDetail` says what was tried |
| First run takes many minutes | normal: the venv bootstrap downloads ~1.6 GB once; use **Prepare runtime** to do it deliberately |
| `STT skipped (no terminal to ask …)` | expected: STT policy is `ask` and a subprocess has no TTY; set the policy to `yes` to transcribe anyway |
| Pipeline never runs | `docker compose build jellyfin` after `plugin-build`, then `up -d --force-recreate jellyfin` |

## License

Licensed under the GNU Lesser General Public License v3.0 only
(LGPL-3.0-only) — see [LICENSE](https://github.com/mbnoimi/thabit-translator/blob/main/LICENSE).
Copyright (C) 2024-2026 mbnoimi.

## Support

- GitHub: https://github.com/mbnoimi/thabit-translator
- Issues: https://github.com/mbnoimi/thabit-translator/issues
