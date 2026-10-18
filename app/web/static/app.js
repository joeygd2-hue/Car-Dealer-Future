// Dealer listings workspace + agency outreach dashboard.
// All data is rendered with textContent (never innerHTML) so inventory, listing
// or email content cannot inject markup. Authorization is enforced by the API;
// hiding a button here is a convenience only.

const state = { token: sessionStorage.getItem("token"), me: null, clientId: sessionStorage.getItem("clientId"), route: "" };
const app = document.getElementById("app");
// replaceChildren() would print null/false as text, so filter them out.
const render = (...nodes) => app.replaceChildren(...nodes.flat().filter((n) => n !== null && n !== undefined && n !== false));

// ---------- tiny DOM helper ----------
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const chip = (text, kind = "") => h("span", { class: `chip ${kind}` }, text);
const fmtMoney = (v) => (v === null || v === undefined ? "—" : `$${Number(v).toLocaleString()}`);
const fmtNum = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString());
const fmtTime = (t) => (t ? new Date(t).toLocaleString() : "—");
function toast(msg) {
  const t = h("div", {}, msg);
  document.getElementById("toast").append(t);
  setTimeout(() => t.remove(), 5000);
}
const STATUS_KIND = {
  approved: "ok", published_confirmed: "ok", available: "ok", valid: "ok", succeeded: "ok", sent: "ok", booked: "ok", healthy: "ok", qualified: "ok",
  awaiting_approval: "warn", draft_generated: "warn", manual_required: "warn", submitted: "warn", warnings: "warn", pending: "warn", partial: "warn",
  ready_to_publish: "warn", needs_validation: "err", failed: "err", invalid: "err", withdrawal_required: "err", rejected: "err", blocked: "err",
  sold: "", removed: "", sold_withdrawn: "", withdrawn: "", do_not_contact: "err", disqualified: "",
};
const LABEL = {
  imported: "Imported", needs_validation: "Needs validation", draft_generated: "Draft (needs fixes)", awaiting_approval: "Awaiting approval",
  approved: "Approved", rejected: "Rejected", sold_withdrawn: "Sold / withdrawn", ready_to_publish: "Ready to publish", submitted: "Submitted",
  published_confirmed: "Published (confirmed)", manual_required: "Manual publication required", failed: "Failed",
  withdrawal_required: "Withdrawal required", withdrawn: "Withdrawn",
};
const statusChip = (s) => chip(LABEL[s] || s, STATUS_KIND[s] ?? "");

// ---------- API ----------
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  let body = opts.body;
  if (body && !(body instanceof FormData)) { headers["Content-Type"] = "application/json"; body = JSON.stringify(body); }
  const res = await fetch(path, { method: opts.method || (body ? "POST" : "GET"), headers, body });
  if (res.status === 401) { logout(); throw new Error("Please sign in again"); }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const e = data?.error || {};
    const details = e.details?.blockers || e.details?.problems || e.details?.validation_errors;
    throw new Error((e.message || res.statusText) + (details ? `: ${details.join("; ")}` : ""));
  }
  return data;
}
const capi = (path, opts) => api(`/api/clients/${state.clientId}${path}`, opts);
async function act(fn, ok) {
  try { const r = await fn(); if (ok) toast(ok); return r; } catch (e) { toast(e.message); throw e; }
}

// ---------- auth & shell ----------
function logout() {
  if (state.token) fetch("/api/auth/logout", { method: "POST", headers: { Authorization: `Bearer ${state.token}` } }).catch(() => {});
  state.token = null; state.me = null;
  sessionStorage.removeItem("token");
  document.getElementById("topbar").hidden = true;
  renderLogin();
}
document.getElementById("logoutBtn").addEventListener("click", logout);

function renderLogin() {
  const email = h("input", { type: "email", autocomplete: "username", required: true });
  const pw = h("input", { type: "password", autocomplete: "current-password", required: true });
  const err = h("p", { class: "err" });
  render(h("form", { class: "panel login", onsubmit: async (ev) => {
    ev.preventDefault();
    try {
      const r = await api("/api/auth/login", { body: { email: email.value, password: pw.value } });
      state.token = r.token; sessionStorage.setItem("token", r.token);
      await boot();
    } catch (e) { err.textContent = e.message; }
  } }, h("h1", {}, "Sign in"), h("label", {}, "Email"), email, h("label", {}, "Password"), pw, h("p"), h("button", { type: "submit" }, "Sign in"), err));
}

const DEALER_ROUTES = [["inventory", "Inventory"], ["listings", "Listings"], ["publishing", "Publishing"], ["imports", "Imports"],
  ["reports", "Reports"], ["settings", "Settings"]];
const AGENCY_ROUTES = [["outreach", "Outreach agent"]];

async function boot() {
  state.me = await api("/api/me");
  const sel = document.getElementById("clientSelect");
  sel.replaceChildren(...state.me.clients.map((c) => h("option", { value: c.id }, `${c.name} (${c.role})`)));
  if (!state.me.clients.find((c) => c.id === state.clientId)) state.clientId = state.me.clients[0]?.id || null;
  sel.value = state.clientId || "";
  sel.hidden = !state.me.clients.length;
  sel.onchange = () => { state.clientId = sel.value; sessionStorage.setItem("clientId", sel.value); route(); };
  document.getElementById("whoName").textContent = state.me.user.name;
  document.getElementById("topbar").hidden = false;
  window.addEventListener("hashchange", route);
  route();
}

function nav() {
  const isAgency = ["platform_admin", "platform_staff"].includes(state.me.user.platform_role);
  const routes = [...(state.clientId ? DEALER_ROUTES : []), ...(isAgency ? AGENCY_ROUTES : [])];
  const cur = state.route.split("/")[0];
  document.getElementById("nav").replaceChildren(...routes.map(([r, l]) => h("a", { href: `#${r}`, class: cur === r ? "active" : "" }, l)));
  return routes;
}

