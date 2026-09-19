"use strict";

const state = {
  cases: [],
  documents: [],
  jobs: [],
  suppliers: [],
  health: null,
  activeView: "overview",
  caseFilter: "all",
  documentFilter: "all",
  caseQuery: "",
  documentQuery: "",
  supportDocumentId: null,
  activeInvoiceDocumentId: null,
  supportRequirements: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function readable(value) {
  return String(value ?? "unknown").replaceAll("_", " ");
}

function errorMessage(payload, fallback = "The request could not be completed.") {
  if (!payload) return fallback;
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail)) return payload.detail.map(item => item.msg).join(" · ");
  return payload.message || fallback;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const error = new Error(errorMessage(body, `Request failed with status ${response.status}.`));
    error.status = response.status;
    error.payload = body;
    throw error;
  }
  return body;
}

function formatMoney(value, currency = "USD", compact = false) {
  const amount = Number(value || 0);
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency", currency, maximumFractionDigits: compact ? 0 : 2,
      notation: compact && Math.abs(amount) >= 100000 ? "compact" : "standard",
    }).format(amount);
  } catch {
    return `${currency} ${amount.toFixed(2)}`;
  }
}

function formatDate(value, withTime = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat("en-US", withTime
    ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
    : { month: "short", day: "numeric", year: "numeric" }).format(date);
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function statusBadge(status) {
  const safeStatus = String(status || "new").replace(/[^a-z_]/gi, "");
  return `<span class="status-badge status-${safeStatus}">${escapeHtml(readable(status))}</span>`;
}

function latestReport(item) {
  return [...(item.events || [])].reverse().find(event => event.event_type === "reconciliation_completed");
}

function exceptionCount(item) {
  return Number(latestReport(item)?.detail?.exception_count || 0);
}

function caseUpdated(item) {
  const events = item.events || [];
  return events.length ? events[events.length - 1].created_at : item.invoice.invoice_date;
}

function supplierName(id) {
  return state.suppliers.find(item => item.id === id)?.legal_name || `Supplier #${id}`;
}

function jobFor(documentId) {
  return state.jobs.find(job => job.document_id === documentId) || null;
}

function showToast(title, message = "", kind = "success") {
  const stack = $("#toast-stack");
  const toast = document.createElement("div");
  toast.className = `toast ${kind === "error" ? "error" : ""}`;
  toast.innerHTML = `<span class="toast-symbol">${kind === "error" ? "!" : "✓"}</span><div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(message)}</small></div><button aria-label="Dismiss">×</button>`;
  toast.querySelector("button").addEventListener("click", () => toast.remove());
  stack.append(toast);
  window.setTimeout(() => toast.remove(), 5500);
}

function switchView(view) {
  state.activeView = view;
  $$(".view").forEach(node => node.classList.toggle("active", node.id === `${view}-view`));
  $$(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.view === view));
  $("#sidebar").classList.remove("open");
  $("#mobile-scrim").classList.remove("open");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function loadAll({ silent = false } = {}) {
  const refresh = $("#refresh-button");
  refresh.classList.add("loading");
  try {
    const results = await Promise.allSettled([
      api("/cases"), api("/documents"), api("/document-jobs"), api("/suppliers"), api("/health/ready"),
    ]);
    const names = ["cases", "documents", "jobs", "suppliers", "health"];
    results.forEach((result, index) => {
      if (result.status === "fulfilled") state[names[index]] = result.value;
    });
    const failed = results.filter(result => result.status === "rejected");
    renderAll();
    $("#last-updated").textContent = `Updated ${new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" }).format(new Date())}`;
    if (failed.length && !silent) showToast("Partial refresh", failed[0].reason.message, "error");
  } catch (error) {
    if (!silent) showToast("Could not refresh", error.message, "error");
  } finally {
    refresh.classList.remove("loading");
  }
}

function renderAll() {
  renderHealth();
  renderOverview();
  renderCases();
  renderDocuments();
}

function renderHealth() {
  const checks = state.health?.checks || {};
  $("#health-database").textContent = checks.database || "unknown";
  $("#health-ocr").textContent = checks.ocr || "unknown";
  $("#health-worker").textContent = checks.worker_queue || "unknown";
  $("#system-pulse").classList.toggle("error", state.health?.status !== "ready");
}

function renderOverview() {
  const open = state.cases.filter(item => !["approved", "rejected"].includes(item.status));
  const review = state.cases.filter(item => item.status === "needs_review");
  const exceptions = state.cases.filter(item => exceptionCount(item) > 0);
  const approved = state.cases.filter(item => item.status === "approved");
  const approvedValue = approved.reduce((sum, item) => sum + Number(item.invoice.total_amount || 0), 0);
  const completeJobs = state.jobs.filter(job => job.status === "complete");
  const activeJobs = state.jobs.filter(job => ["queued", "processing"].includes(job.status));
  const decided = state.cases.filter(item => ["approved", "rejected"].includes(item.status));

  $("#metric-open").textContent = open.length;
  $("#metric-open-note").textContent = `${review.length} require review`;
  $("#metric-exceptions").textContent = state.cases.length ? `${Math.round(exceptions.length / state.cases.length * 100)}%` : "0%";
  $("#metric-value").textContent = formatMoney(approvedValue, approved[0]?.invoice?.currency || "USD", true);
  $("#metric-documents").textContent = completeJobs.length;
  $("#metric-documents-note").textContent = `${activeJobs.length} waiting in queue`;
  $("#nav-review-count").textContent = review.length;
  $("#flow-intake").textContent = `${state.documents.length} documents`;
  $("#flow-extract").textContent = `${activeJobs.length} processing`;
  $("#flow-review").textContent = `${open.length} open cases`;
  $("#flow-decided").textContent = `${decided.length} completed`;

  const priority = [...open].sort((a, b) => {
    const statusWeight = value => value.status === "needs_review" ? 2 : value.status === "matched" ? 1 : 0;
    return statusWeight(b) - statusWeight(a) || b.id - a.id;
  }).slice(0, 5);
  $("#priority-list").innerHTML = priority.length ? priority.map(item => `
    <div class="priority-row" data-open-case="${item.id}" tabindex="0">
      <span class="priority-icon">INV</span>
      <div class="priority-main"><strong>${escapeHtml(item.invoice.invoice_number)}</strong><small>${escapeHtml(supplierName(item.invoice.supplier_id))} · ${exceptionCount(item)} exceptions</small></div>
      <span class="priority-amount">${escapeHtml(formatMoney(item.invoice.total_amount, item.invoice.currency))}</span><span class="row-arrow">›</span>
    </div>`).join("") : `<div class="empty-state"><span>✓</span><h3>Nothing needs attention</h3><p>Open cases will appear here.</p></div>`;

  $("#recent-documents").innerHTML = state.documents.slice(0, 5).map(document => documentRow(document, true)).join("") || `<tr><td colspan="5">No documents uploaded yet.</td></tr>`;
}

function filteredCases() {
  const query = state.caseQuery.trim().toLowerCase();
  return state.cases.filter(item => {
    const filterMatch = state.caseFilter === "all" || item.status === state.caseFilter;
    const text = `${item.invoice.invoice_number} ${supplierName(item.invoice.supplier_id)} ${item.invoice.currency} ${item.status}`.toLowerCase();
    return filterMatch && (!query || text.includes(query));
  });
}

function renderCases() {
  const counts = status => state.cases.filter(item => status === "all" || item.status === status).length;
  $("#filter-all-count").textContent = counts("all");
  $("#filter-review-count").textContent = counts("needs_review");
  $("#filter-matched-count").textContent = counts("matched");
  $("#filter-approved-count").textContent = counts("approved");
  const items = filteredCases();
  $("#case-table").innerHTML = items.map(item => {
    const errors = exceptionCount(item);
    return `<tr>
      <td><div class="invoice-cell"><strong>${escapeHtml(item.invoice.invoice_number)}</strong><small>Case #${item.id} · ${escapeHtml(item.invoice.invoice_date)}</small></div></td>
      <td>${escapeHtml(supplierName(item.invoice.supplier_id))}</td>
      <td><strong>${escapeHtml(formatMoney(item.invoice.total_amount, item.invoice.currency))}</strong></td>
      <td><span class="exception-count ${errors ? "has-errors" : ""}"><i></i>${errors || "None"}</span></td>
      <td>${statusBadge(item.status)}</td><td>${escapeHtml(formatDate(caseUpdated(item), true))}</td>
      <td><button class="row-action" data-open-case="${item.id}" aria-label="Open case">→</button></td>
    </tr>`;
  }).join("");
  $("#case-empty").classList.toggle("hidden", items.length > 0);
  $("#case-table").closest(".table-wrap").classList.toggle("hidden", items.length === 0);
}

function documentRow(document, compact = false) {
  const job = jobFor(document.id);
  const cells = `<td><div class="file-cell"><span class="file-icon">${document.media_type.includes("pdf") ? "PDF" : "IMG"}</span><div><strong>${escapeHtml(document.original_filename)}</strong><small>${escapeHtml(formatBytes(document.size_bytes))} · ${escapeHtml(document.id.slice(0, 8))}</small></div></div></td>
    <td><span class="type-chip">${escapeHtml(readable(document.document_type))}</span></td>
    <td>${statusBadge(job?.status || "queued")}</td>`;
  if (compact) return `<tr>${cells}<td>${escapeHtml(formatDate(document.created_at, true))}</td><td><button class="row-action" data-open-document="${escapeHtml(document.id)}" aria-label="Inspect document">→</button></td></tr>`;
  return `<tr>${cells}<td>${document.parent_type ? `${escapeHtml(readable(document.parent_type))} #${escapeHtml(document.parent_id)}` : "<span class=\"muted\">Not linked</span>"}</td><td>${escapeHtml(formatDate(document.created_at, true))}</td><td><button class="row-action" data-open-document="${escapeHtml(document.id)}" aria-label="Inspect document">→</button></td></tr>`;
}

function filteredDocuments() {
  const query = state.documentQuery.trim().toLowerCase();
  return state.documents.filter(document => {
    const filterMatch = state.documentFilter === "all" || document.document_type === state.documentFilter;
    const job = jobFor(document.id);
    const text = `${document.original_filename} ${document.document_type} ${job?.status || ""}`.toLowerCase();
    return filterMatch && (!query || text.includes(query));
  });
}

function renderDocuments() {
  const types = ["invoice", "purchase_order", "delivery_receipt", "contract"];
  $("#document-summary").innerHTML = types.map(type => `<div class="summary-chip"><span>${escapeHtml(readable(type))}</span><strong>${state.documents.filter(item => item.document_type === type).length}</strong></div>`).join("");
  const items = filteredDocuments();
  $("#document-table").innerHTML = items.map(document => documentRow(document)).join("");
  $("#document-empty").classList.toggle("hidden", items.length > 0);
  $("#document-table").closest(".table-wrap").classList.toggle("hidden", items.length === 0);
}

function openDrawer() {
  $("#case-drawer").classList.add("open");
  $("#drawer-scrim").classList.add("open");
  $("#case-drawer").setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
}

function closeDrawer() {
  $("#case-drawer").classList.remove("open");
  $("#drawer-scrim").classList.remove("open");
  $("#case-drawer").setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
}

async function openCase(caseId) {
  openDrawer();
  $("#drawer-loading").classList.remove("hidden");
  $("#case-detail").innerHTML = "";
  try {
    const item = await api(`/cases/${caseId}`);
    const document = state.documents.find(record => record.parent_type === "invoice" && record.parent_id === item.invoice.id);
    let extraction = null;
    if (document) {
      try { extraction = await api(`/documents/${document.id}/extraction`); } catch { extraction = null; }
    }
    $("#drawer-loading").classList.add("hidden");
    renderCaseDetail(item, document, extraction);
  } catch (error) {
    closeDrawer();
    showToast("Could not open case", error.message, "error");
  }
}

function renderCaseDetail(item, document, extraction) {
  const report = latestReport(item);
  const checks = report?.detail?.checks || [];
  const final = ["approved", "rejected"].includes(item.status);
  const fields = extraction?.output_json?.fields || {};
  const evidence = Object.entries(fields).map(([name, field]) => {
    const quote = field.evidence?.[0];
    return `<div class="evidence-card"><span>${escapeHtml(readable(name))}</span><strong>${escapeHtml(field.value ?? "Not found")}</strong>${quote ? `<div class="evidence-quote">Page ${quote.page} · “${escapeHtml(quote.text)}”</div>` : `<div class="evidence-quote">No source evidence</div>`}</div>`;
  }).join("");
  const checkHtml = checks.length ? checks.map(check => {
    const failed = check.status === "exception" || check.severity === "blocking";
    return `<div class="check-card ${failed ? "exception" : ""}"><span class="check-state">${failed ? "!" : "✓"}</span><div class="check-copy"><strong>${escapeHtml(readable(check.code))}</strong><p>${escapeHtml(check.message)}</p></div><div class="check-values"><span>Expected: ${escapeHtml(check.expected ?? "—")}</span><span>Actual: ${escapeHtml(check.actual ?? "—")}</span></div></div>`;
  }).join("") : `<div class="empty-state"><span>↻</span><h3>Not reconciled yet</h3><p>Run the checks before making a decision.</p></div>`;
  const lines = item.invoice.lines || [];

  $("#case-detail").innerHTML = `
    <div class="drawer-header"><div class="drawer-title"><span class="drawer-doc-icon">INV</span><div><p>Case #${item.id}</p><h2>${escapeHtml(item.invoice.invoice_number)}</h2>${statusBadge(item.status)}</div></div><button class="drawer-close" data-close-drawer aria-label="Close">×</button></div>
    <div class="drawer-body">
      <section class="drawer-section"><div class="section-title"><h3>Invoice summary</h3><small>${lines.length} line item${lines.length === 1 ? "" : "s"}</small></div><div class="invoice-summary">
        <div><span>Supplier</span><strong>${escapeHtml(supplierName(item.invoice.supplier_id))}</strong></div><div><span>Invoice date</span><strong>${escapeHtml(item.invoice.invoice_date)}</strong></div><div><span>Total</span><strong>${escapeHtml(formatMoney(item.invoice.total_amount, item.invoice.currency))}</strong></div>
        <div><span>Purchase order</span><strong>${item.invoice.purchase_order_id ? `#${item.invoice.purchase_order_id}` : "Not linked"}</strong></div><div><span>Subtotal</span><strong>${escapeHtml(formatMoney(item.invoice.subtotal, item.invoice.currency))}</strong></div><div><span>Tax</span><strong>${escapeHtml(formatMoney(item.invoice.tax_amount, item.invoice.currency))}</strong></div>
      </div></section>
      <section class="drawer-section"><div class="section-title"><h3>Reconciliation checks</h3><button class="text-button" data-reconcile-case="${item.id}">${report ? "Run again" : "Run checks"} →</button></div><div class="check-list">${checkHtml}</div></section>
      <section class="drawer-section"><div class="section-title"><h3>Source evidence</h3>${document ? `<a class="text-button" href="/documents/${escapeHtml(document.id)}/source" target="_blank" rel="noopener">Open original ↗</a>` : ""}</div>${evidence ? `<div class="evidence-grid">${evidence}</div>` : `<div class="empty-state"><span>?</span><h3>No linked evidence</h3><p>The invoice was entered manually or extraction is still processing.</p></div>`}</section>
      <section class="drawer-section"><div class="section-title"><h3>Reviewer decision</h3>${!final ? `<button class="text-button" data-toggle-correction>Correct invoice</button>` : ""}</div>
        ${final ? `<div class="final-banner"><strong>✓</strong><span>This case has a final ${escapeHtml(item.status)} decision. The audit trail is locked.</span></div>` : decisionForm(item.id)}
        ${!final ? correctionForm(item) : ""}
      </section>
      <section class="drawer-section"><div class="section-title"><h3>Audit history</h3><small>${item.events.length} events</small></div><div class="audit-list">${auditItems(item.events)}</div></section>
    </div>`;

  bindCaseDetail(item);
}

function decisionForm(caseId) {
  return `<form class="decision-card" id="decision-form" data-case-id="${caseId}"><label>Reviewer<input name="actor" value="local-reviewer" required></label><label>Decision note<textarea name="reason" rows="3" placeholder="Explain a rejection or exception override"></textarea></label><label class="checkbox-row"><input name="override" type="checkbox"> Approve despite blocking exceptions</label><div class="decision-actions"><button type="submit" name="decision" value="approved" class="button button-primary">Approve invoice</button><button type="submit" name="decision" value="rejected" class="button button-danger">Reject</button></div></form>`;
}

function correctionForm(item) {
  return `<form class="correction-form hidden" id="correction-form" data-case-id="${item.id}"><p class="modal-subtitle">Correct extracted values, then run reconciliation again.</p><div class="form-grid"><label class="field-label">Subtotal<input name="subtotal" value="${escapeHtml(item.invoice.subtotal)}" required></label><label class="field-label">Tax<input name="tax_amount" value="${escapeHtml(item.invoice.tax_amount)}" required></label><label class="field-label">Total<input name="total_amount" value="${escapeHtml(item.invoice.total_amount)}" required></label></div><label class="field-label" style="margin-top:10px">Correction reason<textarea name="reason" rows="2" required placeholder="What was wrong with the extracted value?"></textarea></label><input type="hidden" name="actor" value="local-reviewer">${item.invoice.lines.map(line => `<div class="form-grid correction-line" data-line-number="${line.line_number}" style="margin-top:10px"><label class="field-label">Item code<input name="item_code" value="${escapeHtml(line.item_code)}"></label><label class="field-label">Description<input name="description" value="${escapeHtml(line.description)}"></label><label class="field-label">Quantity<input name="quantity" value="${escapeHtml(line.quantity)}"></label><label class="field-label">Unit price<input name="unit_price" value="${escapeHtml(line.unit_price)}"></label><label class="field-label">Line amount<input name="line_amount" value="${escapeHtml(line.line_amount)}"></label></div>`).join("")}<div class="decision-actions"><button class="button button-secondary" type="submit">Save correction</button></div></form>`;
}

function auditItems(events) {
  if (!events.length) return `<p class="modal-subtitle">No audit events yet.</p>`;
  return [...events].reverse().map((event, index) => `<div class="audit-item"><span class="audit-dot"></span><div><strong>${escapeHtml(readable(event.event_type))}</strong><small>${escapeHtml(event.actor)} · ${escapeHtml(formatDate(event.created_at, true))}</small></div><button type="button" data-audit-toggle="audit-${index}">Details</button><pre class="audit-detail hidden" id="audit-${index}">${escapeHtml(JSON.stringify(event.detail, null, 2))}</pre></div>`).join("");
}

function bindCaseDetail(item) {
  $("[data-close-drawer]", $("#case-detail")).addEventListener("click", closeDrawer);
  const reconcile = $("[data-reconcile-case]", $("#case-detail"));
  if (reconcile) reconcile.addEventListener("click", () => reconcileCase(item.id, reconcile));
  const decision = $("#decision-form");
  if (decision) decision.addEventListener("submit", event => submitDecision(event, item.id));
  const toggle = $("[data-toggle-correction]", $("#case-detail"));
  if (toggle) toggle.addEventListener("click", () => $("#correction-form").classList.toggle("hidden"));
  const correction = $("#correction-form");
  if (correction) correction.addEventListener("submit", event => submitCorrection(event, item.id));
  $$('[data-audit-toggle]', $("#case-detail")).forEach(button => button.addEventListener("click", () => $(`#${button.dataset.auditToggle}`).classList.toggle("hidden")));
}

async function reconcileCase(caseId, button) {
  const old = button.textContent;
  button.disabled = true;
  button.textContent = "Running…";
  try {
    const result = await api(`/cases/${caseId}/reconcile`, { method: "POST" });
    showToast("Reconciliation complete", `${result.exception_count} exceptions found.`);
    await loadAll({ silent: true });
    await openCase(caseId);
  } catch (error) {
    showToast("Reconciliation failed", error.message, "error");
    button.disabled = false;
    button.textContent = old;
  }
}

async function submitDecision(event, caseId) {
  event.preventDefault();
  const submitter = event.submitter;
  const form = event.currentTarget;
  submitter.disabled = true;
  try {
    const result = await api(`/cases/${caseId}/decision`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        decision: submitter.value,
        actor: form.elements.actor.value,
        reason: form.elements.reason.value.trim() || null,
        override_blocking_exceptions: form.elements.override.checked,
      }),
    });
    showToast("Decision recorded", `Case ${result.status}.`);
    await loadAll({ silent: true });
    await openCase(caseId);
  } catch (error) {
    showToast("Decision was not saved", error.message, "error");
    submitter.disabled = false;
  }
}

