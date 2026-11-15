"use strict";

// =================================================
// DATASETS MODAL: every dataset with metadata, click one for a preview (depends only on core.js)
// =================================================

const SOURCE_ORDER = ["postgres", "redshift", "dynamodb", "s3", "glue"];

let catalogData = { datasets: [], errors: [] };
let selectedKey = null;
let sampleRequest = 0;

// Stable id for a dataset
function datasetKey(d) {
  return `${d.source}|${d.location}`;
}

// One-line summary under a dataset name
function datasetMeta(d) {
  const parts = [];
  if (d.format) {
    parts.push(d.format.toUpperCase());
  }
  if (d.columns) {
    parts.push(`${d.columns.length} columns`);
  }
  if (d.rows !== undefined && d.rows !== null) {
    parts.push(`${d.rows_estimate ? "≈" : ""}${d.rows.toLocaleString()} rows`);
  }
  if (d.files) {
    parts.push(`${d.files} file${d.files === 1 ? "" : "s"}`);
  }
  if (d.size !== undefined && d.size !== null) {
    parts.push(formatBytes(d.size));
  }
  return parts.join(" · ");
}

// Open the modal and load the catalog
function openDatasets() {
  $("datasets-modal").hidden = false;
  document.body.classList.add("modal-open");
  $("ds-search").focus();
  loadCatalog();
}

function closeDatasets() {
  $("datasets-modal").hidden = true;
  document.body.classList.remove("modal-open");
}

async function loadCatalog() {
  $("ds-list").replaceChildren(el("p", "ds-empty", "Loading datasets…"));
  try {
    catalogData = await api("catalog");
  } catch (e) {
    catalogData = { datasets: [], errors: [e.message] };
  }
  renderCatalog();
  // Keep the selection if it still exists, else show the empty detail state
  const selected = catalogData.datasets.find((d) => datasetKey(d) === selectedKey);
  if (selected) {
    showDataset(selected);
  } else {
    showEmptyDetail();
  }
}

// Grouped, filtered list on the left
function renderCatalog() {
  const list = $("ds-list");
  list.replaceChildren();
  for (const message of catalogData.errors) {
    list.appendChild(el("p", "notice warn", message));
  }
  const query = $("ds-search").value.trim().toLowerCase();
  const matches = catalogData.datasets.filter((d) => !query || `${d.name} ${d.location}`.toLowerCase().includes(query));
  if (!matches.length) {
    list.appendChild(
      el("p", "ds-empty", catalogData.datasets.length ? "No datasets match the filter." : "No datasets yet. Generate one to see it here."),
    );
    return;
  }
  for (const source of SOURCE_ORDER) {
    const group = matches.filter((d) => d.source === source).sort((a, b) => a.location.localeCompare(b.location));
    if (!group.length) {
      continue;
    }
    const head = el("div", "ds-group");
    head.innerHTML = icon(source);
    head.appendChild(el("span", "", `${SOURCE_LABELS[source]} (${group.length})`));
    list.appendChild(head);
    for (const d of group) {
      const item = el("button", "ds-item" + (datasetKey(d) === selectedKey ? " selected" : ""));
      item.type = "button";
      item.appendChild(el("strong", "", d.name));
      item.appendChild(el("span", "loc", d.location));
      item.appendChild(el("span", "meta", datasetMeta(d)));
      item.onclick = () => showDataset(d);
      list.appendChild(item);
    }
  }
}

function showEmptyDetail() {
  selectedKey = null;
  $("datasets-dialog").classList.remove("detail-open");
  $("ds-detail").replaceChildren(el("p", "ds-empty", "Select a dataset to see its details and a preview."));
}