async function route() {
  state.route = location.hash.slice(1) || (state.clientId ? "inventory" : "outreach");
  const routes = nav();
  const [name, id] = state.route.split("/");
  const views = { inventory: id ? vehicleView : inventoryView, listings: id ? listingView : listingsView, publishing: publishingView,
    imports: id ? importView : importsView, reports: reportsView, settings: settingsView, outreach: outreachView };
  if (!routes.find(([r]) => r === name) && name !== "outreach") { render(h("p", {}, "Select a dealership.")); return; }
  render(h("p", { class: "muted" }, "Loading…"));
  try { await views[name](id); } catch (e) { render(h("div", { class: "banner err" }, e.message)); }
}

// ---------- inventory ----------
async function inventoryView() {
  const q = h("input", { placeholder: "Search make, model, VIN, stock #" });
  const status = h("select", {}, ["", "available", "pending", "sold", "removed"].map((s) => h("option", { value: s }, s || "Any status")));
  const validation = h("select", {}, ["", "valid", "warnings", "invalid"].map((s) => h("option", { value: s }, s || "Any data quality")));
  const stale = h("select", {}, h("option", { value: "" }, "Fresh or stale"), h("option", { value: "true" }, "Stale only"));
  const body = h("div");
  const load = async () => {
    const p = new URLSearchParams();
    if (q.value) p.set("q", q.value); if (status.value) p.set("status", status.value);
    if (validation.value) p.set("validation", validation.value); if (stale.value) p.set("stale", stale.value);
    const r = await capi(`/vehicles?${p}`);
    body.replaceChildren(h("p", { class: "muted" }, `${r.total} vehicles`), h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Photo", "Vehicle", "Stock / VIN", "Miles", "Price", "Inventory", "Data", "Listing"].map((x) => h("th", {}, x)))),
      h("tbody", {}, r.items.map((v) => h("tr", { class: "click", onclick: () => (location.hash = `inventory/${v.id}`) },
        h("td", {}, v.photos[0] ? h("img", { src: v.photos[0], alt: "", width: 64, height: 48, loading: "lazy", referrerpolicy: "no-referrer" }) : chip("no photo", "warn")),
        h("td", {}, `${v.year ?? "?"} ${v.make ?? ""} ${v.model ?? ""} ${v.trim ?? ""}`),
        h("td", { class: "muted" }, `${v.stock_number || "—"} / ${v.vin || "no VIN"}`),
        h("td", {}, fmtNum(v.mileage)), h("td", {}, fmtMoney(v.price)),
        h("td", {}, statusChip(v.inventory_status), v.stale ? chip("stale", "err") : null),
        h("td", {}, statusChip(v.validation_status), v.validation_issues.length ? h("span", { class: "muted" }, ` ${v.validation_issues.length} issue(s)`) : null),
        h("td", {}, v.listing ? statusChip(v.listing.status) : "—", v.listing?.content_outdated ? chip("outdated", "warn") : null)))))));
  };
  [q, status, validation, stale].forEach((el) => el.addEventListener("change", load));
  render(h("h1", {}, "Inventory"),
    h("div", { class: "row panel" }, q, status, validation, stale, h("button", { class: "ghost", onclick: load }, "Refresh"),
      h("button", { onclick: () => manualVehicleModal(load) }, "Add vehicle manually")), body);
  await load();
}

function manualVehicleModal(after) {
  const fields = ["year", "make", "model", "trim", "body_style", "mileage", "price", "vin", "stock_number", "exterior_color", "interior_color",
    "transmission", "drivetrain", "fuel_type", "condition", "features", "photos", "location_address", "location_city", "location_state", "location_postal"];
  const inputs = Object.fromEntries(fields.map((f) => [f, h("input", { name: f })]));
  modal("Add vehicle (manual entry)", h("div", {},
    h("p", { class: "muted" }, "Enter only facts you have verified. Separate features and photo URLs with |."),
    h("div", { class: "grid2" }, fields.map((f) => h("div", {}, h("label", {}, f.replace(/_/g, " ")), inputs[f])))),
    async (close) => {
      const body = Object.fromEntries(Object.entries(inputs).filter(([, i]) => i.value.trim()).map(([k, i]) => [k, i.value.trim()]));
      await act(() => capi("/vehicles", { body }), "Vehicle added");
      close(); after();
    }, "Save vehicle");
}

async function vehicleView(id) {
  const v = await capi(`/vehicles/${id}`);
  const setStatus = (s) => act(() => capi(`/vehicles/${id}/status`, { body: { status: s, note: "Set in workspace" } }), `Marked ${s}`).then(() => vehicleView(id));
  render(
    h("p", {}, h("a", { href: "#inventory" }, "← Inventory")),
    h("h1", {}, `${v.year ?? "?"} ${v.make ?? ""} ${v.model ?? ""} ${v.trim ?? ""}`),
    v.stale ? h("div", { class: "banner err" }, "Availability unconfirmed: this vehicle has not appeared in a recent sync. Confirm it or mark it sold/removed.") : null,
    h("div", { class: "row panel" }, statusChip(v.inventory_status), statusChip(v.validation_status),
      v.listing ? h("a", { href: `#listings/${v.listing.id}` }, "Open listing →") : null,
      h("span", { style: "flex:1" }),
      h("button", { class: "ghost", onclick: () => act(() => capi(`/vehicles/${id}/confirm-availability`, { body: {} }), "Availability confirmed").then(() => vehicleView(id)) }, "Confirm still available"),
      h("button", { class: "ghost", onclick: () => setStatus("pending") }, "Mark pending"),
      h("button", { class: "danger", onclick: () => confirm("Mark this vehicle sold? Live listings will be flagged for withdrawal.") && setStatus("sold") }, "Mark sold")),
    h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("h2", {}, "Vehicle data (as supplied)"), kv({
        VIN: v.vin, "Stock #": v.stock_number, Mileage: fmtNum(v.mileage), Price: fmtMoney(v.price), Condition: v.condition,
        Exterior: v.exterior_color, Interior: v.interior_color, Body: v.body_style, Transmission: v.transmission, Drivetrain: v.drivetrain,
        Fuel: v.fuel_type, Engine: v.engine, "Title (dealer-reported)": v.title_status, Owners: v.owner_count,
        Location: [v.location.address, v.location.city, v.location.state, v.location.postal].filter(Boolean).join(", "),
        Features: (v.features || []).join(", "), "Last synced": fmtTime(v.last_synced_at), Version: v.version })),
      h("div", { class: "panel" }, h("h2", {}, "Data issues"), issuesList(v.validation_issues),
        h("h2", {}, `Photos (${v.photo_count})`), photoStrip(v.photos))),
    h("div", { class: "panel" }, h("h2", {}, "Synchronization history"), h("div", { class: "table-wrap" }, h("table", {},
      h("tbody", {}, v.changes.map((c) => h("tr", {}, h("td", {}, fmtTime(c.at)), h("td", {}, chip(c.type)), h("td", {}, h("code", {}, JSON.stringify(c.changes))))))))));
}

