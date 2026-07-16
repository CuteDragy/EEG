"""
EEG Dataset Upload Server
==========================
A Flask REST API that accepts EEG dataset uploads and returns structural
debug/info metadata about them. No signal analysis is performed - this
service is purely for ingesting, validating, and inspecting datasets.

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
    PATCH  /datasets/<dataset_id>         -> rename a dataset's display filename
    DELETE /datasets/<dataset_id>         -> remove a dataset record + file
    DELETE /datasets                      -> bulk delete (?ids=a,b,c or ?confirm=true for all)
    GET    /datasets/<dataset_id>/download -> download the original uploaded file
    POST   /datasets/<dataset_id>/reparse  -> re-run parsing with new query-param opts

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
import struct
import time
import traceback
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request, send_file
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

    # Try to detect a time/timestamp column to estimate sampling rate
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
    # Heuristic: assume (channels, samples) or (samples, channels) for 2D
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
        orientation = opts.get("npy_orientation")  # "channels_x_samples" or "samples_x_channels"
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
        # Column-oriented EEG: {"Fp1": [...], "Fp2": [...], "sfreq": 256, ...}
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
                stacked = np.vstack(channel_arrays)  # (channels, samples)
                info["mne"] = mne_enrich_array(stacked, ch_names, sfreq)
    else:
        info["top_level_type"] = type(data).__name__

    return info


def parse_edf(filepath, opts=None):
    """
    Minimal native EDF/EDF+ header parser.
    EDF spec: fixed-length ASCII header, no external dependency needed.
    Reference layout (byte offsets in the 256-byte main header, plus
    256 bytes per signal in the signal header block):
        8    version
        80   patient id
        80   recording id
        8    start date (dd.mm.yy)
        8    start time (hh.mm.ss)
        8    number of bytes in header
        44   reserved
        8    number of data records
        8    duration of a data record (seconds)
        4    number of signals (ns)
    Then per-signal (ns times each): label(16), transducer(80), physical
    dimension(8), physical min(8), physical max(8), digital min(8),
    digital max(8), prefiltering(80), n samples per record(8), reserved(32)
    """
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

        # EDF signal header field widths, in order, per the spec.
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
# Builds an mne.io.Raw / RawArray purely to read off structural metadata
# that MNE already knows how to compute (channel typing, montage matching,
# annotations/events). No filtering, resampling, or spectral analysis is
# performed - this stays in the same "structural info only" spirit as the
# native parsers above.
# --------------------------------------------------------------------------

_STANDARD_1020_NAMES = None


def _get_standard_1020_names():
    global _STANDARD_1020_NAMES
    if _STANDARD_1020_NAMES is None:
        montage = mne.channels.make_standard_montage("standard_1020")
        _STANDARD_1020_NAMES = {n.upper() for n in montage.ch_names}
    return _STANDARD_1020_NAMES


def guess_channel_types(ch_names):
    """Cheap name-based EEG/EOG/ECG/EMG/stim heuristic (no signal inspection)."""
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
    """Re-reads the EDF via mne.io.read_raw_edf (header only, preload=False)."""
    if not MNE_AVAILABLE:
        return {"available": False, "reason": "mne is not installed"}
    try:
        raw = mne.io.read_raw_edf(filepath, preload=False, verbose="ERROR")
        return build_mne_summary(raw)
    except Exception as e:
        logger.debug("MNE EDF enrichment failed: %s", e)
        return {"available": False, "error": str(e)}


def mne_enrich_array(data_channels_x_samples, ch_names, sfreq):
    """Wraps an in-memory (channels x samples) array in an mne.io.RawArray."""
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

    # Store the raw bytes in GridFS so the actual file (not just its parsed
    # metadata) survives a server restart/redeploy, since UPLOAD_FOLDER lives
    # on ephemeral local disk. This is the only way the data goes away short
    # of an explicit delete.
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
    """
    Bulk delete. Two modes:
      DELETE /datasets?ids=id1,id2,id3   -> delete just those
      DELETE /datasets?confirm=true      -> wipe every dataset (destructive)
    """
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
    """Rename a dataset's display filename (metadata only - stored file/path unchanged)."""
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
    """Re-run parsing on an already-uploaded file with different query-param opts,
    e.g. POST /datasets/<id>/reparse?mne=false or ?sfreq=256 - without re-uploading."""
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


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "File too large. Max upload size is 500MB."}), 413


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Endpoint not found"}), 404

