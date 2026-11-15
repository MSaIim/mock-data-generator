"use strict";

// =================================================
// ADMIN PAGE: POSTGRES / REDSHIFT DATABASES (depends only on core.js)
// =================================================

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
      $("databases").appendChild(
        tableRow(
          [db.name, db.engine, status],
          [
            ["Refresh", "secondary", () => refreshDatabase(db.name)],
            ["Delete", "danger", () => deleteDatabase(db.name)],
          ],
        ),
      );
    }
  } catch (e) {
    $("db-status").textContent = e.message;
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
  $("db-info").hidden = true;
  try {
    const res = await api("refresh_database", { name });
    $("db-status").textContent = `${name}: ${res.status}`;
    if (res.connection) {
      renderKv($("db-info"), res.connection);
      $("db-info").hidden = false;
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
