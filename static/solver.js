(function () {
  var input = document.getElementById("scan"); if (!input) return;
  var status = document.getElementById("scan-status"), out = document.getElementById("problem"), prev = document.getElementById("scan-preview");
  var lib = null;
  function say(t) { status.textContent = t; status.hidden = !t; }
  function load() {
    if (window.Tesseract) return Promise.resolve();
    return new Promise(function (ok, bad) {
      var s = document.createElement("script"); s.src = "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js";
      s.onload = ok; s.onerror = function () { bad(new Error("load")); }; document.head.appendChild(s);
    });
  }
  function shrink(file) {  // downscale big phone photos so OCR is quick
    return new Promise(function (ok) {
      var url = URL.createObjectURL(file), img = new Image();
      img.onload = function () {
        var max = 1600, k = Math.min(1, max / Math.max(img.width, img.height)), c = document.createElement("canvas");
        c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
        var g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height); g.drawImage(img, 0, 0, c.width, c.height);
        c.toBlob(function (b) { ok({ blob: b, url: url }); }, "image/png");
      };
      img.onerror = function () { ok(null); }; img.src = url;
    });
  }
  function tidy(t) {  // OCR output -> something the solver understands
    return t.replace(/[\r\n]+/g, " ").replace(/[×xX](?=\s*\d)/g, "*").replace(/÷/g, "/").replace(/[–—−]/g, "-").replace(/\s+/g, " ").trim();
  }
  input.addEventListener("change", function () {
    var f = input.files[0]; if (!f) return;
    if (f.size > 12 * 1024 * 1024) { say("That photo is too large. Try a smaller one."); return; }
    say("Preparing the photo...");
    shrink(f).then(function (p) {
      if (!p) { say("I could not open that image."); return; }
      prev.src = p.url; prev.hidden = false;
      say("Loading the text reader (first time only)...");
      return load().then(function () {
        say("Reading the text... 0%");
        return window.Tesseract.recognize(p.blob, "eng", { logger: function (m) { if (m.status === "recognizing text") say("Reading the text... " + Math.round(m.progress * 100) + "%"); } });
      }).then(function (r) {
        var t = tidy(r.data.text || "");
        if (!t) { say("I could not find any text. Try a clearer, closer photo, or type the problem."); return; }
        out.value = t.slice(0, 300); say("Done. Check the text above, fix any mistakes, then press Solve."); out.focus();
      });
    }).catch(function () { say("The text reader could not load (are you offline?). You can still type the problem."); });
  });
})();
