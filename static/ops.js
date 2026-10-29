let OPEN = null;
const approver = () => $("#approver").value.trim() || "ops";
const ACTION = {
  "job.create": "Job created", "order.create": "PayPal order created", "order.authorize": "Payment authorized", "job.complete": "Crew marked done",
  "payment.capture": "Payment captured", "margin.check": "Margin check", "payout.send": "Crew payout", "payout.hold": "Payout held",
  "refund.propose": "Refund proposed", "refund.send": "Refund sent", "approval.approved": "Approved by a person", "approval.rejected": "Rejected by a person",
  "authorization.void": "Authorization voided", "approval.decide": "Approval attempt",
};
const LSTATUS = {ok: "ok", pass: "ok", deduped: "info", blocked: "bad", fail: "bad", error: "bad", pending_approval: "warn", verified: "pp", received: ""};
const lstatus = s => `<span class="pill ${LSTATUS[s] ?? ""}">${esc({deduped: "deduplicated", pending_approval: "needs approval"}[s] || s)}</span>`;
const actionLabel = a => ACTION[a] || (a.startsWith("webhook.") ? "Webhook " + a.slice(8).replaceAll("_", " ").toLowerCase() : a);

function kpi(label, value, sub, attn) { return `<div class="card kpi ${attn ? "attn" : ""}"><h3>${esc(label)}</h3><div class="v">${value}</div><div class="s">${sub}</div></div>`; }

function marginCell(j) {
  const m = j.margin || j.margin_preview; if (!m) return "";
  const cls = m.ok ? "m-ok" : "m-bad";
  return `<span class="${cls}">${money(m.margin)}</span><div class="muted" style="font-size:11px">${j.margin ? "" : "est. · "}floor ${money(m.floor)}</div>`;
}

function jobsTable(js) {
  if (!js.length) return `<div class="empty">No jobs yet. Book one on the <a href="/">booking page</a> or run <span class="mono">python demo.py</span>.</div>`;
  return `<table><thead><tr><th>Job</th><th>Status</th><th class="r">Price</th><th>PayPal ids</th><th class="r">Crew pay</th><th class="r">Margin</th></tr></thead><tbody>` +
    js.map(j => `<tr class="click" data-id="${esc(j.id)}">
      <td><div style="font-weight:550">${esc(j.customer_name)}</div><div class="mono muted">${esc(j.id)} · ${j.bedrooms} bed ${esc(j.frequency.replace("_", "-"))}</div></td>
      <td>${statusPill(j.status)}${j.refunded_amount ? `<div class="muted" style="font-size:11px;margin-top:2px">refunded ${money(j.refunded_amount)}</div>` : ""}</td>
      <td class="r num">${money(j.list_price)}</td>
      <td><div class="ids">${j.order_id ? `<span><em>order</em>${esc(j.order_id)}</span>` : ""}${j.authorization_id ? `<span><em>auth</em>${esc(j.authorization_id)}</span>` : ""}${j.capture_id ? `<span><em>capture</em>${esc(j.capture_id)}</span>` : ""}${j.payout_batch_id ? `<span><em>payout</em>${esc(j.payout_batch_id)}</span>` : ""}</div></td>
      <td class="r num">${j.payout_amount ? money(j.payout_amount) : `<span class="muted">${money((j.margin || j.margin_preview || {}).crew_pay)}</span>`}</td>
      <td class="r num">${marginCell(j)}</td></tr>`).join("") + `</tbody></table>`;
}

function approvalsList(as) {
  const pending = as.filter(a => a.status === "pending");
  const done = as.filter(a => a.status !== "pending").slice(0, 4);
  if (!pending.length && !done.length) return `<div class="empty">Nothing waiting. Refunds the agent proposes and payouts that fail the margin check land here.</div>`;
  const card = a => {
    const p = a.proposal || {}, m = p.margin || {};
    const what = a.kind === "refund" ? `Refund to ${esc(a.job.customer_name)}` : `Crew payout to ${esc(a.job.crew_name)}`;
    const why = a.kind === "refund" ? `<b>${esc((p.policy || {}).label || p.category)}</b>. ${esc(a.reason)}${p.customer_message ? `<br><span class="muted">Customer: “${esc(p.customer_message)}”</span>` : ""}`
      : `${esc(a.reason)}<br><span class="muted">crew ${money(m.crew_pay)} incl. ${m.extra_hours || 0}h extra · fees ${money((m.paypal_fee_est || 0) + (m.payout_fee_est || 0))} · list ${money(m.list_price)}</span>`;
    const acts = a.status === "pending" ? `<div class="acts"><button class="btn primary sm" data-approve="${a.id}">Approve ${a.kind}</button><button class="btn sm danger" data-reject="${a.id}">Reject</button></div>`
      : `<span class="pill ${a.status === "approved" ? "ok" : ""}">${esc(a.status)} by ${esc(a.decided_by)}</span>${a.result && (a.result.refund_id || a.result.payout_batch_id) ? ` <span class="mono muted">${esc(a.result.refund_id || a.result.payout_batch_id)}</span>` : ""}`;
    return `<div class="appr"><div class="top"><span class="pill ${a.kind === "refund" ? "bad" : "warn"}">${esc(a.kind)}</span><span class="mono muted">${esc(a.job_id)}</span></div>
      <div class="amt">${money(a.amount)}</div><div style="font-weight:550">${what}</div><p>${why}</p>${acts}</div>`;
  };
  return pending.map(card).join("") + (done.length ? `<div class="sub" style="padding:0 16px">Decided</div>` + done.map(card).join("") : "");
}