async function submitCorrection(event, caseId) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type=submit]");
  submit.disabled = true;
  const lines = $$(".correction-line", form).map(line => ({
    line_number: Number(line.dataset.lineNumber),
    item_code: line.querySelector('[name="item_code"]').value,
    description: line.querySelector('[name="description"]').value,
    quantity: line.querySelector('[name="quantity"]').value,
    unit_price: line.querySelector('[name="unit_price"]').value,
    line_amount: line.querySelector('[name="line_amount"]').value,
  }));
  try {
    await api(`/cases/${caseId}/correct-invoice`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actor: form.elements.actor.value, reason: form.elements.reason.value, subtotal: form.elements.subtotal.value, tax_amount: form.elements.tax_amount.value, total_amount: form.elements.total_amount.value, lines }),
    });
    showToast("Correction saved", "Run reconciliation again before making a decision.");
    await loadAll({ silent: true });
    await openCase(caseId);
  } catch (error) {
    showToast("Correction was not saved", error.message, "error");
    submit.disabled = false;
  }
}

function openUploadModal() {
  $("#upload-title").textContent = "Start with your invoice";
  $("#upload-modal .modal-subtitle").textContent = "Upload the invoice first. LedgerLens extracts it, checks saved records, then asks for the next document only when it is needed.";
  $("#upload-modal").classList.add("open");
  $("#upload-modal").setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
}

