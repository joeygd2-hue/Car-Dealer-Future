/* Embeddable website chat widget (first-party channel). Usage:
   <script src="https://YOUR-HOST/static/chat-widget.js" data-key="PUBLIC_CHAT_KEY" data-base="https://YOUR-HOST" defer></script> */
(function () {
  var s = document.currentScript; if (!s) return;
  var key = s.getAttribute("data-key"), base = s.getAttribute("data-base") || "", name = s.getAttribute("data-name") || "Chat";
  var vid; try { vid = localStorage.getItem("cdf_vid_" + key); if (!vid) { vid = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now()); localStorage.setItem("cdf_vid_" + key, vid); } } catch (e) { vid = String(Math.random()).slice(2) + Date.now(); }
  var lastId = 0, box = document.createElement("div");
  box.style.cssText = "position:fixed;right:16px;bottom:16px;width:min(360px,calc(100vw - 32px));background:#fff;color:#16181d;border:1px solid #dfe3e8;border-radius:12px;box-shadow:0 6px 24px rgba(0,0,0,.15);font:14px system-ui,sans-serif;z-index:99999;display:flex;flex-direction:column;max-height:70vh";
  box.innerHTML = '<div style="padding:10px 12px;border-bottom:1px solid #dfe3e8;font-weight:600"></div>' +
    '<div class="cdf-log" style="padding:10px;overflow:auto;flex:1;display:flex;flex-direction:column;gap:6px"></div>' +
    '<div style="padding:6px 10px;font-size:11px;color:#5d6472">Replies come from our team, sometimes with help from an automated assistant. Please don\'t share sensitive info (SSN, card numbers). Escribe en español si prefieres.</div>' +
    '<form style="display:flex;gap:6px;padding:10px;border-top:1px solid #dfe3e8"><input required maxlength="2000" placeholder="Type a message / Escriba un mensaje" style="flex:1;padding:8px;border:1px solid #dfe3e8;border-radius:8px"><button style="padding:8px 12px;border-radius:8px;border:0;background:#1d5fd1;color:#fff">Send</button></form>';
  box.firstChild.textContent = name;
  var log = box.querySelector(".cdf-log"), form = box.querySelector("form"), input = form.querySelector("input");
  function add(text, mine) { var d = document.createElement("div"); d.textContent = text; d.style.cssText = "padding:7px 10px;border-radius:10px;max-width:85%;" + (mine ? "align-self:flex-end;background:#e8f0fd" : "background:#f0f2f5"); log.appendChild(d); log.scrollTop = log.scrollHeight; }
  form.addEventListener("submit", function (e) {
    e.preventDefault(); var text = input.value.trim(); if (!text) return; input.value = ""; add(text, true);
    fetch(base + "/api/chat/" + key + "/messages", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ visitor_id: vid, text: text, client_message_id: String(Date.now()) + Math.random().toString(16).slice(2) }) })
      .then(function (r) { if (r.status === 429) add("Please wait a moment before sending more messages.", false); })
      .catch(function () { add("Message could not be sent. Please try again.", false); });
  });
  function poll() {
    fetch(base + "/api/chat/" + key + "/messages?visitor_id=" + encodeURIComponent(vid) + "&after_id=" + lastId)
      .then(function (r) { return r.json(); }).then(function (d) { (d.messages || []).forEach(function (m) { lastId = Math.max(lastId, m.id); add(m.text, false); }); })
      .catch(function () {}).finally(function () { setTimeout(poll, 4000); });
  }
  document.body.appendChild(box); poll();
})();