@app.route("/", methods=["GET"])
def dashboard():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>EEG Dataset Server</title>
<style>
  body { font-family: -apple-system, Segoe UI, sans-serif; background:#0f1117; color:#e6e6e6; margin:0; padding:2rem; }
  h1 { color:#7dd3fc; }
  .status { display:inline-block; padding:.25rem .75rem; border-radius:999px; font-size:.85rem; margin-bottom:1.5rem; }
  .ok { background:#14532d; color:#86efac; }
  .bad { background:#7f1d1d; color:#fca5a5; }
  table { width:100%; border-collapse:collapse; margin-top:1rem; }
  th, td { text-align:left; padding:.6rem .8rem; border-bottom:1px solid #262a35; font-size:.9rem; }
  th { color:#94a3b8; font-weight:600; }
  tr:hover { background:#1a1d27; }
  .pill { padding:.15rem .5rem; border-radius:6px; font-size:.75rem; }
  .success { background:#14532d; color:#86efac; }
  .error { background:#7f1d1d; color:#fca5a5; }
  .empty { color:#64748b; padding:2rem 0; text-align:center; }
  a { color:#7dd3fc; }
</style>
</head>
<body>
  <h1>🧠 EEG Dataset Server</h1>
  <span id="statusPill" class="status">checking…</span>
  <div id="counts" style="color:#94a3b8; margin-bottom:1rem;"></div>

  <div style="display:flex; gap:.5rem; margin-bottom:1rem; flex-wrap:wrap;">
    <input id="searchBox" placeholder="Search filename…" style="flex:1; min-width:180px; background:#161923; border:1px solid #262a35; color:#e6e6e6; padding:.5rem .75rem; border-radius:6px;">
    <select id="formatFilter" style="background:#161923; border:1px solid #262a35; color:#e6e6e6; padding:.5rem .75rem; border-radius:6px;">
      <option value="">All formats</option>
      <option value="edf">EDF</option>
      <option value="csv">CSV</option>
      <option value="tsv">TSV</option>
      <option value="npy">NPY</option>
      <option value="json">JSON</option>
    </select>
    <button id="deleteAllBtn" style="background:#7f1d1d; color:#fca5a5; border:none; padding:.5rem 1rem; border-radius:6px; cursor:pointer;">Delete all</button>
  </div>

  <table>
    <thead>
      <tr><th>Filename</th><th>Format</th><th>Size</th><th>Uploaded</th><th>Status</th><th></th></tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="emptyMsg" class="empty" style="display:none;">No datasets found.</div>

<script>
async function load() {
  try {
    const health = await (await fetch("/health")).json();
    const pill = document.getElementById("statusPill");
    pill.textContent = health.mongo_connected ? "● online (db connected)" : "● online (in-memory only)";
    pill.className = "status " + (health.mongo_connected ? "ok" : "bad");

    const search = document.getElementById("searchBox").value.trim();
    const format = document.getElementById("formatFilter").value;
    const params = new URLSearchParams();
    if (search) params.set("search", search);
    if (format) params.set("format", format);

    const data = await (await fetch("/datasets?" + params.toString())).json();
    document.getElementById("counts").textContent =
      `${data.total_matched} of ${data.total} dataset(s) shown`;

    const rows = document.getElementById("rows");
    const emptyMsg = document.getElementById("emptyMsg");
    rows.innerHTML = "";

    if (!data.datasets.length) {
      emptyMsg.style.display = "block";
      return;
    }
    emptyMsg.style.display = "none";

    data.datasets.forEach(ds => {
      const mb = (ds.file_size_bytes / (1024*1024)).toFixed(2);
      const statusClass = ds.parse_status === "ok" ? "success" : "error";
      rows.innerHTML += `
        <tr>
          <td><a href="/datasets/${ds.dataset_id}">${ds.original_filename}</a></td>
          <td>${ds.extension.toUpperCase()}</td>
          <td>${mb} MB</td>
          <td>${new Date(ds.uploaded_at).toLocaleString()}</td>
          <td><span class="pill ${statusClass}">${ds.parse_status}</span></td>
          <td style="white-space:nowrap;">
            <a href="/datasets/${ds.dataset_id}/download" style="margin-right:.75rem;">↓</a>
            <a href="#" onclick="removeDataset('${ds.dataset_id}'); return false;" style="color:#fca5a5;">✕</a>
          </td>
        </tr>`;
    });
  } catch (e) {
    document.getElementById("statusPill").textContent = "● error loading status";
    document.getElementById("statusPill").className = "status bad";
  }
}

async function removeDataset(id) {
  if (!confirm("Delete this dataset? This removes the file and its record.")) return;
  await fetch(`/datasets/${id}`, { method: "DELETE" });
  load();
}

document.getElementById("deleteAllBtn").addEventListener("click", async () => {
  if (!confirm("Delete ALL datasets? This cannot be undone.")) return;
  await fetch("/datasets?confirm=true", { method: "DELETE" });
  load();
});

document.getElementById("searchBox").addEventListener("input", () => load());
document.getElementById("formatFilter").addEventListener("change", () => load());

load();
setInterval(load, 10000); // auto-refresh every 10s
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 0))
    debug_mode = os.environ.get("FLASK_DEBUG", "true").lower() in ("1", "true", "yes")
    logger.info("Starting EEG dataset server on http://0.0.0.0:%s", port)
    # use_reloader=False: avoids the file watcher scanning the whole
    # environment (e.g. site-packages) on every change in dev containers.
    app.run(host="0.0.0.0", port=port, debug=debug_mode, use_reloader=False)