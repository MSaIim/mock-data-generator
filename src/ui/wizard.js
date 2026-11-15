"use strict";

// =================================================
// GENERATOR WIZARD (depends on core.js, admin.js and datasets.js)
// =================================================

// =================================================
// CONSTANTS
// =================================================

const SQL_SOURCES = ["postgres", "redshift"];
const FILE_SOURCES = ["s3", "glue"];
const TYPED_FORMATS = ["parquet", "avro"];
const NAME_RE = /^[a-z][a-z0-9_]{0,62}$/;
const BUCKET_RE = /^[a-z0-9][a-z0-9-]{2,30}$/;
const PREFIX_RE = /^[a-z0-9][a-z0-9_-]{0,62}(\/[a-z0-9][a-z0-9_-]{0,62}){0,9}$/;
const MAX_ROWS = 100000;
const MAX_CELLS = 1000000;
const MAX_BATCHES = 1000;
const FORMAT_LABELS = { csv: "CSV", json: "JSON", parquet: "Parquet", avro: "Avro" };
const STEP_NAMES = ["Destination", "Columns", "Generate"];

const SOURCES = {
  postgres: { desc: "Tables in an Aurora PostgreSQL database", fields: ["database", "schema", "table"] },
  redshift: { desc: "Tables in a Redshift Serverless warehouse", fields: ["database", "schema", "table"] },
  dynamodb: { desc: "A key-value table, one item per row", fields: ["table"] },
  s3: { desc: "Files in a bucket, written as part files", fields: ["bucket", "prefix", "table", "format"] },
  glue: { desc: "Catalog table over S3 files, query it in Athena", fields: ["glueDatabase", "bucket", "prefix", "table", "format"] },
};

// Column names the auto-generator uses for each type
const AUTO_NAMES = {
  string: "code",
  text: "notes",
  integer: "quantity",
  float: "amount",
  boolean: "is_active",
  date: "signup_date",
  timestamp: "updated_at",
  uuid: "ref_id",
  name: "full_name",
  email: "email",
  phone: "phone",
  address: "address",
  city: "city",
  country: "country",
  company: "company",
  url: "website",
  ipv4: "ip_address",
};

// =================================================
// STATE
// =================================================

const state = {
  me: null,
  step: 1,
  source: null,
  sources: { databases: [], buckets: [], glue_databases: [], types: [], formats: [] },
  browse: {},
  datasets: {},
  dest: { database: "", glueDatabase: "", schema: "public", bucket: "", prefix: "", table: "", format: "csv" },
  columns: [
    { name: "id", type: "uuid", null_pct: 0, junk_pct: 0 },
    { name: "full_name", type: "name", null_pct: 5, junk_pct: 5 },
    { name: "email", type: "email", null_pct: 5, junk_pct: 10 },
  ],
  rows: 1000,
  batching: false,
  batchSize: 250,
  preview: null,
  previewError: "",
  result: null,
  running: false,
  footError: "",
};

// =================================================
// HELPERS
// =================================================

// Fill a <datalist> with options
function setOptions(id, values) {
  $(id).replaceChildren(...values.map((v) => new Option(v, v)));
}

// Whether the destination accepts wrong-type junk (must match the server rule)
function allowsWrongTypes() {
  return state.source === "dynamodb" || (state.source === "s3" && !TYPED_FORMATS.includes(state.dest.format));
}

// Batch count for the current settings
function batchCount() {
  const size = state.batching ? state.batchSize : state.rows;
  return size > 0 ? Math.ceil(state.rows / size) : 0;
}

function randomInt(low, high) {
  return low + Math.floor(Math.random() * (high - low + 1));
}

// =================================================
// THEME
// =================================================

// The theme in effect right now
function currentTheme() {
  const set = document.documentElement.dataset.theme;
  if (set) {
    return set;
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function renderThemeButton() {
  $("theme-btn").innerHTML = icon(currentTheme() === "dark" ? "sun" : "moon");
}

function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("mockgen-theme", next);
  } catch (e) {
    /* storage blocked */
  }
  renderThemeButton();
}

// =================================================
// LOGIN AND VIEWS
// =================================================

let setupMode = false;

