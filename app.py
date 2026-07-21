"""
EEG Dataset Upload Server + Live Monitoring Dashboard
=======================================================
A Flask app with two halves:

1. A REST API that accepts EEG dataset uploads and returns structural
   debug/info metadata about them (no signal analysis - purely ingesting,
   validating, and inspecting datasets). See the endpoint list below.

2. A live EEG monitoring dashboard at /live (ported from the original
   Streamlit app.py) that reads raw EEG-chunk documents from MongoDB
   (falling back to generated mock data), and renders a live-sweeping
   multichannel signal chart, per-band topographic maps, and a
   downloadable session report with band-power analysis. This half DOES
   perform signal analysis (band-pass filtering for band power, montage-
   based topomaps) - it is deliberately separate from the upload API above,
   which stays analysis-free.

Supported formats:
  - .csv / .tsv   (rows = samples, columns = channels, optional time column)
  - .edf          (European Data Format - parsed natively, no mne dependency
                    for the core structural parse)
  - .npy          (numpy array dump)
  - .json         (array-of-records or column-oriented EEG data)

MNE integration (optional, additive):
  Every parser's output may include an extra "mne" block built with MNE-Python.
  This is purely structural/metadata enrichment layered on top of the native
  parsers above - it never filters, transforms, or does spectral analysis on
  the signal. It reports things MNE is good at summarizing for free once a
  Raw object exists:
    - channel count/names + a lightweight EEG/EOG/ECG/EMG/stim type guess
    - sfreq, highpass/lowpass as recorded in the file header
    - measurement date, bad channel list
    - 10-20 standard montage match (which channel labels MNE recognizes)
    - annotations and events derived from annotations (mne.events_from_annotations)
  For .edf this uses mne.io.read_raw_edf(..., preload=False) directly. For
  .csv/.tsv/.npy (which have no native montage/annotation concept) this is
  best-effort: it only runs when a sampling rate is known (detected or
  supplied via ?sfreq=) and builds an mne.io.RawArray from the data.
  Disable per-request with ?mne=false. If the `mne` package isn't installed,
  this block is simply omitted (info["mne"]["available"] = False).

Run:
    python app.py
    # Server starts on http://0.0.0.0:0

Endpoints:
    GET    /health                        -> service liveness check
    GET    /stats                         -> storage/dataset aggregate stats
    POST   /upload                        -> upload a dataset file, returns parsed info
    GET    /datasets                      -> list datasets (search/filter/sort/paginate)
    GET    /datasets/<dataset_id>         -> full info for one dataset
    GET    /datasets/<dataset_id>/view    -> HTML detail page for one dataset
    GET    /datasets/<dataset_id>/channel/<idx> -> signal data for channel idx (EDF only)
    PATCH  /datasets/<dataset_id>         -> rename a dataset's display filename
    DELETE /datasets/<dataset_id>         -> remove a dataset record + file
    DELETE /datasets                      -> bulk delete (?ids=a,b,c or ?confirm=true for all)
    GET    /datasets/<dataset_id>/download -> download the original uploaded file
    POST   /datasets/<dataset_id>/reparse  -> re-run parsing with new query-param opts

    GET    /live                           -> live EEG monitoring dashboard (HTML page)
    GET    /live/report.txt                -> download the session report as text
    GET    /live/api/topomap/<band>.png    -> live topographic-map image for one band
                                               (band = Delta|Theta|Alpha|Beta|Gamma|Broadband)

    /live query params (all optional, combinable):
      ?source=auto|mongo|mock   data source (default: auto)
      ?n_channels=64            channel count for mock data (4-256, default 64)
      ?sfreq=160                sampling rate (Hz) for mock data (32-1024, default 160)
      ?regions=Frontal,Central  comma-separated brain regions to display (default: all)
      ?window_sec=6             sweep/analysis window in seconds (2-15, default 6)
      ?topo_refresh=1.0         topomap refresh interval in seconds (0.5-3.0, default 1.0)

    /datasets query params (all optional, combinable):
      ?search=name       case-insensitive filename substring match
      ?format=edf        filter by extension (csv/tsv/edf/npy/json)
      ?status=ok|error   filter by parse_status
      ?sort=uploaded_at|file_size_bytes|original_filename  (default: uploaded_at)
      ?order=asc|desc    (default: desc)
      ?limit=20&offset=0 pagination
"""

import io
import json
import logging
import os
import string
import struct
import time
import traceback
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request, send_file, render_template, abort, Response
from werkzeug.utils import secure_filename

try:
    import mne
    mne.set_log_level("ERROR")
    MNE_AVAILABLE = True
except ImportError:
    mne = None
    MNE_AVAILABLE = False

try:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
    import gridfs
    PYMONGO_AVAILABLE = True
except ImportError:
    MongoClient = None
    PyMongoError = Exception
    gridfs = None
    PYMONGO_AVAILABLE = False

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXTENSIONS = {"csv", "tsv", "edf", "npy", "json"}
MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500 MB cap on upload size

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# --------------------------------------------------------------------------
# Custom Jinja2 filter for datetime formatting
# --------------------------------------------------------------------------

@app.template_filter('datetime_format')
def datetime_format(value):
    if value is None:
        return ''
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.strftime('%Y-%m-%d %H:%M')
    except:
        return value

@app.template_filter('format_channel_types')
def format_channel_types(d):
    """Convert a dict like {'eeg': 32, 'eog': 2} to 'eeg: 32, eog: 2'."""
    if not d:
        return ""
    return ", ".join(f"{k}: {v}" for k, v in d.items())

@app.template_filter('format_event_id_map')
def format_event_id_map(d):
    """Convert event_id_map dict to 'label1=1, label2=2'."""
    if not d:
        return ""
    return ", ".join(f"{k}={v}" for k, v in d.items())

# --------------------------------------------------------------------------
# Logging - verbose debug logging as requested
# --------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("watchdog").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.INFO)
logger = logging.getLogger("eeg-server")

# --------------------------------------------------------------------------
# In-memory dataset registry (fast path, cleared on restart)
# --------------------------------------------------------------------------

DATASETS = {}  # dataset_id -> metadata dict

# --------------------------------------------------------------------------
# MongoDB persistence (optional, additive - same defensive pattern as MNE)
# Configure with env vars:
#   MONGO_URI  (default: mongodb://localhost:27017/)
#   MONGO_DB   (default: eeg_dataset_server)
# If pymongo isn't installed or no server is reachable, the app falls back
# to in-memory-only storage; nothing about the API surface changes.
# --------------------------------------------------------------------------

MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://test:EUk6fKxazi4E2JEG@cluster0.1m2rwpo.mongodb.net/?appName=Cluster0")
MONGO_DB_NAME = os.environ.get("MONGO_DB", "eeg_dataset_server")
MONGO_COLLECTION = "datasets"

mongo_client = None
mongo_collection = None
mongo_fs = None  # GridFS bucket - stores the raw uploaded file bytes durably
MONGO_CONNECTED = False

if PYMONGO_AVAILABLE:
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        mongo_client.admin.command("ping")  # fail fast if unreachable
        mongo_db = mongo_client[MONGO_DB_NAME]
        mongo_collection = mongo_db[MONGO_COLLECTION]
        mongo_fs = gridfs.GridFS(mongo_db, collection="dataset_files")
        MONGO_CONNECTED = True
    except Exception as e:
        logging.getLogger("eeg-server").warning("MongoDB unavailable, falling back to in-memory only: %s", e)
        mongo_client = None
        mongo_collection = None
        mongo_fs = None
        MONGO_CONNECTED = False


def mongo_save(record):
    """Upsert a dataset record into MongoDB, keyed by dataset_id (as _id)."""
    if not MONGO_CONNECTED:
        return False
    try:
        doc = dict(record)
        doc["_id"] = doc["dataset_id"]
        mongo_collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        return True
    except PyMongoError as e:
        logger.error("MongoDB save failed for dataset %s: %s", record.get("dataset_id"), e)
        return False


def mongo_delete(dataset_id):
    if not MONGO_CONNECTED:
        return False
    try:
        mongo_collection.delete_one({"_id": dataset_id})
        return True
    except PyMongoError as e:
        logger.error("MongoDB delete failed for dataset %s: %s", dataset_id, e)
        return False


def gridfs_save(dataset_id, filepath, filename):
    """Store the raw uploaded file's bytes in GridFS, keyed by dataset_id, so
    the data survives server restarts / redeploys (the local disk under
    UPLOAD_FOLDER is ephemeral on most hosts, e.g. Render). Returns the
    GridFS file id (as a str) on success, or None if unavailable/failed."""
    if not MONGO_CONNECTED or mongo_fs is None:
        return None
    try:
        with open(filepath, "rb") as fh:
            file_id = mongo_fs.put(fh, filename=filename, dataset_id=dataset_id)
        return str(file_id)
    except Exception as e:
        logger.error("GridFS save failed for dataset %s: %s", dataset_id, e)
        return None


def gridfs_delete(gridfs_file_id):
    if not MONGO_CONNECTED or mongo_fs is None or not gridfs_file_id:
        return False
    try:
        from bson import ObjectId
        mongo_fs.delete(ObjectId(gridfs_file_id))
        return True
    except Exception as e:
        logger.error("GridFS delete failed for file %s: %s", gridfs_file_id, e)
        return False