function closeUploadModal() {
  $("#upload-modal").classList.remove("open");
  $("#upload-modal").setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
}

function closeDocumentModal() {
  $("#document-modal").classList.remove("open");
  $("#document-modal").setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
}

function closeSupportModal() {
  $("#support-modal").classList.remove("open");
  $("#support-modal").setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
  state.supportDocumentId = null;
  state.supportRequirements = null;
}

function openSupportModal(documentId, requirements) {
  const nextRequired = requirements?.next_required || requirements?.missing || [];
  const missing = requirements?.requirements.filter(item => nextRequired.includes(item.key)) || [];
  if (!missing.length) return;
  state.supportDocumentId = documentId;
  state.activeInvoiceDocumentId = documentId;
  state.supportRequirements = requirements;
  $("#support-requirements").innerHTML = `<div class="requirement-list support-requirement-list">${requirements.requirements.map(item => `<div class="requirement-row requirement-${item.status}"><span>${item.status === "available" ? "✓" : "!"}</span><div><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.detail)}</small></div><b>${item.status === "available" ? "Available" : "Needed"}</b></div>`).join("")}</div>`;
  $("#support-upload-fields").innerHTML = missing.map((item, index) => `<label class="support-file"><input class="support-file-input" data-document-type="${escapeHtml(item.key)}" type="file" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff" required><span>${String(index + 1).padStart(2, "0")}</span><div><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.detail)}</small><b data-file-name>Select file</b></div><i>＋</i></label>`).join("");
  $("#support-modal").classList.add("open");
  $("#support-modal").setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
  $$(".support-file-input").forEach(input => input.addEventListener("change", () => updateSupportState()));
  updateSupportState();
}

