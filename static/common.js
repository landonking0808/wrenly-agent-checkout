const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = v => { const n = Number(v || 0); return (n < 0 ? "-$" : "$") + Math.abs(n).toFixed(2); };
async function api(path, opts = {}) {
  const r = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(d.error || d.detail || ("HTTP " + r.status)); e.data = d; e.status = r.status; throw e; }
  return d;
}
function toast(t) { const el = $("#toast"); if (!el) return; el.textContent = t; el.classList.add("on"); clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove("on"), 2600); }
const STATUS = {
  quoted: ["Quoted", ""], awaiting_payment: ["Awaiting PayPal approval", "info"], authorized: ["Authorized · scheduled", "pp"],
  completed: ["Cleaned · settling", "info"], captured: ["Captured", "pp"], paid_out: ["Crew paid", "ok"],
  payout_held: ["Payout held for review", "warn"], refunded: ["Refunded", "bad"], cancelled: ["Cancelled (voided)", ""],
};
const statusPill = s => { const [l, c] = STATUS[s] || [s, ""]; return `<span class="pill ${c}">${esc(l)}</span>`; };
function ctTime(iso) {
  try { return new Date(iso).toLocaleString("en-US", {timeZone: "America/Chicago", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) + " CT"; }
  catch { return iso; }
}