// Details and preview on the right (replaces the list on small screens)
async function showDataset(d) {
  selectedKey = datasetKey(d);
  $("datasets-dialog").classList.add("detail-open");
  renderCatalog();

  const detail = $("ds-detail");
  detail.replaceChildren();

  // Back button (small screens) and title
  const back = el("button", "btn ghost small ds-back");
  back.innerHTML = icon("back");
  back.appendChild(document.createTextNode("All datasets"));
  back.onclick = showEmptyDetail;
  detail.appendChild(back);
  const head = el("div", "ds-detail-head");
  head.appendChild(el("h3", "", d.name));
  head.appendChild(el("span", "badge accent", SOURCE_LABELS[d.source]));
  if (d.format) {
    head.appendChild(el("span", "badge", d.format.toUpperCase()));
  }

  // Delete actions (the only place datasets can be deleted)
  const actions = el("div", "ds-actions");
  const remove = el("button", "btn danger small", "Delete");
  remove.type = "button";
  remove.onclick = () => deleteDataset(d);
  actions.appendChild(remove);
  if (d.source === "s3") {
    const removeBucket = el("button", "btn danger small", "Delete bucket");
    removeBucket.type = "button";
    removeBucket.onclick = () => deleteBucket(d.dest.bucket);
    actions.appendChild(removeBucket);
  }
  head.appendChild(actions);
  detail.appendChild(head);
  detail.appendChild(el("p", "error-text ds-error"));

  // Metadata
  const info = { location: d.location };
  if (d.columns) {
    info.columns = d.columns.length;
  }
  if (d.rows !== undefined && d.rows !== null) {
    info.rows = `${d.rows_estimate ? "≈ " : ""}${d.rows.toLocaleString()}`;
  }
  if (d.files) {
    info.part_files = d.files;
  }
  if (d.size !== undefined && d.size !== null) {
    info.size = formatBytes(d.size);
  }
  if (d.updated) {
    info.updated = new Date(d.updated).toLocaleString();
  }
  const kv = el("dl", "kv");
  renderKv(kv, info);
  detail.appendChild(kv);

  // Preview (ignore late responses for a dataset that's no longer selected)
  const previewTitle = el("h3", "", "Preview");
  detail.appendChild(previewTitle);
  const preview = el("div");
  preview.appendChild(el("p", "help", "Loading preview…"));
  detail.appendChild(preview);
  const request = ++sampleRequest;
  try {
    const res = await api("sample", { source: d.source, ...d.dest });
    if (request !== sampleRequest) {
      return;
    }
    preview.replaceChildren();
    if (res.row_count !== null && res.row_count !== undefined) {
      previewTitle.textContent = `Preview · ${res.row_count.toLocaleString()} rows total`;
    }
    // Column types: from the catalog when the source knows them, otherwise from the file (Parquet/Avro)
    const types = { ...(res.types || {}) };
    for (const c of d.columns || []) {
      types[c.name] = c.type;
    }
    preview.appendChild(res.rows.length ? buildTable(res.columns, res.rows, types) : el("p", "help", "This dataset is empty."));
  } catch (e) {
    if (request !== sampleRequest) {
      return;
    }
    preview.replaceChildren(el("p", "error-text", e.message));
  }
}

// =================================================
// DELETING
// =================================================

// Show an error under the selected dataset's title
function showDeleteError(message) {
  const target = document.querySelector("#ds-detail .ds-error");
  if (target) {
    target.textContent = message;
  }
}

// After a delete: reload the list and let the generator refresh what it shows
async function afterDelete() {
  selectedKey = null;
  await loadCatalog();
  document.dispatchEvent(new Event("mockgen:datasets-changed"));
}

async function deleteDataset(d) {
  if (!confirm(`Delete ${d.location}? This can't be undone.`)) {
    return;
  }
  try {
    await api("delete_table", { source: d.source, ...d.dest });
    await afterDelete();
  } catch (e) {
    showDeleteError(e.message);
  }
}

async function deleteBucket(bucket) {
  if (!confirm(`Delete bucket ${bucket} and every dataset in it? This can't be undone.`)) {
    return;
  }
  try {
    await api("delete_bucket", { bucket });
    await afterDelete();
  } catch (e) {
    showDeleteError(e.message);
  }
}

// =================================================
// WIRING
// =================================================

$("ds-close").innerHTML = icon("x");
$("ds-refresh").innerHTML = icon("refresh");
$("ds-close").onclick = closeDatasets;
$("ds-refresh").onclick = loadCatalog;
$("ds-search").oninput = renderCatalog;
$("datasets-modal").onclick = (event) => {
  if (event.target === $("datasets-modal")) {
    closeDatasets();
  }
};
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("datasets-modal").hidden) {
    closeDatasets();
  }
});