// Show sign-in, or one-time admin setup on first run
async function showLogin() {
  for (const id of ["wizard-view", "admin-view", "stepper", "footbar", "datasets-btn", "admin-btn", "logout-btn"]) {
    $(id).hidden = true;
  }
  closeDatasets();
  $("whoami").textContent = "";
  $("login-view").hidden = false;
  try {
    const status = await api("status");
    setupMode = status.setup_needed;
    $("local-badge").hidden = !status.local;
  } catch (e) {
    $("login-error").textContent = e.message;
  }
  $("login-title").textContent = setupMode ? "Create admin account" : "Sign in";
  $("login-btn").textContent = setupMode ? "Create admin" : "Sign in";
  $("setup-hint").hidden = !setupMode;
}

async function submitLogin(event) {
  event.preventDefault();
  $("login-error").textContent = "";
  try {
    const me = await api(setupMode ? "setup" : "login", { username: $("login-user").value.trim(), password: $("login-pass").value });
    $("login-pass").value = "";
    showApp(me);
  } catch (e) {
    $("login-error").textContent = e.message;
  }
}

async function showApp(me) {
  state.me = me;
  $("login-view").hidden = true;
  $("logout-btn").hidden = false;
  $("datasets-btn").hidden = false;
  $("admin-btn").hidden = me.role !== "admin";
  $("whoami").textContent = me.username;
  if (me.local !== undefined) {
    $("local-badge").hidden = !me.local;
  }
  await loadSources();
  renderSourceCards();
  renderFormatButtons();
  renderColumns();
  showView("wizard");
  setStep(state.step);
  schedulePreview();
}

// Switch between the generator and the admin page
function showView(view) {
  const wizard = view === "wizard";
  $("wizard-view").hidden = !wizard;
  $("stepper").hidden = !wizard;
  $("footbar").hidden = !wizard;
  $("admin-view").hidden = wizard;
  $("admin-btn").querySelector(".label").textContent = wizard ? "Admin" : "Generator";
  if (!wizard) {
    openAdmin(state.me.username);
    return;
  }
  // Coming back from admin: databases may have become ready or been deleted
  loadSources().then(() => {
    if (state.source) {
      renderFields();
      refreshBrowse();
    }
  });
}

async function loadSources() {
  state.sources = await api("sources");
}

// =================================================
// STEPPER AND FOOTER
// =================================================

// Validate one page; shows field errors and returns the message for the footer ("" when valid)
function pageError(n) {
  if (n === 1) {
    const errors = destErrors();
    showDestErrors(true);
    if (errors.source) {
      return "Pick a data source first.";
    }
    return Object.keys(errors).length ? "Fix the highlighted fields to continue." : "";
  }
  if (n === 2) {
    const errors = columnErrors();
    renderColumnErrors(errors);
    return errors[0] || "";
  }
  return generateError();
}

// Go to a step. Going forward validates every page in between and stops at the first problem
function goTo(n) {
  for (let s = state.step; s < n; s += 1) {
    const message = pageError(s);
    if (message) {
      if (s !== state.step) {
        setStep(s);
      }
      state.footError = message;
      renderFooter();
      return;
    }
  }
  setStep(n);
}

function setStep(n) {
  state.step = n;
  state.footError = "";
  for (const page of document.querySelectorAll("[data-page]")) {
    page.hidden = Number(page.dataset.page) !== n;
  }
  for (const btn of document.querySelectorAll(".step")) {
    const s = Number(btn.dataset.step);
    btn.classList.toggle("done", s < n);
    btn.classList.toggle("current", s === n);
  }
  $("stepper-fill").style.width = `${(n - 1) * 50}%`;
  if (n === 3) {
    $("rows").value = state.rows;
    $("batching").checked = state.batching;
    $("batch-size").value = state.batchSize;
    renderGenerate();
  }
  renderFooter();
  window.scrollTo({ top: 0 });
}

// Footer buttons and message for the current step
function renderFooter() {
  const n = state.step;
  $("foot-back").disabled = n === 1;
  $("foot-new").hidden = !(n === 3 && state.result);

  const next = $("foot-next");
  if (n < 3) {
    next.innerHTML = `Next: ${STEP_NAMES[n]}${icon("next")}`;
    next.disabled = false;
  } else {
    next.textContent = state.running ? "Generating…" : "Generate";
    next.disabled = state.running || Boolean(generateError());
  }

  // Message: an error if there is one, otherwise a short hint
  let message = state.footError;
  if (n === 3 && !message) {
    message = generateError();
  }
  const msg = $("foot-msg");
  msg.classList.toggle("error", Boolean(message));
  if (!message) {
    if (n === 1) {
      message = state.source ? `Step 1 of 3 · ${SOURCE_LABELS[state.source]}` : "Step 1 of 3 · pick a data source";
    }
    if (n === 2) {
      message = `Step 2 of 3 · ${state.columns.length} column${state.columns.length === 1 ? "" : "s"}`;
    }
    if (n === 3) {
      message = state.result ? "Done. Generate again or start a new dataset." : `Step 3 of 3 · ready to write ${state.rows.toLocaleString()} rows`;
    }
  }
  msg.textContent = message;
}