function ledgerCompact(rows) {
  return `<table><thead><tr><th>Time (CT)</th><th>Action</th><th>Result</th><th class="r">Amount</th><th>PayPal id</th></tr></thead><tbody>` +
    rows.map(r => `<tr><td class="muted" style="white-space:nowrap">${esc(ctTime(r.ts).replace(/^\w+ \d+, /, ""))}</td><td>${esc(actionLabel(r.action))}</td><td>${lstatus(r.status)}</td>
      <td class="r num">${r.amount != null ? money(r.amount) : ""}</td><td class="mono">${esc(r.paypal_id || "")}</td></tr>`).join("") + `</tbody></table>`;
}

function ledgerTable(rows) {
  if (!rows.length) return `<div class="empty">The ledger is empty.</div>`;
  return `<table><thead><tr><th>#</th><th>Time (CT)</th><th>Action</th><th>Result</th><th class="r">Amount</th><th>PayPal id</th><th>PayPal-Request-Id</th><th>By</th><th>Note</th></tr></thead><tbody>` +
    rows.slice(0, 60).map(r => `<tr><td class="mono muted">${r.seq}</td><td class="muted" style="white-space:nowrap">${esc(ctTime(r.ts))}</td><td>${esc(actionLabel(r.action))}</td><td>${lstatus(r.status)}</td>
      <td class="r num">${r.amount != null ? money(r.amount) : ""}</td><td class="mono">${esc(r.paypal_id || "")}</td><td class="mono muted">${esc(r.request_id || "")}</td>
      <td>${esc(r.actor)}</td><td class="muted" style="font-size:12px;max-width:280px">${esc(r.detail.reason || r.detail.error || (r.detail.missing && r.detail.missing.length ? "missing: " + r.detail.missing.join(", ") : "") || (r.detail.approval_id ? "approval " + r.detail.approval_id : ""))}</td></tr>`).join("") + `</tbody></table>`;
}

function marginViz(m) {
  if (!m) return "";
  const tot = Math.max(m.list_price, 1), pct = v => Math.max(0, 100 * v / tot).toFixed(1) + "%";
  return `<div class="bar"><i style="width:${pct(m.crew_pay)};background:#4f8f7a"></i><i style="width:${pct(m.paypal_fee_est + m.payout_fee_est)};background:#7aa6e6"></i><i style="width:${pct(m.refunded)};background:#e7a19a"></i><i style="width:${pct(Math.max(0, m.margin))};background:${m.ok ? "#1f5c4a" : "#b42318"}"></i></div>
    <div class="legend"><span><b style="background:#4f8f7a"></b>Crew ${money(m.crew_pay)}</span><span><b style="background:#7aa6e6"></b>PayPal fees ${money(m.paypal_fee_est + m.payout_fee_est)}</span>${m.refunded ? `<span><b style="background:#e7a19a"></b>Refunded ${money(m.refunded)}</span>` : ""}<span><b style="background:${m.ok ? "#1f5c4a" : "#b42318"}"></b>Wrenly ${money(m.margin)} (floor ${money(m.floor)})</span></div>
    ${m.reasons && m.reasons.length ? `<p class="m-bad" style="font-size:13px;margin:8px 0 0">${esc(m.reasons.join("; "))}</p>` : `<p class="m-ok" style="font-size:13px;margin:8px 0 0">Clears the margin floor.</p>`}`;
}

