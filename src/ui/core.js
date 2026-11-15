"use strict";

// =================================================
// SHARED HELPERS (used by admin.js, datasets.js and wizard.js; depends on nothing)
// =================================================

const $ = (id) => document.getElementById(id);

const SOURCE_LABELS = { postgres: "PostgreSQL", redshift: "Redshift", dynamodb: "DynamoDB", s3: "S3", glue: "Glue" };

// Stroke icons (24x24)
const ICONS = {
  postgres: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  redshift: '<path d="M3 21V9l9-6 9 6v12"/><path d="M7 21v-6h10v6"/><path d="M7 11h10"/>',
  dynamodb: '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M13 6l-5 7h4l-1 5 5-7h-4z"/>',
  s3: '<path d="M4 6l2 14h12l2-14"/><ellipse cx="12" cy="6" rx="8" ry="2.5"/>',
  glue: '<path d="M4 5a2 2 0 0 1 2-2h12v18H6a2 2 0 0 1-2-2z"/><path d="M8 8h6M8 12h6M8 16h3"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  swap: '<path d="M4 8h13l-3-3M20 16H7l3 3"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  datasets: '<path d="M12 3l9 4.5-9 4.5-9-4.5z"/><path d="M3 12l9 4.5 9-4.5"/><path d="M3 16.5L12 21l9-4.5"/>',
  admin: '<path d="M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.7L20 8.6"/><path d="M20 4v4.6h-4.6"/>',
  sparkles:
    '<path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
  back: '<path d="M15 6l-6 6 6 6"/>',
  next: '<path d="M9 6l6 6-6 6"/>',
};

// Build an <svg> icon
function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24">${ICONS[name]}</svg>`;
}

// Create an element with optional class and text
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

// Call the API; throws with the server's error message
async function api(action, data = {}) {
  const res = await fetch("/api", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...data }),
  });
  const body = await res.json().catch(() => ({ error: `${res.status} ${res.statusText}` }));
  if (!res.ok) {
    throw new Error(body.error || res.statusText);
  }
  return body;
}

// Run fn after the calls stop for ms milliseconds
function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// Human-readable byte size
function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) {
    return "—";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

// Build a numbered data table inside its own horizontally scrolling box; types (name -> type) show next to headers
function buildTable(names, rows, types = {}) {
  const box = el("div", "preview-wrap");
  const table = el("table", "preview");
  const head = el("tr");
  head.appendChild(el("th", "row-num", "#"));
  for (const name of names) {
    const th = el("th", "", name);
    if (types[name]) {
      th.appendChild(el("span", "th-type", types[name]));
    }
    head.appendChild(th);
  }
  table.appendChild(el("thead")).appendChild(head);
  const body = el("tbody");
  rows.forEach((row, index) => {
    const tr = el("tr");
    tr.appendChild(el("td", "row-num", String(index + 1)));
    for (const name of names) {
      const value = row[name];
      const td =
        value === null || value === undefined
          ? el("td", "null", "NULL")
          : el("td", "", typeof value === "object" ? JSON.stringify(value) : String(value));
      td.title = td.textContent;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  });
  table.appendChild(body);
  box.appendChild(table);
  return box;
}

// Render key/value pairs with copy buttons
function renderKv(dl, info) {
  dl.replaceChildren();
  for (const [key, value] of Object.entries(info)) {
    dl.appendChild(el("dt", "", key.replace(/_/g, " ")));
    const dd = el("dd");
    dd.appendChild(el("code", "", String(value)));
    const btn = el("button", "copy");
    btn.title = "Copy";
    btn.innerHTML = icon("copy");
    btn.onclick = () => {
      try {
        navigator.clipboard.writeText(String(value));
      } catch (e) {
        /* clipboard blocked */
      }
    };
    dd.appendChild(btn);
    dl.appendChild(dd);
  }
}
