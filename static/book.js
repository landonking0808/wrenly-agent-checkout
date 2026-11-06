let QUOTED = false, AMOUNT = 0, CFG = null, CHAT = sessionStorage.getItem("wrenly_chat") || null, JOB = null, ORDER = null, POLL = null;

function bubble(cls, html, meta) {
  const d = document.createElement("div");
  d.className = "msg " + cls;
  d.innerHTML = html + (meta ? `<span class="meta">${esc(meta)}</span>` : "");
  $("#msgs").appendChild(d); $("#msgs").scrollTop = 1e9; return d;
}
function toolLine(ev) {
  const r = ev.result || {};
  const args = Object.entries(ev.args || {}).filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `${k}=${Array.isArray(v) ? "[" + v.join(",") + "]" : v}`).join(", ");
  let res = r.error ? "refused: " + r.error : r.total ? money(r.total) : r.order_id ? "PayPal order " + r.order_id : r.approval_id ? "approval " + r.approval_id + " (" + money(r.amount) + ")" : r.status || "ok";
  const d = document.createElement("div");
  d.className = "tool" + (r.error ? " err" : "");
  d.innerHTML = `<b>${esc(ev.name)}</b>(${esc(args)}) → ${esc(res)}`;
  $("#msgs").appendChild(d);
}

function renderQuote(q) {
  QUOTED = true; if (!JOB) renderSteps(null);
  $("#quote").innerHTML = `
    <div class="total num">${money(q.total)}<span class="muted" style="font-size:14px;font-weight:500"> / visit</span></div>
    <div class="muted">${q.bedrooms} bed · ${q.bathrooms} bath · ${q.frequency.replace("_", "-")}
      ${q.published_price ? '<span class="pill ok" style="margin-left:6px">Published price</span>' : '<span class="pill warn" style="margin-left:6px">Sample price table</span>'}</div>
    <ul class="lines">${q.lines.map(l => `<li><span>${esc(l.label)}</span><span>${money(l.amount)}</span></li>`).join("")}</ul>`;
}

function renderSteps(j) {
  const s = j ? j.status : "none";
  const order = ["awaiting_payment", "authorized", "completed", "captured", "paid_out"];
  const idx = s === "payout_held" ? 3.5 : s === "refunded" ? 4 : order.indexOf(s);
  const steps = [
    ["Quote from the agent", j ? money(j.list_price) : "", idx >= 0 || !!j || QUOTED],
    ["Approve with PayPal (authorize only)", j && j.authorization_id ? "auth " + j.authorization_id : (j && j.order_id ? "order " + j.order_id : ""), idx >= 1],
    ["Crew cleans and checks off the list", j && j.checklist ? "checklist complete" : "", idx >= 2],
    ["Payment captured", j && j.capture_id ? "capture " + j.capture_id : "", idx >= 3],
    [s === "payout_held" ? "Crew payout waiting for a manager" : "Crew paid with PayPal Payouts", j && j.payout_batch_id ? "batch " + j.payout_batch_id : "", idx >= 4],
  ];
  let nowSet = false;
  $("#steps").innerHTML = steps.map(([t, sub, done]) => {
    let cls = done ? "done" : (!nowSet && (nowSet = true) ? "now" : "");
    return `<li class="${cls}"><i>${done ? "✓" : ""}</i><div>${esc(t)}${sub ? `<small>${esc(sub)}</small>` : ""}</div></li>`;
  }).join("");
  if (j && j.refunded_amount > 0) $("#steps").insertAdjacentHTML("beforeend", `<li class="done"><i>↺</i><div>Refunded ${money(j.refunded_amount)}</div></li>`);
}

async function refreshJob() {
  if (!JOB) return renderSteps(null);
  try { const j = await api("/api/jobs/" + JOB); renderSteps(j); if (["authorized", "completed", "captured", "paid_out", "payout_held", "refunded"].includes(j.status)) showAuthorized(j); }
  catch (e) { /* job not created yet */ }
}

