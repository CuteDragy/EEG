import argparse
import os
import sys

import requests
import rich
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich import box

# ── Config ───────────────────────────────────────────────────────────────────
SERVER_URL = "https://eeg-a37i.onrender.com"
FILE_PATH  = r"C:\Users\pzhiy\OneDrive - University of Nottingham Malaysia\Files\Internship\Summer25-26\EEG\uploads\S001R01.edf"
# ─────────────────────────────────────────────────────────────────────────────

console = Console()


def upload(filepath: str) -> dict:
    console.print(f"\n[dim]Uploading[/dim] [white]{os.path.basename(filepath)}[/white] [dim]→ {SERVER_URL}/upload ...[/dim]")
    with open(filepath, "rb") as f:
        resp = requests.post(f"{SERVER_URL}/upload", files={"file": f})
    resp.raise_for_status()
    return resp.json()


def display(data: dict):
    info         = data.get("info", {})
    status_ok    = data.get("parse_status") == "ok"
    status_color = "green" if status_ok else "red"
    status_label = "✔ PARSED OK" if status_ok else f"✘ ERROR: {data.get('parse_error')}"
    filename     = os.path.basename(data.get("original_filename", "unknown"))

    # ── Header ───────────────────────────────────────────────────────────────
    console.print(Panel(
        f"[bold white]{filename}[/bold white]\n"
        f"[dim]{data.get('dataset_id')}[/dim]\n"
        f"[dim]Uploaded: {data.get('uploaded_at')}  ·  Parse: {data.get('processing_time_ms')} ms[/dim]\n"
        f"[{status_color} bold]{status_label}[/{status_color} bold]",
        title="[bold cyan]EEG Dataset Report[/bold cyan]",
        border_style="cyan",
        padding=(1, 2),
    ))

    if not status_ok:
        return

    # ── Key metrics ───────────────────────────────────────────────────────────
    eeg_ch  = info.get("n_signals", 1) - 1
    file_mb = data.get("file_size_bytes", 0) / (1024 * 1024)

    # EDF: uniform sfreq across EEG channels; CSV: estimated_sampling_rate_hz
    sfreq = (
        info.get("channels", [{}])[0].get("sampling_rate_hz")
        or info.get("estimated_sampling_rate_hz")
        or "—"
    )
    duration = (
        info.get("total_duration_seconds")
        or info.get("estimated_duration_seconds")
        or "—"
    )
    n_signals = info.get("n_signals") or info.get("n_channels") or "—"

    console.print(Columns([
        Panel(f"[bold white]{eeg_ch}[/bold white]\n[dim]EEG channels[/dim]",        border_style="blue"),
        Panel(f"[bold white]{sfreq} Hz[/bold white]\n[dim]Sampling rate[/dim]",     border_style="blue"),
        Panel(f"[bold white]{duration} s[/bold white]\n[dim]Duration[/dim]",        border_style="blue"),
        Panel(f"[bold white]{n_signals}[/bold white]\n[dim]Total signals[/dim]",    border_style="blue"),
        Panel(f"[bold white]{file_mb:.2f} MB[/bold white]\n[dim]File size[/dim]",   border_style="blue"),
    ], equal=True, expand=True))

    # ── Recording metadata + Signal config (EDF) ──────────────────────────────
    fmt = info.get("format", "")

    if fmt == "edf":
        channels = info.get("channels", [])
        eeg_chs  = [c for c in channels if c.get("label") != "EDF Annotations"]
        annot_ch = next((c for c in channels if c.get("label") == "EDF Annotations"), {})

        meta = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        meta.add_column(style="dim", no_wrap=True)
        meta.add_column(style="white")
        meta.add_row("Date",            info.get("start_date", "—"))
        meta.add_row("Start time",      info.get("start_time", "—").replace(".", ":"))
        meta.add_row("Recording ID",    info.get("recording_id", "—"))
        meta.add_row("EDF version",     f"{info.get('edf_version', '—')} (standard)")
        meta.add_row("Patient ID",      "Anonymised" if info.get("patient_id", "").strip() in ("X X X X", "") else info.get("patient_id"))
        meta.add_row("Header size",     f"{info.get('header_bytes', 0):,} bytes")
        meta.add_row("Record duration", f"{info.get('record_duration_seconds')} s / record")
        meta.add_row("Data records",    str(info.get("n_data_records", "—")))

        phys_min = eeg_chs[0].get("physical_min", "—") if eeg_chs else "—"
        phys_max = eeg_chs[0].get("physical_max", "—") if eeg_chs else "—"
        spr_eeg  = eeg_chs[0].get("samples_per_record", "—") if eeg_chs else "—"
        prefilt  = eeg_chs[0].get("prefiltering", "—") if eeg_chs else "—"
        transducer = eeg_chs[0].get("transducer", "—") if eeg_chs else "—"

        sig = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        sig.add_column(style="dim", no_wrap=True)
        sig.add_column(style="white")
        sig.add_row("Unit",                "µV (microvolts)")
        sig.add_row("Physical range",      f"{phys_min} / {phys_max} µV")
        sig.add_row("Digital range",       f"{eeg_chs[0].get('digital_min','—')} / {eeg_chs[0].get('digital_max','—')}" if eeg_chs else "—")
        sig.add_row("Samples/rec (EEG)",   str(spr_eeg))
        sig.add_row("Samples/rec (annot)", str(annot_ch.get("samples_per_record", "—")))
        sig.add_row("Prefiltering",        "None applied" if prefilt == "HP:0Hz LP:0Hz N:0Hz" else prefilt)
        sig.add_row("Transducer",          transducer)
        sig.add_row("Resolution",          "1 µV / digit")

        console.print(Columns([
            Panel(meta, title="[bold]Recording Metadata[/bold]",    border_style="dim"),
            Panel(sig,  title="[bold]Signal Configuration[/bold]",  border_style="dim"),
        ], equal=True, expand=True))

        # ── Channel table ─────────────────────────────────────────────────────
        ch_table = Table(
            title="Channels",
            box=box.SIMPLE_HEAD,
            border_style="dim",
            header_style="bold cyan",
            show_lines=False,
        )
        ch_table.add_column("#",            style="dim", justify="right", width=4)
        ch_table.add_column("Label",        style="white", width=18)
        ch_table.add_column("Rate (Hz)",    justify="right", width=10)
        ch_table.add_column("Samples/rec",  justify="right", width=12)
        ch_table.add_column("Phys range",   justify="right", width=16)
        ch_table.add_column("Unit",         width=6)
        ch_table.add_column("Prefilter",    style="dim", width=22)

        for i, ch in enumerate(channels, 1):
            label    = ch.get("label", "").rstrip(".")
            is_annot = label == "EDF Annotations"
            rng      = f"{ch.get('physical_min')} / {ch.get('physical_max')}"
            style    = "dim" if is_annot else ""
            ch_table.add_row(
                str(i),
                f"[dim]{label}[/dim]" if is_annot else label,
                f"[dim]{ch.get('sampling_rate_hz')}[/dim]" if is_annot else str(ch.get("sampling_rate_hz")),
                str(ch.get("samples_per_record")),
                rng,
                ch.get("physical_dimension", ""),
                ch.get("prefiltering", ""),
            )

        console.print(Panel(ch_table, border_style="dim", padding=(0, 1)))

    # ── CSV / TSV ─────────────────────────────────────────────────────────────
    elif fmt in ("csv", "tsv"):
        meta = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        meta.add_column(style="dim", no_wrap=True)
        meta.add_column(style="white")
        meta.add_row("Samples",       str(info.get("n_samples", "—")))
        meta.add_row("Columns",       str(info.get("n_columns", "—")))
        meta.add_row("Time column",   info.get("time_column_detected") or "Not detected")
        meta.add_row("Est. duration", f"{info.get('estimated_duration_seconds', '—')} s")
        console.print(Panel(meta, title="[bold]File Info[/bold]", border_style="dim"))

        ch_table = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", border_style="dim")
        ch_table.add_column("Channel", style="white")
        ch_table.add_column("Dtype",   style="dim")
        ch_table.add_column("Missing", justify="right")
        dtypes  = info.get("dtypes", {})
        missing = info.get("missing_values_per_column", {})
        for col in info.get("channels", []):
            ch_table.add_row(col, dtypes.get(col, "—"), str(missing.get(col, 0)))
        console.print(Panel(ch_table, border_style="dim", padding=(0, 1)))

    # ── NPY ───────────────────────────────────────────────────────────────────
    elif fmt == "npy":
        meta = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        meta.add_column(style="dim", no_wrap=True)
        meta.add_column(style="white")
        meta.add_row("Shape",    str(info.get("shape")))
        meta.add_row("Dtype",    info.get("dtype", "—"))
        meta.add_row("Ndim",     str(info.get("ndim")))
        meta.add_row("Elements", f"{info.get('size_elements', 0):,}")
        meta.add_row("Preview",  str(info.get("preview_first_values", [])))
        if info.get("interpretation_note"):
            meta.add_row("Note", info["interpretation_note"])
        console.print(Panel(meta, title="[bold]Array Info[/bold]", border_style="dim"))

    # ── JSON ──────────────────────────────────────────────────────────────────
    elif fmt == "json":
        meta = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        meta.add_column(style="dim", no_wrap=True)
        meta.add_column(style="white")
        meta.add_row("Top-level type", info.get("top_level_type", "—"))
        if "n_records" in info:
            meta.add_row("Records", str(info["n_records"]))
        if "detected_fields" in info:
            meta.add_row("Fields", ", ".join(info["detected_fields"]))
        if "array_fields_with_length" in info:
            for k, v in info["array_fields_with_length"].items():
                meta.add_row(f"  {k}", f"{v} samples")
        console.print(Panel(meta, title="[bold]JSON Structure[/bold]", border_style="dim"))

    # ── MNE enrichment (optional, present for any format) ──────────────────────
    display_mne_section(info.get("mne"))

    console.print()


