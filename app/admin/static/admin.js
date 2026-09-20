"use strict";

const state = { meta: [], byName: {}, view: null, offset: 0, q: "", filterCol: "", filterVal: "" };
const $ = (sel) => document.querySelector(sel);

const CUSTOM_PAGES = [
  { id: "dashboard", label: "Dashboard", group: "Overview" },
  { id: "partners", label: "Partners", group: "Overview" },
  { id: "identity", label: "Identity queue", group: "Overview" },
  { id: "requests", label: "Requests", group: "Overview" },
  { id: "ledger", label: "Adjust credit", group: "Overview" },
  { id: "demand", label: "Demand", group: "Overview" },
  { id: "runtime", label: "Runtime config", group: "Settings" },
];

/**
 * Call the admin API and unwrap the envelope.
 * Throws an Error carrying the server's code and message on failure.
 * A 401 sends the page back to the login screen.
 */
async function api(method, path, body, headers = {}) {
  const multipart = body instanceof FormData;
  const res = await fetch("/admin/api" + path, {
    method,
    credentials: "same-origin",
    // A FormData body sets its own multipart Content-Type, boundary included.
    headers: multipart ? headers : { "Content-Type": "application/json", ...headers },
    body: body === undefined || multipart ? body : JSON.stringify(body),
  });
  const json = res.status === 204 ? {} : await res.json().catch(() => ({}));
  if (res.status === 401 && path !== "/login") showLogin();
  if (!res.ok) {
    const err = new Error(json.error?.message || res.statusText);
    err.code = json.error?.code;
    err.details = json.error?.details;
    throw err;
  }
  return json;
}

/**
 * Escape text for safe insertion into HTML.
 * Every value from the database goes through this.
 * Null and undefined become an empty string.
 */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

/**
 * Show a short message in the corner for a few seconds.
 * Used after saves, deletes and actions.
 * Replaces any message already showing.
 */
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 2600);
}

/**
 * Format paise as rupees with Indian digit grouping.
 * Money is stored in paise and shown in rupees.
 * Null stays an empty cell.
 */