const kv = (o) => h("table", {}, h("tbody", {}, Object.entries(o).map(([k, v]) => h("tr", {}, h("th", {}, k), h("td", {}, v === null || v === undefined || v === "" ? h("span", { class: "muted" }, "not supplied") : String(v))))));
const issuesList = (issues) => (issues?.length ? h("ul", { class: "issues" }, issues.map((i) => h("li", { class: i.severity === "error" ? "err" : "warn" }, `${i.severity === "error" ? "Error" : "Warning"}: ${i.message}`))) : h("p", { class: "ok" }, "No issues"));
const photoStrip = (urls) => (urls?.length ? h("div", { class: "photos" }, urls.map((u, i) => h("a", { href: u, target: "_blank", rel: "noopener noreferrer" }, h("img", { src: u, alt: `Photo ${i + 1}`, loading: "lazy", referrerpolicy: "no-referrer" })))) : h("p", { class: "warn" }, "No photos supplied"));

// ---------- listings ----------
async function listingsView() {
  const status = h("select", {}, ["", "awaiting_approval", "draft_generated", "needs_validation", "approved", "rejected", "imported", "sold_withdrawn"]
    .map((s) => h("option", { value: s }, s ? LABEL[s] : "All statuses")));
  const selected = new Set();
  const body = h("div");
  const load = async () => {
    selected.clear();
    const r = await capi(`/listings${status.value ? `?status=${status.value}` : ""}`);
    body.replaceChildren(h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["", "Listing", "Price", "Status", "Content checks", "Channels"].map((x) => h("th", {}, x)))),
      h("tbody", {}, r.items.map((l) => h("tr", {},
        h("td", {}, h("input", { type: "checkbox", "aria-label": "Select for batch approval", onchange: (e) => (e.target.checked ? selected.add(l.id) : selected.delete(l.id)) })),
        h("td", {}, h("a", { href: `#listings/${l.id}` }, l.title || `${l.vehicle.year ?? ""} ${l.vehicle.make ?? ""} ${l.vehicle.model ?? ""}`),
          l.vehicle.stale ? chip("stale", "err") : null, l.content_outdated ? chip("outdated", "warn") : null),
        h("td", {}, fmtMoney(l.vehicle.price)), h("td", {}, statusChip(l.status)),
        h("td", {}, l.errors ? chip(`${l.errors} blocking`, "err") : null, l.warnings ? chip(`${l.warnings} warnings`, "warn") : null, !l.errors && !l.warnings ? chip("clean", "ok") : null),
        h("td", {}, l.publications.map((p) => h("div", {}, `${p.channel}: `, statusChip(p.status), p.needs_update ? chip("needs update", "warn") : null)))))))));
  };
  status.addEventListener("change", load);
  render(h("h1", {}, "Listings"),
    h("div", { class: "row panel" }, status, h("button", { class: "ghost", onclick: load }, "Refresh"),
      h("button", { onclick: () => batchApprove([...selected], load) }, "Approve selected…")), body);
  await load();
}

async function batchApprove(ids, after) {
  if (!ids.length) { toast("Select listings first"); return; }
  const pv = await act(() => capi("/listings/batch/preview", { body: { listing_ids: ids } }));
  const ack = h("input", { type: "checkbox" });
  modal(`Batch approval: ${pv.eligible_count} of ${pv.items.length} eligible`, h("div", {},
    h("p", { class: "muted" }, "Each listing is re-checked individually. Listings with blockers are skipped; listings with warnings are approved only if you acknowledge them."),
    h("ul", {}, pv.items.map((i) => h("li", {}, i.title || i.listing_id, " ", i.eligible ? chip("eligible", "ok") : chip("skipped", "err"),
      i.blockers.length ? h("span", { class: "err" }, ` ${i.blockers.join("; ")}`) : null,
      i.eligible && i.warnings.length ? h("span", { class: "warn" }, ` ${i.warnings.length} warning(s)`) : null))),
    pv.with_warnings ? h("label", {}, ack, ` I reviewed and accept the warnings on ${pv.with_warnings} listing(s)`) : null),
  async (close) => {
    const r = await act(() => capi("/listings/batch/approve", { body: { listing_ids: ids, expected_count: pv.eligible_count, confirm: true, acknowledge_all_warnings: ack.checked } }));
    toast(`Approved ${r.approved.length}, skipped ${r.skipped.length}`);
    close(); after();
  }, `Approve ${pv.eligible_count}`);
}