def display_mne_section(mne_info):
    if not mne_info:
        return  # ?mne=false was used, or this server version predates MNE support

    if not mne_info.get("available"):
        reason = mne_info.get("reason") or mne_info.get("error") or "unknown reason"
        console.print(Panel(
            f"[dim]{reason}[/dim]",
            title="[bold yellow]MNE Enrichment — unavailable[/bold yellow]",
            border_style="yellow",
            padding=(0, 1),
        ))
        return

    type_summary = mne_info.get("ch_types_guess_summary", {})
    type_str = ", ".join(f"{v} {k}" for k, v in type_summary.items()) or "—"
    montage = mne_info.get("montage", {})
    ann = mne_info.get("annotations", {})

    meta = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    meta.add_column(style="dim", no_wrap=True)
    meta.add_column(style="white")
    meta.add_row("MNE version",       mne_info.get("mne_version", "—"))
    meta.add_row("Channels",          f"{mne_info.get('n_channels', '—')} ({type_str})")
    meta.add_row("Sampling rate",     f"{mne_info.get('sfreq_hz', '—')} Hz")
    meta.add_row("Highpass / Lowpass", f"{mne_info.get('highpass_hz', '—')} / {mne_info.get('lowpass_hz', '—')} Hz")
    meta.add_row("Duration",          f"{mne_info.get('duration_seconds', '—')} s ({mne_info.get('n_times', '—')} samples)")
    meta.add_row("Measurement date",  mne_info.get("measurement_date") or "—")
    meta.add_row("Bad channels",      ", ".join(mne_info.get("bads", [])) or "None")
    meta.add_row(
        "10-20 montage match",
        f"{montage.get('standard_1020_match_pct', '—')}%  "
        f"({len(montage.get('matched_channels', []))}/{mne_info.get('n_channels', '—')} channels)",
    )

    ann_meta = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    ann_meta.add_column(style="dim", no_wrap=True)
    ann_meta.add_column(style="white")
    ann_meta.add_row("Annotation count", str(ann.get("count", 0)))
    ann_meta.add_row("Unique labels",    ", ".join(ann.get("unique_descriptions", [])) or "—")
    events = ann.get("events_from_annotations", {})
    ann_meta.add_row("Events derived",   str(events.get("n_events", 0)))
    if events.get("event_id_map"):
        ann_meta.add_row("Event ID map", ", ".join(f"{k}={v}" for k, v in events["event_id_map"].items()))

    console.print(Columns([
        Panel(meta,     title="[bold]MNE Summary[/bold]",      border_style="magenta"),
        Panel(ann_meta, title="[bold]MNE Annotations[/bold]",  border_style="magenta"),
    ], equal=True, expand=True))