async function openJob(id) {
  OPEN = id;
  const j = await api("/api/jobs/" + encodeURIComponent(id));
  const m = j.margin || j.margin_preview;
  $("#drawer-in").innerHTML = `
    <div style="display:flex;gap:8px;align-items:center"><span class="mono muted">${esc(j.id)}</span><div class="spacer"></div><button class="btn sm" id="close">Close</button></div>
    <h2>${esc(j.customer_name)} · ${money(j.list_price)}</h2>
    <div>${statusPill(j.status)} <span class="muted">${j.bedrooms} bed · ${j.bathrooms} bath · ${esc(j.frequency.replace("_", "-"))} · ${esc(j.service_date)}</span></div>
    <dl class="dl">
      <dt>PayPal order</dt><dd class="mono">${esc(j.order_id || "—")}</dd>
      <dt>Authorization</dt><dd class="mono">${esc(j.authorization_id || "—")}</dd>
      <dt>Capture</dt><dd class="mono">${esc(j.capture_id || "—")} ${j.captured_amount ? money(j.captured_amount) : ""}</dd>
      <dt>Payout batch</dt><dd class="mono">${esc(j.payout_batch_id || "—")} ${j.payout_amount ? money(j.payout_amount) : ""}</dd>
      <dt>Refunded</dt><dd>${money(j.refunded_amount)}</dd>
      <dt>Crew</dt><dd>${esc(j.crew_name)} <span class="mono muted">${esc(j.crew_email)}</span></dd>
    </dl>
    <div class="sub">Margin ${j.margin ? "" : "(estimate)"}</div>${marginViz(m)}
    <div class="sub">Actions</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <a class="btn sm" href="/crew/${encodeURIComponent(j.id)}" target="_blank" rel="noopener">Crew page</a>
      <button class="btn sm" id="retry" ${j.capture_id ? "" : "disabled"}>Retry crew payout</button>
      <button class="btn sm danger" id="void" ${j.status === "authorized" ? "" : "disabled"}>Void authorization</button>
    </div>
    <p class="muted" style="font-size:12px">Retry is safe: the payout uses sender_batch_id <span class="mono">wrenly-${esc(j.id)}-payout</span>, so a second press is deduplicated and never pays twice.</p>
    <div class="sub">Ledger for this job</div>
    ${ledgerCompact(j.ledger)}`;
  $("#drawer").classList.add("on"); $("#scrim").classList.add("on");
  $("#close").onclick = closeJob;
  $("#retry").onclick = async () => { try { const r = await api(`/api/jobs/${j.id}/payout`, {method: "POST"}); toast(r.deduped ? "Already paid: retry deduplicated, nothing sent to PayPal" : "Payout sent " + r.payout_batch_id); } catch (e) { toast("Refused: " + e.message); } refresh(); openJob(j.id); };
  $("#void").onclick = async () => { try { await api(`/api/jobs/${j.id}/void`, {method: "POST"}); toast("Authorization voided"); } catch (e) { toast(e.message); } refresh(); openJob(j.id); };
}
function closeJob() { OPEN = null; $("#drawer").classList.remove("on"); $("#scrim").classList.remove("on"); }

async function decide(id, approve) {
  try { const r = await api(`/api/approvals/${id}`, {method: "POST", body: {approve, by: approver()}}); toast(approve ? `Approved · ${r.result && (r.result.refund_id || r.result.payout_batch_id) || "done"}` : "Rejected"); }
  catch (e) { toast("Error: " + e.message); }
  refresh();
}

async function refresh() {
  try {
    const [s, js, as, led] = await Promise.all([api("/api/summary"), api("/api/jobs"), api("/api/approvals"), api("/api/ledger?limit=120")]);
    $("#kpis").innerHTML = kpi("Captured", money(s.captured_total), `${s.jobs} jobs · ${s.authorized} authorized, not yet charged`) +
      kpi("Paid to crews", money(s.payouts_total), "PayPal Payouts, 70% of list") +
      kpi("Wrenly margin", money(s.margin_total), "on settled jobs, after est. PayPal fees") +
      kpi("Needs a human", s.pending_approvals, "refunds + held payouts", s.pending_approvals > 0) +
      kpi("Guardrails", `${s.blocked} <span class="muted" style="font-size:14px;font-weight:500">blocked</span> · ${s.deduped} <span class="muted" style="font-size:14px;font-weight:500">deduped</span>`, `${s.ledger_rows} ledger rows`);
    $("#jobs").innerHTML = jobsTable(js); $("#jobcount").textContent = js.length ? `${js.length} total` : "";
    $("#approvals").innerHTML = approvalsList(as);
    $("#ledger").innerHTML = ledgerTable(led);
    $("#updated").textContent = "Updated " + new Date().toLocaleTimeString("en-US", {timeZone: "America/Chicago", hour: "numeric", minute: "2-digit", second: "2-digit"}) + " CT";
    document.querySelectorAll("tr.click").forEach(tr => tr.onclick = () => openJob(tr.dataset.id));
    document.querySelectorAll("[data-approve]").forEach(b => b.onclick = () => { b.disabled = true; decide(b.dataset.approve, true); });
    document.querySelectorAll("[data-reject]").forEach(b => b.onclick = () => { b.disabled = true; decide(b.dataset.reject, false); });
  } catch (e) { $("#updated").textContent = "Could not load: " + e.message; }
}

(async function init() {
  const cfg = await api("/api/config");
  $("#ai-badge").lastChild.textContent = cfg.ai.startsWith("gemini") ? "AI: " + cfg.ai : "AI: offline rules";
  $("#pp-badge").lastChild.textContent = "PayPal: " + cfg.paypal_mode;
  $("#approver").value = localStorage.getItem("wrenly_approver") || "Landon (ops)";
  $("#approver").onchange = () => localStorage.setItem("wrenly_approver", approver());
  $("#scrim").onclick = closeJob;
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeJob(); });
  await refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 3000);
  const open = new URLSearchParams(location.search).get("job"); if (open) openJob(open);
})();