async function listingView(id) {
  const l = await capi(`/listings/${id}`);
  const rev = l.current_revision;
  const veh = await capi(`/vehicles/${l.vehicle_id}`);
  const reload = () => listingView(id);
  const head = [h("p", {}, h("a", { href: "#listings" }, "← Listings")),
    h("h1", {}, `${veh.year ?? "?"} ${veh.make ?? ""} ${veh.model ?? ""} ${veh.trim ?? ""}`),
    h("div", { class: "row panel" }, statusChip(l.status), l.content_outdated ? chip(`outdated: ${l.outdated_reason}`, "warn") : null,
      rev ? h("span", { class: "muted" }, `Revision ${rev.number} · ${rev.origin.replace("_", " ")}${rev.model ? ` (${rev.model})` : ""} · ${fmtTime(rev.created_at)}`) : null,
      h("span", { style: "flex:1" }),
      h("button", { class: "ghost", onclick: () => act(() => capi(`/listings/${id}/generate`, { body: {} }), "Draft regenerated").then(reload) }, rev ? "Regenerate" : "Generate draft"))];
  if (!rev) {
    render(...head, h("div", { class: "panel" }, h("h2", {}, "Vehicle data issues"), issuesList(veh.validation_issues)));
    return;
  }
  const g = rev.guardrails;
  const ackBoxes = rev.warning_keys.map((k, i) => ({ key: k, box: h("input", { type: "checkbox" }), w: g.warnings[i] }));
  const editors = {};
  const langCol = (lang, label) => {
    const c = rev.content[lang];
    if (!c) return h("div", { class: "panel" }, h("h2", {}, label), h("p", { class: "err" }, "Missing"));
    const title = h("input", { value: c.title, style: "width:100%" });
    const desc = h("textarea", { rows: 18 }, c.description);
    editors[lang] = { title, desc, orig: c };
    return h("div", { class: "panel" }, h("h2", {}, label),
      h("label", {}, `Title (${c.title.length} chars)`), title, h("label", {}, "Description"), desc,
      h("h3", {}, "Features (from dealer data)"), h("p", {}, c.features.join(" · ") || h("span", { class: "muted" }, "none supplied")),
      h("h3", {}, "Search terms"), h("p", { class: "muted" }, c.search_terms.join(", ")),
      h("h3", {}, "Common questions"), h("div", {}, c.faq.map((f) => h("details", {}, h("summary", {}, f.q), h("p", {}, f.a)))));
  };
  const saveEdits = async () => {
    const changes = {};
    for (const [lang, e] of Object.entries(editors)) {
      const ch = {};
      if (e.title.value !== e.orig.title) ch.title = e.title.value;
      if (e.desc.value !== e.orig.description) ch.description = e.desc.value;
      if (Object.keys(ch).length) changes[lang] = ch;
    }
    if (!Object.keys(changes).length) { toast("No changes"); return; }
    await act(() => capi(`/listings/${id}/edit`, { body: { changes } }), "Saved as a new revision (re-checked)");
    reload();
  };
  const note = h("input", { placeholder: "Reason / feedback", style: "min-width:280px" });
  render(...head,
    g.errors.length ? h("div", { class: "banner err" }, h("strong", {}, "Blocking issues (fix before approval)"), h("ul", {}, g.errors.map((e) => h("li", {}, `${e.lang ? `[${e.lang}] ` : ""}${e.message}`)))) : null,
    rev.uncertainty.length ? h("div", { class: "banner warn" }, h("strong", {}, "Uncertain or missing information (not invented)"), h("ul", {}, rev.uncertainty.map((u) => h("li", {}, u)))) : null,
    h("div", { class: "grid2" }, langCol("en", "English"), langCol("es", "Español")),
    h("div", { class: "panel" }, h("h2", {}, "Review and approve"),
      ackBoxes.length ? h("div", {}, h("p", {}, "Acknowledge each warning:"), ackBoxes.map((a) => h("label", {}, a.box, ` [${a.w.lang || "-"}] ${a.w.message}`))) : h("p", { class: "ok" }, "No warnings."),
      l.status === "approved" ? h("p", { class: "ok" }, `Approved ${fmtTime(l.approved_at)}. Edits or inventory changes create a new revision that needs approval.`)
        : l.approval_blockers?.length ? h("p", { class: "err" }, `Cannot approve yet: ${l.approval_blockers.join("; ")}`) : null,
      h("div", { class: "row" },
        h("button", { class: "ghost", onclick: saveEdits }, "Save edits"),
        h("button", { disabled: l.status === "approved" || l.approval_blockers?.some((b) => !b.includes("acknowledged")) || false, onclick: async () => {
          await act(() => capi(`/listings/${id}/approve`, { body: { revision_id: rev.id, acknowledged_warnings: ackBoxes.filter((a) => a.box.checked).map((a) => a.key) } }), "Approved");
          reload();
        } }, "Approve this revision"),
        note,
        h("button", { class: "ghost", onclick: () => act(() => capi(`/listings/${id}/regenerate`, { body: { note: note.value } }), "Regeneration requested").then(reload) }, "Request regeneration"),
        h("button", { class: "danger", onclick: () => act(() => capi(`/listings/${id}/reject`, { body: { note: note.value } }), "Rejected").then(reload) }, "Reject"))),
    h("div", { class: "panel" }, h("h2", {}, "Photos (from the dealer's inventory)"), photoStrip(rev.content.photos)),
    h("div", { class: "panel" }, h("h2", {}, "Channels"), l.publications.length ? l.publications.map((p) => publicationCard(p, reload))
      : h("p", { class: "muted" }, "Channels appear here after approval.")),
    h("div", { class: "panel" }, h("h2", {}, "Revision history"), h("div", { id: "revs" }, h("button", { class: "ghost", onclick: async (e) => {
      const revs = await capi(`/listings/${id}/revisions`);
      e.target.parentElement.replaceChildren(h("table", {}, h("tbody", {}, revs.map((r) => h("tr", {},
        h("td", {}, `#${r.number}`), h("td", {}, r.origin), h("td", {}, r.created_by), h("td", {}, fmtTime(r.created_at)),
        h("td", {}, r.is_approved ? chip("approved", "ok") : null, r.is_current ? chip("current") : null),
        h("td", {}, `${r.guardrails.errors.length} errors, ${r.guardrails.warnings.length} warnings`), h("td", { class: "muted" }, r.feedback || ""))))));
    } }, "Show revisions"))),
    h("div", { class: "panel" }, h("h2", {}, "Review log"), h("ul", {}, l.reviews.map((r) => h("li", {}, `${fmtTime(r.at)} · ${r.decision}${r.note ? ` · ${r.note}` : ""}`)))));
}