# ── File management (list / view / rename / delete / download / stats) ───────

def api_get(path: str, **params) -> dict:
    resp = requests.get(f"{SERVER_URL}{path}", params=params)
    resp.raise_for_status()
    return resp.json()


def cmd_list(args):
    data = api_get(
        "/datasets",
        search=args.search or "", format=args.format or "", status=args.status or "",
        sort=args.sort, order=args.order, limit=args.limit or 0, offset=args.offset,
    )
    console.print(f"[dim]{data['total_matched']} of {data['total']} dataset(s)[/dim]\n")

    table = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", border_style="dim")
    table.add_column("ID", style="dim", width=10)
    table.add_column("Filename", style="white")
    table.add_column("Format", justify="center")
    table.add_column("Size", justify="right")
    table.add_column("Uploaded", style="dim")
    table.add_column("Status")
    for ds in data["datasets"]:
        mb = ds["file_size_bytes"] / (1024 * 1024)
        status_style = "green" if ds["parse_status"] == "ok" else "red"
        table.add_row(
            ds["dataset_id"][:8],
            ds["original_filename"],
            ds["extension"].upper(),
            f"{mb:.2f} MB",
            ds["uploaded_at"],
            f"[{status_style}]{ds['parse_status']}[/{status_style}]",
        )
    console.print(table)


