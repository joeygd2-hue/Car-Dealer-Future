// Reference website-chat widget for /chat/{key}. Loaded as a file so the CSP can forbid inline script.
// Shows only replies the platform really delivered (status "sent"); simulated replies never reach the visitor.
(function () {
  var log = document.getElementById("chat-log");
  var form = document.getElementById("chat-form");
  if (!log || !form) return;
  var key = log.getAttribute("data-key");
  var base = "/api/public/chat/" + encodeURIComponent(key) + "/messages";
  var visitor = null;
  try { visitor = window.localStorage.getItem("df-visitor"); } catch (e) { visitor = null; }
  if (!visitor) {
    visitor = "v" + Math.random().toString(36).slice(2) + Date.now().toString(36);
    try { window.localStorage.setItem("df-visitor", visitor); } catch (e) { /* private mode: per-page visitor */ }
  }
  var lastId = 0;
  function add(text, who) {
    var p = document.createElement("p");
    p.className = "chatmsg " + who;
    p.textContent = text;  // never innerHTML: replies are plain text
    log.appendChild(p);
  }
  function poll() {
    fetch(base + "?visitor_id=" + encodeURIComponent(visitor) + "&after_id=" + lastId)
      .then(function (r) { return r.ok ? r.json() : { messages: [] }; })
      .then(function (d) { (d.messages || []).forEach(function (m) { lastId = Math.max(lastId, m.id); add(m.text, "them"); }); })
      .catch(function () { /* offline: try again on the next tick */ });
  }
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var f = new FormData(form);
    var payload = { visitor_id: visitor, client_message_id: "m" + Date.now().toString(36), text: f.get("text"),
                    name: f.get("name"), email: f.get("email"), phone: f.get("phone"), vehicle_stock: f.get("vehicle_stock") };
    var status = document.getElementById("chat-status");
    status.textContent = "Sending…";
    fetch(base, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (res.ok) { add(payload.text, "me"); form.elements.text.value = ""; status.textContent = "Sent. A team member will reply here."; }
        else { status.textContent = (res.d && (res.d.error || res.d.detail)) || "Could not send."; }
      })
      .catch(function () { status.textContent = "Could not send. Check your connection."; });
  });
  poll();
  window.setInterval(poll, 5000);
})();