// Clear a footer error once the problem is fixed
function refreshFootError() {
  if (state.footError && !pageError(state.step)) {
    state.footError = "";
  }
  renderFooter();
}

// =================================================
// PAGE 1: DESTINATION
// =================================================

function renderSourceCards() {
  const wrap = $("source-cards");
  wrap.replaceChildren();
  for (const [key, info] of Object.entries(SOURCES)) {
    const card = el("button", "source-card" + (state.source === key ? " selected" : ""));
    card.type = "button";
    card.innerHTML = `<div class="icon-wrap">${icon(key)}</div><strong></strong><span></span>`;
    card.querySelector("strong").textContent = SOURCE_LABELS[key];
    card.querySelector("span").textContent = info.desc;
    card.onclick = () => selectSource(key);
    wrap.appendChild(card);
  }
}

function renderFormatButtons() {
  const wrap = $("format-buttons");
  wrap.replaceChildren();
  for (const fmt of state.sources.formats) {
    const btn = el("button", state.dest.format === fmt ? "active" : "", FORMAT_LABELS[fmt]);
    btn.type = "button";
    btn.onclick = () => {
      state.dest.format = fmt;
      renderFormatButtons();
      onDestChange();
      schedulePreview();
    };
    wrap.appendChild(btn);
  }
  $("format-help").textContent = TYPED_FORMATS.includes(state.dest.format)
    ? "Typed format: junk values keep the column's type."
    : state.source === "glue"
      ? "Glue tables are typed: junk values keep the column's type."
      : "Untyped format: junk can include values of the wrong type.";
}

function selectSource(key) {
  state.source = key;
  state.result = null;
  // Default to the first ready database of this engine
  if (SQL_SOURCES.includes(key)) {
    const dbs = state.sources.databases.filter((d) => d.engine === key);
    if (!dbs.some((d) => d.name === state.dest.database)) {
      state.dest.database = dbs.length ? dbs[0].name : "";
    }
  }
  renderSourceCards();
  renderFields();
  renderFormatButtons();
  refreshBrowse();
  refreshDatasets();
  schedulePreview();
  refreshFootError();
}

// Show only the fields that apply to the source, filled from state
function renderFields() {
  const info = SOURCES[state.source];
  $("dest-fields").hidden = !info;
  if (!info) {
    return;
  }
  for (const field of document.querySelectorAll("#dest-fields [data-field]")) {
    field.hidden = !info.fields.includes(field.dataset.field);
  }
  $("table-label").textContent = state.source === "s3" ? "Dataset name" : "Table";

  // Database select (admin-created only)
  const select = $("f-database");
  select.replaceChildren();
  const dbs = state.sources.databases.filter((d) => d.engine === state.source);
  if (!dbs.length) {
    select.add(new Option("No ready databases — ask an admin", ""));
  }
  for (const db of dbs) {
    select.add(new Option(db.name, db.name, false, db.name === state.dest.database));
  }
  select.disabled = !dbs.length;
  if (dbs.length && !dbs.some((d) => d.name === state.dest.database)) {
    state.dest.database = dbs[0].name;
  }

  for (const name of ["glueDatabase", "schema", "bucket", "prefix", "table"]) {
    $(`f-${name}`).value = state.dest[name];
  }
  setOptions("l-bucket", state.sources.buckets);
  setOptions("l-glueDatabase", state.sources.glue_databases);
  updateDatalists();
  showDestErrors(false);
}

// What exists in the selected database / Glue database / DynamoDB
async function refreshBrowse() {
  const s = state.source;
  const d = state.dest;
  state.browse = {};
  try {
    if (SQL_SOURCES.includes(s) && d.database) {
      state.browse = await api("browse", { source: s, database: d.database });
    }
    if (s === "dynamodb") {
      state.browse = await api("browse", { source: s });
    }
    if (s === "glue" && state.sources.glue_databases.includes(d.glueDatabase)) {
      state.browse = await api("browse", { source: s, database: d.glueDatabase });
    }
  } catch (e) {
    state.footError = e.message;
    renderFooter();
  }
  updateDatalists();
}

