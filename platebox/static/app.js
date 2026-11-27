(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function api(url, body) {
    const opt = { credentials: "same-origin", headers: { "Content-Type": "application/json" } };
    if (body !== undefined) { opt.method = "POST"; opt.body = JSON.stringify(body); }
    const r = await fetch(url, opt);
    let j = null;
    try { j = await r.json(); } catch (e) { /* not JSON */ }
    if (r.status === 401) { location.href = "/login"; throw new Error("Signed out"); }
    if (!r.ok) throw new Error((j && (j.error || j.detail)) || r.statusText);
    return j;
  }

  /* ---- times: the camera's own clock as printed, the box's UTC converted to this device ---- */
  const fmt = (d) => d.toLocaleString([], { dateStyle: "medium", timeStyle: "medium" });
  function localize(root = document) {
    $$("time[data-utc]", root).forEach((t) => { const d = new Date(t.dataset.utc); if (!isNaN(d)) t.textContent = fmt(d); });
    $$("time[data-local]", root).forEach((t) => { const d = new Date(t.dataset.local.replace(" ", "T")); if (!isNaN(d)) t.textContent = fmt(d); });
  }

  /* ---- click to copy, show/hide the password ---- */
  function copyable(root = document) {
    $$("[data-copy]", root).forEach((el) => {
      el.title = "Click to copy";
      el.addEventListener("click", async () => {
        const text = el.dataset.value || el.textContent.trim();
        if (el.hasAttribute("data-secret") && !el.dataset.shown) return;
        try { await navigator.clipboard.writeText(text); } catch (e) {
          const r = document.createRange(); r.selectNodeContents(el);
          const s = getSelection(); s.removeAllRanges(); s.addRange(r); document.execCommand("copy");
        }
        const old = el.style.outline; el.style.outline = "2px solid var(--ok)"; setTimeout(() => (el.style.outline = old), 600);
      });
    });
    const rev = $("#reveal");
    if (rev) rev.addEventListener("click", () => {
      const s = $("[data-secret]");
      if (s.dataset.shown) { s.textContent = "••••••••"; delete s.dataset.shown; rev.textContent = "show"; }
      else { s.textContent = s.dataset.value || ""; s.dataset.shown = "1"; rev.textContent = "hide"; }
    });
  }

  /* ---- plates screen: refresh when something new has been seen ---- */
  function platesPage() {
    const list = $("#list");
    let sig = ($("#counts") || {}).dataset ? $("#counts").dataset.sig : "";
    async function tick() {
      try {
        const s = await api("/api/plates/summary");
        const now = `${s.sightings}|${s.last}`;
        if (sig !== now && !(sig === "" && s.sightings === 0)) {
          const open = new Set($$("article.plate details[open]", list).map((d) => d.closest("article").dataset.plate));
          const r = await fetch(`/fragment/plates?q=${encodeURIComponent(list.dataset.q || "")}`, { credentials: "same-origin" });
          if (r.ok) {
            list.innerHTML = await r.text();
            $$("article.plate", list).forEach((a) => { if (open.has(a.dataset.plate)) $("details", a).open = true; });
            localize(list);
            sig = now;
          }
        }
      } catch (e) { /* box restarting: try again */ }
      setTimeout(tick, 4000);
    }
    setTimeout(tick, 4000);
  }

  /* ---- setup wizard ---- */
  const setStep = (step) => api("/api/setup/step", { step }).then(() => location.reload());

  function netStep() {
    const status = $("#netstatus"), actions = $("#netactions"), wifi = $("#wifi");
    const list = $("#wifilist"), form = $("#wifiform"), msg = $("#wifimsg");
    let chosen = null;

    async function refresh() {
      try {
        const n = await api("/api/net/status");
        if (n.connected) {
          const how = n.kind === "ethernet" ? "by cable" : n.kind === "hotspot" ? "to its own setup Wi-Fi" : `to Wi-Fi “${n.ssid}”`;
          status.className = "status ok";
          status.textContent = `Connected ${how}. The box's address is ${n.ip}.`;
          $("#net-continue").disabled = n.kind === "hotspot";
          if (n.kind === "hotspot") status.textContent += " Join the camera's network below to continue.";
          actions.hidden = false;
          if (n.kind === "hotspot") openWifi();
        } else {
          status.className = "status bad"; status.textContent = "Not connected to a network yet.";
          actions.hidden = false; openWifi();
        }
        if (!n.has_wifi) { $("#net-change").hidden = true; wifi.hidden = true; if (!n.connected) status.textContent = "Plug the box into the network with a cable."; }
      } catch (e) { status.className = "status bad"; status.textContent = e.message; }
    }

    async function openWifi() {
      wifi.hidden = false; list.innerHTML = "<li>Scanning…</li>";
      try {
        const r = await api("/api/net/scan");
        list.innerHTML = "";
        if (!r.networks.length) list.innerHTML = "<li>No networks found.</li>";
        r.networks.forEach((w) => {
          const li = document.createElement("li"); li.tabIndex = 0;
          li.innerHTML = `<span></span><span class="sub"></span>`;
          li.firstChild.textContent = (w.secured ? "🔒 " : "") + w.ssid + (w.in_use ? " (current)" : "");
          li.lastChild.textContent = `${w.signal}%`;
          const pick = () => { chosen = w; $("#wifiname").textContent = `Password for “${w.ssid}”`; form.hidden = !w.secured; msg.hidden = true; if (!w.secured) join(); else { $("#wifipass").value = ""; $("#wifipass").focus(); } };
          li.addEventListener("click", pick); li.addEventListener("keydown", (e) => { if (e.key === "Enter") pick(); });
          list.appendChild(li);
        });
      } catch (e) { list.innerHTML = `<li>${e.message}</li>`; }
    }

    async function join() {
      msg.hidden = false; msg.className = "msg"; msg.textContent = `Joining “${chosen.ssid}”… If this page stops responding, reconnect your phone or laptop to “${chosen.ssid}” and open http://platebox.local again.`;
      try {
        const r = await api("/api/net/connect", { ssid: chosen.ssid, password: $("#wifipass").value });
        msg.className = "msg " + (r.ok ? "good" : "bad");
        msg.textContent = r.ok ? `Joined “${chosen.ssid}”.` : r.message;
        if (r.ok) refresh();
      } catch (e) {
        msg.className = "msg warn";
        msg.textContent = `The box has moved to “${chosen.ssid}”. Join that Wi-Fi with this device, then open http://platebox.local to carry on.`;
      }
    }

    form.addEventListener("submit", (e) => { e.preventDefault(); join(); });
    $("#net-change").addEventListener("click", openWifi);
    $("#wifi-rescan").addEventListener("click", openWifi);
    $("#net-continue").addEventListener("click", () => setStep("camera"));
    refresh();
  }

  function cameraStep() {
    const btn = $("#cam-search"), box = $("#camstatus"), list = $("#camlist");
    async function choose(ip, label) {
      try { await api("/api/camera/select", { ip, label }); location.reload(); }
      catch (e) { box.hidden = false; box.className = "status bad"; box.textContent = e.message; }
    }
    async function search() {
      btn.disabled = true; list.innerHTML = ""; box.hidden = false; box.className = "status wait"; box.textContent = "Looking for cameras on this network… (about 15 seconds)";
      await api("/api/camera/search", {});
      for (let i = 0; i < 90; i++) {
        await sleep(1000);
        const r = await api("/api/camera/search");
        if (!r.running) {
          btn.disabled = false;
          if (!r.results.length) { box.className = "status bad"; box.textContent = "No camera found. Check that it has power and is on this same network, then search again, or enter its address by hand."; return; }
          box.className = "status ok"; box.textContent = r.results.length === 1 ? "Found 1 camera. Choose it:" : `Found ${r.results.length} cameras. Choose yours:`;
          r.results.forEach((c) => {
            const li = document.createElement("li"); li.tabIndex = 0;
            li.innerHTML = "<span></span><span class='sub'></span>"; li.firstChild.textContent = c.label; li.lastChild.textContent = c.ip;
            const go = () => choose(c.ip, c.label);
            li.addEventListener("click", go); li.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
            list.appendChild(li);
          });
          return;
        }
      }
      btn.disabled = false; box.className = "status bad"; box.textContent = "The search took too long. Try again.";
    }
    btn.addEventListener("click", () => search().catch((e) => { btn.disabled = false; box.hidden = false; box.className = "status bad"; box.textContent = e.message; }));
    $("#cam-manual").addEventListener("submit", (e) => { e.preventDefault(); choose($("#cam-ip").value.trim(), "Camera (entered by hand)"); });
    $("#back-network").addEventListener("click", () => setStep("network"));
  }

  function configureStep() {
    const conn = $("#chk-conn"), pic = $("#chk-pic"), fin = $("#finished"), force = $("#force");
    let finishing = false, started = Date.now();
    const link = $("#camlink");
    async function tick() {
      try {
        const s = await api("/api/setup/status");
        const c = s.camera;
        if (c.camera_ip && link) { link.textContent = `https://${c.camera_ip}`; link.href = `https://${c.camera_ip}`; }
        $("#ipwarn").hidden = !c.ip_changed;
        if (s.sftp) {
          $$("[data-k]").forEach((el) => {
            const v = String(s.sftp[el.dataset.k]);
            if (el.hasAttribute("data-secret")) el.dataset.value = v; else el.textContent = v;
          });
        }
        if (s.receiver && !s.receiver.listening) { conn.className = "status bad"; conn.textContent = s.receiver.error || "The camera receiver is not running."; }
        if (s.receiver && s.receiver.diagnosis) {
          const d = s.receiver.diagnosis, why = $("#chk-why");
          why.hidden = false; why.textContent = "What PlateBox sees: " + d.text;
          why.className = "status " + (d.level === "ok" ? "ok" : d.level === "bad" ? "bad" : "");
        }
        if (c.camera_connected) { conn.className = "status ok"; conn.textContent = c.serial ? `✓ The camera has connected (serial ${c.serial}).` : "✓ The camera has connected."; }
        if (c.picture_received && !finishing) {
          finishing = true; pic.className = "status ok"; pic.textContent = "✓ A picture has arrived.";
          await api("/api/setup/finish", {});
          fin.hidden = false; fin.scrollIntoView({ behavior: "smooth", block: "center" });
          return;
        }
        if (Date.now() - started > 120000 && !c.picture_received) force.hidden = false;
      } catch (e) { /* box busy: next tick */ }
      if (!finishing) setTimeout(tick, 2000);
    }
    force.addEventListener("click", async () => { finishing = true; await api("/api/setup/finish", { force: true }); location.href = "/"; });
    const back = $("#back-camera"); if (back) back.addEventListener("click", () => setStep("camera"));
    tick();
  }



  /* ---- motion log: refresh the first page when something new has been kept ---- */
  function motionPage() {
    const list = $("#list");
    const sig = () => ($(".usage", list) || { dataset: {} }).dataset.sig || "";
    let last = sig();
    async function tick() {
      try {
        const s = await api("/api/motion/summary");
        const now = `${s.count}|${s.last}`;
        if (now !== last && !/page=[1-9]/.test(list.dataset.q)) {
          const r = await fetch(`/fragment/motion?${list.dataset.q}`, { credentials: "same-origin" });
          if (r.ok) { list.innerHTML = await r.text(); localize(list); last = sig(); }
        }
      } catch (e) { /* box restarting: try again */ }
      setTimeout(tick, 5000);
    }
    setTimeout(tick, 5000);
  }

  /* ---- live view: the stream runs only while this page is open and visible ---- */
  function livePage() {
    const test = $("#v-test");
    if (test) test.addEventListener("click", async () => {
      const m = $("#v-testmsg"); m.hidden = false; m.className = "msg"; m.textContent = "Testing… (up to 25 seconds)"; test.disabled = true;
      try { const r = await api("/api/live/test", {}); m.className = "msg " + (r.ok ? "good" : "bad"); m.textContent = r.message; }
      catch (e) { m.className = "msg bad"; m.textContent = e.message; }
      test.disabled = false;
    });
    const view = $("#viewer"); if (!view) return;
    const img = $("#v"), msg = $("#vmsg"), toggle = $("#v-toggle");
    let on = false;
    function show(text, cls) { msg.hidden = false; msg.className = "status " + cls; msg.textContent = text; img.hidden = true; }
    function start() {
      on = true; toggle.textContent = "Pause"; show("Connecting to the camera…", "wait");
      img.src = view.dataset.src + "?t=" + Date.now();
    }
    // Replacing the address (not just removing it) is what makes the browser drop an open picture stream.
    const BLANK = "data:image/gif;base64,R0lGODlhAQABAAAAACw=";
    function stop(label) { on = false; img.src = BLANK; toggle.textContent = "Resume"; if (label) show(label, ""); }
    img.addEventListener("load", () => { if (!on) return; msg.hidden = true; img.hidden = false; });
    img.addEventListener("error", async () => {
      if (!on) return;
      let why = "The camera is not sending a picture.";
      try { const s = await api("/api/live/status"); if (s.error) why = s.error; else if (s.viewers >= s.max_viewers) why = "Too many people are watching right now."; } catch (e) { /* keep default */ }
      stop(why + " Press Resume to try again."); msg.className = "status bad";
    });
    toggle.addEventListener("click", () => (on ? stop("Paused.") : start()));
    $("#v-full").addEventListener("click", () => { const f = $(".frame", view); (document.fullscreenElement ? document.exitFullscreen() : f.requestFullscreen && f.requestFullscreen()); });
    document.addEventListener("visibilitychange", () => { if (document.hidden) { if (on) { stop("Paused while this tab is in the background."); } } else if (!on && msg.textContent.startsWith("Paused while")) start(); });
    window.addEventListener("pagehide", () => { img.src = BLANK; });
    start();
  }

  function settingsPage() {
    const wt = $("#watch-test");
    if (wt) wt.addEventListener("click", async () => {
      const m = $("#watch-msg"); wt.disabled = true; m.hidden = false; m.className = "msg"; m.textContent = "Testing… (up to 25 seconds)";
      try { const r = await api("/api/live/test", {}); m.className = "msg " + (r.ok ? "good" : "bad"); m.textContent = r.message; }
      catch (e) { m.className = "msg bad"; m.textContent = e.message; }
      wt.disabled = false;
    });
    const ws = $("#watch-state");
    if (ws) (async function poll() {
      try { const r = await api("/api/watch"); ws.textContent = r.text; ws.className = "status " + (r.level === "ok" ? "ok" : r.level === "bad" ? "bad" : ""); } catch (e) { /* restarting */ }
      setTimeout(poll, 5000);
    })();
    const tr = $("#recv-trace");
    if (tr) tr.addEventListener("click", async () => {
      const m = $("#recv-msg"); tr.disabled = true; m.hidden = false; m.className = "msg"; m.textContent = "Starting…";
      try { const r = await api("/api/receiver/trace", {}); m.className = "msg " + (r.ok ? "good" : "bad"); m.textContent = r.message; }
      catch (e) { m.className = "msg bad"; m.textContent = e.message; }
      tr.disabled = false;
    });
    const b = $("#recv-check");
    if (!b) return;
    b.addEventListener("click", async () => {
      const m = $("#recv-msg"); b.disabled = true; m.hidden = false; m.className = "msg"; m.textContent = "Checking…";
      try {
        const r = await api("/api/receiver/check", {});
        m.className = "msg " + (r.ok ? "good" : "bad"); m.textContent = r.message;
        if (r.diagnosis) { const d = $("#diag"); d.textContent = r.diagnosis.text; d.className = "status " + (r.diagnosis.level === "ok" ? "ok" : r.diagnosis.level === "bad" ? "bad" : ""); }
      } catch (e) { m.className = "msg bad"; m.textContent = e.message; }
      b.disabled = false;
    });
  }

  /* ---- the address guard's banner, on every page once set up ---- */
  function addressBanner() {
    const bar = $("#alert"); if (!bar) return;
    const paint = (s) => {
      const show = s.level && s.level !== "ok";
      bar.hidden = !show;
      bar.className = "alert" + (show ? " " + s.level : "");
      $("#alert-title").textContent = s.title || ""; $("#alert-text").textContent = s.text || "";
      $("#alert-ack").hidden = s.level !== "bad"; $("#alert-more").hidden = !show;
      const page = $("#addr-state"); if (page) { page.textContent = show ? (s.title + ". " + s.text) : "All good: the camera's address and this computer's address match."; page.className = "status " + (show ? (s.level === "bad" ? "bad" : "ok") : "ok"); }
    };
    $("#alert-ack").addEventListener("click", async () => {
      const b = $("#alert-ack"); b.disabled = true;
      try { paint(await api("/api/address/ack", {})); } catch (e) { /* next poll */ }
      b.disabled = false;
    });
    async function poll() {
      try { paint(await api("/api/address")); } catch (e) { /* box restarting */ }
      setTimeout(poll, 10000);
    }
    setTimeout(poll, 10000);
  }

  document.addEventListener("DOMContentLoaded", () => {
    localize(); copyable();
    if (document.body.dataset.nav) addressBanner();
    const page = document.body.dataset.page;
    if (page === "plates") platesPage();
    if (page === "live") livePage();
    if (page === "motion") motionPage();
    if (page === "settings") settingsPage();
    if (page === "setup") {
      const step = ($("#setup") || {}).dataset ? $("#setup").dataset.step : "";
      if (step === "network") netStep();
      if (step === "camera") cameraStep();
      if (step === "configure") configureStep();
    }
  });
})();