function updateSupportState() {
  const inputs = $$(".support-file-input");
  const selected = inputs.filter(input => input.files.length > 0);
  selected.forEach(input => {
    const label = input.closest(".support-file");
    label.classList.add("ready");
    label.querySelector("[data-file-name]").textContent = `${input.files[0].name} · ${formatBytes(input.files[0].size)}`;
  });
  $("#support-count").textContent = `${selected.length} of ${inputs.length} required sources selected`;
  $("#support-submit").disabled = selected.length !== inputs.length;
}

async function submitSupportUpload(event) {
  event.preventDefault();
  const inputs = $$(".support-file-input");
  if (inputs.some(input => !input.files.length)) return;
  const submit = $("#support-submit");
  submit.disabled = true;
  try {
    const uploaded = [];
    for (let index = 0; index < inputs.length; index += 1) {
      const input = inputs[index];
      submit.textContent = `Uploading ${index + 1} of ${inputs.length}…`;
      const formData = new FormData();
      formData.append("file", input.files[0]);
      formData.append("document_type", input.dataset.documentType);
      uploaded.push(await api("/documents", { method: "POST", body: formData }));
    }
    submit.textContent = "Extracting sources…";
    await Promise.all(uploaded.map(item => waitForExtraction(item.job.id, submit)));
    closeSupportModal();
    closeDocumentModal();
    await loadAll({ silent: true });
    showToast("Sources extracted", "Confirm the extracted record to continue reconciliation.");
    openDocument(uploaded[0].document.id);
  } catch (error) {
    showToast("Supporting upload stopped", error.message, "error");
    submit.disabled = false;
  } finally {
    submit.textContent = "Upload required sources";
  }
}