// What datasets exist in the selected bucket
async function refreshDatasets() {
  state.datasets = {};
  if (FILE_SOURCES.includes(state.source) && bucketExists()) {
    try {
      state.datasets = (await api("browse", { source: "s3", bucket: state.dest.bucket })).datasets;
    } catch (e) {
      state.footError = e.message;
      renderFooter();
    }
  }
  updateDatalists();
}

// Tables at the currently selected location
function knownTables() {
  if (SQL_SOURCES.includes(state.source)) {
    return (state.browse.schemas || {})[state.dest.schema] || [];
  }
  if (state.source === "s3") {
    return state.datasets[state.dest.prefix] || [];
  }
  return state.browse.tables || [];
}

function tableExists() {
  return knownTables().includes(state.dest.table);
}

function bucketExists() {
  return state.sources.buckets.includes(state.dest.bucket);
}

// Refresh the dropdown suggestions, existence badges and the panel
function updateDatalists() {
  const schemas = Object.keys(state.browse.schemas || {});
  if (!schemas.includes("public")) {
    schemas.unshift("public");
  }
  setOptions("l-schema", schemas);
  setOptions(
    "l-prefix",
    Object.keys(state.datasets).filter((p) => p),
  );
  setOptions("l-table", knownTables());

  // Badges: new vs existing
  const badge = (value, exists, existsText) =>
    value ? `<span class="badge ${exists ? "replace" : "new"}">${exists ? existsText : "New"}</span>` : "";
  $("table-badge").innerHTML = badge(state.dest.table, tableExists(), "Exists · will be replaced");
  $("bucket-badge").innerHTML = badge(state.dest.bucket, bucketExists(), "Exists");
  renderPanel();
}

// Problems with the destination fields, keyed by field
function destErrors() {
  const errors = {};
  if (!state.source) {
    errors.source = "Pick a data source";
    return errors;
  }
  const d = state.dest;
  const fields = SOURCES[state.source].fields;
  if (fields.includes("database") && !d.database) {
    errors.database = "No database selected";
  }
  if (fields.includes("glueDatabase") && !NAME_RE.test(d.glueDatabase)) {
    errors.glueDatabase = "Lowercase letters, digits and _; starts with a letter";
  }
  if (fields.includes("schema") && !NAME_RE.test(d.schema)) {
    errors.schema = "Lowercase letters, digits and _; starts with a letter";
  }
  if (fields.includes("bucket") && !BUCKET_RE.test(d.bucket)) {
    errors.bucket = "3–31 characters: lowercase letters, digits and -";
  }
  if (fields.includes("prefix") && d.prefix && !PREFIX_RE.test(d.prefix)) {
    errors.prefix = "Folders of lowercase letters, digits, - and _, separated by /";
  }
  if (!NAME_RE.test(d.table)) {
    errors.table = "Lowercase letters, digits and _; starts with a letter";
  }
  return errors;
}

// Show field errors (only for touched fields unless forced)
function showDestErrors(force) {
  const errors = destErrors();
  for (const name of ["database", "glueDatabase", "schema", "bucket", "prefix", "table"]) {
    const input = $(`f-${name}`);
    const show = Boolean(errors[name]) && (force || (Boolean(input.dataset.touched) && state.dest[name] !== ""));
    $(`e-${name}`).textContent = show ? errors[name] : "";
    input.classList.toggle("invalid", show);
  }
}

// Any destination edit
function onDestChange() {
  state.result = null;
  updateDatalists();
  showDestErrors(false);
  if (state.footError) {
    refreshFootError();
  } else {
    renderFooter();
  }
}

const browseSoon = debounce(refreshBrowse, 300);
const datasetsSoon = debounce(refreshDatasets, 300);

// Wire a destination input to state
function bindDestInput(name) {
  const input = $(`f-${name}`);
  input.addEventListener("input", () => {
    let value = input.value.trim();
    if (name === "prefix") {
      value = value.replace(/^\/+|\/+$/g, "");
    }
    state.dest[name] = value;
    input.dataset.touched = "1";
    if (name === "glueDatabase") {
      browseSoon();
    }
    if (name === "bucket") {
      datasetsSoon();
    }
    onDestChange();
  });
}