def gridfs_restore_to_disk(gridfs_file_id, filepath):
    """Write the GridFS-stored bytes back out to filepath if they aren't
    already there. Used at startup (and lazily on access) to rehydrate files
    after a restart wiped the ephemeral local disk."""
    if not MONGO_CONNECTED or mongo_fs is None or not gridfs_file_id:
        return False
    if os.path.exists(filepath):
        return True
    try:
        from bson import ObjectId
        grid_out = mongo_fs.get(ObjectId(gridfs_file_id))
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "wb") as fh:
            fh.write(grid_out.read())
        return True
    except Exception as e:
        logger.error("GridFS restore-to-disk failed for file %s: %s", gridfs_file_id, e)
        return False


def mongo_load_all():
    """Load all previously persisted datasets back into the in-memory cache
    (called once at startup so restarts don't lose data), and rehydrate each
    dataset's raw file from GridFS onto local disk since UPLOAD_FOLDER itself
    does not survive a restart on most hosts."""
    if not MONGO_CONNECTED:
        return 0
    loaded = 0
    try:
        for doc in mongo_collection.find({}):
            doc.pop("_id", None)
            DATASETS[doc["dataset_id"]] = doc
            loaded += 1
            gridfs_file_id = doc.get("gridfs_file_id")
            if gridfs_file_id:
                filepath = os.path.join(UPLOAD_FOLDER, doc["stored_filename"])
                gridfs_restore_to_disk(gridfs_file_id, filepath)
    except PyMongoError as e:
        logger.error("MongoDB load-all failed: %s", e)
    return loaded


if MONGO_CONNECTED:
    _restored = mongo_load_all()
    logger.info("MongoDB connected (%s) - restored %d dataset(s) into memory", MONGO_DB_NAME, _restored)
else:
    logger.info("MongoDB not connected - running in-memory only")


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def build_parse_opts(args):
    """
    Translate request query params into the opts dict the parsers/MNE
    enrichment understand:
      ?mne=false                          -> disable MNE enrichment entirely
      ?sfreq=256                          -> override/supply a sampling rate
      ?npy_orientation=channels_x_samples -> required for .npy MNE enrichment
      ?npy_channel_names=Fp1,Fp2,Cz       -> optional channel names for .npy
    """
    enable_mne = args.get("mne", "true").strip().lower() not in ("false", "0", "no")

    sfreq_override = None
    raw_sfreq = args.get("sfreq")
    if raw_sfreq:
        try:
            sfreq_override = float(raw_sfreq)
        except ValueError:
            logger.warning("Ignoring invalid ?sfreq= value: %r", raw_sfreq)

    npy_channel_names = None
    raw_names = args.get("npy_channel_names")
    if raw_names:
        npy_channel_names = [n.strip() for n in raw_names.split(",") if n.strip()]

    return {
        "enable_mne": enable_mne and MNE_AVAILABLE,
        "sfreq_override": sfreq_override,
        "npy_orientation": args.get("npy_orientation"),
        "npy_channel_names": npy_channel_names,
    }


# --------------------------------------------------------------------------
# Format-specific parsers
# Each parser returns a dict of structural info. They must NOT do any
# signal analysis (no filtering, no spectral stuff, no artifact detection).
# --------------------------------------------------------------------------


def parse_csv(filepath, ext, opts=None):
    opts = opts or {}
    sep = "\t" if ext == "tsv" else ","
    df = pd.read_csv(filepath, sep=sep)

    n_rows, n_cols = df.shape
    dtypes = {col: str(dt) for col, dt in df.dtypes.items()}
    missing = df.isna().sum().to_dict()
    missing = {k: int(v) for k, v in missing.items()}

    time_col = None
    for candidate in ["time", "timestamp", "Time", "Timestamp", "t"]:
        if candidate in df.columns:
            time_col = candidate
            break

    estimated_sfreq = None
    duration_seconds = None
    if time_col is not None and n_rows > 1:
        try:
            t = pd.to_numeric(df[time_col], errors="coerce").dropna()
            if len(t) > 1:
                diffs = t.diff().dropna()
                median_dt = float(diffs.median())
                if median_dt > 0:
                    estimated_sfreq = round(1.0 / median_dt, 4)
                    duration_seconds = round(float(t.iloc[-1] - t.iloc[0]), 4)
        except Exception as e:
            logger.debug("Could not estimate sampling rate from time column: %s", e)

    channels = [c for c in df.columns if c != time_col]

    preview = df.head(5).to_dict(orient="records")

    result = {
        "format": "csv" if ext == "csv" else "tsv",
        "n_samples": int(n_rows),
        "n_columns": int(n_cols),
        "channels": channels,
        "n_channels": len(channels),
        "time_column_detected": time_col,
        "estimated_sampling_rate_hz": estimated_sfreq,
        "estimated_duration_seconds": duration_seconds,
        "dtypes": dtypes,
        "missing_values_per_column": missing,
        "preview_first_5_rows": preview,
    }

    if opts.get("enable_mne", True) and channels:
        sfreq = opts.get("sfreq_override") or estimated_sfreq
        if sfreq:
            numeric = df[channels].apply(pd.to_numeric, errors="coerce")
            usable_channels = [c for c in channels if numeric[c].notna().any()]
            if usable_channels:
                data = numeric[usable_channels].fillna(0.0).to_numpy(dtype=np.float64).T
                result["mne"] = mne_enrich_array(data, usable_channels, sfreq)
            else:
                result["mne"] = {"available": False, "reason": "no numeric channel columns to build a Raw from"}
        else:
            result["mne"] = {
                "available": False,
                "reason": "no sampling rate detected; retry with ?sfreq=<Hz> to enable MNE enrichment",
            }

    return result


def parse_npy(filepath, opts=None):
    opts = opts or {}
    arr = np.load(filepath, allow_pickle=False)
    info = {
        "format": "npy",
        "shape": list(arr.shape),
        "ndim": arr.ndim,
        "dtype": str(arr.dtype),
        "size_elements": int(arr.size),
    }
    if arr.ndim == 2:
        rows, cols = arr.shape
        info["interpretation_note"] = (
            "2D array detected. Could be (channels, samples) or "
            "(samples, channels) - shape is reported as-is; no orientation "
            "assumption is made."
        )
        info["dim0_size"] = int(rows)
        info["dim1_size"] = int(cols)
    info["preview_first_values"] = np.asarray(arr).flatten()[:10].tolist()

    if opts.get("enable_mne", True):
        sfreq = opts.get("sfreq_override")
        orientation = opts.get("npy_orientation")
        if arr.ndim != 2:
            info["mne"] = {"available": False, "reason": "MNE enrichment needs a 2D (channels x samples) array"}
        elif not sfreq:
            info["mne"] = {
                "available": False,
                "reason": "shape is ambiguous and .npy has no header; retry with "
                           "?sfreq=<Hz>&npy_orientation=channels_x_samples|samples_x_channels",
            }
        else:
            data = arr if orientation == "channels_x_samples" else arr.T if orientation == "samples_x_channels" else None
            if data is None:
                info["mne"] = {
                    "available": False,
                    "reason": "sfreq given but npy_orientation missing; pass "
                              "npy_orientation=channels_x_samples|samples_x_channels",
                }
            else:
                ch_names = opts.get("npy_channel_names") or [f"CH{i+1:03d}" for i in range(data.shape[0])]
                info["mne"] = mne_enrich_array(data, ch_names, sfreq)

    return info


def parse_json_file(filepath, opts=None):
    opts = opts or {}
    with open(filepath, "r") as f:
        data = json.load(f)

    info = {"format": "json"}

    if isinstance(data, list):
        info["top_level_type"] = "array"
        info["n_records"] = len(data)
        if len(data) > 0 and isinstance(data[0], dict):
            keys = set()
            for row in data[:50]:
                if isinstance(row, dict):
                    keys.update(row.keys())
            info["detected_fields"] = sorted(keys)
        info["preview_first_3_records"] = data[:3]

    elif isinstance(data, dict):
        info["top_level_type"] = "object"
        info["top_level_keys"] = list(data.keys())
        array_like_keys = {}
        scalar_keys = {}
        for k, v in data.items():
            if isinstance(v, list):
                array_like_keys[k] = len(v)
            else:
                scalar_keys[k] = v
        if array_like_keys:
            info["array_fields_with_length"] = array_like_keys
        if scalar_keys:
            info["scalar_fields"] = scalar_keys

        if opts.get("enable_mne", True) and array_like_keys:
            sfreq = (
                opts.get("sfreq_override")
                or scalar_keys.get("sfreq")
                or scalar_keys.get("sampling_rate")
                or scalar_keys.get("fs")
            )
            lengths = set(array_like_keys.values())
            if not sfreq:
                info["mne"] = {
                    "available": False,
                    "reason": "no sfreq/sampling_rate/fs field found; retry with ?sfreq=<Hz>",
                }
            elif len(lengths) != 1:
                info["mne"] = {
                    "available": False,
                    "reason": "array fields have mismatched lengths, can't treat them as channels of one Raw",
                }
            else:
                ch_names = list(array_like_keys.keys())
                channel_arrays = [np.asarray(data[name], dtype=np.float64) for name in ch_names]
                stacked = np.vstack(channel_arrays)
                info["mne"] = mne_enrich_array(stacked, ch_names, sfreq)
    else:
        info["top_level_type"] = type(data).__name__

    return info


