# EEG Dataset Upload Server

A minimal Flask REST API for accepting EEG dataset files and returning
**structural/debug information about them** (shape, channels, sampling
rate, header metadata, missing values, etc.). It does **not** perform
any signal analysis (no filtering, spectral analysis, artifact
detection) — that's intentionally out of scope for now.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

The server starts on `http://0.0.0.0:5000`.

## Supported file formats

| Extension | What's parsed |
|---|---|
| `.csv` / `.tsv` | shape, column/channel names, dtypes, missing values, auto-detected sampling rate (if a `time`/`timestamp` column exists), preview rows |
| `.edf` | EDF/EDF+ header — patient/recording id, start date/time, number of signals, per-channel labels, units, sampling rate, total duration. Parsed natively (no external EEG library required) |
| `.npy` | shape, dtype, number of elements, preview values |
| `.json` | detects array-of-records vs column-oriented structure, field names, lengths |

## Endpoints

### `GET /health`
Liveness check.
```bash
curl http://localhost:5000/health
```

### `POST /upload`
Upload a dataset file (`multipart/form-data`, field name `file`).
```bash
curl -F "file=@my_eeg_recording.edf" http://localhost:5000/upload
```
Returns a JSON record with `dataset_id`, file metadata, `parse_status`
(`ok` / `error`), and an `info` object containing the format-specific
debug details described above.

### `GET /datasets`
List all datasets uploaded in this server session (summary view).
```bash
curl http://localhost:5000/datasets
```

### `GET /datasets/<dataset_id>`
Full stored record (including the detailed `info` block) for one dataset.
```bash
curl http://localhost:5000/datasets/<dataset_id>
```

### `DELETE /datasets/<dataset_id>`
Remove a dataset's record and its file from disk.
```bash
curl -X DELETE http://localhost:5000/datasets/<dataset_id>
```

## Notes

- Uploaded files are saved under `uploads/`, prefixed with a UUID so
  filenames never collide.
- Dataset metadata is currently kept **in memory** (a Python dict) — it
  resets when the server restarts. Swap `DATASETS` for a real database
  (SQLite/Postgres) if you need persistence.
- Max upload size is capped at 500MB (`MAX_CONTENT_LENGTH` in `app.py`).
- All requests/responses are logged to stdout (and `server.log` if
  redirected) at DEBUG level for troubleshooting.
- This is a development server (Flask's built-in WSGI server). For
  production, run behind something like `gunicorn`:
  ```bash
  gunicorn -w 4 -b 0.0.0.0:5000 app:app
  ```

## Next steps (not implemented yet, by design)

- Actual signal analysis (filtering, ICA, band power, etc.)
- Authentication / API keys
- Persistent storage (database instead of in-memory dict)
- Chunked/streaming upload for very large recordings