// Something was deleted in the datasets modal: refresh suggestions, badges and the plan
async function onDatasetsChanged() {
  await loadSources();
  if (state.source) {
    setOptions("l-bucket", state.sources.buckets);
    setOptions("l-glueDatabase", state.sources.glue_databases);
    await Promise.all([refreshBrowse(), refreshDatasets()]);
  }
  if (state.step === 3) {
    renderGenerate();
  }
}

// The destination fields the server expects for this source
function destPayload() {
  const d = state.dest;
  const payload = { table: d.table };
  if (SQL_SOURCES.includes(state.source)) {
    Object.assign(payload, { database: d.database, schema: d.schema });
  }
  if (state.source === "glue") {
    payload.database = d.glueDatabase;
  }
  if (FILE_SOURCES.includes(state.source)) {
    Object.assign(payload, { bucket: d.bucket, prefix: d.prefix, format: d.format });
  }
  return payload;
}

// Human-readable path for the panel
function destPath() {
  const d = state.dest;
  const table = d.table || "…";
  if (SQL_SOURCES.includes(state.source)) {
    return `${state.source}://${d.database || "…"}/${d.schema || "…"}.${table}`;
  }
  if (state.source === "dynamodb") {
    return `dynamodb://${table}`;
  }
  const s3 = `s3://${d.bucket || "…"}/${d.prefix ? d.prefix + "/" : ""}${table}/`;
  return state.source === "glue" ? `${d.glueDatabase || "…"}.${table}  →  ${s3}` : s3;
}

// =================================================
// PAGE 2: COLUMNS
// =================================================

function newColumn() {
  const used = new Set(state.columns.map((c) => c.name));
  let i = state.columns.length + 1;
  while (used.has(`col_${i}`)) {
    i += 1;
  }
  return { name: `col_${i}`, type: "string", null_pct: 0, junk_pct: 0 };
}

// Build the column editor rows from state
function renderColumns() {
  const wrap = $("columns");
  wrap.replaceChildren();
  state.columns.forEach((col, index) => {
    const row = el("div", "col-row");
    row.appendChild(el("span", "col-index", String(index + 1)));

    const name = el("input");
    name.value = col.name;
    name.setAttribute("aria-label", "Column name");
    name.oninput = () => {
      col.name = name.value.trim();
      onColumnsChange();
    };

    const type = el("select");
    for (const t of state.sources.types) {
      type.add(new Option(t, t, false, t === col.type));
    }
    type.setAttribute("aria-label", "Type");
    type.onchange = () => {
      col.type = type.value;
      onColumnsChange();
    };

    const pct = (key, label) => {
      const input = el("input");
      input.type = "number";
      input.min = 0;
      input.max = 100;
      input.value = col[key];
      input.setAttribute("aria-label", label);
      input.oninput = () => {
        col[key] = input.value === "" ? 0 : Number(input.value);
        onColumnsChange();
      };
      return input;
    };

    const remove = el("button", "icon-btn");
    remove.type = "button";
    remove.title = "Remove column";
    remove.innerHTML = icon("x");
    remove.disabled = state.columns.length === 1;
    remove.onclick = () => {
      state.columns.splice(index, 1);
      renderColumns();
      onColumnsChange();
    };

    row.append(name, type, pct("null_pct", "Null %"), pct("junk_pct", "Junk %"), remove);
    wrap.appendChild(row);
  });
  $("col-count").value = state.columns.length;
  renderJunkHint();
}

// Grow or shrink the column list to n
function setColumnCount(n) {
  const count = Math.max(1, Math.min(100, Math.floor(n) || 1));
  while (state.columns.length > count) {
    state.columns.pop();
  }
  while (state.columns.length < count) {
    state.columns.push(newColumn());
  }
  renderColumns();
  onColumnsChange();
}

// Random column count, types, names and junk levels
function autoColumns() {
  const count = randomInt(4, 12);
  const types = state.sources.types.filter((t) => t !== "uuid");
  const columns = [{ name: "id", type: "uuid", null_pct: 0, junk_pct: 0 }];
  const used = new Set(["id"]);
  while (columns.length < count) {
    const type = types[randomInt(0, types.length - 1)];
    const base = AUTO_NAMES[type] || type;
    let name = base;
    for (let i = 2; used.has(name); i += 1) {
      name = `${base}_${i}`;
    }
    used.add(name);
    columns.push({
      name,
      type,
      null_pct: Math.random() < 0.5 ? 0 : randomInt(1, 15),
      junk_pct: Math.random() < 0.4 ? 0 : randomInt(1, 25),
    });
  }
  state.columns = columns;
  renderColumns();
  onColumnsChange();
}