async function openDocument(documentId) {
  const documentRecord = state.documents.find(item => item.id === documentId);
  if (!documentRecord) return;
  const modal = $("#document-modal");
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
  $("#document-detail").innerHTML = `<div class="inline-loader"><span class="spinner"></span><p>Loading extraction…</p></div>`;
  const [extractionResult, pagesResult, matchResult, requirementsResult] = await Promise.allSettled([
    api(`/documents/${documentId}/extraction`), api(`/documents/${documentId}/pages`),
    documentRecord.document_type === "invoice" ? api(`/documents/${documentId}/record-match`) : Promise.resolve(null),
    documentRecord.document_type === "invoice" ? api(`/documents/${documentId}/reconciliation-requirements`) : Promise.resolve(null),
  ]);
  renderDocumentDetail(
    documentRecord,
    extractionResult.status === "fulfilled" ? extractionResult.value : null,
    pagesResult.status === "fulfilled" ? pagesResult.value : [],
    requirementsResult.status === "fulfilled" ? requirementsResult.value : null,
  );
  if (documentRecord.document_type === "invoice") {
    const match = matchResult.status === "fulfilled" ? matchResult.value : null;
    const linkedCase = state.cases.find(item => item.invoice_id === documentRecord.parent_id && documentRecord.parent_type === "invoice");
    const create = $("#create-review-case");
    if (create) {
      create.disabled = !match?.ready_to_link || Boolean(linkedCase);
      create.textContent = linkedCase ? "Case already created" : "Create review case";
    }
    const detail = linkedCase ? "This invoice already has a reconciliation result. Open its case to inspect checks and make a decision." : match
      ? `Supplier: ${readable(match.supplier_status)} · Purchase order: ${readable(match.purchase_order_status)}. ${match.notes.join(" ") || "Saved purchasing records are available. Create a case to check price, quantity, and totals."}`
      : "Matching becomes available after extraction completes. Keep the worker running, then reopen this document.";
    const requirements = requirementsResult.status === "fulfilled" ? requirementsResult.value : null;
    const requirementRows = requirements?.requirements.map(item => `<div class="requirement-row requirement-${item.status}"><span>${item.status === "available" ? "✓" : "!"}</span><div><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.detail)}</small></div><b>${item.status === "available" ? "Available" : "Needed"}</b></div>`).join("") || "";
    const nextRequired = requirements?.next_required || requirements?.missing || [];
    const missingButton = nextRequired.length ? `<button class="button button-primary" id="request-support">Add ${nextRequired.length} needed record${nextRequired.length === 1 ? "" : "s"}</button>` : "";
    $(".document-actions", $("#document-detail")).insertAdjacentHTML("afterend", `<section class="record-readiness"><strong>${linkedCase ? "Reconciliation available" : "Reconciliation readiness"}</strong><p>${escapeHtml(detail)}</p>${requirementRows ? `<div class="requirement-list">${requirementRows}</div>` : ""}${linkedCase ? `<button class="button button-primary" id="open-linked-case">View reconciliation →</button>` : missingButton}</section>`);
    $("#open-linked-case")?.addEventListener("click", () => { closeDocumentModal(); switchView("cases"); openCase(linkedCase.id); });
    $("#request-support")?.addEventListener("click", () => openSupportModal(document.id, requirements));
  }
}

