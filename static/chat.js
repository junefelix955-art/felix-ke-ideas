(function () {
  var box = document.getElementById("chat"); if (!box) return;
  var log = document.getElementById("log"), csrf = box.dataset.csrf || "";
  var last = parseInt(box.dataset.last, 10) || 0, kind = box.dataset.kind, busy = false;
  var form = document.getElementById("composer"), body = document.getElementById("body"), fileIn = document.getElementById("file");
  function bottom() { log.scrollTop = log.scrollHeight; }
  bottom();
  function post(url, data) {
    return fetch(url, { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf, Accept: "application/json" }, body: data });
  }
  function nearBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 120; }
  function renderReacts(el, list) {
    var r = el.querySelector("[data-reacts]"); if (!r) return;
    r.textContent = "";
    list.forEach(function (x) { var b = document.createElement("button"); b.type = "button"; b.className = "react" + (x.mine ? " mine" : ""); b.dataset.emoji = x.emoji; b.textContent = x.emoji + " " + x.count; r.appendChild(b); });
  }
  function apply(u) {
    var el = log.querySelector('.msg[data-id="' + u.id + '"]'); if (!el) return;
    if (u.deleted && !el.querySelector(".bubble.deleted")) { var b = el.querySelector(".bubble"); b.className = "bubble deleted"; b.textContent = ""; var e = document.createElement("em"); e.textContent = "This message was deleted"; b.appendChild(e); var t = el.querySelector(".msg-tools"); if (t) t.remove(); }
    renderReacts(el, u.reactions || []);
    var tick = el.querySelector("[data-tick]");
    if (tick && u.status) { tick.className = "tick " + u.status; tick.textContent = (u.status === "sent" ? "✓" : "✓✓"); tick.title = u.status.charAt(0).toUpperCase() + u.status.slice(1); }
    var sb = el.querySelector("[data-seenby]"); if (sb && kind === "squad") sb.textContent = u.seen_by ? " Seen by " + u.seen_by : "";
  }
  function poll() {
    if (document.hidden || busy) return;
    fetch(box.dataset.poll + "?after=" + last, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
        if (!d) return;
        if (d.html) { var stick = nearBottom(), empty = document.getElementById("empty"); if (empty) empty.remove(); var end = document.getElementById("end"); end.insertAdjacentHTML("beforebegin", d.html); if (stick) bottom(); }
        last = d.last || last; (d.updates || []).forEach(apply);
        if (d.online !== null && d.online !== undefined) { var p = document.getElementById("presence"); if (p && d.online) p.textContent = "Online now"; }
      }).catch(function () {});
  }
  var timer = setInterval(poll, 3000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });

  var replyBar = document.getElementById("reply-bar"), replyTo = document.getElementById("reply_to");
  function clearReply() { if (replyTo) replyTo.value = ""; if (replyBar) replyBar.hidden = true; }
  var rc = document.getElementById("reply-cancel"); if (rc) rc.addEventListener("click", clearReply);

  log.addEventListener("click", function (e) {
    var btn = e.target.closest("button"); if (!btn) return;
    var msg = btn.closest(".msg"); if (!msg) return;
    var id = msg.dataset.id, fd = new FormData();
    if (btn.classList.contains("react")) { fd.append("emoji", btn.dataset.emoji); post(box.dataset.base + "/react/" + id, fd).then(poll); return; }
    var act = btn.dataset.act; if (!act) return;
    if (act === "react") { fd.append("emoji", btn.dataset.emoji); post(box.dataset.base + "/react/" + id, fd).then(poll); }
    else if (act === "delete") { if (confirm("Delete this message for everyone?")) post(box.dataset.base + "/delete/" + id, fd).then(poll); }
    else if (act === "reply" && replyTo) { replyTo.value = id; document.getElementById("reply-text").textContent = "Replying to " + btn.dataset.who + ": " + btn.dataset.text; replyBar.hidden = false; body.focus(); }
    else if (act === "report") { var why = prompt("Why are you reporting this message?"); if (why) { fd.append("kind", "msg"); fd.append("target_id", id); fd.append("reason", why); post(box.dataset.report, fd).then(function () { alert("Thanks. A moderator will review it."); }); } }
  });

  if (!form) return;
  fileIn.addEventListener("change", function () { var n = document.getElementById("file-name"); if (fileIn.files[0]) { n.textContent = "Attached: " + fileIn.files[0].name; n.hidden = false; } else n.hidden = true; });
  body.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.innerWidth > 700) { e.preventDefault(); form.requestSubmit(); } });
  body.addEventListener("input", function () { body.style.height = "auto"; body.style.height = Math.min(body.scrollHeight, 140) + "px"; });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (!body.value.trim() && !fileIn.files[0]) return;
    var send = document.getElementById("send"); send.disabled = true; busy = true;
    post(box.dataset.send, new FormData(form)).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); }).then(function (x) {
      if (!x.ok) { alert(x.d.error || "Could not send."); return; }
      body.value = ""; body.style.height = "auto"; fileIn.value = ""; document.getElementById("file-name").hidden = true; clearReply();
    }).catch(function () { alert("Network problem. Please try again."); }).then(function () { send.disabled = false; busy = false; poll(); setTimeout(bottom, 150); });
  });
})();