function renderJunkHint() {
  $("junk-hint").textContent = allowsWrongTypes()
    ? 'Junk here can be malformed values (bad emails, out-of-range numbers) or values of the wrong type, like "abc" in an integer column.'
    : "This destination has fixed column types, so junk keeps the type but breaks the pattern: bad emails and phones, empty strings, out-of-range numbers and dates. Boolean junk becomes null.";
}

// Problems with the columns, one message per issue
function columnErrors() {
  const errors = [];
  const seen = new Set();
  if (!state.columns.length || state.columns.length > 100) {
    errors.push("Define between 1 and 100 columns.");
  }
  state.columns.forEach((c, i) => {
    const label = c.name || `Column ${i + 1}`;
    if (!NAME_RE.test(c.name)) {
      errors.push(`${label}: name must be lowercase letters, digits and _, starting with a letter.`);
    }
    if (seen.has(c.name)) {
      errors.push(`${label} is defined twice.`);
    }
    if (state.source === "dynamodb" && c.name === "pk") {
      errors.push("pk is reserved for the DynamoDB partition key.");
    }
    for (const key of ["null_pct", "junk_pct"]) {
      if (!Number.isInteger(c[key]) || c[key] < 0 || c[key] > 100) {
        errors.push(`${label}: percentages must be whole numbers from 0 to 100.`);
      }
    }
    if (c.null_pct + c.junk_pct > 100) {
      errors.push(`${label}: null % plus junk % can't be more than 100.`);
    }
    seen.add(c.name);
  });
  return errors;
}

// Outline invalid name inputs
function renderColumnErrors(errors) {
  const rows = $("columns").children;
  state.columns.forEach((c, i) => {
    const bad = errors.some((e) => e.startsWith(`${c.name || `Column ${i + 1}`}:`) || e.startsWith(`${c.name} is defined`));
    rows[i].querySelector("input").classList.toggle("invalid", bad);
  });
}

function onColumnsChange() {
  state.result = null;
  $("col-count").value = state.columns.length;
  if (state.footError) {
    refreshFootError();
  } else {
    renderFooter();
  }
  renderPanel();
  schedulePreview();
}

// =================================================
// PREVIEW
// =================================================

const schedulePreview = debounce(loadPreview, 350);

async function loadPreview() {
  if (!state.source) {
    state.preview = null;
    state.previewError = "Pick a data source to see a preview.";
  } else if (columnErrors().length) {
    state.preview = null;
    state.previewError = "Fix the column errors to see a preview.";
  } else {
    try {
      state.preview = (await api("preview", { source: state.source, format: state.dest.format, columns: state.columns })).rows;
      state.previewError = "";
    } catch (e) {
      state.preview = null;
      state.previewError = e.message;
    }
  }
  renderJunkHint();
  const wrap = $("p-preview");
  wrap.replaceChildren(
    state.preview
      ? buildTable(
          state.columns.map((c) => c.name),
          state.preview,
        )
      : el("p", "help", state.previewError || "Loading…"),
  );
}

// =================================================
// RIGHT PANEL
// =================================================