function renderDocumentDetail(document, extraction, pages) {
  const job = jobFor(document.id);
  const fields = extraction?.output_json?.fields || {};
  const fieldRows = Object.entries(fields).map(([name, field]) => `<tr><td>${escapeHtml(readable(name))}</td><td><strong>${escapeHtml(field.value ?? "Not found")}</strong></td><td>${field.evidence?.[0] ? `Page ${field.evidence[0].page} · “${escapeHtml(field.evidence[0].text)}”` : "—"}</td></tr>`).join("");
  $("#document-detail").innerHTML = `
    <div class="document-detail-head"><span class="file-icon">${document.media_type.includes("pdf") ? "PDF" : "IMG"}</span><div><p class="eyebrow">Document inspection</p><h2 id="document-modal-title">${escapeHtml(document.original_filename)}</h2><div class="document-detail-meta"><span class="type-chip">${escapeHtml(readable(document.document_type))}</span>${statusBadge(job?.status || "queued")}<span class="type-chip">${escapeHtml(formatBytes(document.size_bytes))}</span><span class="type-chip">${pages.length} page${pages.length === 1 ? "" : "s"}</span></div></div></div>
    <div class="document-actions"><a class="button button-secondary" href="/documents/${escapeHtml(document.id)}/source" target="_blank" rel="noopener">Open source ↗</a>${document.document_type === "invoice" && extraction ? `<button class="button button-primary" id="create-review-case">Create review case</button><button class="button button-secondary" id="compare-models">Compare models</button><label class="checkbox-row field-label"><input type="checkbox" id="include-local"> Include local QLoRA</label>` : ""}${document.document_type !== "invoice" && extraction ? `<button class="button button-primary" id="save-support-record" ${document.parent_type ? "disabled" : ""}>${document.parent_type ? "Record saved" : `Save ${escapeHtml(readable(document.document_type))}`}</button>` : ""}${job?.status === "failed" ? `<button class="button button-danger" id="retry-job">Retry job</button>` : ""}</div>
    ${extraction ? `<div class="extractor-banner"><span>Extractor</span><strong>${escapeHtml(extraction.extractor_version)}</strong></div><div class="table-wrap"><table><thead><tr><th>Field</th><th>Extracted value</th><th>Source evidence</th></tr></thead><tbody>${fieldRows}</tbody></table></div>${extraction.notes.length ? `<p class="modal-subtitle">${extraction.notes.map(escapeHtml).join(" · ")}</p>` : ""}` : `<div class="empty-state"><span>…</span><h3>Extraction is not ready</h3><p>Start the worker and refresh after this job completes.</p></div>`}
    <div id="comparison-result"></div>`;
  const compare = $("#compare-models");
  if (compare) compare.addEventListener("click", () => compareModels(document.id, extraction, compare));
  const createCase = $("#create-review-case");
  if (createCase) createCase.addEventListener("click", () => createReviewCase(document.id, createCase));
  const saveSupport = $("#save-support-record");
  if (saveSupport) saveSupport.addEventListener("click", () => saveSupportingRecord(document.id, saveSupport));
  const retry = $("#retry-job");
  if (retry) retry.addEventListener("click", () => retryJob(job.id, retry));
}

async function saveSupportingRecord(documentId, button) {
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const result = await api(`/documents/${documentId}/save-purchasing-record`, { method: "POST" });
    closeDocumentModal();
    await loadAll({ silent: true });
    showToast("Purchasing record saved", result.message);
    if (state.activeInvoiceDocumentId) {
      const requirements = await api(`/documents/${state.activeInvoiceDocumentId}/reconciliation-requirements`);
      if (requirements.next_required?.length) openSupportModal(state.activeInvoiceDocumentId, requirements);
      else await createReviewCaseForInvoice(state.activeInvoiceDocumentId);
    }
  } catch (error) {
    showToast("Could not save record", error.message, "error");
    button.disabled = false;
    button.textContent = "Save record";
  }
}

async function createReviewCase(documentId, button) {
  button.disabled = true;
  button.textContent = "Reconciling…";
  try {
    await createReviewCaseForInvoice(documentId);
  } catch (error) {
    showToast("Could not create review case", error.message, "error");
    button.disabled = false;
    button.textContent = "Create review case";
  }
}

async function createReviewCaseForInvoice(documentId) {
  const result = await api(`/documents/${documentId}/create-review-case`, { method: "POST" });
  closeDocumentModal();
  closeSupportModal();
  await loadAll({ silent: true });
  switchView("cases");
  const outcome = result.exception_count
    ? `${result.exception_count} exception${result.exception_count === 1 ? "" : "s"} need review.`
    : "All checks passed.";
  showToast("Review case created", outcome);
}

