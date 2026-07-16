async function load() {
  try {
    const health = await (await fetch("/health")).json();
    const pill = document.getElementById("statusPill");
    pill.textContent = health.mongo_connected
      ? "● online (db connected)"
      : "● online (in-memory only)";
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

    data.datasets.forEach((ds) => {
      const mb = (ds.file_size_bytes / (1024 * 1024)).toFixed(2);
      const statusClass = ds.parse_status === "ok" ? "success" : "error";
      rows.innerHTML += `
        <tr>
          <td><a href="/datasets/${ds.dataset_id}/view">${ds.original_filename}</a></td>
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
    document.getElementById("statusPill").textContent =
      "● error loading status";
    document.getElementById("statusPill").className = "status bad";
  }
}

async function removeDataset(id) {
  if (!confirm("Delete this dataset? This removes the file and its record."))
    return;
  await fetch(`/datasets/${id}`, { method: "DELETE" });
  load();
}

document.getElementById("deleteAllBtn").addEventListener("click", async () => {
  if (!confirm("Delete ALL datasets? This cannot be undone.")) return;
  await fetch("/datasets?confirm=true", { method: "DELETE" });
  load();
});

document.getElementById("searchBox").addEventListener("input", () => load());
document
  .getElementById("formatFilter")
  .addEventListener("change", () => load());

load();
setInterval(load, 10000);