function publicationCard(p, reload) {
  const card = h("div", { class: "panel" }, h("div", { class: "row" }, h("strong", {}, p.channel_label), statusChip(p.status),
    chip(p.mode === "manual" ? "manual workflow" : p.mode === "api" ? "API integration" : "sandbox (simulated)"),
    p.confirmation_type ? chip(p.confirmation_type === "api" ? "confirmed by API" : "confirmed by staff attestation", "ok") : null,
    p.needs_update ? chip("needs update", "warn") : null),
    p.update_reason ? h("p", { class: "warn" }, p.update_reason) : null, p.last_error ? h("p", { class: "err" }, p.last_error) : null,
    p.external_url ? h("p", {}, "Live listing: ", h("a", { href: p.external_url, target: "_blank", rel: "noopener noreferrer" }, p.external_url)) : null);
  const actions = h("div", { class: "row" });
  card.append(actions);
  if (p.mode === "manual") {
    actions.append(h("button", { onclick: () => manualPackage(p.id, reload) }, "Open publishing package"));
  } else {
    if (["ready_to_publish", "published_confirmed"].includes(p.status))
      actions.append(h("button", { onclick: () => act(() => capi(`/publications/${p.id}/publish`, { body: {} }), "Queued — status updates after the provider responds").then(reload) },
        p.status === "published_confirmed" ? "Push update" : "Publish"));
    if (p.status === "failed") actions.append(h("button", { onclick: () => act(() => capi(`/publications/${p.id}/retry`, { body: {} }), "Retry queued").then(reload) }, "Retry"));
  }
  return card;
}

async function manualPackage(pubId, reload) {
  const p = await capi(`/publications/${pubId}`);
  const pkg = p.package;
  const copyBtn = (text, label = "Copy") => h("button", { class: "ghost", onclick: () => navigator.clipboard.writeText(String(text ?? "")).then(() => toast("Copied")) }, label);
  const url = h("input", { placeholder: "https://www.facebook.com/marketplace/item/…", style: "width:100%" });
  const attest = h("input", { type: "checkbox" });
  const notes = h("input", { placeholder: "Notes (optional)", style: "width:100%" });
  const content = h("div", {},
    h("div", { class: "banner warn" }, "Facebook does not offer an API for Marketplace vehicle listings. A staff member posts this manually from their own account, then records the live URL here."),
    p.package_problems?.length ? h("div", { class: "banner err" }, p.package_problems.join("; ")) : null,
    pkg ? h("div", {},
      h("h3", {}, "Fields"), h("table", {}, h("tbody", {}, Object.entries(pkg.fields).map(([k, v]) => h("tr", {}, h("th", {}, k.replace(/_/g, " ")),
        h("td", {}, v ?? h("span", { class: "muted" }, "leave blank")), h("td", {}, v !== null && v !== undefined ? copyBtn(v) : null))))),
      ...Object.entries(pkg.copy).map(([lang, c]) => h("div", {}, h("h3", {}, lang === "en" ? "English copy" : "Spanish copy"),
        h("div", { class: "row" }, h("strong", {}, c.title), copyBtn(c.title, "Copy title")), h("pre", {}, c.description), copyBtn(c.description, "Copy description"))),
      h("h3", {}, `Photos (${pkg.photos.length}) — upload in this order`), h("ol", {}, pkg.photos.map((u) => h("li", {}, h("a", { href: u, target: "_blank", rel: "noopener noreferrer" }, u)))),
      h("h3", {}, "Checklist"), h("ol", {}, pkg.checklist.map((c) => h("li", {}, c)))) : null,
    h("h3", {}, "Record the result"),
    p.status === "manual_required" || p.status === "published_confirmed" ? h("div", {},
      h("label", {}, "Live listing URL"), url, h("label", {}, attest, " I personally published this listing and checked that it matches this package."), notes) : null,
    h("h3", {}, "History"), h("ul", {}, (p.operations || []).map((o) => h("li", {}, `${fmtTime(o.created_at)} · ${o.operation} · ${o.status}${o.error ? ` · ${o.error}` : ""}`))));
  const extra = [];
  if (p.status === "published_confirmed") extra.push(["Record that I updated the live listing", () => capi(`/publications/${pubId}/record-update`, { body: { attested: attest.checked, notes: notes.value } })]);
  if (["published_confirmed", "withdrawal_required", "manual_required"].includes(p.status)) extra.push(["Record removal from Facebook", () => capi(`/publications/${pubId}/record-removal`, { body: { attested: attest.checked, notes: notes.value } })]);
  modal("Facebook Marketplace — manual publishing", content, p.status === "manual_required" ? async (close) => {
    await act(() => capi(`/publications/${pubId}/confirm-manual`, { body: { url: url.value, attested: attest.checked, notes: notes.value } }), "Publication recorded");
    close(); reload();
  } : null, "Record publication", extra.map(([label, fn]) => [label, async (close) => { await act(fn, "Recorded"); close(); reload(); }]));
}

// ---------- publishing ----------
async function publishingView() {
  const [pubs, channels] = await Promise.all([capi("/publications"), capi("/channels")]);
  const attention = pubs.filter((p) => p.needs_update || ["withdrawal_required", "failed", "manual_required"].includes(p.status));
  render(h("h1", {}, "Publishing"),
    h("div", { class: "panel" }, h("h2", {}, `Needs action (${attention.length})`), attention.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("tbody", {}, attention.map((p) => h("tr", { class: "click", onclick: () => (location.hash = `listings/${p.listing_id}`) },
        h("td", {}, p.channel_label), h("td", {}, statusChip(p.status)), h("td", {}, p.update_reason || p.last_error || ""))))))
      : h("p", { class: "ok" }, "Nothing needs attention.")),
    h("div", { class: "panel" }, h("h2", {}, "Channels"), channels.map((c) => h("details", {},
      h("summary", {}, `${c.label} `, chip(c.mode === "manual" ? "manual workflow" : c.mode === "api" ? "API integration" : "sandbox"),
        c.connection?.live_publishing ? chip("live publishing authorized", "warn") : null),
      kv(Object.fromEntries(Object.entries(c.docs).map(([k, v]) => [k.replace(/_/g, " "), Array.isArray(v) ? v.join(", ") : v])))))),
    h("div", { class: "panel" }, h("h2", {}, "Exports"), h("p", { class: "muted" }, "Approved, available listings as CSV for authorised manual upload."),
      h("div", { class: "row" }, ["en", "es"].map((lang) => h("button", { class: "ghost", onclick: () => download(`/exports/listings.csv?lang=${lang}`, `approved-listings-${lang}.csv`) }, `Download ${lang.toUpperCase()} CSV`)))));
}