async function compareModels(documentId, baseline, button) {
  const includeLocal = $("#include-local").checked;
  button.disabled = true;
  button.textContent = includeLocal ? "Running three extractors…" : "Calling Groq…";
  $("#comparison-result").innerHTML = `<div class="inline-loader"><span class="spinner"></span><p>This can take a moment. The local model loads once and is then cached.</p></div>`;
  try {
    const result = await api(`/documents/${documentId}/compare-extractors?include_local=${includeLocal}`, { method: "POST" });
    const names = [...new Set([...Object.keys(result.rules_output.fields), ...Object.keys(result.llm_output.fields), ...Object.keys(result.local_output?.fields || {})])];
    $("#comparison-result").innerHTML = `<div class="drawer-section"><div class="section-title"><h3>Model comparison</h3><small>Agreement is measured against rules</small></div><div class="table-wrap"><table class="comparison-table"><thead><tr><th>Field</th><th>Rules</th><th>Groq · ${escapeHtml(result.model)}</th>${result.local_output ? `<th>Local · ${escapeHtml(result.local_model)}</th>` : ""}</tr></thead><tbody>${names.map(name => `<tr><td>${escapeHtml(readable(name))}</td><td>${escapeHtml(result.rules_output.fields[name]?.value ?? "—")}</td><td>${escapeHtml(result.llm_output.fields[name]?.value ?? "—")} <span class="${result.field_agreement[name] ? "agreement-yes" : "agreement-no"}">${result.field_agreement[name] ? "✓" : "≠"}</span></td>${result.local_output ? `<td>${escapeHtml(result.local_output.fields[name]?.value ?? "—")} <span class="${result.local_field_agreement[name] ? "agreement-yes" : "agreement-no"}">${result.local_field_agreement[name] ? "✓" : "≠"}</span></td>` : ""}</tr>`).join("")}</tbody></table></div></div>`;
    showToast("Comparison complete", includeLocal ? "Rules, Groq, and local results are ready." : "Rules and Groq results are ready.");
  } catch (error) {
    $("#comparison-result").innerHTML = "";
    showToast("Comparison failed", error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Compare models";
  }
}

async function retryJob(jobId, button) {
  button.disabled = true;
  try {
    await api(`/document-jobs/${jobId}/retry`, { method: "POST" });
    showToast("Job queued again", "The worker will retry this document.");
    closeDocumentModal();
    await loadAll({ silent: true });
  } catch (error) {
    showToast("Retry failed", error.message, "error");
    button.disabled = false;
  }
}

async function submitUpload(event) {
  event.preventDefault();
  const invoiceInput = $('.bundle-file-input[data-document-type="invoice"]');
  const file = invoiceInput.files[0];
  if (!file) return showToast("Invoice required", "Choose an invoice to begin reconciliation.", "error");
  const submit = $("#upload-submit");
  submit.disabled = true;
  try {
    submit.textContent = "Uploading invoice…";
    const formData = new FormData();
    formData.append("file", file);
    formData.append("document_type", "invoice");
    const uploaded = await api("/documents", { method: "POST", body: formData });
    $("#upload-title").textContent = "Reading your invoice";
    $("#upload-modal .modal-subtitle").textContent = "Extracting supplier, purchase order, amounts, and source evidence. The next required document will appear automatically.";
    await waitForExtraction(uploaded.job.id, submit);
    const requirements = await api(`/documents/${uploaded.document.id}/reconciliation-requirements`);
    closeUploadModal();
    $("#upload-form").reset();
    resetBundleSlots();
    await loadAll({ silent: true });
    if (requirements.next_required?.length) {
      showToast("Invoice checked", "LedgerLens found a missing record and is requesting it now.");
      openSupportModal(uploaded.document.id, requirements);
    } else {
      showToast("Invoice is ready", "All supporting records were found. Creating the review case now.");
      await createReviewCaseForInvoice(uploaded.document.id);
    }
  } catch (error) {
    showToast("Invoice processing paused", error.message, "error");
    await loadAll({ silent: true });
    switchView("documents");
  } finally {
    $("#upload-title").textContent = "Start with your invoice";
    submit.textContent = "Extract invoice →";
    updateBundleState();
  }
}

async function waitForExtraction(jobId, button) {
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    const job = await api(`/document-jobs/${jobId}`);
    if (job.status === "complete") return;
    if (job.status === "failed") throw new Error(job.error || "Invoice extraction failed.");
    button.textContent = job.status === "processing" ? "Extracting invoice…" : "Waiting for worker…";
    await new Promise(resolve => window.setTimeout(resolve, 1200));
  }
  throw new Error("Extraction is still queued. Start the worker, then open Documents to continue.");
}

function updateBundleSlot(input) {
  const slot = input.closest(".bundle-file");
  const file = input.files[0];
  slot.classList.toggle("ready", Boolean(file));
  slot.querySelector("[data-file-name]").textContent = file
    ? `${file.name} · ${formatBytes(file.size)}`
    : "Select file";
  updateBundleState();
}

function updateBundleState() {
  const count = $$(".bundle-file-input").filter(input => input.files.length > 0).length;
  const hasInvoice = $('.bundle-file-input[data-document-type="invoice"]').files.length > 0;
  $("#bundle-count").textContent = hasInvoice ? `${count} file${count === 1 ? "" : "s"} ready · Invoice included` : "Add an invoice to continue";
  $("#upload-submit").disabled = !hasInvoice;
}