def parse_edf(filepath, opts=None):
    with open(filepath, "rb") as f:
        header = f.read(256)
        if len(header) < 256:
            raise ValueError("File too small to be a valid EDF header")

        version = header[0:8].decode("ascii", errors="replace").strip()
        patient_id = header[8:88].decode("ascii", errors="replace").strip()
        recording_id = header[88:168].decode("ascii", errors="replace").strip()
        start_date = header[168:176].decode("ascii", errors="replace").strip()
        start_time = header[176:184].decode("ascii", errors="replace").strip()
        n_header_bytes = int(header[184:192].decode("ascii", errors="replace").strip())
        n_data_records = int(header[236:244].decode("ascii", errors="replace").strip())
        record_duration = float(header[244:252].decode("ascii", errors="replace").strip())
        n_signals = int(header[252:256].decode("ascii", errors="replace").strip())

        signal_header_size = 256 * n_signals
        sig_header = f.read(signal_header_size)
        if len(sig_header) < signal_header_size:
            raise ValueError("EDF signal header truncated/corrupt")

        field_widths = [
            ("label", 16),
            ("transducer", 80),
            ("physical_dimension", 8),
            ("physical_min", 8),
            ("physical_max", 8),
            ("digital_min", 8),
            ("digital_max", 8),
            ("prefiltering", 80),
            ("samples_per_record", 8),
            ("reserved", 32),
        ]

        def field(block, width, n, offset):
            return [
                block[offset + i * width:offset + (i + 1) * width]
                .decode("ascii", errors="replace").strip()
                for i in range(n)
            ], offset + width * n

        parsed_fields = {}
        cursor = 0
        for name, width in field_widths:
            values, cursor = field(sig_header, width, n_signals, cursor)
            parsed_fields[name] = values

        labels = parsed_fields["label"]
        transducers = parsed_fields["transducer"]
        phys_dims = parsed_fields["physical_dimension"]
        phys_mins = parsed_fields["physical_min"]
        phys_maxs = parsed_fields["physical_max"]
        dig_mins = parsed_fields["digital_min"]
        dig_maxs = parsed_fields["digital_max"]
        prefiltering = parsed_fields["prefiltering"]
        samples_per_record = parsed_fields["samples_per_record"]

        samples_per_record_int = [int(s) for s in samples_per_record]
        sfreq_per_channel = [
            round(s / record_duration, 4) if record_duration > 0 else None
            for s in samples_per_record_int
        ]
        total_duration = round(n_data_records * record_duration, 4)

        channels = []
        for i in range(n_signals):
            channels.append({
                "label": labels[i],
                "transducer": transducers[i],
                "physical_dimension": phys_dims[i],
                "physical_min": phys_mins[i],
                "physical_max": phys_maxs[i],
                "digital_min": dig_mins[i],
                "digital_max": dig_maxs[i],
                "prefiltering": prefiltering[i],
                "samples_per_record": samples_per_record_int[i],
                "sampling_rate_hz": sfreq_per_channel[i],
            })

        result = {
            "format": "edf",
            "edf_version": version,
            "patient_id": patient_id,
            "recording_id": recording_id,
            "start_date": start_date,
            "start_time": start_time,
            "header_bytes": n_header_bytes,
            "n_data_records": n_data_records,
            "record_duration_seconds": record_duration,
            "total_duration_seconds": total_duration,
            "n_signals": n_signals,
            "channels": channels,
            "channel_labels": labels,
        }

    opts = opts or {}
    if opts.get("enable_mne", True):
        result["mne"] = mne_enrich_edf(filepath)

    return result


# --------------------------------------------------------------------------
# MNE enrichment (optional, additive)
# --------------------------------------------------------------------------

_STANDARD_1020_NAMES = None


def _get_standard_1020_names():
    global _STANDARD_1020_NAMES
    if _STANDARD_1020_NAMES is None:
        montage = mne.channels.make_standard_montage("standard_1020")
        _STANDARD_1020_NAMES = {n.upper() for n in montage.ch_names}
    return _STANDARD_1020_NAMES


def guess_channel_types(ch_names):
    types = {}
    for name in ch_names:
        n = name.strip().rstrip(".").upper()
        if n in ("EDF ANNOTATIONS", "ANNOTATIONS", "STATUS", "STI", "STI014", "EVENT", "MARKER"):
            types[name] = "stim"
        elif n.startswith("EOG"):
            types[name] = "eog"
        elif n.startswith("ECG") or n.startswith("EKG"):
            types[name] = "ecg"
        elif n.startswith("EMG"):
            types[name] = "emg"
        else:
            types[name] = "eeg"
    return types


def summarize_montage(ch_names):
    standard_names = _get_standard_1020_names()
    matched = [raw for raw in ch_names if raw.strip().rstrip(".").upper() in standard_names]
    unmatched = [raw for raw in ch_names if raw not in matched]
    pct = round(100.0 * len(matched) / len(ch_names), 1) if ch_names else 0.0
    return {
        "standard_1020_match_pct": pct,
        "matched_channels": matched,
        "unmatched_channels": unmatched,
    }


def summarize_annotations(raw):
    ann = raw.annotations
    if len(ann) == 0:
        return {
            "count": 0,
            "unique_descriptions": [],
            "first_5": [],
            "events_from_annotations": {"n_events": 0, "event_id_map": {}},
        }

    descriptions = list(ann.description)
    out = {
        "count": len(ann),
        "unique_descriptions": sorted(set(descriptions))[:20],
        "first_5": [
            {"onset_s": round(float(o), 4), "duration_s": round(float(d), 4), "description": str(desc)}
            for o, d, desc in list(zip(ann.onset, ann.duration, ann.description))[:5]
        ],
    }
    try:
        events, event_id = mne.events_from_annotations(raw, verbose="ERROR")
        out["events_from_annotations"] = {
            "n_events": int(events.shape[0]),
            "event_id_map": {str(k): int(v) for k, v in event_id.items()},
        }
    except Exception as e:
        out["events_from_annotations"] = {"error": str(e)}
    return out


def build_mne_summary(raw):
    info = raw.info
    ch_names = raw.ch_names
    type_counts = {}
    for t in guess_channel_types(ch_names).values():
        type_counts[t] = type_counts.get(t, 0) + 1

    meas_date = info.get("meas_date")
    sfreq = float(info["sfreq"]) if info.get("sfreq") else None

    return {
        "available": True,
        "mne_version": mne.__version__,
        "n_channels": len(ch_names),
        "ch_names": ch_names,
        "ch_types_guess_summary": type_counts,
        "sfreq_hz": sfreq,
        "highpass_hz": float(info["highpass"]) if info.get("highpass") is not None else None,
        "lowpass_hz": float(info["lowpass"]) if info.get("lowpass") is not None else None,
        "n_times": int(raw.n_times),
        "duration_seconds": round(raw.n_times / sfreq, 4) if sfreq else None,
        "measurement_date": meas_date.isoformat() if meas_date is not None else None,
        "bads": list(info.get("bads", [])),
        "montage": summarize_montage(ch_names),
        "annotations": summarize_annotations(raw),
    }


def mne_enrich_edf(filepath):
    if not MNE_AVAILABLE:
        return {"available": False, "reason": "mne is not installed"}
    try:
        raw = mne.io.read_raw_edf(filepath, preload=False, verbose="ERROR")
        return build_mne_summary(raw)
    except Exception as e:
        logger.debug("MNE EDF enrichment failed: %s", e)
        return {"available": False, "error": str(e)}


def mne_enrich_array(data_channels_x_samples, ch_names, sfreq):
    if not MNE_AVAILABLE:
        return {"available": False, "reason": "mne is not installed"}
    if not sfreq or sfreq <= 0:
        return {"available": False, "reason": "no known sampling rate (detect one or pass ?sfreq=)"}
    try:
        ch_types = list(guess_channel_types(ch_names).values())
        info = mne.create_info(ch_names=list(ch_names), sfreq=float(sfreq), ch_types=ch_types)
        raw = mne.io.RawArray(
            np.asarray(data_channels_x_samples, dtype=np.float64), info, verbose="ERROR"
        )
        return build_mne_summary(raw)
    except Exception as e:
        logger.debug("MNE array enrichment failed: %s", e)
        return {"available": False, "error": str(e)}


PARSERS = {
    "csv": lambda fp, opts: parse_csv(fp, "csv", opts),
    "tsv": lambda fp, opts: parse_csv(fp, "tsv", opts),
    "npy": lambda fp, opts: parse_npy(fp, opts),
    "json": lambda fp, opts: parse_json_file(fp, opts),
    "edf": lambda fp, opts: parse_edf(fp, opts),
}