async function download(path, filename) {
  const res = await fetch(`/api/clients/${state.clientId}${path}`, { headers: { Authorization: `Bearer ${state.token}` } });
  if (!res.ok) { toast("Download failed"); return; }
  const a = h("a", { href: URL.createObjectURL(await res.blob()), download: filename });
  a.click();
}

// ---------- imports ----------
async function importsView() {
  const [runs, sources] = await Promise.all([capi("/imports"), capi("/sources")]);
  const file = h("input", { type: "file", accept: ".csv,.tsv,.txt,.xlsx,.json" });
  const srcSel = h("select", {}, h("option", { value: "" }, "New upload source"), sources.filter((s) => s.source_type !== "manual").map((s) => h("option", { value: s.id }, `${s.name} (${s.source_type})`)));
  const cmap = h("input", { placeholder: '{"price": "Internet Price"} (optional column mapping)', style: "min-width:320px" });
  render(h("h1", {}, "Imports"),
    h("form", { class: "panel", onsubmit: async (e) => {
      e.preventDefault();
      if (!file.files[0]) { toast("Choose a file"); return; }
      const fd = new FormData(); fd.append("file", file.files[0]);
      if (srcSel.value) fd.append("source_id", srcSel.value); if (cmap.value) fd.append("column_map", cmap.value);
      const r = await act(() => capi("/imports/upload", { body: fd }));
      toast(`Import ${r.status}: ${r.counts.created} new, ${r.counts.updated} updated, ${r.counts.rejected} rejected`);
      location.hash = `imports/${r.id}`;
    } }, h("h2", {}, "Upload dealership inventory (CSV, spreadsheet or DMS export)"), h("div", { class: "row" }, file, srcSel, cmap, h("button", { type: "submit" }, "Import"))),
    h("div", { class: "panel" }, h("h2", {}, "Sources"), h("table", {}, h("tbody", {}, sources.map((s) => h("tr", {},
      h("td", {}, s.name), h("td", {}, chip(s.source_type)), h("td", {}, s.last_status ? statusChip(s.last_status) : chip("never synced")),
      h("td", { class: "muted" }, fmtTime(s.last_sync_at)), h("td", { class: "muted" }, s.schedule_minutes ? `every ${s.schedule_minutes} min` : "on upload"),
      h("td", {}, s.config?.url ? h("button", { class: "ghost", onclick: () => act(() => capi(`/sources/${s.id}/sync`, { body: {} }), "Sync queued") }, "Sync now") : null)))))),
    h("div", { class: "panel" }, h("h2", {}, "Import history"), h("table", {}, h("tbody", {}, runs.map((r) => h("tr", { class: "click", onclick: () => (location.hash = `imports/${r.id}`) },
      h("td", {}, fmtTime(r.started_at)), h("td", {}, r.file_name || r.trigger), h("td", {}, statusChip(r.status)),
      h("td", { class: "muted" }, `${r.counts.created ?? 0} new · ${r.counts.updated ?? 0} updated · ${r.counts.removed ?? 0} removed · ${r.counts.rejected ?? 0} rejected`),
      h("td", { class: "err" }, r.error || "")))))));
}

async function importView(id) {
  const r = await capi(`/imports/${id}`);
  render(h("p", {}, h("a", { href: "#imports" }, "← Imports")), h("h1", {}, `Import ${r.file_name || r.trigger}`),
    h("div", { class: "row panel" }, statusChip(r.status), h("span", { class: "muted" }, JSON.stringify(Object.fromEntries(Object.entries(r.counts).filter(([, v]) => typeof v === "number"))))),
    r.error ? h("div", { class: "banner warn" }, r.error) : null,
    r.counts.unmapped_headers?.length ? h("div", { class: "banner warn" }, `Columns not used: ${r.counts.unmapped_headers.join(", ")}`) : null,
    h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Row", "ID", "Outcome", "Issues", "Source row"].map((x) => h("th", {}, x)))),
      h("tbody", {}, r.records.map((x) => h("tr", {}, h("td", {}, x.row), h("td", {}, x.vehicle_id ? h("a", { href: `#inventory/${x.vehicle_id}` }, x.external_id) : x.external_id || "—"),
        h("td", {}, chip(x.outcome, x.outcome === "rejected" ? "err" : "")), h("td", {}, issuesList(x.issues)),
        h("td", {}, h("details", {}, h("summary", {}, "raw"), h("pre", {}, JSON.stringify(x.raw, null, 1))))))))));
}