function showAuthorized(j) {
  const area = $("#paypal-area");
  area.dataset.state = "authorized";
  const label = {authorized: `Payment authorized · ${money(j.list_price)} held, not charged`, completed: "Clean done · settling payment",
    captured: `Charged ${money(j.captured_amount)} after the clean`, paid_out: `Charged ${money(j.captured_amount)} · crew paid`,
    payout_held: `Charged ${money(j.captured_amount)} · crew payout in review`, refunded: `Refunded ${money(j.refunded_amount)}`}[j.status] || j.status;
  const html = `<div class="pill pp" style="padding:6px 10px">${esc(label)}</div>
    <p class="note">For the demo: <a href="/crew/${encodeURIComponent(j.id)}" target="_blank" rel="noopener">open the crew's job page</a> to finish the clean, or watch it in the <a href="/ops">ops dashboard</a>.</p>`;
  if (area.innerHTML !== html) area.innerHTML = html;
}

async function authorize(orderId) {
  const r = await api(`/api/orders/${encodeURIComponent(orderId)}/authorize`, {method: "POST", body: {job_id: JOB}});
  toast("Payment authorized: " + r.authorization_id);
  bubble("bot", `PayPal approved. I've authorized ${money(AMOUNT)} on your account (authorization ${esc(r.authorization_id)}). Nothing is charged until the crew finishes.`, "agent · PayPal Orders v2 authorize");
  await refreshJob();
  clearInterval(POLL); POLL = setInterval(refreshJob, 3000);
}

function loadPayPalSdk(clientId, timeoutMs = 10000) {
  return new Promise((res, rej) => {
    if (window.paypal && window.paypal.Buttons) return res();
    const s = document.createElement("script");
    s.src = `https://www.paypal.com/sdk/js?client-id=${encodeURIComponent(clientId)}&intent=authorize&currency=USD&components=buttons&disable-funding=paylater,venmo`;
    const t = setTimeout(() => rej(new Error("PayPal SDK did not load in time")), timeoutMs);
    s.onload = () => { clearTimeout(t); window.paypal && window.paypal.Buttons ? res() : rej(new Error("PayPal SDK loaded without Buttons")); };
    s.onerror = () => { clearTimeout(t); rej(new Error("PayPal SDK failed to load")); };
    document.head.appendChild(s);
  });
}

async function mockApprove() {
  await api(`/api/mock/approve/${ORDER}`, {method: "POST"});
  await authorize(ORDER);
}

// Mock mode: render PayPal's real JS SDK buttons with PayPal's documented `client-id=sb` test shortcut.
// The sb buttons need an order PayPal knows, so they create one client-side (actions.order.create) under PayPal's
// shared sandbox test merchant; that order is only for the button and its popup. The app's own order, authorization,
// capture, refund and payout stay on the server-side mock. If the SDK cannot load (offline), the mock button still works.
async function renderSbButtons() {
  const box = $("#pp-sb");
  try {
    await loadPayPalSdk(CFG.paypal_sdk_client_id);
    await paypal.Buttons({
      style: {layout: "vertical", color: "gold", shape: "rect", label: "pay", height: 44},
      createOrder: (data, actions) => actions.order.create({
        intent: "AUTHORIZE",
        purchase_units: [{amount: {currency_code: "USD", value: Number(AMOUNT).toFixed(2)}, description: "Wrenly demo (PayPal sb test shortcut)"}],
      }),
      onApprove: async data => {
        bubble("bot", `PayPal's sandbox popup approved its test order ${esc(data.orderID)} (PayPal's sb test merchant). The app's own payment steps still run on the local mock, so I'm continuing with mock order ${esc(ORDER)}.`, "PayPal JS SDK (client-id=sb) · server on mock");
        try { await mockApprove(); } catch (e) { toast(e.message); }
      },
      onError: err => toast("PayPal error: " + (err && err.message || err)),
    }).render("#pp-sb");
    box.dataset.state = "rendered";
    $("#pp-sb-note").textContent = "Real PayPal JS SDK buttons, loaded with PayPal's client-id=sb test shortcut. They open PayPal's sandbox popup for a client-side test order; the payment steps on the server run on the local mock. Without a sandbox buyer login, use the demo button below.";
  } catch (e) {
    box.dataset.state = "unavailable";
    box.innerHTML = "";
    $("#pp-sb-note").textContent = "PayPal's JS SDK could not load (" + e.message + "); the demo button below still works.";
  }
}