# ==========================================================================
# LIVE EEG MONITORING DASHBOARD  (merged in from the Streamlit app.py)
# ==========================================================================

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    plt = None
    MATPLOTLIB_AVAILABLE = False

try:
    from scipy.signal import decimate as _scipy_decimate
    SCIPY_AVAILABLE = True
except ImportError:
    _scipy_decimate = None
    SCIPY_AVAILABLE = False

import threading
from functools import lru_cache
from io import BytesIO

LIVE_BANDS = {"Delta": (0.5, 4), "Theta": (4, 8), "Alpha": (8, 13), "Beta": (13, 30), "Gamma": (30, 45)}
LIVE_BAND_ORDER = ["Delta", "Theta", "Alpha", "Beta", "Gamma", "Broadband"]
LIVE_BAND_DESCRIPTIONS = {
    "Delta": "slow, high-amplitude activity generally associated with deep rest or drowsiness",
    "Theta": "activity often linked to a light, meditative, or drowsy state",
    "Alpha": "activity generally associated with a calm, relaxed, eyes-closed-type state",
    "Beta": "activity often associated with active thinking, alertness, or mild tension",
    "Gamma": "activity sometimes linked to high-level cognitive processing or focus",
}
LIVE_CANVAS_POINT_BUDGET = 3000
LIVE_REGION_ORDER = ["Frontal", "Central", "Temporal", "Parietal", "Occipital", "Other"]

LIVE_CHANNEL_KEYS = ["channels", "channel_names", "ch_names"]
LIVE_SFREQ_KEYS = ["sampling_rate", "sfreq", "fs", "sample_rate"]
LIVE_DATA_KEYS = ["data", "eeg_data", "samples", "values"]
LIVE_ORDER_KEYS = ["chunk_index", "sequence", "sequence_index", "part", "part_number", "order"]

_live_docs_cache = {"docs": None, "ts": 0.0}
_live_cache_lock = threading.Lock()

LIVE_STATS_LOCK = threading.Lock()
LIVE_STATS = {"session_start": time.time(), "refresh_count": 0, "regions_seen": set()}


def _live_first_present(doc, candidate_keys):
    for k in candidate_keys:
        if k in doc and doc[k] is not None:
            return doc[k]
    return None


def fetch_live_documents(ttl=30):
    if not MONGO_CONNECTED:
        return None
    with _live_cache_lock:
        if _live_docs_cache["docs"] is not None and (time.time() - _live_docs_cache["ts"]) < ttl:
            return _live_docs_cache["docs"]
    docs = None
    try:
        query = {"$or": [{k: {"$exists": True}} for k in LIVE_DATA_KEYS]}
        docs = list(mongo_collection.find(query))
    except PyMongoError as e:
        logger.error("Live dashboard: MongoDB fetch failed: %s", e)
    with _live_cache_lock:
        _live_docs_cache["docs"] = docs if docs else None
        _live_docs_cache["ts"] = time.time()
    return _live_docs_cache["docs"]


def build_live_raw_from_documents(docs):
    if not docs:
        return None, None, None, 0

    def sort_key(d):
        for k in LIVE_ORDER_KEYS:
            if k in d:
                return d[k]
        return d.get("_id")

    docs = sorted(docs, key=sort_key)

    ch_names, sfreq, chunks = None, None, []
    for d in docs:
        names = _live_first_present(d, LIVE_CHANNEL_KEYS)
        fs = _live_first_present(d, LIVE_SFREQ_KEYS)
        raw_vals = _live_first_present(d, LIVE_DATA_KEYS)
        if raw_vals is None:
            continue
        arr = np.array(raw_vals, dtype=float)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        if names and arr.shape[0] != len(names) and arr.ndim == 2 and arr.shape[1] == len(names):
            arr = arr.T
        chunks.append(arr)
        ch_names = ch_names or names
        sfreq = sfreq or fs

    if not chunks or ch_names is None or sfreq is None:
        return None, None, None, 0

    try:
        full = np.concatenate(chunks, axis=1)
    except ValueError:
        min_ch = min(c.shape[0] for c in chunks)
        full = np.concatenate([c[:min_ch] for c in chunks], axis=1)
        ch_names = ch_names[:min_ch]

    return full, list(ch_names), float(sfreq), len(chunks)


@lru_cache(maxsize=4)
def get_live_fallback_pool():
    return tuple(mne.channels.make_standard_montage("standard_1005").ch_names)


def pick_live_channel_names(n_channels):
    exact = {16: "biosemi16", 32: "biosemi32", 64: "biosemi64"}
    if n_channels in exact:
        return list(mne.channels.make_standard_montage(exact[n_channels]).ch_names)
    pool = list(get_live_fallback_pool())
    if n_channels <= len(pool):
        return pool[:n_channels]
    return pool + [f"CH{i}" for i in range(len(pool), n_channels)]


def classify_live_region(ch):
    c = ch.upper()
    if c.startswith("FP") or c.startswith("AF"):
        return "Frontal"
    if c.startswith("FC"):
        return "Central"
    if c.startswith("FT"):
        return "Temporal"
    if c.startswith("F"):
        return "Frontal"
    if c.startswith("CP"):
        return "Parietal"
    if c.startswith("TP"):
        return "Temporal"
    if c.startswith("C"):
        return "Central"
    if c.startswith("PO"):
        return "Occipital"
    if c.startswith("P"):
        return "Parietal"
    if c.startswith("T"):
        return "Temporal"
    if c.startswith("O") or c == "IZ":
        return "Occipital"
    return "Other"


@lru_cache(maxsize=4)
def get_live_montage():
    return mne.channels.make_standard_montage("standard_1005")


@lru_cache(maxsize=16)
def build_live_info(ch_names, sfreq):
    info = mne.create_info(ch_names=list(ch_names), sfreq=sfreq, ch_types="eeg")
    info.set_montage(get_live_montage(), on_missing="ignore")
    return info


@lru_cache(maxsize=16)
def generate_mock_eeg(n_channels, sfreq, duration_sec=60):
    ch_names = pick_live_channel_names(n_channels)
    n_samples = duration_sec * sfreq
    t = np.arange(n_samples) / sfreq
    rng = np.random.default_rng(42)
    data_uv = np.zeros((n_channels, n_samples))
    for i in range(n_channels):
        sig = np.zeros(n_samples)
        for freq, base_amp in [(2, 8), (6, 5), (10, 6), (20, 3), (40, 2)]:
            amp = base_amp * rng.uniform(0.1, 1.5)
            sig += amp * np.sin(2 * np.pi * freq * t + rng.uniform(0, 2 * np.pi))
        pink = np.cumsum(rng.normal(0, 1, n_samples))
        pink = (pink - pink.mean()) / pink.std() * 4
        data_uv[i] = sig + pink
    return data_uv, tuple(ch_names), float(sfreq)


def downsample_for_display(data_uv, target_points=LIVE_CANVAS_POINT_BUDGET):
    out = data_uv
    if SCIPY_AVAILABLE:
        while out.shape[1] > target_points * 2:
            out = _scipy_decimate(out, 2, axis=1, zero_phase=True)
    if out.shape[1] > target_points:
        idx = np.linspace(0, out.shape[1] - 1, target_points).astype(int)
        out = out[:, idx]
    return out


def live_band_power(chan_data_uv, sfreq, lo, hi):
    min_sec = max(2.0, 3.0 / lo)
    if chan_data_uv.shape[1] < min_sec * sfreq:
        return None
    filtered = mne.filter.filter_data(chan_data_uv, sfreq, lo, hi, verbose="ERROR")
    return np.mean(filtered ** 2, axis=1)


def resolve_live_dataset(args):
    source = args.get("source", "auto")
    try:
        n_channels = max(4, min(256, int(args.get("n_channels", 64))))
    except (TypeError, ValueError):
        n_channels = 64
    try:
        sfreq_mock = max(32, min(1024, int(args.get("sfreq", 160))))
    except (TypeError, ValueError):
        sfreq_mock = 160

    docs = fetch_live_documents()
    mongo_data, mongo_ch_names, mongo_sfreq, n_files = build_live_raw_from_documents(docs)
    mongo_available = mongo_data is not None

    use_mongo = mongo_available and source != "mock"
    if source == "mongo" and not mongo_available:
        use_mongo = False

    meta = {
        "n_files": n_files,
        "mongo_available": mongo_available,
        "source": "mongo" if use_mongo else "mock",
        "n_channels_requested": n_channels,
        "sfreq_requested": sfreq_mock,
    }

    if use_mongo:
        raw_data = mongo_data * 1e-6 if np.nanmax(np.abs(mongo_data)) > 1 else mongo_data
        ch_names = list(mongo_ch_names)
        sfreq = float(mongo_sfreq)
    else:
        mock_uv, ch_names_t, sfreq = generate_mock_eeg(n_channels, sfreq_mock)
        raw_data = mock_uv * 1e-6
        ch_names = list(ch_names_t)

    return raw_data, ch_names, sfreq, meta