// ---------- reports ----------
async function reportsView() {
  const m = await capi("/reports/summary?days=30");
  const tile = (v, l) => h("div", { class: "tile" }, h("div", { class: "v" }, fmtNum(v)), h("div", { class: "l" }, l));
  render(h("h1", {}, "Reports (last 30 days)"),
    h("div", { class: "tiles" },
      tile(m.inventory.imported_in_period, "Vehicles imported"), tile(m.listings.generated_in_period, "Listings generated"),
      tile(m.listings.approved_in_period, "Listings approved"), tile(m.publishing.published_confirmed_via_api, "Published via confirmed API"),
      tile(m.publishing.published_confirmed_manually, "Published manually (staff-attested)"), tile(m.publishing.manual_publication_required, "Manual publication required"),
      tile(m.publishing.failed, "Failed publications"), tile(m.inventory.stale, "Stale inventory"),
      tile(m.publishing.sold_or_removed_requiring_withdrawal, "Sold/removed needing withdrawal"), tile(m.inventory.with_missing_or_inconsistent_data, "Vehicles with data issues")),
    h("div", { class: "panel" }, h("h2", {}, "Synchronization health"), h("table", {}, h("tbody", {}, m.sync_health.map((s) => h("tr", {},
      h("td", {}, s.name), h("td", {}, chip(s.type)), h("td", {}, chip(s.health, s.health === "healthy" ? "ok" : s.health === "never_synced" ? "" : "err")), h("td", { class: "muted" }, fmtTime(s.last_sync_at))))))),
    h("p", { class: "muted" }, `Not measured: ${m.not_measured.join(", ")} — no channel provides reliable data for these yet.`));
}

// ---------- settings ----------
async function settingsView() {
  const { settings: s, version } = await capi("/listing-settings");
  const inp = (v, attrs = {}) => h("input", { value: v ?? "", style: "width:100%", ...attrs });
  const f = {
    brand_voice: h("select", {}, ["professional", "friendly", "straightforward"].map((x) => h("option", { value: x }, x))),
    brand_voice_notes: inp(s.brand_voice_notes),
    dealership_name: inp(s.contact.dealership_name), phone: inp(s.contact.phone), email: inp(s.contact.email), website: inp(s.contact.website), address: inp(s.contact.address),
    hours_en: inp(s.business_hours.en), hours_es: inp(s.business_hours.es),
    cta_en: inp(s.call_to_action.en), cta_es: inp(s.call_to_action.es),
    fin_en: inp(s.financing_statement.en), fin_es: inp(s.financing_statement.es),
    disclaimers: h("textarea", { rows: 4 }, s.disclaimers.map((d) => `${d.en} || ${d.es}`).join("\n")),
    title_template: inp(s.title_template), required_fields: inp(s.required_fields.join(", ")), enabled_channels: inp(s.enabled_channels.join(", ")),
    allow_agency_approval: h("input", { type: "checkbox", checked: s.review.allow_agency_approval }),
    require_both_languages: h("input", { type: "checkbox", checked: s.review.require_both_languages }),
    show_vin: h("input", { type: "checkbox", checked: s.show_vin }), use_dealer_description: h("input", { type: "checkbox", checked: s.use_dealer_description }),
  };
  f.brand_voice.value = s.brand_voice;
  const L = (label, el) => h("div", {}, h("label", {}, label), el);
  const C = (label, el) => h("label", {}, el, ` ${label}`);
  render(h("h1", {}, `Listing settings (v${version})`),
    h("form", { class: "panel", onsubmit: async (e) => {
      e.preventDefault();
      const split = (x) => x.split(",").map((y) => y.trim()).filter(Boolean);
      await act(() => capi("/listing-settings", { method: "PUT", body: {
        brand_voice: f.brand_voice.value, brand_voice_notes: f.brand_voice_notes.value,
        contact: { dealership_name: f.dealership_name.value, phone: f.phone.value, email: f.email.value, website: f.website.value, address: f.address.value },
        business_hours: { en: f.hours_en.value, es: f.hours_es.value }, call_to_action: { en: f.cta_en.value, es: f.cta_es.value },
        financing_statement: { en: f.fin_en.value, es: f.fin_es.value },
        disclaimers: f.disclaimers.value.split("\n").filter((l) => l.trim()).map((l) => { const [en, es] = l.split("||"); return { en: (en || "").trim(), es: (es || "").trim() }; }),
        title_template: f.title_template.value, required_fields: split(f.required_fields.value), enabled_channels: split(f.enabled_channels.value),
        review: { ...s.review, allow_agency_approval: f.allow_agency_approval.checked, require_both_languages: f.require_both_languages.checked },
        show_vin: f.show_vin.checked, use_dealer_description: f.use_dealer_description.checked,
      } }), "Settings saved. Regenerate drafts to apply them.");
      settingsView();
    } },
      h("div", { class: "grid2" },
        L("Brand voice", f.brand_voice), L("Voice notes", f.brand_voice_notes), L("Dealership name", f.dealership_name), L("Phone", f.phone),
        L("Email", f.email), L("Website", f.website), L("Address", f.address), L("Title template ({year} {make} {model} {trim})", f.title_template),
        L("Business hours (English)", f.hours_en), L("Horario (Español)", f.hours_es), L("Call to action (English, optional)", f.cta_en),
        L("Llamado a la acción (Español, opcional)", f.cta_es), L("Approved financing statement (English; leave blank to never mention financing)", f.fin_en),
        L("Declaración de financiamiento aprobada (Español)", f.fin_es), L("Required fields", f.required_fields), L("Enabled channels", f.enabled_channels)),
      L("Approved disclaimers, one per line: English || Español", f.disclaimers),
      h("div", { class: "row" }, C("Agency staff may approve listings", f.allow_agency_approval), C("Require both languages", f.require_both_languages),
        C("Show VIN in listings", f.show_vin), C("Include dealer-written notes (verify claims)", f.use_dealer_description)),
      h("p"), h("button", { type: "submit" }, "Save settings")));
}

