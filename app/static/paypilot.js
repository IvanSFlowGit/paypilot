const $ = (s) => document.querySelector(s);
let CUSTOMERS = [];

async function loadConfig() {
  try {
    const r = await fetch("/config");
    const cfg = await r.json();
    CUSTOMERS = cfg.customers || [];
    const sel = $("#customer");
    // Escape every field even though this is trusted seed config - a stray quote
    // in a name/plan would otherwise break the option markup.
    sel.innerHTML = CUSTOMERS.map(c => {
      const id = escapeHtml(c.id), plan = escapeHtml(c.plan), name = escapeHtml(c.name);
      const mrr = escapeHtml(String(c.mrr)), fc = escapeHtml(c.last_failure_code || "");
      const cur = escapeHtml((c.currency || "usd"));
      const sym = curSymbol(cur);
      return `<option value="${id}" data-plan="${plan}" data-mrr="${mrr}" data-fc="${fc}" data-cur="${cur}">${name} - ${plan} (${sym}${mrr}/mo)</option>`;
    }).join("");
    const pill = $("#modePill");
    if (cfg.mock) {
      pill.textContent = "Demo mode: deterministic, no API key needed";
      pill.style.color = "var(--warn)";
    } else {
      pill.textContent = "Live: powered by " + (cfg.model || "OpenAI");
    }
    syncCustomer();
  } catch (e) {
    $("#customer").innerHTML = '<option value="cust_001">Acme Robotics - Scale</option>';
  }
}

function curSymbol(code) {
  return { usd:"$", eur:"€", gbp:"£", USD:"$", EUR:"€", GBP:"£" }[code] || "";
}

function syncCustomer() {
  const opt = $("#customer").selectedOptions[0];
  if (!opt) return;
  const fc = opt.getAttribute("data-fc");
  if (fc) $("#failure").value = fc;
  const mrr = opt.getAttribute("data-mrr");
  const sym = curSymbol(opt.getAttribute("data-cur") || "usd");
  $("#amountNote").textContent = mrr ? `Amount on this invoice: ${sym}${mrr}` : "";
}
$("#customer").addEventListener("change", syncCustomer);

function setFlow(active) {
  document.querySelectorAll("#flow .node").forEach(n => {
    n.classList.toggle("active", active.includes(n.getAttribute("data-n")));
  });
}