function rupees(minor) {
  if (minor === null || minor === undefined || minor === "") return "";
  return "₹" + (Number(minor) / 100).toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

/**
 * Render one cell value for a table by its column type.
 * Booleans become chips, points become coordinates, long text is trimmed.
 * Columns ending in _minor are shown as rupees.
 */
function cell(col, value) {
  if (value === null || value === undefined) return '<span class="muted">—</span>';
  if (col.type === "bool") return value ? '<span class="chip ok">yes</span>' : '<span class="chip">no</span>';
  if (col.type === "point") return `<span class="mono">${value.lat.toFixed(5)}, ${value.lng.toFixed(5)}</span>`;
  if (col.type === "image") return `<img class="thumb" alt="" src="${esc(value)}">`;
  if (col.name.endsWith("_minor")) return rupees(value);
  if (Array.isArray(value)) return esc(value.join(", "));
  if (col.type === "timestamp") return esc(String(value).replace("T", " ").slice(0, 19));
  if (["status", "state"].includes(col.name)) return `<span class="chip ${chipClass(value)}">${esc(value)}</span>`;
  return esc(value);
}

/**
 * Pick a colour class for a status chip.
 * Good states are green, stopped ones red, waiting ones amber.
 * Unknown states get the neutral chip.
 */
function chipClass(value) {
  if (["ACTIVE", "VERIFIED", "COMPLETED", "BOOKED", "RESOLVED", "DONE"].includes(value)) return "ok";
  if (["SUSPENDED", "REJECTED", "CANCELLED", "WITHDRAWN"].includes(value)) return "bad";
  if (["PENDING", "ACTIVATED", "NEEDS_ACTION", "OPEN", "IN_REVIEW", "REQUESTED", "ARRIVED"].includes(value)) return "warn";
  return "";
}

/**
 * Set the page heading, subtitle and header buttons.
 * actions is a list of {label, onClick, primary}.
 * Called at the start of every view.
 */
function header(title, subtitle = "", actions = []) {
  $("#title").textContent = title;
  $("#subtitle").textContent = subtitle;
  const box = $("#actions");
  box.innerHTML = "";
  for (const a of actions) {
    const b = document.createElement("button");
    b.textContent = a.label;
    if (a.primary) b.className = "primary";
    b.onclick = a.onClick;
    box.appendChild(b);
  }
}

/**
 * Build the sidebar from the custom pages and the resource registry.
 * Resources are grouped the same way the registry groups them.
 * The active item is highlighted.
 */
function renderNav() {
  const groups = {};
  for (const p of CUSTOM_PAGES) (groups[p.group] ||= []).push({ id: "page:" + p.id, label: p.label });
  for (const r of state.meta) (groups[r.group] ||= []).push({ id: "res:" + r.name, label: r.label });
  $("#nav").innerHTML = Object.entries(groups)
    .map(([g, items]) => `<div class="nav-group">${esc(g)}</div>` +
      items.map((i) => `<button class="nav-item ${state.view === i.id ? "active" : ""}" data-view="${i.id}">${esc(i.label)}</button>`).join(""))
    .join("");
  for (const b of document.querySelectorAll(".nav-item")) b.onclick = () => go(b.dataset.view);
}

/**
 * Switch to a view and remember it in the URL hash.
 * Views are page:<id> for custom pages or res:<name> for tables.
 * Resets search and paging.
 */
function go(view) {
  state.view = view;
  state.offset = 0;
  state.q = state.filterCol = state.filterVal = "";
  location.hash = view;
  document.querySelector(".sidebar").classList.remove("open");
  renderNav();
  render();
}

/**
 * Draw whichever view is current.
 * Errors are shown in the page instead of breaking it.
 * Called after navigation and after every change.
 */
async function render() {
  const view = $("#view");
  view.innerHTML = '<p class="muted">Loading…</p>';
  try {
    const [kind, id] = state.view.split(":");
    if (kind === "page") await PAGES[id](view);
    else await renderResource(view, state.byName[id]);
  } catch (e) {
    view.innerHTML = `<p class="error">${esc(e.code || "")} ${esc(e.message)}</p>`;
  }
}

/**
 * Draw a generic table view for one registry resource.
 * Search, one exact filter, paging, and click-to-edit rows.
 * Singleton settings open straight into their form.
 */
async function renderResource(view, res) {
  if (res.singleton) return renderSingleton(view, res);
  const actions = res.creatable ? [{ label: "New", primary: true, onClick: () => openForm(res, null) }] : [];
  header(res.label, res.help || (res.editable ? "Click a row to edit." : "Read only."), actions);
  const params = new URLSearchParams({ offset: state.offset });
  if (state.q) params.set("q", state.q);
  if (state.filterCol && state.filterVal) params.set("f." + state.filterCol, state.filterVal);
  const { data, meta } = await api("GET", `/r/${res.name}?${params}`);
  const cols = res.columns.filter((c) => c.list && !c.hidden);
  view.innerHTML = `
    <div class="toolbar">
      <input id="q" placeholder="Search text columns" value="${esc(state.q)}">
      <select id="fcol"><option value="">Filter column…</option>${res.columns.map((c) => `<option ${c.name === state.filterCol ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select>
      <input id="fval" placeholder="equals…" value="${esc(state.filterVal)}">
      <button id="apply">Apply</button>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>${cols.map((c) => `<th>${esc(c.label)}</th>`).join("")}</tr></thead>
      <tbody>${data.map((row, i) => `<tr data-i="${i}">${cols.map((c) => `<td class="${["int", "numeric"].includes(c.type) ? "num" : ""}">${cell(c, row[c.name])}</td>`).join("")}</tr>`).join("") ||
        `<tr><td colspan="${cols.length}" class="muted">Nothing here yet.</td></tr>`}</tbody>
    </table></div>
    <div class="pager"><span>${meta.total} row(s)</span><span>
      <button id="prev" ${state.offset === 0 ? "disabled" : ""}>Previous</button>
      <button id="next" ${state.offset + meta.pageSize >= meta.total ? "disabled" : ""}>Next</button></span></div>`;
  const apply = () => { state.q = $("#q").value.trim(); state.filterCol = $("#fcol").value; state.filterVal = $("#fval").value.trim(); state.offset = 0; render(); };
  $("#apply").onclick = apply;
  $("#q").onkeydown = (e) => e.key === "Enter" && apply();
  $("#fval").onkeydown = (e) => e.key === "Enter" && apply();
  $("#prev").onclick = () => { state.offset = Math.max(0, state.offset - meta.pageSize); render(); };
  $("#next").onclick = () => { state.offset += meta.pageSize; render(); };
  for (const tr of view.querySelectorAll("tbody tr[data-i]")) {
    tr.onclick = () => res.name === "request" ? openRequest(data[tr.dataset.i].id) : openForm(res, data[tr.dataset.i]);
  }
}

/**
 * Draw the single-row settings form inline.
 * Catalog and platform settings each have exactly one row.
 * Saving writes the whole form.
 */
async function renderSingleton(view, res) {
  header(res.label, res.help || "One row. Changes reach the apps on their next catalog read.");
  const { data } = await api("POST", `/r/${res.name}/get`, { key: { id: 1 } });
  view.innerHTML = `<form class="card" id="single"><div class="form-grid">${await fields(res, data)}</div>
    <p class="error" id="single-error"></p><button class="primary">Save</button></form>`;
  $("#single").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("PUT", `/r/${res.name}`, { key: { id: 1 }, values: readFields(res, $("#single")) });
      toast("Saved");
    } catch (err) {
      $("#single-error").textContent = err.message;
    }
  };
}

const refCache = {};

/**
 * Load the choices for a column that points at another resource.
 * The first column of the key is the value; a name or label is shown.
 * Cached for the life of the page.
 */
async function refOptions(ref) {
  if (!refCache[ref]) {
    const res = state.byName[ref];
    const { data } = await api("GET", `/r/${ref}`);
    const keyCol = res.key[res.key.length - 1];
    refCache[ref] = data.map((r) => ({ value: r[keyCol], label: r.name || r.label || r.display_name || r[keyCol] }));
  }
  return refCache[ref];
}

/**
 * Build the HTML for every form field of a resource.
 * Each column type gets the right control; keys lock once created.
 * Returns an HTML string.
 */
async function fields(res, row) {
  const parts = [];
  for (const c of res.columns) {
    if (c.hidden) continue;
    const v = row ? row[c.name] : null;
    const locked = c.readonly || c.type === "timestamp" || (row && res.key.includes(c.name)) || (!res.editable && row);
    const dis = locked ? "disabled" : "";
    const help = c.help ? `<div class="help">${esc(c.help)}</div>` : "";
    const req = c.required ? " *" : "";
    let input;
    if (c.type === "bool") {
      parts.push(`<label class="check"><input type="checkbox" name="${c.name}" ${v ? "checked" : ""} ${dis}>${esc(c.label)}</label>`);
      continue;
    } else if (c.choices.length) {
      input = `<select name="${c.name}" ${dis}><option value=""></option>${c.choices.map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    } else if (c.ref && !locked) {
      const opts = await refOptions(c.ref);
      input = `<select name="${c.name}"><option value=""></option>${opts.map((o) => `<option value="${esc(o.value)}" ${o.value === v ? "selected" : ""}>${esc(o.label)} (${esc(o.value)})</option>`).join("")}</select>`;
    } else if (c.type === "image") {
      input = `<div class="image-field" data-purpose="${esc(c.purpose)}">
        <input type="hidden" name="${c.name}" value="${esc(v ?? "")}">
        <img class="thumb big" alt="" ${v ? `src="${esc(v)}"` : "hidden"}>
        <div class="inline"><input type="file" accept="image/png,image/jpeg" ${dis}><button type="button" data-clear ${dis}>Remove</button></div>
        <div class="help" data-status></div></div>`;
    } else if (c.type === "point") {
      input = `<div class="inline"><input name="${c.name}__lat" placeholder="latitude" value="${esc(v?.lat ?? "")}" ${dis}><input name="${c.name}__lng" placeholder="longitude" value="${esc(v?.lng ?? "")}" ${dis}></div>`;
    } else if (c.type === "geojson" || c.type === "longtext" || c.type === "json") {
      input = `<textarea name="${c.name}" ${dis}>${esc(v ?? "")}</textarea>`;
    } else if (c.type === "int_array" || c.type === "text_array") {
      input = `<input name="${c.name}" value="${esc((v || []).join(", "))}" ${dis}>`;
    } else {
      input = `<input name="${c.name}" value="${esc(v ?? "")}" ${dis} ${["int", "numeric"].includes(c.type) ? 'inputmode="decimal"' : ""}>`;
    }
    const wide = ["geojson", "longtext", "json"].includes(c.type) ? "wide" : "";
    parts.push(`<label class="${wide}">${esc(c.label)}${req}${input}${help}</label>`);
  }
  return parts.join("");
}

// Pictures uploaded in the open dialog and not yet saved on a row: url -> media id.
const pendingImages = new Map();

/**
 * Throw away a picture uploaded in this dialog that will not be saved.
 * The picture the row already held is not in pendingImages and is left alone;
 * the server removes it once the row is saved with a different one.
 * A picture a saved row now uses is refused by the server, which is fine.
 */
async function discardPending(url) {
  const id = pendingImages.get(url);
  if (!id) return;
  pendingImages.delete(url);
  try { await api("DELETE", `/media/${id}`); } catch { /* already gone or in use */ }
}

/**
 * Make each image field upload as soon as a file is chosen.
 * The upload returns a media id and URL; the URL goes into a hidden input
 * that the form's save sends with the row, so the row is written in a second request.
 * Choosing another file replaces the first, which is discarded if it was never saved.
 */
function wireImages(root) {
  for (const box of root.querySelectorAll(".image-field")) {
    const value = box.querySelector('input[type="hidden"]');
    const file = box.querySelector('input[type="file"]');
    const preview = box.querySelector("img");
    const status = box.querySelector("[data-status]");
    const show = () => {
      preview.hidden = !value.value;
      if (value.value) preview.src = value.value;
    };
    file.onchange = async () => {
      const picked = file.files[0];
      if (!picked) return;
      const form = new FormData();
      form.append("purpose", box.dataset.purpose);
      form.append("file", picked);
      status.textContent = "Uploading…";
      try {
        const { data } = await api("POST", "/media", form);
        const previous = value.value;
        pendingImages.set(data.url, data.mediaId);
        value.value = data.url;
        show();
        status.textContent = "Uploaded. It is attached when you save.";
        discardPending(previous);
      } catch (e) {
        status.textContent = `${e.code ? e.code + ": " : ""}${e.message}`;
      }
      file.value = "";
    };
    box.querySelector("[data-clear]").onclick = () => {
      discardPending(value.value);
      value.value = "";
      show();
      status.textContent = "Removed. It is cleared when you save.";
    };
  }
}

$("#modal").addEventListener("close", () => {
  for (const url of [...pendingImages.keys()]) discardPending(url);
});

/**
 * Read a resource form back into a values object.
 * Disabled fields are skipped; points are read as {lat, lng}.
 * Checkboxes always send true or false.
 */
function readFields(res, form) {
  const values = {};
  for (const c of res.columns) {
    if (c.type === "point") {
      const lat = form.elements[c.name + "__lat"], lng = form.elements[c.name + "__lng"];
      if (lat && !lat.disabled) values[c.name] = { lat: lat.value, lng: lng.value };
      continue;
    }
    const el = form.elements[c.name];
    if (!el || el.disabled) continue;
    values[c.name] = c.type === "bool" ? el.checked : el.value;
  }
  return values;
}

/**
 * Open the create or edit dialog for one row.
 * row null means a new row; delete appears only where allowed.
 * Saving re-renders the table.
 */
async function openForm(res, row) {
  const key = row ? Object.fromEntries(res.key.map((k) => [k, row[k]])) : null;
  $("#modal-title").textContent = (row ? "Edit " : "New ") + res.label.replace(/s$/, "").toLowerCase();
  $("#modal-body").innerHTML = `<div class="form-grid">${await fields(res, row)}</div>`;
  wireImages($("#modal-body"));
  $("#modal-error").textContent = "";
  const foot = $("#modal-foot");
  foot.innerHTML = "";
  if (row && res.deletable) foot.appendChild(button("Delete", "danger", async () => {
    if (!confirm("Delete this row? This cannot be undone.")) return;
    await guarded(() => api("DELETE", `/r/${res.name}`, { key }), "Deleted");
  }));
  if (!row || res.editable) foot.appendChild(button(row ? "Save" : "Create", "primary", async () => {
    const values = readFields(res, $("#modal-form"));
    await guarded(() => row ? api("PUT", `/r/${res.name}`, { key, values }) : api("POST", `/r/${res.name}`, { values }), "Saved");
  }));
  $("#modal").showModal();
}

/**
 * Make a button for the dialog footer.
 * kind is a CSS class such as primary or danger.
 * type=button so it never submits the dialog form.
 */
function button(label, kind, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.className = kind;
  b.onclick = onClick;
  return b;
}

/**
 * Run a dialog action, closing and refreshing on success.
 * Failures are shown inside the dialog, which stays open.
 * Clears cached dropdowns because the data may have changed.
 */
async function guarded(action, message) {
  try {
    await action();
    Object.keys(refCache).forEach((k) => delete refCache[k]);
    $("#modal").close();
    toast(message);
    render();
  } catch (e) {
    $("#modal-error").textContent = `${e.code ? e.code + ": " : ""}${e.message}`;
  }
}

/**
 * Draw a simple table from rows and column definitions.
 * columns are [key, label, formatter?].
 * Returns an HTML string.
 */
function simpleTable(rows, columns) {
  if (!rows.length) return '<p class="muted">Nothing yet.</p>';
  return `<div class="table-wrap"><table><thead><tr>${columns.map((c) => `<th>${esc(c[1])}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${columns.map((c) => `<td>${c[2] ? c[2](r[c[0]], r) : esc(r[c[0]] ?? "—")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

/**
 * Show one request with its answers, notifications, offers and bookings.
 * Includes a button to send the next matching batch now.
 * The completion PIN is never part of this view.
 */
async function openRequest(id) {
  const { data } = await api("GET", `/requests/${id}/detail`);
  const r = data.request;
  $("#modal-title").textContent = `${r.service_name} · ${r.id}`;
  $("#modal-body").innerHTML = `
    <dl class="kv">
      <dt>State</dt><dd><span class="chip ${chipClass(r.state)}">${esc(r.state)}</span> ${esc(r.ended_reason || "")}</dd>
      <dt>Customer</dt><dd>${esc(r.customer_name || "—")} · ${esc(r.phone_e164)}</dd>
      <dt>When</dt><dd>${esc(r.schedule_date)} · ${esc(r.day_part)}</dd>
      <dt>Where</dt><dd>${esc(r.place_name)} ${r.landmark ? "· " + esc(r.landmark) : ""}</dd>
      <dt>Description</dt><dd>${esc(r.description || "—")}</dd>
      <dt>Origin</dt><dd>${esc(r.origin_source || "—")}</dd>
    </dl>
    <h4>Answers</h4>${simpleTable(data.answers, [["label", "Question"], ["displayValue", "Answer"]])}
    <h4>Who was told</h4>${simpleTable(data.notifications, [["display_name", "Partner"], ["batch", "Batch"], ["notified_at", "Notified"], ["declined_at", "Declined"]])}
    <h4>Offers</h4>${simpleTable(data.offers, [["display_name", "Partner"], ["status", "Status", (v) => `<span class="chip ${chipClass(v)}">${esc(v)}</span>`],
      ["pricing_type", "Type"], ["comparable_cost_minor", "Cost", rupees], ["rate_minor", "Rate", (v, row) => v ? rupees(v) + " / " + esc(row.unit_code) : "—"],
      ["inspection_charge_minor", "Visit", rupees], ["offered_day_part", "Daypart"]])}
    <h4>Bookings</h4>${simpleTable(data.bookings, [["id", "Booking"], ["state", "State"], ["booked_cost_minor", "Booked", rupees], ["final_amount_minor", "Final", rupees], ["completed_by", "Completed by"]])}`;
  $("#modal-error").textContent = "";
  const foot = $("#modal-foot");
  foot.innerHTML = "";
  if (r.state === "REQUESTED") foot.appendChild(button("Send next batch now", "primary", async () => {
    try {
      const res = await api("POST", `/requests/${id}/run-matching`);
      toast(`Notified ${res.data.notified.length} partner(s)`);
      openRequest(id);
    } catch (e) { $("#modal-error").textContent = e.message; }
  }));
  if (!$("#modal").open) $("#modal").showModal();
}

const PAGES = {
  /**
   * Draw every setting from config.py, grouped, with live controls.
   * Changes apply to the running server within seconds, no restart.
   * Reset puts the .env value back; restart-only settings are shown read-only.
   */
  async runtime(view) {
    header("Runtime config", "Everything in config.py. Overrides are stored in the database and applied live.");
    const { data } = await api("GET", "/config");
    const groups = {};
    for (const item of data) (groups[item.group] ||= []).push(item);
    view.innerHTML = Object.entries(groups).map(([group, items]) => `
      <div class="card" style="margin-bottom:12px"><h4>${esc(group)}</h4>
        <div class="table-wrap"><table><thead><tr><th>Setting</th><th>Live value</th><th>.env value</th><th></th></tr></thead><tbody>
        ${items.map((i) => `<tr>
          <td><code>${esc(i.key)}</code>${i.overridden ? ' <span class="chip warn">overridden</span>' : ""}${i.restartRequired ? ' <span class="chip">restart</span>' : ""}
            ${i.help ? `<div class="help">${esc(i.help)}</div>` : ""}</td>
          <td>${!i.editable ? esc(i.effective) : i.kind === "bool"
            ? `<label class="check" style="margin:0"><input type="checkbox" data-key="${esc(i.key)}" ${i.effective === "true" ? "checked" : ""}> ${i.effective === "true" ? "On" : "Off"}</label>`
            : `<div class="inline"><input data-key="${esc(i.key)}" value="${i.secret ? "" : esc(i.effective)}" placeholder="${i.secret ? esc(i.effective) : ""}"><button data-save="${esc(i.key)}">Save</button></div>`}</td>
          <td class="muted">${esc(i.envValue)}</td>
          <td>${i.overridden ? `<button class="link" data-reset="${esc(i.key)}">Reset to .env</button>` : ""}</td>
        </tr>`).join("")}
        </tbody></table></div></div>`).join("");
    const save = async (key, value) => {
      try {
        await api("PUT", `/config/${key}`, { value: String(value) });
        toast(`${key} saved`);
      } catch (e) { toast(`${e.code}: ${e.message}`); }
      render();
    };
    for (const box of view.querySelectorAll('input[type="checkbox"][data-key]')) box.onchange = () => save(box.dataset.key, box.checked);
    for (const b of view.querySelectorAll("[data-save]")) b.onclick = () => save(b.dataset.save, view.querySelector(`input[data-key="${CSS.escape(b.dataset.save)}"]`).value);
    for (const b of view.querySelectorAll("[data-reset]")) b.onclick = async () => { await api("DELETE", `/config/${b.dataset.reset}`); toast("Back to .env"); render(); };
  },

  /**
   * Draw the dashboard home with counts and demand.
   * Everything waiting on a person is at the top.
   * A button runs the scheduled jobs immediately.
   */
  async dashboard(view) {
    header("Dashboard", "What needs attention.", [{ label: "Run scheduled jobs now", onClick: async () => { await api("POST", "/jobs/tick"); toast("Jobs ran"); render(); } }]);
    const { data } = await api("GET", "/stats");
    const count = (list, key) => Object.fromEntries(list.map((x) => [x[key], x.n]));
    const partners = count(data.partnersByStatus, "status");
    const requests = count(data.requestsByState, "state");
    const bookings = count(data.bookingsByState, "state");
    const stat = (label, value) => `<div class="card stat"><div class="value">${value ?? 0}</div><div class="label">${esc(label)}</div></div>`;
    view.innerHTML = `
      <div class="grid">
        ${stat("Identity checks waiting", data.identityPending)}
        ${stat("Partners waiting for approval", partners.ACTIVATED)}
        ${stat("Open requests", requests.REQUESTED)}
        ${stat("Jobs running", (bookings.BOOKED || 0) + (bookings.ARRIVED || 0))}
        ${stat("Jobs completed", bookings.COMPLETED)}
        ${stat("Offers in the last 24h", data.offersToday)}
        ${stat("Customers", data.customers)}
        ${stat("Open complaints", data.openComplaints)}
      </div>
      <div class="cols">
        <div class="card"><h4>Partners by status</h4>${simpleTable(data.partnersByStatus, [["status", "Status"], ["n", "Count"]])}</div>
        <div class="card"><h4>Requests by state</h4>${simpleTable(data.requestsByState, [["state", "State"], ["n", "Count"]])}</div>
        <div class="card"><h4>Villages asked for</h4>${simpleTable(data.topPlaceInterest, [["name", "Village"], ["n", "Times"]])}</div>
      </div>`;
  },

  /**
   * Draw every partner with the actions that change his status.
   * Approve needs finished setup and a verified identity.
   * statusNote is shown to him, so the prompt asks for plain words.
   */
  async partners(view) {
    header("Partners", "Approve, suspend or reinstate. Only ACTIVE partners receive work.");
    const { data } = await api("GET", "/partners");
    view.innerHTML = simpleTable(data, [
      ["display_name", "Name", (v, r) => `${esc(v || "—")}<div class="muted mono">${esc(r.user_id)}</div>`],
      ["phone_e164", "Phone"], ["status", "Status", (v) => `<span class="chip ${chipClass(v)}">${esc(v)}</span>`],
      ["identity_status", "Identity", (v) => v ? `<span class="chip ${chipClass(v)}">${esc(v)}</span>` : "—"],
      ["next_step", "Setup"], ["services", "Services"], ["base_place", "Base"], ["travel_radius_km", "Radius"],
      ["completed", "Done"], ["balance_minor", "Balance", rupees],
      ["user_id", "", (id, r) => partnerButtons(r)],
    ]);
    for (const b of view.querySelectorAll("[data-status]")) {
      b.onclick = async () => {
        const status = b.dataset.status;
        let note = null;
        if (status === "SUSPENDED") {
          note = prompt("Why is he being paused? He will read this.");
          if (note === null) return;
        }
        try {
          await api("POST", `/partners/${b.dataset.id}/status`, { status, statusNote: note });
          toast(`Now ${status}`);
          render();
        } catch (e) { toast(`${e.code}: ${e.message}`); }
      };
    }
  },

  /**
   * Draw the identity checks waiting on a person.
   * Both images are shown side by side for comparison.
   * Deciding VERIFIED or REJECTED deletes the images for good.
   */
  async identity(view) {
    header("Identity queue", "Compare the document with the selfie. Final decisions delete both images.");
    const { data } = await api("GET", "/r/identity_verification?f.status=PENDING");
    if (!data.length) { view.innerHTML = '<p class="muted">Nobody is waiting.</p>'; return; }
    view.innerHTML = data.map((r) => `
      <div class="card" style="margin-bottom:12px">
        <h4>${esc(r.partner_id)} · ${esc(r.document_type)}</h4>
        <p class="muted">Sent ${esc(String(r.submitted_at).slice(0, 16).replace("T", " "))}</p>
        <div class="id-images">
          ${r.document_media_id ? `<img alt="document" src="/admin/api/media/${esc(r.document_media_id)}/file">` : '<span class="muted">No document image (seed data)</span>'}
          ${r.selfie_media_id ? `<img alt="selfie" src="/admin/api/media/${esc(r.selfie_media_id)}/file">` : ""}
        </div>
        <label>Note for him (required unless verifying)<textarea data-note="${esc(r.partner_id)}"></textarea></label>
        <div class="inline" style="margin-top:8px">
          <button class="primary" data-decide="VERIFIED" data-id="${esc(r.partner_id)}">Verify and activate</button>
          <button data-decide="NEEDS_ACTION" data-id="${esc(r.partner_id)}">Ask him to send again</button>
          <button class="danger" data-decide="REJECTED" data-id="${esc(r.partner_id)}">Reject</button>
        </div>
      </div>`).join("");
    for (const b of view.querySelectorAll("[data-decide]")) {
      b.onclick = async () => {
        const note = view.querySelector(`[data-note="${CSS.escape(b.dataset.id)}"]`).value;
        try {
          await api("POST", `/identity/${b.dataset.id}/decision`, { status: b.dataset.decide, reviewNote: note, activate: true });
          toast("Decided");
          render();
        } catch (e) { toast(`${e.code}: ${e.message}`); }
      };
    }
  },

  /**
   * Draw the requests table; rows open the full request story.
   * Uses the generic resource view with a custom row click.
   * Filter by state to see what is open.
   */
  async requests(view) {
    state.view = "res:request";
    renderNav();
    await renderResource(view, state.byName.request);
  },

  /**
   * Draw the form that adds one credit ledger row.
   * Amounts are typed in rupees and sent in paise; negative takes credit away.
   * Each submit carries a fresh Idempotency-Key.
   */
  async ledger(view) {
    header("Adjust credit", "Append-only. A correction is a new row, never an edit.");
    const { data } = await api("GET", "/partners");
    view.innerHTML = `
      <form class="card" id="adjust" style="max-width:560px">
        <label>Partner<select name="partnerId" required>${data.map((p) => `<option value="${esc(p.user_id)}">${esc(p.display_name || p.user_id)} · balance ${rupees(p.balance_minor)}</option>`).join("")}</select></label>
        <label>Type<select name="type">${["TOP_UP", "MANUAL_ADJUSTMENT", "PROMO", "SYSTEM_RESTORE", "REFERRAL_REWARD"].map((t) => `<option>${t}</option>`).join("")}</select></label>
        <label>Amount in rupees (negative to deduct)<input name="rupees" inputmode="decimal" required></label>
        <label>Why<textarea name="note" required></textarea></label>
        <p class="error" id="adjust-error"></p>
        <button class="primary">Add ledger row</button>
      </form>`;
    $("#adjust").onsubmit = async (e) => {
      e.preventDefault();
      const f = e.target.elements;
      const amountMinor = Math.round(Number(f.rupees.value) * 100);
      try {
        const res = await api("POST", "/ledger/adjust", { partnerId: f.partnerId.value, type: f.type.value, amountMinor, note: f.note.value },
          { "Idempotency-Key": crypto.randomUUID() });
        toast(`Added. Balance now ${rupees(res.data.balanceMinor)}`);
        render();
      } catch (err) { $("#adjust-error").textContent = `${err.code}: ${err.message}`; }
    };
  },

  /**
   * Draw the list of villages people outside the area typed.
   * Grouped by name, most asked first.
   * This is the list that decides where to open next.
   */
  async demand(view) {
    header("Demand", "Villages we do not serve yet, as people typed them.");
    const { data } = await api("GET", "/demand");
    view.innerHTML = simpleTable(data, [["name", "Village"], ["n", "Times asked"], ["with_phone", "Left a number"], ["first", "First"], ["last", "Latest"]]);
  },
};

/**
 * Build the action buttons for one partner row.
 * Only transitions that make sense from his current status appear.
 * Buttons carry the partner id and target status.
 */
function partnerButtons(r) {
  const b = (status, label, cls = "") => `<button class="${cls}" data-status="${status}" data-id="${esc(r.user_id)}">${label}</button>`;
  if (r.status === "ACTIVATED") return b("ACTIVE", "Approve", "primary") + " " + b("SUSPENDED", "Suspend", "danger");
  if (r.status === "ACTIVE") return b("SUSPENDED", "Suspend", "danger");
  if (r.status === "SUSPENDED") return b(r.next_step === "COMPLETE" && r.identity_status === "VERIFIED" ? "ACTIVE" : "ACTIVATED", "Reinstate");
  return '<span class="muted">Setting up</span>';
}

/**
 * Show the login screen and hide the console.
 * Called at start-up and whenever the API says 401.
 * The form posts to /admin/api/login.
 */
function showLogin() {
  $("#shell").classList.add("hidden");
  $("#login").classList.remove("hidden");
}

/**
 * Load the registry and open the console.
 * Restores the view named in the URL hash, or the dashboard.
 * Called after a successful login or an existing session.
 */
async function boot(username) {
  $("#who").textContent = username;
  const { data } = await api("GET", "/meta");
  state.meta = data;
  state.byName = Object.fromEntries(data.map((r) => [r.name, r]));
  $("#login").classList.add("hidden");
  $("#shell").classList.remove("hidden");
  go(location.hash.slice(1) || "page:dashboard");
}

$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  try {
    const { data } = await api("POST", "/login", { username: f.username.value, password: f.password.value });
    boot(data.username);
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
};
$("#logout").onclick = async () => { await api("POST", "/logout"); showLogin(); };
$("#menu").onclick = () => document.querySelector(".sidebar").classList.toggle("open");

api("GET", "/me").then(({ data }) => boot(data.username)).catch(showLogin);