function renderPanel() {
  // Destination
  const dest = $("p-dest");
  if (!state.source) {
    dest.className = "muted";
    dest.textContent = "Pick a data source to start.";
    $("p-status").innerHTML = "";
  } else {
    dest.className = "";
    dest.innerHTML = `<div class="dest-line">${icon(state.source)}<span></span></div><div class="path"></div>`;
    dest.querySelector(".dest-line span").textContent =
      SOURCE_LABELS[state.source] + (FILE_SOURCES.includes(state.source) ? ` · ${FORMAT_LABELS[state.dest.format]}` : "");
    dest.querySelector(".path").textContent = destPath();
    $("p-status").innerHTML = state.dest.table
      ? `<span class="badge ${tableExists() ? "replace" : "new"}">${tableExists() ? "Replace" : "New"}</span>`
      : "";
  }

  // Columns: aligned name / type / null / junk table
  $("p-col-count").textContent = String(state.columns.length);
  const summary = $("p-columns");
  summary.replaceChildren();
  const head = el("div", "cs-row head");
  for (const text of ["#", "Name", "Type", "Null", "Junk"]) {
    head.appendChild(el("span", text === "Null" || text === "Junk" ? "cs-num" : text === "#" ? "cs-index" : "", text));
  }
  summary.appendChild(head);
  state.columns.forEach((c, index) => {
    const row = el("div", "cs-row");
    row.appendChild(el("span", "cs-index", String(index + 1)));
    const name = el("span", "cs-name", c.name || "?");
    name.title = c.name;
    row.appendChild(name);
    row.appendChild(el("span", "cs-type", c.type));
    row.appendChild(el("span", "cs-num" + (c.null_pct ? " on" : ""), c.null_pct ? `${c.null_pct}%` : "—"));
    row.appendChild(el("span", "cs-num" + (c.junk_pct ? " on" : ""), c.junk_pct ? `${c.junk_pct}%` : "—"));
    summary.appendChild(row);
  });

  // Generation settings
  const batches = batchCount();
  const gen = {
    rows: state.rows.toLocaleString(),
    batches: state.batching ? `${batches.toLocaleString()} × ${state.batchSize.toLocaleString()} rows` : "single batch",
  };
  if (FILE_SOURCES.includes(state.source)) {
    gen.part_files = batches.toLocaleString();
  }
  if (state.result) {
    gen.last_run = `${state.result.rows.toLocaleString()} rows written`;
  }
  const dl = $("p-gen");
  dl.replaceChildren();
  for (const [k, v] of Object.entries(gen)) {
    dl.appendChild(el("dt", "", k.replace(/_/g, " ")));
    dl.appendChild(el("dd", "", v));
  }
}

// =================================================
// PAGE 3: GENERATE
// =================================================

// Problems with the generation settings
function generateError() {
  if (!Number.isInteger(state.rows) || state.rows < 1 || state.rows > MAX_ROWS) {
    return `Rows must be a whole number from 1 to ${MAX_ROWS.toLocaleString()}.`;
  }
  if (state.rows * state.columns.length > MAX_CELLS) {
    return `Rows × columns can't be more than ${MAX_CELLS.toLocaleString()}.`;
  }
  if (state.batching) {
    if (!Number.isInteger(state.batchSize) || state.batchSize < 1 || state.batchSize > state.rows) {
      return "Rows per batch must be between 1 and the row count.";
    }
    if (batchCount() > MAX_BATCHES) {
      return `That's ${batchCount().toLocaleString()} batches; the limit is ${MAX_BATCHES.toLocaleString()}.`;
    }
  }
  return "";
}

// The list of steps the server will take
function planSteps() {
  const d = state.dest;
  const steps = [];
  const rows = state.rows.toLocaleString();
  const batches = batchCount();
  const inBatches = batches > 1 ? ` in ${batches.toLocaleString()} batches` : "";
  const [verbIcon, verb] = tableExists() ? ["swap", "Replace"] : ["plus", "Create"];
  if (SQL_SOURCES.includes(state.source)) {
    if (!Object.keys(state.browse.schemas || {}).includes(d.schema)) {
      steps.push(["plus", `Create schema ${d.schema}`]);
    }
    steps.push([verbIcon, `${verb} table ${d.schema}.${d.table}`]);
    steps.push(["check", `Insert ${rows} rows${inBatches}`]);
  } else if (state.source === "dynamodb") {
    steps.push([verbIcon, `${verb} table ${d.table} (partition key: pk)`]);
    steps.push(["check", `Write ${rows} items${inBatches}`]);
  } else {
    if (!bucketExists()) {
      steps.push(["plus", `Create bucket ${d.bucket}`]);
    }
    if (state.source === "glue" && !state.sources.glue_databases.includes(d.glueDatabase)) {
      steps.push(["plus", `Create Glue database ${d.glueDatabase}`]);
    }
    const folder = `${d.bucket}/${d.prefix ? d.prefix + "/" : ""}${d.table}/`;
    steps.push([
      "check",
      `Write ${rows} rows as ${batches.toLocaleString()} ${FORMAT_LABELS[d.format]} part file${batches === 1 ? "" : "s"} to ${folder}`,
    ]);
    if (state.source === "glue") {
      steps.push([verbIcon, `${verb} Glue table ${d.glueDatabase}.${d.table}`]);
    } else if (tableExists()) {
      steps.push(["swap", "Replace the dataset's existing part files"]);
    }
  }
  return steps;
}