async function showCheckout(ev) {
  JOB = ev.job_id; ORDER = ev.order_id; AMOUNT = ev.amount;
  sessionStorage.setItem("wrenly_job", JOB);
  renderSteps({id: JOB, status: "awaiting_payment", list_price: ev.amount, order_id: ORDER});
  const area = $("#paypal-area");
  area.dataset.state = "checkout";
  if (CFG.paypal_mode === "mock") {
    const sb = CFG.paypal_sdk_client_id === "sb";
    area.innerHTML = (sb ? `<div id="pp-sb" data-state="loading"></div><p class="note" id="pp-sb-note">Loading PayPal's JS SDK (client-id=sb)…</p>` : "") +
      `<button class="mockpp" id="mockpp">${sb ? "Demo approval (mock, no PayPal login)" : "Pay with <i>PayPal</i>"}</button>
      <p class="note">Demo mode: this button stands in for the PayPal approval popup and is answered by the local mock (simulated, no real money). Add PayPal sandbox keys to .env to use the real PayPal JS SDK buttons with your own sandbox app.</p>`;
    $("#mockpp").onclick = async () => {
      $("#mockpp").disabled = true;
      try { await mockApprove(); }
      catch (e) { toast(e.message); $("#mockpp").disabled = false; }
    };
    if (sb) renderSbButtons();
    return;
  }
  area.innerHTML = `<div id="pp-buttons"></div><p class="note">PayPal sandbox: log in with the sandbox buyer account from the README.</p>`;
  try {
    await loadPayPalSdk(CFG.paypal_client_id);
    paypal.Buttons({
      style: {layout: "vertical", color: "gold", shape: "rect", label: "pay", height: 44},
      createOrder: () => ORDER,
      onApprove: async data => { try { await authorize(data.orderID); } catch (e) { toast(e.message); } },
      onError: err => toast("PayPal error: " + (err && err.message || err)),
    }).render("#pp-buttons");
  } catch (e) { area.innerHTML = `<p class="note">${esc(e.message)}</p>`; }
}

async function send(text) {
  text = text.trim(); if (!text) return;
  bubble("me", esc(text));
  $("#input").value = ""; $("#send").disabled = true;
  const typing = bubble("bot", '<span class="muted">Thinking…</span>');
  try {
    const r = await api("/api/chat", {method: "POST", body: {message: text, chat_id: CHAT}});
    typing.remove();
    CHAT = r.chat_id; sessionStorage.setItem("wrenly_chat", CHAT);
    for (const ev of r.events) {
      if (ev.type === "tool") toolLine(ev);
      if (ev.type === "quote") renderQuote(ev.quote);
    }
    bubble("bot", esc(r.reply), r.engine.startsWith("gemini") ? "agent · " + r.engine : "agent · " + r.engine);
    for (const ev of r.events) {
      if (ev.type === "checkout" && ev.order_id && (ev.job_id !== JOB || $("#paypal-area").dataset.state !== "authorized")) await showCheckout(ev);
      if (ev.type === "refund_proposed") toast("Refund proposed: waiting for a person to approve");
    }
    if (r.job_id) { JOB = r.job_id; refreshJob(); }
  } catch (e) { typing.remove(); bubble("bot", "Sorry, something went wrong: " + esc(e.message)); }
  finally { $("#send").disabled = false; $("#input").focus(); }
}

(async function init() {
  CFG = await api("/api/config");
  $("#ai-badge").lastChild.textContent = CFG.ai.startsWith("gemini") ? "AI: " + CFG.ai : "AI: offline rules";
  $("#pp-badge").lastChild.textContent = CFG.paypal_mode === "mock" ? "PayPal: mock" : "PayPal: " + CFG.paypal_mode;
  bubble("bot", "Hi! I'm Wrenly's booking agent. Tell me about your home (bedrooms, bathrooms, how often) and I'll price it from our list.", "agent");
  renderSteps(null);
  $("#composer").onsubmit = e => { e.preventDefault(); send($("#input").value); };
  document.querySelectorAll(".chip").forEach(c => c.onclick = () => send(c.textContent));
  const q = new URLSearchParams(location.search).get("q");
  if (q) send(q);
})();