def live_window_params(args):
    try:
        window_sec = max(2, min(15, int(float(args.get("window_sec", 6)))))
    except (TypeError, ValueError):
        window_sec = 6
    try:
        topo_refresh = max(0.5, min(3.0, float(args.get("topo_refresh", 1.0))))
    except (TypeError, ValueError):
        topo_refresh = 1.0
    return window_sec, topo_refresh


def live_selected_channels(ch_names, regions_param):
    region_map = {}
    for ch in ch_names:
        region_map.setdefault(classify_live_region(ch), []).append(ch)
    region_options = [r for r in LIVE_REGION_ORDER if r in region_map]
    if regions_param is None:
        region_choice = list(region_options)
    else:
        requested = [r.strip() for r in regions_param.split(",") if r.strip()]
        region_choice = [r for r in requested if r in region_options] or list(region_options)
    selected = [ch for ch in ch_names if classify_live_region(ch) in region_choice] or ch_names
    return region_options, region_choice, selected


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    logger.debug("Health check pinged")
    return jsonify({
        "status": "ok",
        "service": "eeg-dataset-server",
        "time": now_iso(),
        "datasets_in_memory": len(DATASETS),
        "mongo_connected": MONGO_CONNECTED,
    }), 200


@app.route("/stats", methods=["GET"])
def stats():
    logger.debug("Stats requested")
    total_bytes = sum(ds["file_size_bytes"] for ds in DATASETS.values())
    by_format = {}
    by_status = {"ok": 0, "error": 0}
    for ds in DATASETS.values():
        fmt = ds["extension"]
        by_format[fmt] = by_format.get(fmt, 0) + 1
        by_status[ds["parse_status"]] = by_status.get(ds["parse_status"], 0) + 1
    return jsonify({
        "count": len(DATASETS),
        "total_size_bytes": total_bytes,
        "total_size_mb": round(total_bytes / (1024 * 1024), 2),
        "by_format": by_format,
        "by_status": by_status,
        "mongo_connected": MONGO_CONNECTED,
    }), 200


@app.route("/upload", methods=["POST"])
def upload():
    start_time = time.time()
    logger.info("Incoming upload request from %s", request.remote_addr)

    if "file" not in request.files:
        logger.warning("Upload rejected: no 'file' part in request")
        return jsonify({
            "error": "No file part in request. Send multipart/form-data with field name 'file'."
        }), 400

    f = request.files["file"]

    if f.filename == "":
        logger.warning("Upload rejected: empty filename")
        return jsonify({"error": "No file selected"}), 400

    if not allowed_file(f.filename):
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else "unknown"
        logger.warning("Upload rejected: unsupported extension '%s'", ext)
        return jsonify({
            "error": f"Unsupported file extension '{ext}'. "
                     f"Allowed: {sorted(ALLOWED_EXTENSIONS)}"
        }), 400

    dataset_id = str(uuid.uuid4())
    original_filename = secure_filename(f.filename)
    ext = original_filename.rsplit(".", 1)[1].lower()
    stored_filename = f"{dataset_id}_{original_filename}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], stored_filename)

    try:
        f.save(filepath)
        file_size = os.path.getsize(filepath)
        logger.debug("Saved upload to %s (%d bytes)", filepath, file_size)
    except Exception as e:
        logger.error("Failed to save uploaded file: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": f"Failed to save file: {str(e)}"}), 500

    parser = PARSERS.get(ext)
    parse_error = None
    parsed_info = {}
    opts = build_parse_opts(request.args)

    try:
        parsed_info = parser(filepath, opts)
    except Exception as e:
        parse_error = str(e)
        logger.error("Failed to parse '%s' as %s: %s\n%s",
                      original_filename, ext, e, traceback.format_exc())

    elapsed_ms = round((time.time() - start_time) * 1000, 2)

    gridfs_file_id = gridfs_save(dataset_id, filepath, original_filename)

    record = {
        "dataset_id": dataset_id,
        "original_filename": original_filename,
        "stored_filename": stored_filename,
        "extension": ext,
        "file_size_bytes": file_size,
        "uploaded_at": now_iso(),
        "parse_status": "error" if parse_error else "ok",
        "parse_error": parse_error,
        "processing_time_ms": elapsed_ms,
        "info": parsed_info,
        "gridfs_file_id": gridfs_file_id,
    }
    DATASETS[dataset_id] = record
    mongo_ok = mongo_save(record)
    record["persisted_to_db"] = mongo_ok
    record["file_persisted"] = gridfs_file_id is not None

    status_code = 200 if not parse_error else 422
    log_fn = logger.info if not parse_error else logger.warning
    log_fn("Upload %s: dataset_id=%s file=%s ext=%s size=%dB time=%sms status=%s db=%s",
           "succeeded" if not parse_error else "saved but failed to parse",
           dataset_id, original_filename, ext, file_size, elapsed_ms, record["parse_status"],
           "saved" if mongo_ok else "skipped/unavailable")

    return jsonify(record), status_code


def dataset_summary(ds):
    return {
        "dataset_id": ds["dataset_id"],
        "original_filename": ds["original_filename"],
        "extension": ds["extension"],
        "file_size_bytes": ds["file_size_bytes"],
        "uploaded_at": ds["uploaded_at"],
        "parse_status": ds["parse_status"],
    }


@app.route("/datasets", methods=["GET"])
def list_datasets():
    args = request.args
    items = list(DATASETS.values())

    search = args.get("search", "").strip().lower()
    if search:
        items = [d for d in items if search in d["original_filename"].lower()]

    fmt = args.get("format", "").strip().lower()
    if fmt:
        items = [d for d in items if d["extension"] == fmt]

    status = args.get("status", "").strip().lower()
    if status:
        items = [d for d in items if d["parse_status"] == status]

    sort_key = args.get("sort", "uploaded_at")
    if sort_key not in ("uploaded_at", "file_size_bytes", "original_filename"):
        sort_key = "uploaded_at"
    reverse = args.get("order", "desc").strip().lower() != "asc"
    items.sort(key=lambda d: d[sort_key], reverse=reverse)

    total_matched = len(items)

    try:
        offset = max(0, int(args.get("offset", 0)))
    except ValueError:
        offset = 0
    try:
        limit = int(args.get("limit", 0)) or None
    except ValueError:
        limit = None
    if limit is not None:
        items = items[offset:offset + limit]
    elif offset:
        items = items[offset:]

    logger.debug("Listing %d dataset(s) (matched %d of %d total)",
                  len(items), total_matched, len(DATASETS))
    return jsonify({
        "count": len(items),
        "total_matched": total_matched,
        "total": len(DATASETS),
        "datasets": [dataset_summary(d) for d in items],
    }), 200


@app.route("/datasets/<dataset_id>", methods=["GET"])
def get_dataset(dataset_id):
    ds = DATASETS.get(dataset_id)
    if ds is None:
        logger.warning("Dataset not found: %s", dataset_id)
        return jsonify({"error": "Dataset not found"}), 404
    return jsonify(ds), 200


@app.route("/datasets/<dataset_id>/view", methods=["GET"])
def view_dataset(dataset_id):
    ds = DATASETS.get(dataset_id)
    if ds is None:
        abort(404)
    return render_template("detail.html", dataset=ds, active="dashboard")


@app.route("/datasets/<dataset_id>/channel/<int:channel_idx>", methods=["GET"])
def get_channel_data(dataset_id, channel_idx):
    ds = DATASETS.get(dataset_id)
    if ds is None:
        return jsonify({"error": "Dataset not found"}), 404
    if ds["extension"] != "edf":
        return jsonify({"error": "Only EDF files support channel data"}), 400

    filepath = os.path.join(app.config["UPLOAD_FOLDER"], ds["stored_filename"])
    if not os.path.exists(filepath):
        gridfs_restore_to_disk(ds.get("gridfs_file_id"), filepath)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 410

    try:
        import mne
        raw = mne.io.read_raw_edf(filepath, preload=True, verbose="ERROR")
        sfreq = raw.info["sfreq"]
        ch_names = raw.ch_names
        if channel_idx < 0 or channel_idx >= len(ch_names):
            return jsonify({"error": "Channel index out of range"}), 400
        data, times = raw[channel_idx, :]
        data = data.flatten()
        ch_name = ch_names[channel_idx]
    except Exception as e:
        logger.error("Failed to read EDF with MNE: %s", e)
        return jsonify({"error": f"Failed to read EDF: {str(e)}"}), 500

    mode = request.args.get("mode", "both")
    max_points = int(request.args.get("max_points", 1000))
    max_freq = float(request.args.get("max_freq", sfreq/2))

    response = {
        "channel_name": ch_name,
        "sfreq": sfreq,
        "n_samples": len(data),
        "duration_seconds": len(data) / sfreq,
    }

    if mode in ("time", "both"):
        step = max(1, len(data) // max_points)
        decimated_data = data[::step]
        decimated_times = times[::step]
        response["time"] = {
            "times": decimated_times.tolist(),
            "values": decimated_data.tolist(),
        }

    if mode in ("fft", "both"):
        fft_vals = np.fft.rfft(data)
        freqs = np.fft.rfftfreq(len(data), d=1/sfreq)
        magnitude = np.abs(fft_vals)
        idx = freqs <= max_freq
        response["fft"] = {
            "frequencies": freqs[idx].tolist(),
            "magnitude": magnitude[idx].tolist(),
        }

    return jsonify(response)


@app.route("/datasets/<dataset_id>", methods=["DELETE"])
def delete_dataset(dataset_id):
    ds = DATASETS.pop(dataset_id, None)
    if ds is None:
        return jsonify({"error": "Dataset not found"}), 404
    try:
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], ds["stored_filename"])
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception as e:
        logger.error("Failed to delete file for %s: %s", dataset_id, e)
    gridfs_delete(ds.get("gridfs_file_id"))
    mongo_delete(dataset_id)
    logger.info("Deleted dataset %s", dataset_id)
    return jsonify({"deleted": dataset_id}), 200