// ---------- outreach (agency only) ----------
async function outreachView() {
  const [d, pending, esc, prospects] = await Promise.all([api("/api/outreach/dashboard"), api("/api/outreach/messages?status=awaiting_approval"),
    api("/api/outreach/escalations"), api("/api/outreach/prospects?limit=50")]);
  const m = d.measured;
  const tile = (v, l, est) => h("div", { class: "tile" }, h("div", { class: "v" }, v ?? "—"), h("div", { class: "l" }, l, est ? " (estimate)" : ""));
  const pct = (x) => (x === null || x === undefined ? "—" : `${(x * 100).toFixed(1)}%`);
  const reload = () => outreachView();
  const stopReason = h("input", { placeholder: "Reason for emergency stop" });
  render(h("h1", {}, "Outreach agent"),
    d.agent.emergency_stop ? h("div", { class: "banner err" }, "EMERGENCY STOP is active. No outreach actions will run.") : null,
    m.sent_to_simulated_prospects === m.outreach_attempts_sent && m.outreach_attempts_sent ? h("div", { class: "banner" }, "All sends so far went to simulated prospects through sandbox providers.") : null,
    h("div", { class: "row panel" }, h("strong", {}, "Agent state: "), chip(d.agent.state, d.agent.state === "active" ? "ok" : "warn"),
      h("span", { class: "muted" }, d.agent.last_run ? `last run ${fmtTime(d.agent.last_run.started_at)} · ${d.agent.last_run.state}` : "never run"),
      h("span", { class: "muted" }, ` · workers: ${d.agent.workers.map((w) => `${w.status}`).join(", ") || "none running"}`),
      h("span", { style: "flex:1" }),
      h("button", { class: "ghost", onclick: () => act(() => api("/api/outreach/tick", { body: {} }), "Tick queued").then(reload) }, "Run a tick now"),
      h("button", { class: "ghost", onclick: () => act(() => api("/api/outreach/pause", { body: { paused: !d.agent.global_pause } }), "Updated").then(reload) }, d.agent.global_pause ? "Resume" : "Pause all"),
      stopReason, h("button", { class: "danger", onclick: () => confirm("Stop ALL outreach immediately?") && act(() => api("/api/outreach/emergency-stop", { body: { reason: stopReason.value || "manual stop" } }), "Emergency stop engaged").then(reload) }, "Emergency stop")),
    h("h2", {}, "Measured results (last 30 days)"),
    h("div", { class: "tiles" }, tile(m.prospects_total, "Prospects in pipeline"), tile(m.prospects_researched, "Prospects researched"),
      tile(m.outreach_attempts_sent, "Emails sent"), tile(m.replies, "Replies"), tile(m.positive_replies, "Positive replies"), tile(m.opt_outs, "Opt-outs"),
      tile(m.follow_ups_due, "Follow-ups due"), tile(m.awaiting_approval, "Awaiting approval"), tile(m.meetings_booked, "Meetings booked"),
      tile(m.open_escalations, "Open escalations"), tile(m.errors.failed_messages + m.errors.dead_jobs, "Errors"),
      tile(pct(m.conversion.reply_rate), "Reply rate"), tile(pct(m.conversion.meeting_rate), "Meeting rate"),
      tile(`$${d.costs.measured_usd.toFixed(2)}`, "Costs (measured)"), tile(`$${d.costs.estimated_usd.toFixed(2)}`, "Costs", true)),
    h("div", { class: "panel" }, h("h2", {}, `Approval queue (${pending.length})`), pending.length ? pending.map((msg) => {
      const subj = h("input", { value: msg.subject, style: "width:100%" });
      const body = h("textarea", { rows: 10 }, msg.body);
      return h("div", { class: "panel" }, h("div", { class: "row" }, chip(msg.kind), msg.step ? chip(`step ${msg.step}`) : null, h("span", {}, `To: ${msg.to}`)),
        subj, body, h("div", { class: "row" },
          h("button", { onclick: () => act(() => api(`/api/outreach/messages/${msg.id}/approve`, { body: { subject: subj.value, body: body.value } }), "Approved; it sends within limits and schedule").then(reload) }, "Approve"),
          h("button", { class: "ghost", onclick: () => act(() => api(`/api/outreach/messages/${msg.id}/reject`, { body: { reason: prompt("Reason?") || "rejected" } }), "Rejected").then(reload) }, "Reject")));
    }) : h("p", { class: "muted" }, "Nothing waiting.")),
    h("div", { class: "panel" }, h("h2", {}, `Escalations (${esc.length})`), esc.map((e) => h("div", { class: "row" }, chip(e.kind, "warn"), h("span", {}, e.summary),
      h("button", { class: "ghost", onclick: () => act(() => api(`/api/outreach/escalations/${e.id}/resolve`, { body: { resolution: prompt("Resolution?") || "handled" } }), "Resolved").then(reload) }, "Resolve")))),
    h("div", { class: "panel" }, h("h2", {}, "Integrations"), h("table", {}, h("tbody", {}, d.integrations.map((i) => h("tr", {},
      h("td", {}, i.name), h("td", {}, chip(i.kind)), h("td", {}, chip(i.live ? "live provider" : "sandbox", i.live ? "warn" : "")),
      h("td", {}, i.live ? chip(i.live_authorized ? "authorized" : "not authorized", i.live_authorized ? "ok" : "err") : null),
      h("td", {}, chip(i.health, i.health === "ok" ? "ok" : "warn")), h("td", { class: "muted" }, i.detail || "")))))),
    h("div", { class: "panel" }, h("h2", {}, "Top prospects"), h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, prospects.map((p) => h("tr", {},
      h("td", {}, p.business_name, p.is_simulated ? chip("simulated") : null), h("td", {}, p.city || ""), h("td", {}, statusChip(p.status)),
      h("td", {}, Math.round(p.priority_score)), h("td", { class: "muted" }, p.disqualify_reason || ""))))))));
}

// ---------- modal ----------
function modal(title, content, onOk, okLabel = "OK", extra = []) {
  const bg = h("div", { class: "modal-bg" });
  const close = () => bg.remove();
  bg.addEventListener("click", (e) => { if (e.target === bg) close(); });
  bg.append(h("div", { class: "modal", role: "dialog", "aria-label": title }, h("h2", {}, title), content,
    h("div", { class: "row", style: "margin-top:12px" },
      onOk ? h("button", { onclick: () => onOk(close).catch(() => {}) }, okLabel) : null,
      ...extra.map(([label, fn]) => h("button", { class: "ghost", onclick: () => fn(close).catch(() => {}) }, label)),
      h("button", { class: "ghost", onclick: close }, "Close"))));
  document.body.append(bg);
}

if (state.token) boot().catch(() => logout()); else renderLogin();
