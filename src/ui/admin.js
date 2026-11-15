"use strict";

// =================================================
// ADMIN PAGE: POSTGRES / REDSHIFT DATABASES (depends only on core.js)
// =================================================

// Names of the database rows that are expanded (kept across reloads)
const expandedDatabases = new Set();

// Load the admin page
function openAdmin() {
  loadDatabases();
}

// Build a table row with text cells and action buttons
function tableRow(cells, buttons) {
  const tr = el("tr");
  for (const text of cells) {
    tr.appendChild(el("td", "", text));
  }
  const td = el("td", "actions");
  for (const [label, cls, onClick] of buttons) {
    const btn = el("button", `btn small ${cls}`, label);
    btn.onclick = onClick;
    td.appendChild(btn);
  }
  tr.appendChild(td);
  return tr;
}

async function loadDatabases() {
  try {
    const res = await api("databases");
    $("databases").replaceChildren();
    for (const db of res.databases) {
      const status = db.error ? `${db.status}: ${db.error}` : db.status;
      const row = tableRow(
        [db.name, db.engine, status],
        [
          ["Refresh", "secondary", () => refreshDatabase(db.name)],
          ["Delete", "danger", () => deleteDatabase(db.name)],
        ],
      );
      row.classList.add("expandable");

      // Hidden details row under it, filled when expanded
      const detail = el("tr", "db-detail");
      const cell = el("td");
      cell.colSpan = 4;
      detail.appendChild(cell);
      detail.hidden = true;

      // Clicking the row (not its buttons) toggles the details
      row.onclick = (e) => {
        if (e.target.closest("button")) {
          return;
        }
        if (expandedDatabases.has(db.name)) {
          expandedDatabases.delete(db.name);
          row.classList.remove("open");
          detail.hidden = true;
        } else {
          expandedDatabases.add(db.name);
          showDetails(db.name, row, detail, cell);
        }
      };
      $("databases").append(row, detail);
      if (expandedDatabases.has(db.name)) {
        showDetails(db.name, row, detail, cell);
      }
    }
  } catch (e) {
    $("db-status").textContent = e.message;
  }
}

// Expand a database row: check its current state and show its details and credentials
async function showDetails(name, row, detail, cell) {
  row.classList.add("open");
  detail.hidden = false;
  cell.replaceChildren(el("p", "help", "Loading…"));
  try {
    const res = await api("refresh_database", { name });
    // A finished deletion: the database is gone
    if (res.status === "deleted") {
      expandedDatabases.delete(name);
      await loadDatabases();
      return;
    }
    row.children[2].textContent = res.error ? `${res.status}: ${res.error}` : res.status;
    const info = { engine: res.engine, status: res.status, created: new Date(res.created_at * 1000).toLocaleString() };
    if (res.error) {
      info.error = res.error;
    }
    const kv = el("dl", "kv");
    renderKv(kv, { ...info, ...(res.connection || {}) });
    cell.replaceChildren(kv);
    if (!res.connection) {
      cell.appendChild(el("p", "help", "Credentials appear here once the database is available."));
    }
  } catch (e) {
    cell.replaceChildren(el("p", "error-text", e.message));
  }
}

async function createDatabase() {
  $("create-db-btn").disabled = true;
  $("db-status").textContent = "Starting…";
  try {
    await api("create_database", {
      name: $("db-name").value.trim(),
      engine: $("db-engine").value,
    });
    $("db-status").textContent = "Creating. Press Refresh in a few minutes.";
    $("db-name").value = "";
  } catch (e) {
    $("db-status").textContent = e.message;
  } finally {
    $("create-db-btn").disabled = false;
    loadDatabases();
  }
}

async function refreshDatabase(name) {
  $("db-status").textContent = `Checking ${name}…`;
  try {
    const res = await api("refresh_database", { name });
    $("db-status").textContent = `${name}: ${res.status}`;
    // Show the credentials in the row's details once it's up
    if (res.connection) {
      expandedDatabases.add(name);
    }
    await loadDatabases();
  } catch (e) {
    $("db-status").textContent = e.message;
  }
}

async function deleteDatabase(name) {
  if (!confirm(`Delete database ${name} and all its tables? This can't be undone.`)) {
    return;
  }
  try {
    await api("delete_database", { name });
    $("db-status").textContent = `Deleting ${name}. Press Refresh until it disappears.`;
    await loadDatabases();
  } catch (e) {
    $("db-status").textContent = e.message;
  }
}

$("create-db-btn").onclick = createDatabase;