@app.route("/datasets", methods=["DELETE"])
def delete_datasets_bulk():
    ids_param = request.args.get("ids", "").strip()
    confirm = request.args.get("confirm", "false").strip().lower() == "true"

    if not ids_param and not confirm:
        return jsonify({
            "error": "Refusing to delete. Pass ?ids=id1,id2 for specific datasets, "
                     "or ?confirm=true to delete ALL datasets."
        }), 400

    target_ids = [i.strip() for i in ids_param.split(",") if i.strip()] if ids_param else list(DATASETS.keys())

    deleted, missing = [], []
    for dataset_id in target_ids:
        ds = DATASETS.pop(dataset_id, None)
        if ds is None:
            missing.append(dataset_id)
            continue
        try:
            filepath = os.path.join(app.config["UPLOAD_FOLDER"], ds["stored_filename"])
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception as e:
            logger.error("Failed to delete file for %s: %s", dataset_id, e)
        gridfs_delete(ds.get("gridfs_file_id"))
        mongo_delete(dataset_id)
        deleted.append(dataset_id)

    logger.info("Bulk delete: %d deleted, %d missing", len(deleted), len(missing))
    return jsonify({"deleted": deleted, "not_found": missing, "deleted_count": len(deleted)}), 200


@app.route("/datasets/<dataset_id>", methods=["PATCH"])
def rename_dataset(dataset_id):
    ds = DATASETS.get(dataset_id)
    if ds is None:
        return jsonify({"error": "Dataset not found"}), 404

    body = request.get_json(silent=True) or {}
    new_name = (body.get("original_filename") or "").strip()
    if not new_name:
        return jsonify({"error": "Provide JSON body: {\"original_filename\": \"new_name.edf\"}"}), 400

    new_name = secure_filename(new_name)
    old_name = ds["original_filename"]
    ds["original_filename"] = new_name
    mongo_save(ds)
    logger.info("Renamed dataset %s: '%s' -> '%s'", dataset_id, old_name, new_name)
    return jsonify(ds), 200


@app.route("/datasets/<dataset_id>/download", methods=["GET"])
def download_dataset(dataset_id):
    ds = DATASETS.get(dataset_id)
    if ds is None:
        return jsonify({"error": "Dataset not found"}), 404
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], ds["stored_filename"])
    if not os.path.exists(filepath):
        gridfs_restore_to_disk(ds.get("gridfs_file_id"), filepath)
    if not os.path.exists(filepath):
        logger.warning("Download failed, file missing on disk and in GridFS: %s", filepath)
        return jsonify({"error": "File no longer exists on server"}), 410
    logger.info("Downloading dataset %s (%s)", dataset_id, ds["original_filename"])
    return send_file(filepath, as_attachment=True, download_name=ds["original_filename"])


@app.route("/datasets/<dataset_id>/reparse", methods=["POST"])
def reparse_dataset(dataset_id):
    ds = DATASETS.get(dataset_id)
    if ds is None:
        return jsonify({"error": "Dataset not found"}), 404

    filepath = os.path.join(app.config["UPLOAD_FOLDER"], ds["stored_filename"])
    if not os.path.exists(filepath):
        gridfs_restore_to_disk(ds.get("gridfs_file_id"), filepath)
    if not os.path.exists(filepath):
        return jsonify({"error": "Original file no longer exists on server"}), 410

    parser = PARSERS.get(ds["extension"])
    opts = build_parse_opts(request.args)
    start_time = time.time()
    parse_error = None
    parsed_info = {}
    try:
        parsed_info = parser(filepath, opts)
    except Exception as e:
        parse_error = str(e)
        logger.error("Reparse failed for %s: %s\n%s", dataset_id, e, traceback.format_exc())

    ds["parse_status"] = "error" if parse_error else "ok"
    ds["parse_error"] = parse_error
    ds["processing_time_ms"] = round((time.time() - start_time) * 1000, 2)
    ds["info"] = parsed_info
    ds["reparsed_at"] = now_iso()
    mongo_ok = mongo_save(ds)
    ds["persisted_to_db"] = mongo_ok

    status_code = 200 if not parse_error else 422
    logger.info("Reparsed dataset %s: status=%s", dataset_id, ds["parse_status"])
    return jsonify(ds), status_code


def compute_live_context(args):
    raw_data, ch_names, sfreq, meta = resolve_live_dataset(args)
    window_sec, topo_refresh = live_window_params(args)
    region_options, region_choice, selected_channels = live_selected_channels(ch_names, args.get("regions"))

    with LIVE_STATS_LOCK:
        LIVE_STATS["refresh_count"] += 1
        LIVE_STATS["regions_seen"] |= set(region_choice)
        refresh_count = LIVE_STATS["refresh_count"]
        session_minutes = (time.time() - LIVE_STATS["session_start"]) / 60
        regions_seen_sorted = sorted(LIVE_STATS["regions_seen"])

    total_samples = raw_data.shape[1]
    fallback_pool = set(get_live_fallback_pool())
    recognized = [ch for ch in ch_names if ch in fallback_pool]
    custom_channels = [ch for ch in ch_names if ch not in fallback_pool]

    sel_idx = [ch_names.index(ch) for ch in selected_channels]
    display_data = downsample_for_display(raw_data[sel_idx])
    display_sfreq = sfreq * display_data.shape[1] / raw_data.shape[1] if raw_data.shape[1] else sfreq
    sweep_payload = {
        "channels": selected_channels,
        "windowSec": window_sec,
        "dt": 1.0 / display_sfreq if display_sfreq else 1.0,
        "data": display_data.tolist(),
        "totalPoints": display_data.shape[1],
    }

    analysis_window = min(total_samples, int(min(60, total_samples / sfreq) * sfreq)) if sfreq else 0
    report_data_uv = raw_data[:, -analysis_window:] * 1e6 if analysis_window > 0 else raw_data * 1e6

    band_powers = {}
    for band_name, (lo, hi) in LIVE_BANDS.items():
        p = live_band_power(report_data_uv, sfreq, lo, hi) if MNE_AVAILABLE and sfreq else None
        band_powers[band_name] = float(np.mean(p)) if p is not None else 0.0
    total_power = sum(band_powers.values()) or 1.0
    band_pct = {k: 100 * v / total_power for k, v in band_powers.items()}
    dominant_band = max(band_pct, key=band_pct.get) if any(band_pct.values()) else None
    recording_seconds = total_samples / sfreq if sfreq else 0

    qs = urlencode({
        "source": args.get("source", "auto"),
        "n_channels": args.get("n_channels", 64),
        "sfreq": args.get("sfreq", 160),
        "regions": ",".join(region_choice),
        "window_sec": window_sec,
    })

    return {
        "meta": meta, "ch_names": ch_names, "sfreq": sfreq, "region_options": region_options,
        "region_choice": region_choice, "selected_channels": selected_channels,
        "sweep_payload": sweep_payload, "window_sec": window_sec, "topo_refresh": topo_refresh,
        "recognized": recognized, "custom_channels": custom_channels, "band_pct": band_pct,
        "dominant_band": dominant_band, "analysis_window": analysis_window,
        "session_minutes": session_minutes, "recording_seconds": recording_seconds,
        "regions_seen": regions_seen_sorted, "refresh_count": refresh_count, "qs": qs,
        "args": args, "total_samples": total_samples,
    }


