# EEG Dataset Server

A Flask application with two parts:

1. **REST API** for uploading EEG dataset files (`.csv`, `.tsv`, `.edf`, `.npy`, `.json`) and getting back structural/debug metadata — shape, channels, sampling rate, header info, missing values, etc. It intentionally does **not** run any signal analysis (no filtering, no spectral analysis, no artifact detection).
2. **Live Monitoring Dashboard** (`/live`) that renders a live-sweeping multichannel EEG chart, per-band topographic maps, and a downloadable session report. This half *does* perform signal analysis (band-pass filtering for band power, montage-based topomaps) and is kept deliberately separate from the analysis-free upload API.

Live demo: **https://eeg-a37i.onrender.com**
Repo: **github.com/CuteDragy/EEG**

---

## Table of contents

- [Project structure](#project-structure)
- [Quick start (local)](#quick-start-local)
- [Environment variables](#environment-variables)
- [Setting up MongoDB Atlas](#setting-up-mongodb-atlas)
- [Deploying to Render](#deploying-to-render)
- [Supported file formats](#supported-file-formats)
- [API reference](#api-reference)
- [`/live` dashboard reference](#live-dashboard-reference)
- [MNE-Python integration](#mne-python-integration)
- [The `display.py` CLI helper](#the-displaypy-cli-helper)
- [Known limitations / roadmap](#known-limitations--roadmap)
- [Contributing](#contributing)

---

## Project structure

```
.
├── app.py                # Flask app: upload API + /live dashboard, all routes
├── display.py             # Rich-powered terminal client for testing /upload
├── requirements.txt
├── runtime.txt             # pins Python version for Render (add this - see below)
├── Procfile                 # tells Render how to start the app (add this - see below)
├── templates/
│   ├── base.html            # shared layout (nav, page shell)
│   ├── index.html           # dashboard (/) - dataset list, search, filter
│   ├── upload.html          # upload page (/upload-page)
│   ├── detail.html          # single-dataset detail/signal-viewer page
│   └── 404.html
├── static/
│   ├── css/style.css        # NOT included in this upload - see note below
│   └── js/
│       ├── dashboard.js     # NOT included in this upload - see note below
│       └── upload.js        # NOT included in this upload - see note below
└── uploads/                 # uploaded files land here (created automatically)
```

> **⚠️ CSS/JS note:** `base.html` references `static/css/style.css`, and `index.html`/`upload.html` reference `static/js/dashboard.js` / `static/js/upload.js` via `url_for('static', filename=...)`. Flask expects these under a top-level `static/` folder. If you're cloning this repo, make sure the actual CSS/JS files are committed there too — the templates alone won't render/function correctly without them.

## Quick start (local)

```bash
git clone https://github.com/CuteDragy/EEG.git
cd EEG

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
python app.py
```

By default the app reads `PORT` from the environment (falls back to `0`, i.e. Flask's default `5000` if unset — see [Environment variables](#environment-variables)). Once running:

- Dashboard: `http://localhost:5000/`
- Upload page: `http://localhost:5000/upload-page`
- Live monitor: `http://localhost:5000/live`

MongoDB is **optional for local dev** — if `pymongo` can't connect, the app silently falls back to in-memory storage (a plain Python dict), so you can develop and test without setting up a database at all. Metadata just won't survive a restart.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017/` | MongoDB connection string. Set this to your Atlas connection string in production. |
| `MONGO_DB` | `eeg_dataset_server` | Database name to use inside the MongoDB cluster. |
| `PORT` | `0` (→ Flask default `5000`) | Port the server binds to. Render sets this automatically — don't hardcode a port yourself. |
| `FLASK_DEBUG` | `true` | Set to `false`/`0` in production. Debug mode should never be left on for a public deployment. |

Create a `.env` file for local overrides (and load it with `python-dotenv`, or just `export` them in your shell) — just don't commit real credentials to the repo. Add a `.env.example` with blank/placeholder values so contributors know what to set.

## Setting up MongoDB Atlas

1. Create a free account at [mongodb.com/cloud/atlas](https://www.mongodb.com/cloud/atlas) and spin up a free (M0) cluster.
2. Under **Database Access**, create a database user with a username/password (not your Atlas account login).
3. Under **Network Access**, add an IP allowlist entry. For a Render-hosted app, the simplest option is to allow `0.0.0.0/0` (all IPs) since Render's outbound IPs aren't static on the free tier — tighten this later if you upgrade to a plan with static IPs.
4. Click **Connect → Drivers**, choose Python, and copy the connection string. It looks like:
   ```
   mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
   ```
5. Set that as your `MONGO_URI` environment variable (both locally and on Render). Replace `<password>` with your actual database user password, URL-encoding any special characters.
6. `MONGO_DB` can stay as the default `eeg_dataset_server`, or set it to whatever database name you want — pymongo creates it automatically on first write.

The app uses GridFS (via the `gridfs` package, bundled with `pymongo`) alongside a regular collection: metadata goes in the `datasets` collection, and the raw uploaded file bytes are stored durably in GridFS so they survive server restarts (unlike the local `uploads/` folder on an ephemeral host like Render's free tier).

## Deploying to Render

1. Push the repo to GitHub, then in Render click **New → Web Service** and connect the repo.
2. **Build command:**
   ```
   pip install -r requirements.txt
   ```
3. **Start command:**
   ```
   gunicorn -w 4 -b 0.0.0.0:$PORT app:app
   ```
   Render injects `$PORT` automatically — don't hardcode `5000`.
4. Add a `runtime.txt` to the repo root to pin the Python version (avoids build failures from version drift, e.g. `mne`/`numpy` wheel availability):
   ```
   python-3.11.9
   ```
5. Add a `Procfile` to the repo root (some Render setups use this instead of/alongside the dashboard start command):
   ```
   web: gunicorn -w 4 -b 0.0.0.0:$PORT app:app
   ```
6. In the Render dashboard, under **Environment**, add:
   - `MONGO_URI` — your Atlas connection string
   - `MONGO_DB` — your database name
   - `FLASK_DEBUG` — `false`
7. Deploy. Render's free tier spins the service down after inactivity, so the first request after idle time will be slow (cold start) — this is normal, not a bug.
8. If you need a teammate to access the live API, just share the Render URL — no additional auth is configured (see [Known limitations](#known-limitations--roadmap): there's currently no API key/auth layer, so anyone with the URL can hit every endpoint including deletes).

## Supported file formats

| Extension | What's parsed |
|---|---|
| `.csv` / `.tsv` | shape, column/channel names, dtypes, missing values, auto-detected sampling rate (if a `time`/`timestamp` column exists), preview rows |
| `.edf` | EDF/EDF+ header — patient/recording id, start date/time, number of signals, per-channel labels, units, sampling rate, total duration. Parsed natively, no external EEG library required for the core parse |
| `.npy` | shape, dtype, number of elements, preview values |
| `.json` | detects array-of-records vs column-oriented structure, field names, lengths |

Max upload size is capped at **500 MB** (`MAX_CONTENT_LENGTH` in `app.py`).

## API reference

All endpoints return JSON unless noted otherwise.

### `GET /health`
Liveness check.

### `GET /stats`
Aggregate stats across all stored datasets (counts, storage totals, format breakdown).

### `POST /upload`
Upload a dataset file (`multipart/form-data`, field name `file`).
```bash
curl -F "file=@my_eeg_recording.edf" https://eeg-a37i.onrender.com/upload
```
Returns a JSON record with `dataset_id`, file metadata, `parse_status` (`ok`/`error`), and an `info` object with the format-specific details above. Optional query params:
- `?mne=false` — skip the MNE metadata enrichment block (see [MNE integration](#mne-python-integration))
- `?sfreq=<hz>` — supply a sampling rate for `.csv`/`.tsv`/`.npy` files where it can't be auto-detected, enabling the MNE block for those formats too

### `GET /datasets`
List datasets (summary view), with search/filter/sort/pagination:
- `?search=name` — case-insensitive filename substring match
- `?format=edf` — filter by extension (`csv`/`tsv`/`edf`/`npy`/`json`)
- `?status=ok|error` — filter by parse status
- `?sort=uploaded_at|file_size_bytes|original_filename` (default `uploaded_at`)
- `?order=asc|desc` (default `desc`)
- `?limit=20&offset=0` — pagination

### `GET /datasets/<dataset_id>`
Full stored record (including the detailed `info` block) for one dataset.

### `GET /datasets/<dataset_id>/view`
HTML detail page for one dataset (rendered via `detail.html`).

### `GET /datasets/<dataset_id>/channel/<idx>`
Raw signal data for one channel, by index (EDF files only).

### `PATCH /datasets/<dataset_id>`
Rename a dataset's display filename.

### `DELETE /datasets/<dataset_id>`
Remove one dataset's record and its stored file.

### `DELETE /datasets`
Bulk delete — `?ids=a,b,c` for specific datasets, or `?confirm=true` to wipe everything.

### `GET /datasets/<dataset_id>/download`
Download the original uploaded file as-is.

### `POST /datasets/<dataset_id>/reparse`
Re-run parsing on an already-uploaded file with new query-param options (e.g. a different `?sfreq=`).

## `/live` dashboard reference

`GET /live` — renders the live monitoring page. Query params (all optional, combinable):

| Param | Default | Notes |
|---|---|---|
| `source` | `auto` | `auto` \| `mongo` \| `dataset` \| `mock` — where the signal comes from |
| `n_channels` | 64 | channel count for mock data (4–256) |
| `sfreq` | 160 | sampling rate in Hz for mock data (32–1024) |
| `regions` | all | comma-separated brain regions to display, e.g. `Frontal,Central` |
| `window_sec` | 6 | sweep/analysis window in seconds (2–15) |
| `topo_refresh` | 1.0 | topomap refresh interval in seconds (0.5–3.0) |

Other `/live` endpoints:
- `GET /live/report.txt` — downloadable session report (band-power breakdown, dominant band, usage summary) as plain text.
- `GET /live/api/topomap/<band>.png` — live topographic-map image for one band. `band` is one of `Delta`, `Theta`, `Alpha`, `Beta`, `Gamma`, `Broadband`. Requires `mne` and `matplotlib` to be installed; returns `503` otherwise.

The dominant-band note shown on the page is explicitly labeled a general research-based association, **not a clinical or diagnostic assessment** — keep that framing if you extend this page.

## MNE-Python integration

Every parser's output can include an extra `mne` block, built with [MNE-Python](https://mne.tools/), purely as structural/metadata enrichment layered on top of the native parsers — it never filters or transforms the signal for the upload API. It reports:

- channel count/names + a lightweight EEG/EOG/ECG/EMG/stim type guess
- `sfreq`, highpass/lowpass as recorded in the file header
- measurement date, bad channel list
- 10-20 standard montage match (which channel labels MNE recognizes)
- annotations and events derived from annotations

For `.edf` this uses `mne.io.read_raw_edf(..., preload=False)` directly. For `.csv`/`.tsv`/`.npy` it's best-effort and only runs when a sampling rate is known (auto-detected or supplied via `?sfreq=`). Disable per-request with `?mne=false`. If `mne` isn't installed, this block is simply omitted (`info["mne"]["available"] = False`) rather than erroring — same defensive pattern used for the optional MongoDB layer.

## The `display.py` CLI helper

A terminal client (built with [`rich`](https://github.com/Textualize/rich)) for uploading a file and pretty-printing the parsed result — handy for quickly sanity-checking the API without curl. As shipped, it has a hardcoded `SERVER_URL` and `FILE_PATH` at the top of the file meant for local testing:

```python
SERVER_URL = "https://eeg-a37i.onrender.com"
FILE_PATH  = r"C:\path\to\your\file.edf"
```

If you're reusing this script, edit those two constants (or wire them up to `argparse`/CLI flags) to point at your own server URL and test file before running:

```bash
pip install rich requests
python display.py
```

## Known limitations / roadmap

By design, not yet implemented:

- **No authentication** — every endpoint, including deletes, is open to anyone with the URL. Don't put sensitive/real patient data on a public deployment of this as-is.
- **No persistent local storage of file bytes by default** — on Render's free tier the filesystem is ephemeral, so file bytes live in GridFS (durable) but the local `uploads/` folder copy won't survive a redeploy/restart.
- **No chunked/streaming upload** — large recordings are uploaded in one request, capped at 500MB.
- **No actual signal analysis in the upload API** (filtering, ICA, band power, etc.) — that's confined to the `/live` dashboard.

## Contributing

Issues and PRs are welcome. If you're extending the `/live` dashboard or `detail.html`, keep the "not a clinical/diagnostic tool" framing intact anywhere band-power or topomap results are shown to a user.