function resetBundleSlots() {
  $$(".bundle-file-input").forEach(input => updateBundleSlot(input));
}

async function reconcileAll() {
  const button = $("#reconcile-all");
  const open = state.cases.filter(item => !["approved", "rejected"].includes(item.status));
  if (!open.length) return showToast("Nothing to reconcile", "All cases have final decisions.");
  button.disabled = true;
  button.textContent = `Running 0/${open.length}…`;
  let complete = 0;
  const results = [];
  for (const item of open) {
    try { results.push(await api(`/cases/${item.id}/reconcile`, { method: "POST" })); } catch (error) { results.push(error); }
    complete += 1;
    button.textContent = `Running ${complete}/${open.length}…`;
  }
  button.disabled = false;
  button.textContent = "Run open reconciliations";
  await loadAll({ silent: true });
  const failed = results.filter(result => result instanceof Error).length;
  showToast("Batch reconciliation complete", failed ? `${failed} cases could not be reconciled.` : `${complete} cases updated.`, failed ? "error" : "success");
}

function bindEvents() {
  $$(".nav-item").forEach(button => button.addEventListener("click", () => switchView(button.dataset.view)));
  $$('[data-view-link]').forEach(button => button.addEventListener("click", () => switchView(button.dataset.viewLink)));
  $$('[data-open-upload]').forEach(button => button.addEventListener("click", openUploadModal));
  $$('[data-close-modal]').forEach(button => button.addEventListener("click", closeUploadModal));
  $("[data-close-document]").addEventListener("click", closeDocumentModal);
  $$('[data-close-support]').forEach(button => button.addEventListener("click", closeSupportModal));
  $("#upload-modal").addEventListener("click", event => { if (event.target === event.currentTarget) closeUploadModal(); });
  $("#document-modal").addEventListener("click", event => { if (event.target === event.currentTarget) closeDocumentModal(); });
  $("#support-modal").addEventListener("click", event => { if (event.target === event.currentTarget) closeSupportModal(); });
  $("#drawer-scrim").addEventListener("click", closeDrawer);
  $("#refresh-button").addEventListener("click", () => loadAll());
  $("#reconcile-all").addEventListener("click", reconcileAll);
  $("#upload-form").addEventListener("submit", submitUpload);
  $("#support-upload-form").addEventListener("submit", submitSupportUpload);
  $$(".bundle-file-input").forEach(input => input.addEventListener("change", () => updateBundleSlot(input)));
  $$(".bundle-file").forEach(slot => {
    ["dragenter", "dragover"].forEach(name => slot.addEventListener(name, event => {
      event.preventDefault();
      slot.classList.add("dragging");
    }));
    ["dragleave", "drop"].forEach(name => slot.addEventListener(name, event => {
      event.preventDefault();
      slot.classList.remove("dragging");
    }));
    slot.addEventListener("drop", event => {
      const file = event.dataTransfer.files[0];
      if (!file) return;
      const input = slot.querySelector(".bundle-file-input");
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      updateBundleSlot(input);
    });
  });

  $("#case-filters").addEventListener("click", event => {
    const button = event.target.closest("[data-case-filter]");
    if (!button) return;
    state.caseFilter = button.dataset.caseFilter;
    $$('[data-case-filter]').forEach(node => node.classList.toggle("active", node === button));
    renderCases();
  });
  $("#document-filters").addEventListener("click", event => {
    const button = event.target.closest("[data-document-filter]");
    if (!button) return;
    state.documentFilter = button.dataset.documentFilter;
    $$('[data-document-filter]').forEach(node => node.classList.toggle("active", node === button));
    renderDocuments();
  });
  $("#case-search").addEventListener("input", event => { state.caseQuery = event.target.value; renderCases(); });
  $("#document-search").addEventListener("input", event => { state.documentQuery = event.target.value; renderDocuments(); });
  $("#global-search").addEventListener("input", event => {
    const value = event.target.value;
    if (state.activeView === "documents") { state.documentQuery = value; $("#document-search").value = value; renderDocuments(); }
    else { state.caseQuery = value; $("#case-search").value = value; renderCases(); if (value && state.activeView === "overview") switchView("cases"); }
  });

  document.addEventListener("click", event => {
    const caseButton = event.target.closest("[data-open-case]");
    if (caseButton) openCase(Number(caseButton.dataset.openCase));
    const documentButton = event.target.closest("[data-open-document]");
    if (documentButton) openDocument(documentButton.dataset.openDocument);
  });
  document.addEventListener("keydown", event => {
    if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) { event.preventDefault(); $("#global-search").focus(); }
    if (event.key === "Escape") { closeDrawer(); closeUploadModal(); closeDocumentModal(); closeSupportModal(); }
  });
  $("#mobile-menu").addEventListener("click", () => { $("#sidebar").classList.add("open"); $("#mobile-scrim").classList.add("open"); });
  $("#mobile-scrim").addEventListener("click", () => { $("#sidebar").classList.remove("open"); $("#mobile-scrim").classList.remove("open"); });
}

function initializeDate() {
  const now = new Date();
  $("#today-weekday").textContent = new Intl.DateTimeFormat("en-US", { weekday: "long" }).format(now);
  $("#today-date").textContent = new Intl.DateTimeFormat("en-US", { month: "long", day: "numeric" }).format(now);
}

document.addEventListener("DOMContentLoaded", () => {
  initializeDate();
  bindEvents();
  loadAll();
  window.setInterval(() => {
    if (!document.hidden && !$(".modal-backdrop.open") && !$("#case-drawer").classList.contains("open")) loadAll({ silent: true });
  }, 10000);
});