LIVE_PAGE_TEMPLATE = string.Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>EEG Monitoring Dashboard</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #f7f8fa; color: #1a1a1a; }
  header { padding: 18px 24px; background: #fff; border-bottom: 1px solid #e0e0e0; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 22px; margin: 0; }
  header nav a { margin-left: 16px; color: #2563eb; text-decoration: none; font-size: 14px; }
  .layout { display: flex; align-items: flex-start; }
  .sidebar { width: 280px; flex-shrink: 0; background: #fff; border-right: 1px solid #e0e0e0; padding: 18px; min-height: 100vh; }
  .sidebar h3 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: #666; margin: 18px 0 8px; }
  .sidebar h3:first-child { margin-top: 0; }
  .sidebar label { display: block; font-size: 13px; margin: 6px 0 3px; }
  .sidebar select, .sidebar input[type=number] { width: 100%; padding: 5px 6px; margin-bottom: 8px; border: 1px solid #ccc; border-radius: 5px; }
  .sidebar input[type=range] { width: 100%; }
  .region-check { font-size: 13px; margin: 3px 0; }
  .sidebar button { width: 100%; margin-top: 12px; padding: 8px; background: #2563eb; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }
  .main { flex: 1; padding: 20px 24px; }
  .caption { color: #666; font-size: 13px; margin-bottom: 10px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; margin-right: 6px; }
  .badge.ok { background: #dcfce7; color: #166534; }
  .badge.warn { background: #fef3c7; color: #92400e; }
  .columns { display: flex; gap: 24px; align-items: flex-start; flex-wrap: wrap; }
  .col-chart { flex: 3; min-width: 360px; }
  .col-topo { flex: 2; min-width: 300px; }
  .card { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 12px; margin-bottom: 14px; }
  .topo-grid img { width: 100%; max-width: 320px; display: block; margin: 0 auto 10px; }
  .bar-row { display: flex; align-items: center; margin: 4px 0; font-size: 12px; }
  .bar-label { width: 60px; }
  .bar-track { flex: 1; background: #eee; border-radius: 4px; overflow: hidden; height: 14px; margin: 0 8px; }
  .bar-fill { height: 100%; background: #2563eb; }
  .bar-pct { width: 42px; text-align: right; }
  .report-list { font-size: 13px; line-height: 1.7; }
  .divider { border: none; border-top: 1px solid #e0e0e0; margin: 20px 0; }
  a.download { display: inline-block; margin-top: 8px; padding: 7px 14px; background: #111827; color: #fff; border-radius: 6px; text-decoration: none; font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>EEG Monitoring Dashboard</h1>
  <nav><a href="/">Dataset manager</a><a href="/upload-page">Upload</a></nav>
</header>
<div class="layout">
  <form class="sidebar" method="GET" action="/live">
    <h3>Data Source</h3>
    <label>Source</label>
    <select name="source">
      <option value="auto" $SEL_AUTO>Auto (MongoDB, fallback to mock)</option>
      <option value="mongo" $SEL_MONGO>MongoDB only</option>
      <option value="mock" $SEL_MOCK>Mock data only</option>
    </select>
    $MONGO_STATUS

    <h3>Mock Data Settings</h3>
    <label>Number of channels</label>
    <input type="number" name="n_channels" min="4" max="256" value="$N_CHANNELS">
    <label>Sampling rate (Hz)</label>
    <input type="number" name="sfreq" min="32" max="1024" value="$SFREQ_MOCK">

    <h3>Display Controls</h3>
    <label>Sweep / analysis window (seconds): <span id="windowSecLabel">$WINDOW_SEC</span></label>
    <input type="range" name="window_sec" min="2" max="15" value="$WINDOW_SEC"
           oninput="document.getElementById('windowSecLabel').textContent = this.value">
    <label>Topomap refresh interval (s): <span id="topoRefreshLabel">$TOPO_REFRESH</span></label>
    <input type="range" name="topo_refresh" min="0.5" max="3.0" step="0.5" value="$TOPO_REFRESH"
           oninput="document.getElementById('topoRefreshLabel').textContent = this.value">

    <h3>Brain regions to display</h3>
    $REGION_CHECKBOXES

    <button type="submit">Apply</button>
  </form>

  <div class="main">
    <div class="columns">
      <div class="col-chart">
        <div class="card">
          <h3 style="margin-top:0">Live Multi-Channel EEG Signal</h3>
          <div class="caption">$CHART_CAPTION</div>
          <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:6px;">
            <canvas id="eegCanvas" style="width:100%;display:block;"></canvas>
          </div>
        </div>
      </div>
      <div class="col-topo">
        <div class="card topo-grid">
          <h3 style="margin-top:0">Topographic Maps - All Bands</h3>
          $TOPO_IMGS
          $CUSTOM_CHANNELS_NOTE
        </div>
      </div>
    </div>

    <hr class="divider">

    <h2>Session Report</h2>
    <div class="columns">
      <div class="card" style="flex:1; min-width:280px;">
        <h3 style="margin-top:0">App usage</h3>
        <div class="report-list">$REPORT_USAGE</div>
      </div>
      <div class="card" style="flex:1; min-width:280px;">
        <h3 style="margin-top:0">Brainwave band mix <span style="font-weight:normal;font-size:12px;color:#666">(last $ANALYSIS_WINDOW_SEC s, selected channels)</span></h3>
        $BAND_BARS
      </div>
    </div>
    $DOMINANT_BAND_NOTE
    <a class="download" href="/live/report.txt?$QS">Download report (.txt)</a>
  </div>
</div>

<script>
(function() {
    const cfg = $SWEEP_JSON;
    const channels = cfg.channels;
    const windowSec = cfg.windowSec;
    const canvas = document.getElementById("eegCanvas");
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const totalHeight = Math.max(200, channels.length * 34);

    function resize() {
        const w = canvas.clientWidth || canvas.parentElement.clientWidth;
        const h = totalHeight;
        canvas.style.height = h + "px";
        canvas.width = w * dpr;
        canvas.height = h * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    window.addEventListener("resize", resize);
    resize();

    const totalDur = cfg.dt * (cfg.totalPoints - 1);

    function valueAt(chIndex, tSec) {
        let tt = tSec % totalDur;
        if (tt < 0) tt += totalDur;
        const idxF = tt / cfg.dt;
        const i0 = Math.floor(idxF);
        const i1 = Math.min(i0 + 1, cfg.totalPoints - 1);
        const frac = idxF - i0;
        const arr = cfg.data[chIndex];
        return arr[i0] + (arr[i1] - arr[i0]) * frac;
    }

    const POINTS = 300;
    const startT = performance.now() / 1000;

    function draw(tsMs) {
        const nowSec = tsMs / 1000 - startT;
        const w = canvas.clientWidth || canvas.parentElement.clientWidth;
        const h = totalHeight;
        const laneH = h / channels.length;

        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, w, h);

        channels.forEach((name, i) => {
            const laneY = i * laneH;
            const midY = laneY + laneH / 2;
            const amp = laneH * 0.42;

            ctx.strokeStyle = "rgba(0,0,0,0.08)";
            ctx.beginPath(); ctx.moveTo(0, laneY); ctx.lineTo(w, laneY); ctx.stroke();

            ctx.strokeStyle = "#000000";
            ctx.lineWidth = 1.2;
            ctx.beginPath();
            for (let p = 0; p <= POINTS; p++) {
                const frac = p / POINTS;
                const x = frac * w;
                const tAtPoint = nowSec - windowSec * (1 - frac);
                const val = valueAt(i, tAtPoint) * 1e6;
                const y = midY - (val / 25) * amp;
                if (p === 0) { ctx.moveTo(x, y); } else { ctx.lineTo(x, y); }
            }
            ctx.stroke();

            ctx.fillStyle = "#333333";
            ctx.font = "11px monospace";
            ctx.fillText(name, 6, laneY + 13);
        });

        requestAnimationFrame(draw);
    }
    requestAnimationFrame(draw);

    // OPTIMIZATION: Reload topomap images without stacking requests
    const topoImgs = document.querySelectorAll("img.topo-live");
    
    function refreshTopomap(img) {
        if (img.dataset.loading === "true") return; // Skip if previous request is still pending
        img.dataset.loading = "true";
        
        const base = img.getAttribute("data-src");
        const tempImg = new Image();
        
        tempImg.onload = () => {
            img.src = tempImg.src;
            img.dataset.loading = "false";
        };
        tempImg.onerror = () => {
            img.dataset.loading = "false";
        };
        
        tempImg.src = base + "&t=" + Date.now();
    }

    setInterval(() => {
        topoImgs.forEach(img => refreshTopomap(img));
    }, Math.max(2000, $TOPO_REFRESH_MS)); // Enforce a minimum 2-second interval
})();
</script>
</body>
</html>
""")


def render_live_page(ctx):
    meta = ctx["meta"]
    region_options = ctx["region_options"]
    region_choice = ctx["region_choice"]
    recognized = ctx["recognized"]
    custom_channels = ctx["custom_channels"]
    band_pct = ctx["band_pct"]
    dominant_band = ctx["dominant_band"]
    args = ctx["args"]

    region_boxes = "".join(
        f'<div class="region-check"><label>'
        f'<input type="checkbox" name="regions" value="{r}" {"checked" if r in region_choice else ""}> {r}'
        f'</label></div>'
        for r in region_options
    ) or "<div class='caption'>No recognizable regions.</div>"

    if meta["mongo_available"]:
        mongo_status = f'<div class="badge ok">MongoDB: {meta["n_files"]} file(s) stitched</div>'
    else:
        mongo_status = '<div class="badge warn">MongoDB unavailable - using mock data</div>'

    source = args.get("source", "auto")
    chart_caption = (
        f'{len(ctx["selected_channels"])} channels &middot; {ctx["sfreq"]:.0f} Hz &middot; '
        f'{ctx["window_sec"]}s window &middot; '
        f'{" + ".join(region_choice) if region_choice else "All regions"} &middot; '
        f'{"MongoDB (live)" if meta["source"] == "mongo" else "Mock data"}'
    )

    if MNE_AVAILABLE and MATPLOTLIB_AVAILABLE:
        if recognized:
            topo_imgs = "".join(
                f'<img class="topo-live" data-src="/live/api/topomap/{band}.png?{ctx["qs"]}" '
                f'src="/live/api/topomap/{band}.png?{ctx["qs"]}" alt="{band} topomap">'
                for band in LIVE_BAND_ORDER
            )
        else:
            topo_imgs = (
                "<div class='caption'>None of the current channels match a known electrode montage, "
                "so no topomap can be drawn. See the Session Report below for a signal summary instead.</div>"
            )
    else:
        topo_imgs = "<div class='caption'>Topomaps unavailable on this server (mne/matplotlib not installed).</div>"

    custom_note = ""
    if custom_channels:
        shown = ", ".join(custom_channels[:6]) + ("..." if len(custom_channels) > 6 else "")
        custom_note = f'<div class="caption">{len(custom_channels)} channel(s) don\'t match a known montage and aren\'t shown here: {shown}. See Session Report.</div>'

    report_usage = (
        f'- Session length: {ctx["session_minutes"]:.1f} min<br>'
        f'- Data source: {"MongoDB (" + str(meta["n_files"]) + " file(s) stitched)" if meta["source"] == "mongo" else "Mock data"}<br>'
        f'- Recording represented: {ctx["recording_seconds"]:.0f}s across {len(ctx["ch_names"])} channels<br>'
        f'- Regions viewed this session: {", ".join(ctx["regions_seen"]) or "-"}<br>'
        f'- Dashboard refreshes: {ctx["refresh_count"]}'
    )

    band_bars = "".join(
        f'<div class="bar-row"><div class="bar-label">{band}</div>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{band_pct.get(band, 0):.1f}%"></div></div>'
        f'<div class="bar-pct">{band_pct.get(band, 0):.1f}%</div></div>'
        for band in LIVE_BANDS
    )

    dominant_note = ""
    if dominant_band:
        dominant_note = (
            f'<p>Over this window, <strong>{dominant_band}</strong> power is dominant '
            f'({band_pct[dominant_band]:.0f}% of band power) - {LIVE_BAND_DESCRIPTIONS[dominant_band]}. '
            f'This is a general, research-based association, <strong>not a clinical or diagnostic assessment.</strong></p>'
        )
    if custom_channels:
        dominant_note += (
            f'<div class="caption">Includes {len(custom_channels)} non-standard-montage channel(s) that '
            f"can't appear on the topomap: {', '.join(custom_channels[:10])}</div>"
        )

    html = LIVE_PAGE_TEMPLATE.substitute(
        SEL_AUTO="selected" if source == "auto" else "",
        SEL_MONGO="selected" if source == "mongo" else "",
        SEL_MOCK="selected" if source == "mock" else "",
        MONGO_STATUS=mongo_status,
        N_CHANNELS=meta["n_channels_requested"],
        SFREQ_MOCK=meta["sfreq_requested"],
        WINDOW_SEC=ctx["window_sec"],
        TOPO_REFRESH=ctx["topo_refresh"],
        REGION_CHECKBOXES=region_boxes,
        CHART_CAPTION=chart_caption,
        TOPO_IMGS=topo_imgs,
        CUSTOM_CHANNELS_NOTE=custom_note,
        REPORT_USAGE=report_usage,
        ANALYSIS_WINDOW_SEC=f'{ctx["analysis_window"] / ctx["sfreq"]:.0f}' if ctx["sfreq"] else "0",
        BAND_BARS=band_bars,
        DOMINANT_BAND_NOTE=dominant_note,
        QS=ctx["qs"],
        SWEEP_JSON=json.dumps(ctx["sweep_payload"]),
        TOPO_REFRESH_MS=int(ctx["topo_refresh"] * 1000),
    )
    return html


def render_live_report_text(ctx):
    meta = ctx["meta"]
    band_pct = ctx["band_pct"]
    dominant_band = ctx["dominant_band"]
    lines = [
        "EEG Session Report",
        f"Generated: {now_iso()}",
        "",
        "App usage:",
        f"- Session length: {ctx['session_minutes']:.1f} min",
        f"- Data source: {'MongoDB (' + str(meta['n_files']) + ' file(s) stitched)' if meta['source'] == 'mongo' else 'Mock data'}",
        f"- Recording represented: {ctx['recording_seconds']:.0f}s across {len(ctx['ch_names'])} channels",
        f"- Regions viewed: {', '.join(ctx['regions_seen']) or 'none'}",
        "",
        f"Band power mix (last {ctx['analysis_window'] / ctx['sfreq']:.0f}s): " + ", ".join(f"{k} {v:.1f}%" for k, v in band_pct.items()),
    ]
    if dominant_band:
        lines.append(f"Dominant band: {dominant_band} - {LIVE_BAND_DESCRIPTIONS.get(dominant_band, '')}")
    lines.append("This is a general, research-based association, not a clinical or diagnostic assessment.")
    return "\n".join(lines) + "\n"


@app.route("/live", methods=["GET"])
def live_dashboard():
    ctx = compute_live_context(request.args)
    return render_live_page(ctx)


@app.route("/live/report.txt", methods=["GET"])
def live_report_txt():
    ctx = compute_live_context(request.args)
    text = render_live_report_text(ctx)
    return Response(text, mimetype="text/plain", headers={
        "Content-Disposition": "attachment; filename=eeg_session_report.txt"
    })

_topomap_cache = {}
_topomap_lock = threading.Lock()
TOPOMAP_CACHE_TTL = 2.0

@app.route("/live/api/topomap/<band>.png", methods=["GET"])
def live_topomap(band):
    if not (MNE_AVAILABLE and MATPLOTLIB_AVAILABLE):
        return jsonify({"error": "mne/matplotlib not installed on this server"}), 503
    if band not in LIVE_BANDS and band != "Broadband":
        return jsonify({"error": f"Unknown band '{band}'"}), 404

    args = request.args
    cache_key = (
        band,
        args.get("source", "auto"),
        args.get("n_channels", "64"),
        args.get("sfreq", "160"),
        args.get("regions", ""),
        args.get("window_sec", "6")
    )

    now = time.time()
    with _topomap_lock:
        if cache_key in _topomap_cache:
            cached_time, cached_png = _topomap_cache[cache_key]
            if now - cached_time < TOPOMAP_CACHE_TTL:
                resp = send_file(BytesIO(cached_png), mimetype="image/png")
                resp.headers["Cache-Control"] = "no-store"
                return resp

    raw_data, ch_names, sfreq, meta = resolve_live_dataset(args)
    window_sec, _ = live_window_params(args)
    fallback_pool = set(get_live_fallback_pool())
    recognized = [ch for ch in ch_names if ch in fallback_pool]

    fig, ax = plt.subplots(figsize=(4.2, 4.2))
    try:
        if not recognized:
            ax.axis("off")
            ax.text(0.5, 0.5, "No channels match a\nknown montage", ha="center", va="center", fontsize=9)
        else:
            total_samples = raw_data.shape[1]
            window_samples = max(1, int(window_sec * sfreq))
            pos = int(time.time() * sfreq) % total_samples if total_samples else 0
            idx = np.arange(pos - window_samples, pos) % total_samples

            recognized_idx = [ch_names.index(ch) for ch in recognized]
            chan_data_uv = raw_data[recognized_idx][:, idx] * 1e6
            recognized_info = build_live_info(tuple(recognized), sfreq)

            if band == "Broadband":
                power = np.mean(np.abs(chan_data_uv), axis=1)
                cbar_label = "uV"
                insufficient = False
            else:
                lo, hi = LIVE_BANDS[band]
                power = live_band_power(chan_data_uv, sfreq, lo, hi)
                cbar_label = "uV^2"
                insufficient = power is None

            if insufficient:
                lo = LIVE_BANDS[band][0]
                ax.axis("off")
                ax.text(0.5, 0.5, f"Collecting data\n(needs ~{max(2.0, 3.0/lo):.0f}s+ window)",
                         ha="center", va="center", fontsize=9)
            else:
                im, _ = mne.viz.plot_topomap(
                    power, recognized_info, axes=ax, show=False, cmap="jet",
                    sensors=True, contours=0, extrapolate="head",
                )
                ax.set_title(band, fontsize=11)
                cbar = fig.colorbar(im, ax=ax, shrink=0.75)
                cbar.ax.tick_params(labelsize=7)
                cbar.set_label(cbar_label, fontsize=8)

        fig.tight_layout()
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=110)
        png_bytes = buf.getvalue()
    finally:
        plt.close(fig)
        plt.clf()

    with _topomap_lock:
        _topomap_cache[cache_key] = (now, png_bytes)

    resp = send_file(BytesIO(png_bytes), mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File too large. Max upload size is 500MB."}), 413


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.route("/", methods=["GET"])
def dashboard():
    return render_template("index.html", active="dashboard")


@app.route("/upload-page", methods=["GET"])
def upload_page():
    return render_template("upload.html", active="upload")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 0))
    debug_mode = os.environ.get("FLASK_DEBUG", "true").lower() in ("1", "true", "yes")
    logger.info("Starting EEG dataset server on http://0.0.0.0:%s", port or 5000)
    app.run(host="0.0.0.0", port=port, debug=debug_mode, use_reloader=False)