def cmd_view(args):
    ds_id = resolve_id(args.dataset_id)
    data = api_get(f"/datasets/{ds_id}")
    display(data)


def cmd_stats(args):
    data = api_get("/stats")
    meta = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    meta.add_column(style="dim", no_wrap=True)
    meta.add_column(style="white")
    meta.add_row("Datasets",  str(data["count"]))
    meta.add_row("Total size", f"{data['total_size_mb']} MB")
    meta.add_row("By format", ", ".join(f"{k}={v}" for k, v in data["by_format"].items()) or "—")
    meta.add_row("By status", ", ".join(f"{k}={v}" for k, v in data["by_status"].items()) or "—")
    meta.add_row("DB connected", "yes" if data["mongo_connected"] else "no")
    console.print(Panel(meta, title="[bold]Server Stats[/bold]", border_style="cyan"))


def cmd_delete(args):
    ds_id = resolve_id(args.dataset_id)
    if not args.yes:
        confirm = console.input(f"[yellow]Delete dataset {ds_id[:8]}? [y/N]: [/yellow]")
        if confirm.strip().lower() != "y":
            console.print("[dim]Cancelled.[/dim]")
            return
    resp = requests.delete(f"{SERVER_URL}/datasets/{ds_id}")
    resp.raise_for_status()
    console.print(f"[green]✔ Deleted[/green] {ds_id}")


def cmd_delete_all(args):
    if not args.yes:
        confirm = console.input("[bold red]Delete ALL datasets? This cannot be undone. [y/N]: [/bold red]")
        if confirm.strip().lower() != "y":
            console.print("[dim]Cancelled.[/dim]")
            return
    resp = requests.delete(f"{SERVER_URL}/datasets", params={"confirm": "true"})
    resp.raise_for_status()
    data = resp.json()
    console.print(f"[green]✔ Deleted {data['deleted_count']} dataset(s)[/green]")