async function run() {
  const btn = $("#runBtn");
  const opt = $("#customer").selectedOptions[0];
  const mrr = parseFloat(opt?.getAttribute("data-mrr") || "0");
  const payload = {
    customer_id: $("#customer").value,
    amount: mrr,
    currency: opt?.getAttribute("data-cur") || "usd",
    failure_code: $("#failure").value,
    attempt: parseInt($("#attempt").value, 10) || 1,
  };
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> Running...';
  $("#emptyState").style.display = "none";
  const res = $("#result");
  res.style.display = "block";
  res.innerHTML = '<div class="block"><div class="v" style="color:var(--muted)">Running the recovery graph...</div></div>';

  // animate the flow while we wait - but honour reduced-motion preference
  const order = ["retrieve","risk","diagnose","strategy","schedule","draft","finalize"];
  const steps = order.map((_, i) => order.slice(0, i + 1));
  const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let i = 0;
  const anim = reduceMotion ? null : setInterval(() => { setFlow(steps[Math.min(i++, steps.length-1)]); }, 260);

  try {
    const r = await fetch("/payment-failed", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    clearInterval(anim);
    setFlow(order);
    const s = d.strategy || {};
    const sc = d.schedule || {};
    const im = d.impact || {};
    const rk = d.risk || {};
    const churn = String(im.churn_risk || rk.churn_risk || "low");
    const escBadge = s.escalated ? ` <span class="chip">escalated</span>` : "";
    const money = (n) => (im.currency === "USD" ? "$" : "") + Number(n || 0).toLocaleString(undefined, {maximumFractionDigits: 0}) + (im.currency && im.currency !== "USD" ? " " + escapeHtml(im.currency) : "");
    const pct = Math.round((im.recovery_likelihood || 0) * 100);
    res.innerHTML = `
      <div class="block">
        <h3 class="bt">Revenue at stake <span class="chip">churn risk: ${escapeHtml(churn)}</span></h3>
        <table class="kv">
          <tr><th>Expected recovered</th><td>${money(im.expected_recovered)}</td></tr>
          <tr><th>Amount at risk</th><td>${money(im.amount_at_risk)}</td></tr>
          <tr><th>Recovery likelihood</th><td>${pct}%</td></tr>
          <tr><th>Annual value if the customer leaves</th><td>${money(im.annual_value_at_risk)}</td></tr>
        </table>
        <p class="note">An estimate from the failure type and the customer's plan value, not a measured recovery rate.</p>
      </div>
      <div class="block diagnosis">
        <h3 class="bt">Diagnosis</h3>
        <div class="v">${escapeHtml(d.diagnosis || "")}</div>
      </div>
      <div class="block">
        <h3 class="bt">Strategy <span class="chip">fixed rules table</span>${escBadge}</h3>
        <table class="kv">
          <tr><th>Action</th><td>${escapeHtml(humanize(s.action))}</td></tr>
          <tr><th>Retry in</th><td>${escapeHtml(String(s.retry_in_days ?? "-"))} day${s.retry_in_days === 1 ? "" : "s"}</td></tr>
          <tr><th>Next retry</th><td>${escapeHtml(sc.retry_on || "-")} (${escapeHtml(sc.timezone || "UTC")})</td></tr>
          <tr><th>Offer</th><td>${escapeHtml(s.offer || "-")}</td></tr>
        </table>
      </div>
      <div class="block message">
        <h3 class="bt">Drafted email <button class="copy-btn" data-copy="1" aria-label="Copy the drafted email">Copy</button></h3>
        <div class="v">${escapeHtml(d.message || "")}${d.disclosure ? `<div class="disclosure">${escapeHtml(d.disclosure)}</div>` : ""}</div>
      </div>`;
    window.__lastEmail = d.disclosure ? `${d.message || ""}\n\n${d.disclosure}` : (d.message || "");
  } catch (e) {
    clearInterval(anim);
    res.innerHTML = `<div class="block"><h3 class="bt">Error</h3><div class="v">${escapeHtml(e.message)}. If a real key was just added, the first call also builds the vector store - try again.</div></div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Run recovery agent";
  }
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}
function humanize(s) {
  if (!s) return "-";
  return String(s).replace(/_/g, " ").replace(/^\w/, c => c.toUpperCase());
}
function copyEmail(btn) {
  const text = window.__lastEmail || "";
  const done = () => { btn.textContent = "Copied"; setTimeout(() => { btn.textContent = "Copy"; }, 1500); };
  const fail = () => { btn.textContent = "Copy failed"; setTimeout(() => { btn.textContent = "Copy"; }, 1500); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(fail);
    return;
  }
  // Fallback for insecure contexts / older browsers where clipboard API is absent.
  try {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.focus(); ta.select();
    document.execCommand("copy"); document.body.removeChild(ta); done();
  } catch (e) { fail(); }
}

const RINTENT = { already_paid: "Customer says they have paid", dispute: "Dispute or double charge", cancel: "Cancellation", opt_out: "Asks not to be contacted", update_details: "Wants to update payment details", extension: "Asks for more time", unclear: "Not clear, goes to a person" };
const RCHECK = { unchecked: "No bank feed connected", feed_unreachable: "Bank feed not reachable", found_full_pending: "Matching payment found, pending", found_full_booked: "Matching payment found, booked", found_part: "Matching payment found, less than the invoice", not_found: "No matching payment found", expired: "A matching payment was seen earlier and has not been seen since", currency_mismatch: "Matching payment in a different currency" };
document.querySelectorAll("#rsamples .textbtn").forEach(b => b.addEventListener("click", () => { $("#rmsg").value = b.getAttribute("data-m"); }));
async function readReply() {
  const btn = $("#rrunBtn");
  btn.disabled = true; btn.textContent = "Reading...";
  const out = $("#rout");
  try {
    const r = await fetch("/inbound/read", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: $("#rmsg").value, customer_first_name: $("#rname").value || null, bank_feed: $("#rfeed").value }) });
    const j = await r.json();
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : "The request was refused");
    const acts = (j.actions || []).map(humanize).join(", ");
    out.innerHTML = `
      <div class="block">
        <h3 class="bt">Reading</h3>
        <table class="kv">
          <tr><th>Intent</th><td>${escapeHtml(RINTENT[j.reading.intent] || j.reading.intent)}</td></tr>
          <tr><th>Payment check</th><td>${escapeHtml(RCHECK[j.payment_check] || j.payment_check)}</td></tr>
          <tr><th>Rule applied</th><td>${escapeHtml(j.rule_fired)}</td></tr>
          <tr><th>Actions</th><td>${escapeHtml(acts || "-")}</td></tr>
          <tr><th>A person must act</th><td>${j.person_must_act ? "Yes, beyond approving the draft" : "No, approval is enough"}</td></tr>
        </table>
      </div>
      <div class="block message">
        <h3 class="bt">Draft reply for approval <button class="copy-btn" data-copy="1" aria-label="Copy the drafted reply">Copy</button></h3>
        <div class="v">${escapeHtml(j.reply.subject)}\n\n${escapeHtml(j.reply.body)}</div>
      </div>`;
    window.__lastEmail = j.reply.subject + "\n\n" + j.reply.body;
  } catch (e) {
    out.innerHTML = `<div class="block"><h3 class="bt">Error</h3><div class="v">${escapeHtml(e.message)}</div></div>`;
  } finally { btn.disabled = false; btn.textContent = "Read the reply"; }
}
$("#rrunBtn").addEventListener("click", readReply);

$("#runBtn").addEventListener("click", run);
loadConfig();
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (b) copyEmail(b);
});
