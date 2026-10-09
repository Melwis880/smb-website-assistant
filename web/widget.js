/*
 * Brightsmile Dental website assistant - embeddable chat widget (demo).
 * Usage: <script src="https://YOUR-HOST/widget.js" data-api="https://YOUR-HOST" defer></script>
 * No dependencies. All bot text is inserted with textContent (never innerHTML).
 */
(function () {
  "use strict";
  var script = document.currentScript;
  var API = ((script && script.getAttribute("data-api")) || "").replace(/\/$/, "");
  var TITLE = (script && script.getAttribute("data-title")) || "Ask Brightsmile Dental";
  var STORE_KEY = "bsd-chat-session";
  var LOG_KEY = "bsd-chat-log";
  var sessionId = null;
  var transcript = [];
  // Restore both the session and the visible transcript, so a booking flow started on one page
  // is still on screen on the next page (the server keeps the flow state per session).
  try {
    sessionId = sessionStorage.getItem(STORE_KEY);
    transcript = JSON.parse(sessionStorage.getItem(LOG_KEY) || "[]");
    if (!Array.isArray(transcript)) transcript = [];
  } catch (e) { transcript = []; }

  var css =
    ".bsd-btn{position:fixed;right:20px;bottom:20px;z-index:2147483000;border:0;border-radius:28px;padding:14px 20px;" +
    "background:#0f766e;color:#fff;font:600 15px/1 system-ui,sans-serif;box-shadow:0 4px 14px rgba(0,0,0,.2);cursor:pointer}" +
    ".bsd-panel{position:fixed;right:20px;bottom:80px;z-index:2147483000;width:min(360px,calc(100vw - 32px));height:min(520px,calc(100vh - 120px));" +
    "display:none;flex-direction:column;background:#fff;color:#111;border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.25);" +
    "font:14px/1.45 system-ui,sans-serif;overflow:hidden}" +
    ".bsd-panel.open{display:flex}" +
    ".bsd-head{background:#0f766e;color:#fff;padding:12px 14px;font-weight:600;display:flex;justify-content:space-between;align-items:center}" +
    ".bsd-head button{background:none;border:0;color:#fff;font-size:20px;cursor:pointer}" +
    ".bsd-log{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px;background:#f8fafc}" +
    ".bsd-msg{max-width:85%;padding:8px 11px;border-radius:12px;white-space:pre-wrap;word-wrap:break-word}" +
    ".bsd-bot{background:#fff;border:1px solid #e2e8f0;align-self:flex-start}" +
    ".bsd-user{background:#0f766e;color:#fff;align-self:flex-end}" +
    ".bsd-src{font-size:11px;color:#64748b;margin-top:4px}" +
    ".bsd-form{display:flex;border-top:1px solid #e2e8f0}" +
    ".bsd-form input{flex:1;border:0;padding:12px;font:inherit;outline:none;min-width:0}" +
    ".bsd-form button{border:0;background:#0f766e;color:#fff;padding:0 16px;font:inherit;cursor:pointer}" +
    ".bsd-note{font-size:11px;color:#64748b;padding:4px 12px 8px;background:#fff}";

  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }

  var btn = el("button", "bsd-btn", "Questions? Chat with us");
  btn.setAttribute("aria-expanded", "false");
  var panel = el("div", "bsd-panel");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", TITLE);
  var head = el("div", "bsd-head", TITLE);
  var close = el("button", null, "×");
  close.setAttribute("aria-label", "Close chat");
  head.appendChild(close);
  var log = el("div", "bsd-log");
  log.setAttribute("aria-live", "polite");
  var form = el("form", "bsd-form");
  var input = el("input");
  input.placeholder = "Ask about prices, hours, insurance...";
  input.maxLength = 1000;
  input.setAttribute("aria-label", "Your message");
  var send = el("button", null, "Send");
  send.type = "submit";
  form.appendChild(input);
  form.appendChild(send);
  var note = el("div", "bsd-note", "Automated assistant. Answers come from our website content; no medical advice.");
  panel.appendChild(head);
  panel.appendChild(log);
  panel.appendChild(form);
  panel.appendChild(note);
  document.body.appendChild(btn);
  document.body.appendChild(panel);

  function render(text, who, sources) {
    var m = el("div", "bsd-msg " + (who === "user" ? "bsd-user" : "bsd-bot"), text);
    if (sources && sources.length) m.appendChild(el("div", "bsd-src", "Source: " + sources.join("; ")));
    log.appendChild(m);
    log.scrollTop = log.scrollHeight;
    return m;
  }

  function add(text, who, sources) {
    transcript.push({ text: String(text), who: who, sources: sources || null });
    transcript = transcript.slice(-50);
    try { sessionStorage.setItem(LOG_KEY, JSON.stringify(transcript)); } catch (e) {}
    return render(text, who, sources);
  }

  transcript.forEach(function (h) {
    if (h && typeof h.text === "string") render(h.text, h.who, Array.isArray(h.sources) ? h.sources : null);
  });

  function toggle(open) {
    panel.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", String(open));
    if (open) {
      if (!log.childNodes.length)
        add("Hi! I can answer questions about our services, prices, opening hours and insurance, or take a booking request.", "bot");
      input.focus();
    }
  }
  btn.addEventListener("click", function () { toggle(!panel.classList.contains("open")); });
  close.addEventListener("click", function () { toggle(false); });

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    add(text, "user");
    var pending = render("…", "bot");
    send.disabled = true;
    fetch(API + "/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId })
    })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (data) {
        sessionId = data.session_id;
        try { sessionStorage.setItem(STORE_KEY, sessionId); } catch (e) {}
        log.removeChild(pending);
        add(data.reply, "bot", data.type === "answer" ? data.sources : null);
      })
      .catch(function () {
        if (pending.parentNode) log.removeChild(pending);
        add("Sorry, something went wrong. Please call us or try again.", "bot");
      })
      .then(function () { send.disabled = false; input.focus(); });
  });
})();