// Refresh page 3 from state (input values are set once in setStep so typing isn't overwritten)
function renderGenerate() {
  $("batch-size-field").hidden = !state.batching;
  const unit = FILE_SOURCES.includes(state.source)
    ? "Each batch becomes one part file."
    : state.source === "dynamodb"
      ? "Each batch is written separately."
      : "Each batch is inserted separately, all in one transaction.";
  $("batch-help").textContent = `${batchCount().toLocaleString()} batches. ${unit}`;

  const plan = $("plan");
  plan.replaceChildren();
  if (state.source) {
    for (const [name, text] of planSteps()) {
      const li = el("li");
      li.innerHTML = icon(name);
      li.appendChild(el("span", "", text));
      plan.appendChild(li);
    }
  }
  $("result-card").hidden = !state.result;
  if (state.result) {
    $("result-title").textContent = `${state.result.rows.toLocaleString()} rows written to ${destPath()}`;
    renderKv($("result-kv"), state.result.connection);
  }
  renderPanel();
  renderFooter();
}

async function generate() {
  if (generateError() || state.running) {
    return;
  }
  state.running = true;
  state.result = null;
  state.footError = "";
  renderGenerate();
  $("gen-running").hidden = false;
  const started = Date.now();
  const tick = () => {
    $("gen-status").textContent = `Generating ${state.rows.toLocaleString()} rows… ${Math.round((Date.now() - started) / 1000)}s`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  try {
    state.result = await api("generate", {
      source: state.source,
      ...destPayload(),
      rows: state.rows,
      batch_size: state.batching ? state.batchSize : null,
      columns: state.columns,
    });
    // New buckets, Glue databases and tables now exist
    await loadSources();
    await Promise.all([refreshBrowse(), refreshDatasets()]);
  } catch (e) {
    state.footError = e.message;
  } finally {
    clearInterval(timer);
    state.running = false;
    $("gen-running").hidden = true;
    renderGenerate();
  }
}

// Start over with a new table name, keeping source and columns
function newDataset() {
  state.result = null;
  state.dest.table = "";
  $("f-table").value = "";
  updateDatalists();
  setStep(1);
}

// =================================================
// WIRING
// =================================================

// Static icons
$("datasets-btn").querySelector(".btn-icon").innerHTML = icon("datasets");
$("admin-btn").querySelector(".btn-icon").innerHTML = icon("admin");
$("logout-btn").querySelector(".btn-icon").innerHTML = icon("logout");
$("foot-back").querySelector(".btn-icon").innerHTML = icon("back");
$("auto-cols-btn").querySelector(".btn-icon").innerHTML = icon("sparkles");

// Header
$("theme-btn").onclick = toggleTheme;
$("login-form").onsubmit = submitLogin;
$("logout-btn").onclick = async () => {
  await api("logout");
  showLogin();
};
$("datasets-btn").onclick = openDatasets;
$("admin-btn").onclick = () => showView($("admin-view").hidden ? "admin" : "wizard");
$("back-btn").onclick = () => showView("wizard");
for (const btn of document.querySelectorAll(".step")) {
  btn.onclick = () => goTo(Number(btn.dataset.step));
}

// Footer
$("foot-back").onclick = () => setStep(state.step - 1);
$("foot-next").onclick = () => (state.step < 3 ? goTo(state.step + 1) : generate());
$("foot-new").onclick = newDataset;

// Page 1
$("f-database").onchange = () => {
  state.dest.database = $("f-database").value;
  refreshBrowse();
  onDestChange();
};
for (const name of ["glueDatabase", "schema", "bucket", "prefix", "table"]) {
  bindDestInput(name);
}

// Page 2
$("col-count").onchange = () => setColumnCount(Number($("col-count").value));
$("add-col-btn").onclick = () => setColumnCount(state.columns.length + 1);
$("auto-cols-btn").onclick = autoColumns;

// Page 3
$("rows").oninput = () => {
  state.rows = Number($("rows").value);
  state.result = null;
  renderGenerate();
};
$("batching").onchange = () => {
  state.batching = $("batching").checked;
  renderGenerate();
};
$("batch-size").oninput = () => {
  state.batchSize = Number($("batch-size").value);
  renderGenerate();
};

document.addEventListener("mockgen:unauthorized", showLogin);
document.addEventListener("mockgen:datasets-changed", onDatasetsChanged);

renderThemeButton();
api("me")
  .then(showApp)
  .catch(() => showLogin());
