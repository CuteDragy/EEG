const box = document.getElementById("uploadBox");
const fileInput = document.getElementById("fileInput");
const pickBtn = document.getElementById("pickBtn");
const submitBtn = document.getElementById("submitBtn");
const resultBox = document.getElementById("uploadResult");
const fileNameLabel = document.getElementById("fileNameLabel");

let selectedFile = null;

function setFile(file) {
  selectedFile = file;
  fileNameLabel.textContent = file ? file.name : "No file selected";
  submitBtn.disabled = !file;
}

pickBtn.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => setFile(fileInput.files[0] || null));

// drag & drop
["dragenter", "dragover"].forEach(evt =>
  box.addEventListener(evt, e => {
    e.preventDefault();
    box.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach(evt =>
  box.addEventListener(evt, e => {
    e.preventDefault();
    box.classList.remove("dragover");
  })
);
box.addEventListener("drop", e => {
  const file = e.dataTransfer.files[0];
  if (file) setFile(file);
});

submitBtn.addEventListener("click", async () => {
  if (!selectedFile) return;

  submitBtn.disabled = true;
  submitBtn.textContent = "Uploading…";
  resultBox.className = "";
  resultBox.style.display = "none";

  const formData = new FormData();
  formData.append("file", selectedFile);

  try {
    const res = await fetch("/upload", { method: "POST", body: formData });
    const data = await res.json();

    if (res.ok) {
      resultBox.className = "success";
      resultBox.textContent =
        `Uploaded "${data.original_filename}" (${data.extension.toUpperCase()}, ` +
        `${(data.file_size_bytes / (1024 * 1024)).toFixed(2)} MB)\n` +
        `Parse status: ${data.parse_status}` +
        (data.parse_error ? `\nParse error: ${data.parse_error}` : "");
    } else {
      resultBox.className = "error";
      resultBox.textContent = data.error || "Upload failed.";
    }
  } catch (e) {
    resultBox.className = "error";
    resultBox.textContent = "Network error: " + e.message;
  }

  resultBox.style.display = "block";
  submitBtn.disabled = false;
  submitBtn.textContent = "Upload";
  setFile(null);
  fileInput.value = "";
});