def cmd_download(args):
    ds_id = resolve_id(args.dataset_id)
    resp = requests.get(f"{SERVER_URL}/datasets/{ds_id}/download")
    resp.raise_for_status()
    filename = args.output
    if not filename:
        cd = resp.headers.get("Content-Disposition", "")
        filename = cd.split("filename=")[-1].strip('"') if "filename=" in cd else f"{ds_id}.bin"
    with open(filename, "wb") as f:
        f.write(resp.content)
    console.print(f"[green]✔ Saved to[/green] {filename}")


def cmd_rename(args):
    ds_id = resolve_id(args.dataset_id)
    resp = requests.patch(f"{SERVER_URL}/datasets/{ds_id}", json={"original_filename": args.new_name})
    resp.raise_for_status()
    console.print(f"[green]✔ Renamed to[/green] {resp.json()['original_filename']}")


def cmd_upload(args):
    result = upload(args.filepath)
    display(result)


def resolve_id(partial_id: str) -> str:
    """Allows using a short 8-char prefix (as shown by `list`) instead of the full UUID."""
    if len(partial_id) >= 32:
        return partial_id
    data = api_get("/datasets", limit=0)
    matches = [d["dataset_id"] for d in data["datasets"] if d["dataset_id"].startswith(partial_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        console.print(f"[red]Ambiguous ID prefix '{partial_id}' matches {len(matches)} datasets.[/red]")
        sys.exit(1)
    return partial_id  # let the server 404 with a clear error


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EEG Dataset Server client - upload, view, and manage datasets.")
    p.add_argument("--server", default=SERVER_URL, help=f"Server URL (default: {SERVER_URL})")
    sub = p.add_subparsers(dest="command", required=False)

    up = sub.add_parser("upload", help="Upload a file and display its parsed report")
    up.add_argument("filepath", nargs="?", default=FILE_PATH, help="Path to the file (defaults to configured FILE_PATH)")
    up.set_defaults(func=cmd_upload)

    ls = sub.add_parser("list", help="List datasets, with optional search/filter/sort")
    ls.add_argument("--search", help="Filter by filename substring")
    ls.add_argument("--format", choices=["csv", "tsv", "edf", "npy", "json"])
    ls.add_argument("--status", choices=["ok", "error"])
    ls.add_argument("--sort", default="uploaded_at", choices=["uploaded_at", "file_size_bytes", "original_filename"])
    ls.add_argument("--order", default="desc", choices=["asc", "desc"])
    ls.add_argument("--limit", type=int, default=0)
    ls.add_argument("--offset", type=int, default=0)
    ls.set_defaults(func=cmd_list)

    vw = sub.add_parser("view", help="Show the full report for one dataset")
    vw.add_argument("dataset_id", help="Full dataset ID or unique 8-char prefix")
    vw.set_defaults(func=cmd_view)

    dl = sub.add_parser("download", help="Download the original file for a dataset")
    dl.add_argument("dataset_id")
    dl.add_argument("-o", "--output", help="Output filename (default: original filename)")
    dl.set_defaults(func=cmd_download)

    rn = sub.add_parser("rename", help="Rename a dataset's display filename")
    rn.add_argument("dataset_id")
    rn.add_argument("new_name")
    rn.set_defaults(func=cmd_rename)

    de = sub.add_parser("delete", help="Delete one dataset")
    de.add_argument("dataset_id")
    de.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    de.set_defaults(func=cmd_delete)

    da = sub.add_parser("delete-all", help="Delete ALL datasets (destructive)")
    da.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    da.set_defaults(func=cmd_delete_all)

    st = sub.add_parser("stats", help="Show aggregate server/storage stats")
    st.set_defaults(func=cmd_stats)

    return p


if __name__ == "__main__":
    parser = build_arg_parser()
    args = parser.parse_args()
    SERVER_URL = args.server  # allow overriding the configured server for this run
    if args.command is None:
        # No subcommand given (e.g. run via IDE "Run" button) -> old default behavior:
        # upload the configured FILE_PATH and display its report.
        cmd_upload(argparse.Namespace(filepath=FILE_PATH))
    else:
        args.func(args)