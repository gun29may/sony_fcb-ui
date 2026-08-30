/* Sony FCB camera web GUI - front-end logic. */
"use strict";

const $ = (id) => document.getElementById(id);
const dragging = new Set();          // controls the user is currently holding

/* ------------------------------------------------------------------ api */

async function api(path, body) {
  const opts = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok || data.ok === false) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

/** Fire a VISCA action; surface failures as a toast instead of throwing. */
async function visca(action, payload = {}) {
  try {
    return await api(`/api/visca/${action}`, payload);
  } catch (err) {
    toast(`${action}: ${err.message}`, true);
    return null;
  }
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = "show" + (isError ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ""; }, 2600);
}

/* ------------------------------------------------------- zoom conversion */

/* Approximate magnification curves; the exact table varies per FCB model. */
const ZOOM_CURVES = {
  10: [0x0000, 0x1600, 0x2000, 0x2700, 0x2C00, 0x3000, 0x3400, 0x3800, 0x3C00, 0x4000],
  20: [0x0000, 0x1AC4, 0x2510, 0x2AAB, 0x2EEE, 0x3221, 0x3479, 0x36B1, 0x3853, 0x39BD,
       0x3ADD, 0x3BDA, 0x3CA4, 0x3D50, 0x3DE0, 0x3E5D, 0x3EC8, 0x3F25, 0x3F73, 0x4000],
  30: [0x0000, 0x1745, 0x2145, 0x2745, 0x2B34, 0x2E0A, 0x3043, 0x3222, 0x33A4, 0x34EF,
       0x3610, 0x3711, 0x37F6, 0x38C3, 0x397D, 0x3A25, 0x3ABE, 0x3B4A, 0x3BCA, 0x3C41,
       0x3CAF, 0x3D16, 0x3D76, 0x3DD0, 0x3E25, 0x3E75, 0x3EC1, 0x3F09, 0x3F4D, 0x4000],
};

function zoomLabel(position) {
  const curve = ZOOM_CURVES[$("selLens").value] || ZOOM_CURVES[30];
  const hex = "0x" + position.toString(16).toUpperCase().padStart(4, "0");
  if (position > 0x4000) {                       // inside the digital range
    const digital = 1 + ((position - 0x4000) / (0x7AC0 - 0x4000)) * 11;
    return `${hex} · ${curve.length}x + D${digital.toFixed(1)}`;
  }
  let mag = 1;
  for (let i = 1; i < curve.length; i++) {
    if (position <= curve[i]) {
      const span = curve[i] - curve[i - 1] || 1;
      mag = i + (position - curve[i - 1]) / span;
      break;
    }
    mag = i + 1;
  }
  return `${hex} · ~${mag.toFixed(1)}x`;
}

/* --------------------------------------------------------- hold buttons */

/** Wire a button so it drives while held and stops on release. */
function holdButton(el, startFn, stopFn) {
  let keepalive = null;
  const start = (ev) => {
    ev.preventDefault();
    if (keepalive) return;
    el.classList.add("held");
    startFn();
    keepalive = setInterval(startFn, 1200);      // refresh the server watchdog
  };
  const stop = () => {
    if (!keepalive) return;
    clearInterval(keepalive);
    keepalive = null;
    el.classList.remove("held");
    stopFn();
  };
  el.addEventListener("pointerdown", start);
  ["pointerup", "pointerleave", "pointercancel"].forEach((e) => el.addEventListener(e, stop));
  window.addEventListener("blur", stop);
  el._holdStart = start;
  el._holdStop = stop;
}

/* ---------------------------------------------------- state -> controls */

function syncControl(el, value, formatter) {
  if (value === null || value === undefined) return;
  if (dragging.has(el.id) || document.activeElement === el) return;
  if (el.type === "checkbox") el.checked = !!value;
  else el.value = value;
  if (formatter) formatter(value);
}

let lastState = {};

function applyState(status) {
  const state = status.state || {};
  lastState = state;

  const video = status.video;
  const videoUp = !!(video && video.running);
  $("dotVideo").className = "dot" + (videoUp ? " on" : "");
  $("pillVideo").textContent = videoUp ? `${video.width}x${video.height}` : "off";
  $("pillFps").textContent = videoUp ? (video.measured_fps || "-") : "-";

  const visca = status.visca || {};
  $("dotVisca").className = "dot" + (visca.connected ? " on" : "");
  $("pillVisca").textContent = visca.connected ? `${visca.port} @${visca.baud}` : "off";
  $("pillModel").textContent = visca.name || "-";

  showStream(videoUp);
  if (!videoUp) {
    const ph = $("placeholder");
    ph.textContent = "";
    const head = document.createElement("b");
    if (!video) {
      head.textContent = "No video device connected.";
      ph.append(head, document.createElement("br"),
                "Plug in the camera, then open Setup \u2192 Connect.");
    } else if (video.error) {
      head.textContent = "Video not streaming.";
      ph.append(head, document.createElement("br"), video.error,
                document.createElement("br"));
      const note = document.createElement("small");
      note.style.color = "var(--dim)";
      note.textContent = `Retrying ${video.device} automatically\u2026 (${video.reopens} reopen attempts)`;
      ph.append(note);
    } else {
      head.textContent = "Waiting for the first frame\u2026";
      ph.append(head);
    }
  }

  if (state.zoom !== null && state.zoom !== undefined) {
    syncControl($("zoomPos"), state.zoom);
    $("zoomPosVal").textContent = zoomLabel(state.zoom);
  }
  if (state.focus !== null && state.focus !== undefined) {
    syncControl($("focusPos"), state.focus);
    $("focusPosVal").textContent = "0x" + state.focus.toString(16).toUpperCase().padStart(4, "0");
  }
  syncControl($("chkAf"), state.focus_auto);
  syncControl($("selAe"), state.ae_mode);
  syncControl($("rngShutter"), state.shutter, (v) => ($("valShutter").textContent = v));
  syncControl($("rngIris"), state.iris, (v) => ($("valIris").textContent = v));
  syncControl($("rngGain"), state.gain, (v) => ($("valGain").textContent = v));
  syncControl($("rngBright"), state.bright, (v) => ($("valBright").textContent = v));
  syncControl($("rngExpComp"), state.exp_comp, (v) => ($("valExpComp").textContent = v));
  syncControl($("chkBacklight"), state.backlight);
  syncControl($("selWb"), state.wb_mode);
  syncControl($("rngRGain"), state.r_gain, (v) => ($("valRGain").textContent = v));
  syncControl($("rngBGain"), state.b_gain, (v) => ($("valBGain").textContent = v));
  syncControl($("rngAperture"), state.aperture, (v) => ($("valAperture").textContent = v));
  syncControl($("chkIcr"), state.ir_cut);

  showDiagnostics(status.diagnostics || []);
  renderForward(status.forward);

  $("btnRec").classList.toggle("on", !!status.recording);
  $("btnRec").textContent = status.recording ? "Stop recording" : "Record";

  updateOsd(status);
  updateAeEnabling();
}

let shownDiagnostics = "";
function showDiagnostics(notes) {
  const key = notes.join("|");
  if (key === shownDiagnostics) return;
  shownDiagnostics = key;
  const bar = $("diagnostics");
  bar.innerHTML = "";
  notes.forEach((note) => {
    const div = document.createElement("div");
    div.className = "note";
    div.textContent = note;
    bar.appendChild(div);
  });
  bar.style.display = notes.length ? "block" : "none";
}

function updateOsd(status) {
  const osd = $("osd");
  if (!$("chkOsd").checked || !status.video || !status.video.running) {
    osd.style.display = "none";
    return;
  }
  const state = status.state || {};
  const parts = [`${status.video.width}x${status.video.height}`, `${status.video.measured_fps || 0} fps`];
  if (state.zoom !== null && state.zoom !== undefined) parts.push("Z " + zoomLabel(state.zoom));
  if (state.focus_auto !== null && state.focus_auto !== undefined) parts.push(state.focus_auto ? "AF" : "MF");
  if (state.ae_mode) parts.push("AE " + state.ae_mode);
  if (state.wb_mode) parts.push("WB " + state.wb_mode);
  osd.textContent = parts.join("   ");
  osd.style.display = "block";
}

/** Grey out the exposure sliders that the current AE mode ignores. */
function updateAeEnabling() {
  const mode = $("selAe").value;
  const enable = {
    shutter: ["manual", "shutter"].includes(mode),
    iris: ["manual", "iris"].includes(mode),
    gain: mode === "manual",
    bright: mode === "bright",
  };
  $("rngShutter").disabled = !enable.shutter;
  $("rngIris").disabled = !enable.iris;
  $("rngGain").disabled = !enable.gain;
  $("rngBright").disabled = !enable.bright;
}

/* ------------------------------------------------------------- streaming */

let streamOn = false;
function showStream(on) {
  const img = $("video");
  if (on && !streamOn) {
    img.src = `/stream.mjpg?quality=${$("selQuality").value}&scale=${$("selScale").value}&t=${Date.now()}`;
    img.style.display = "block";
    $("placeholder").style.display = "none";
    streamOn = true;
  } else if (!on && streamOn) {
    img.removeAttribute("src");
    img.style.display = "none";
    $("placeholder").style.display = "block";
    streamOn = false;
  }
}

function reloadStream() {
  if (!streamOn) return;
  streamOn = false;
  showStream(true);
}

/* ---------------------------------------------------------------- network */

/** Copy helper - the async clipboard API needs a secure context, which plain
 *  http on a LAN address is not, so fall back to a temporary selection. */
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => toast("Copied"),
                                             () => toast("Could not copy", true));
    return;
  }
  const scratch = document.createElement("textarea");
  scratch.value = text;
  scratch.style.position = "fixed";
  scratch.style.opacity = "0";
  document.body.appendChild(scratch);
  scratch.select();
  try {
    document.execCommand("copy");
    toast("Copied");
  } catch (e) {
    toast("Could not copy - select it by hand", true);
  }
  document.body.removeChild(scratch);
}

function urlRow(who, url) {
  const row = document.createElement("div");
  row.className = "urlrow";
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who;
  const code = document.createElement("code");
  code.textContent = url;
  code.title = url;
  const copy = document.createElement("button");
  copy.textContent = "Copy";
  copy.addEventListener("click", () => copyText(url));
  const open = document.createElement("button");
  open.textContent = "Open";
  open.addEventListener("click", () => window.open(url, "_blank", "noopener"));
  row.append(label, code, copy, open);
  return row;
}

async function loadNetwork() {
  let net;
  try {
    net = await api("/api/network");
  } catch (err) {
    $("netStatus").textContent = "Could not read network info: " + err.message;
    return;
  }
  const status = $("netStatus");
  status.textContent = "";
  const badge = document.createElement("span");
  badge.className = "badge " + (net.shared ? "ok" : "warn");
  badge.textContent = net.shared ? "Shared on the local network" : "This machine only";
  status.append(badge, ` \u00a0 host ${net.hostname} \u00b7 port ${net.port}`);
  if (net.shared) {
    const lock = document.createElement("span");
    lock.className = "badge " + (net.protected ? "ok" : "warn");
    lock.style.marginLeft = "8px";
    lock.textContent = net.protected ? "token required" : "no token";
    status.append(lock);
  }

  const list = $("netUrls");
  list.innerHTML = "";
  if (net.shared) {
    net.urls.forEach((entry) => {
      list.appendChild(urlRow(entry.interface, entry.gui));
      list.appendChild(urlRow("stream", entry.stream));
    });
    if (!net.urls.length) list.appendChild(urlRow("local", net.local.gui));
  } else {
    list.appendChild(urlRow("local", net.local.gui));
  }

  $("netHint").innerHTML = net.shared
    ? "Open a GUI link on any machine on this network. The <b>stream</b> link is "
      + "plain MJPEG - paste it into VLC (Media \u2192 Open Network Stream), OBS "
      + "(Browser or Media source), or an <code>&lt;img&gt;</code> tag. If a "
      + "device cannot connect, allow the port: "
      + "<code>sudo ufw allow " + net.port + "/tcp</code>"
    : "The server is bound to localhost, so only this machine can reach it. "
      + "Restart with <code>./run.sh --lan</code> (add <code>--token SECRET</code> "
      + "to require a password) to share it.";
}

/* ------------------------------------------------------------- forwarding */

function renderForward(fwd) {
  const box = $("fwdStatus");
  box.textContent = "";
  if (!fwd) {
    box.textContent = "Not forwarding.";
    $("btnFwdStart").disabled = false;
    $("btnFwdStop").disabled = true;
    return;
  }
  const badge = document.createElement("span");
  badge.className = "badge " + (fwd.running ? "live" : "warn");
  badge.textContent = fwd.running ? "forwarding" : "stopped";
  box.append(badge, ` \u00a0 ${fwd.url}`);
  box.append(document.createElement("br"));
  box.append(`${fwd.codec} \u00b7 ${fwd.fps} fps \u00b7 ${fwd.bitrate} \u00b7 `
             + `${fwd.frames} frames sent \u00b7 up ${fwd.uptime}s`);
  if (fwd.error) {
    box.append(document.createElement("br"));
    const err = document.createElement("span");
    err.style.color = "var(--bad)";
    err.textContent = fwd.error;
    box.append(err);
  }
  $("btnFwdStart").disabled = !!fwd.running;
  $("btnFwdStop").disabled = !fwd.running;
}

/* ----------------------------------------------------------- device setup */

let discovered = { videos: [], serials: [] };

async function rescan() {
  discovered = await api("/api/devices");
  const selVideo = $("selVideo");
  selVideo.innerHTML = "";
  discovered.videos.forEach((dev) => {
    const opt = new Option(`${dev.path} - ${dev.board || dev.name}`, dev.path);
    selVideo.add(opt);
  });
  if (!discovered.videos.length) selVideo.add(new Option("no video device found", ""));
  if (discovered.video) selVideo.value = discovered.video;

  const selSerial = $("selSerial");
  selSerial.innerHTML = "";
  discovered.serials.forEach((dev) => {
    const label = `${dev.path}${dev.board ? " - " + dev.board : ""}${dev.writable ? "" : " (no permission)"}`;
    selSerial.add(new Option(label, dev.path));
  });
  if (!discovered.serials.length) selSerial.add(new Option("no serial port found", ""));
  if (discovered.serial) selSerial.value = discovered.serial;

  populateFormats();
  toast(`Found ${discovered.videos.length} video, ${discovered.serials.length} serial device(s)`);
}

function populateFormats() {
  const dev = discovered.videos.find((d) => d.path === $("selVideo").value);
  const selFormat = $("selFormat");
  selFormat.innerHTML = "";
  if (!dev) { populateResolutions(); return; }
  dev.formats.forEach((f) => selFormat.add(new Option(`${f.fourcc} - ${f.description}`, f.fourcc)));
  const mjpg = dev.formats.find((f) => f.fourcc === "MJPG");
  if (mjpg) selFormat.value = "MJPG";           // cheapest path over USB
  populateResolutions();
}

function populateResolutions() {
  const dev = discovered.videos.find((d) => d.path === $("selVideo").value);
  const selRes = $("selRes");
  selRes.innerHTML = "";
  const format = dev && dev.formats.find((f) => f.fourcc === $("selFormat").value);
  if (!format) { selRes.add(new Option("default", "1920x1080x30")); return; }
  const sizes = [...format.sizes].sort((a, b) => b.width * b.height - a.width * a.height);
  sizes.forEach((s) => {
    const fps = s.fps && s.fps.length ? s.fps : [30];
    fps.forEach((rate) => selRes.add(
      new Option(`${s.width} x ${s.height} @ ${rate} fps`, `${s.width}x${s.height}x${Math.round(rate)}`)));
  });
}

async function connect() {
  const [width, height, fps] = ($("selRes").value || "1920x1080x30").split("x").map(Number);
  const body = {
    video: $("selVideo").value || null,
    serial: $("selSerial").value || null,
    fourcc: $("selFormat").value || "MJPG",
    width, height, fps,
    baud: Number($("selBaud").value),
    autobaud: $("chkAutobaud").checked,
  };
  try {
    const res = await api("/api/connect", body);
    streamOn = false;
    (res.warnings || []).forEach((w) => toast(w, true));
    if (!res.warnings.length) toast("Connected");
    refresh();
  } catch (err) {
    toast("Connect failed: " + err.message, true);
  }
}

/* ------------------------------------------------------------------ wiring */

function wireSlider(id, action, valueId, transform = (v) => v) {
  const el = $(id);
  el.addEventListener("pointerdown", () => dragging.add(id));
  ["pointerup", "pointercancel", "blur"].forEach((e) =>
    el.addEventListener(e, () => setTimeout(() => dragging.delete(id), 600)));
  el.addEventListener("input", () => {
    if (valueId) $(valueId).textContent = el.value;
  });
  el.addEventListener("change", () => visca(action, { value: transform(Number(el.value)) }));
}

function wireToggle(id, action) {
  $(id).addEventListener("change", (ev) => visca(action, { on: ev.target.checked }));
}

function wireSelect(id, action, transform = (v) => v) {
  $(id).addEventListener("change", (ev) => visca(action, { value: transform(ev.target.value) }));
}

function wireButton(id, fn) { $(id).addEventListener("click", fn); }

function init() {
  /* --- zoom --- */
  const speed = () => Number($("zoomSpeed").value);
  holdButton($("zoomTele"), () => visca("zoom_tele", { speed: speed() }), () => visca("zoom_stop"));
  holdButton($("zoomWide"), () => visca("zoom_wide", { speed: speed() }), () => visca("zoom_stop"));
  $("zoomSpeed").addEventListener("input", (e) => ($("zoomSpeedVal").textContent = e.target.value));

  const zoomPos = $("zoomPos");
  zoomPos.addEventListener("pointerdown", () => dragging.add("zoomPos"));
  ["pointerup", "pointercancel"].forEach((e) =>
    zoomPos.addEventListener(e, () => setTimeout(() => dragging.delete("zoomPos"), 800)));
  zoomPos.addEventListener("input", () => ($("zoomPosVal").textContent = zoomLabel(Number(zoomPos.value))));
  zoomPos.addEventListener("change", () => visca("zoom_to", { value: Number(zoomPos.value) }));
  document.querySelectorAll("[data-zoomto]").forEach((btn) =>
    btn.addEventListener("click", () => visca("zoom_to", { value: Number(btn.dataset.zoomto) })));
  $("chkDzoom").addEventListener("change", (ev) => {
    visca("digital_zoom", { on: ev.target.checked });
    zoomPos.max = ev.target.checked ? 31424 : 16384;   // 0x7AC0 : 0x4000
  });

  /* --- focus --- */
  holdButton($("focusFar"), () => visca("focus_far", { speed: speed() }), () => visca("focus_stop"));
  holdButton($("focusNear"), () => visca("focus_near", { speed: speed() }), () => visca("focus_stop"));
  wireToggle("chkAf", "focus_auto");
  wireButton("btnOnePushAf", () => visca("focus_one_push"));
  wireSelect("selAfMode", "af_mode", Number);
  const focusPos = $("focusPos");
  focusPos.addEventListener("pointerdown", () => dragging.add("focusPos"));
  ["pointerup", "pointercancel"].forEach((e) =>
    focusPos.addEventListener(e, () => setTimeout(() => dragging.delete("focusPos"), 800)));
  focusPos.addEventListener("input", () => {
    $("focusPosVal").textContent = "0x" + Number(focusPos.value).toString(16).toUpperCase().padStart(4, "0");
  });
  focusPos.addEventListener("change", () => {
    if ($("chkAf").checked) { $("chkAf").checked = false; visca("focus_auto", { on: false }); }
    visca("focus_to", { value: Number(focusPos.value) });
  });

  /* --- exposure --- */
  $("selAe").addEventListener("change", () => { visca("ae_mode", { value: $("selAe").value }); updateAeEnabling(); });
  wireSlider("rngShutter", "shutter", "valShutter");
  wireSlider("rngIris", "iris", "valIris");
  wireSlider("rngGain", "gain", "valGain");
  wireSlider("rngBright", "bright", "valBright");
  wireSlider("rngGainLimit", "gain_limit", "valGainLimit");
  wireSlider("rngExpComp", "exp_comp_level", "valExpComp");
  wireToggle("chkExpComp", "exp_comp");
  wireToggle("chkBacklight", "backlight");
  wireToggle("chkWdr", "wide_dynamic");
  wireToggle("chkSlowShutter", "slow_shutter");
  wireToggle("chkHighSens", "high_sensitivity");

  /* --- white balance --- */
  wireSelect("selWb", "wb_mode");
  wireButton("btnWbTrigger", () => visca("wb_trigger").then(() => toast("One-push white balance latched")));
  wireSlider("rngRGain", "r_gain", "valRGain");
  wireSlider("rngBGain", "b_gain", "valBGain");

  /* --- image --- */
  wireSlider("rngAperture", "aperture", "valAperture");
  wireSlider("rngNr", "noise_reduction", "valNr");
  wireSlider("rngGamma", "gamma", "valGamma");
  wireSlider("rngChroma", "chroma_suppress", "valChroma");
  wireSelect("selEffect", "picture_effect");
  wireToggle("chkMirror", "mirror");
  wireToggle("chkFlip", "flip");
  wireToggle("chkFreeze", "freeze");
  wireToggle("chkStab", "stabilizer");
  wireToggle("chkDefog", "defog");
  wireToggle("chkIcr", "ir_cut");
  wireToggle("chkAutoIcr", "auto_ir_cut");

  /* --- presets --- */
  const grid = $("presetGrid");
  for (let slot = 0; slot < 16; slot++) {
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.textContent = slot;
    btn.title = `Recall preset ${slot}`;
    btn.addEventListener("click", () => {
      $("presetSlot").value = slot;
      visca("preset_recall", { slot }).then(() => toast(`Recalled preset ${slot}`));
    });
    grid.appendChild(btn);
  }
  wireButton("btnPresetSet", () => {
    const slot = Number($("presetSlot").value);
    visca("preset_set", { slot }).then(() => toast(`Stored preset ${slot}`));
  });
  wireButton("btnPresetReset", () => {
    const slot = Number($("presetSlot").value);
    visca("preset_reset", { slot }).then(() => toast(`Cleared preset ${slot}`));
  });

  /* --- system --- */
  wireButton("btnMenuOn", () => visca("menu", { on: true }));
  wireButton("btnMenuOff", () => visca("menu", { on: false }));
  document.querySelectorAll("[data-nav]").forEach((btn) =>
    btn.addEventListener("click", () => visca("menu_nav", { value: btn.dataset.nav })));
  wireButton("btnPowerOn", () => visca("power", { on: true }));
  wireButton("btnPowerOff", () => visca("power", { on: false }));
  wireButton("btnIfClear", () => visca("if_clear").then(() => toast("VISCA interface cleared")));
  wireButton("btnRawSend", async () => {
    const cmd = $("rawCmd").value.trim();
    if (!cmd) return;
    const log = $("rawLog");
    const res = await visca("raw", { value: cmd });
    log.textContent += `\n> ${cmd}\n< ${res ? (res.result || "(completion, no data)") : "error"}`;
    log.scrollTop = log.scrollHeight;
  });
  $("rawCmd").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btnRawSend").click(); });

  /* --- network --- */
  $("fwdFps").addEventListener("input", (e) => ($("fwdFpsVal").textContent = e.target.value));
  wireButton("btnFwdStart", async () => {
    const body = {
      url: $("fwdUrl").value.trim(),
      codec: $("fwdCodec").value,
      fps: Number($("fwdFps").value),
      bitrate: $("fwdBitrate").value.trim() || "4M",
      scale: Number($("fwdScale").value),
    };
    try {
      const res = await api("/api/forward/start", body);
      renderForward(res.forward);
      toast("Forwarding to " + body.url);
    } catch (err) {
      toast("Forward failed: " + err.message, true);
    }
  });
  wireButton("btnFwdStop", async () => {
    try {
      await api("/api/forward/stop", {});
      renderForward(null);
      toast("Forwarding stopped");
    } catch (err) { toast(err.message, true); }
  });

  /* --- setup --- */
  wireButton("btnRescan", () => rescan().catch((e) => toast(e.message, true)));
  wireButton("btnConnect", connect);
  wireButton("btnDisconnect", async () => {
    await api("/api/disconnect", {});
    streamOn = true; showStream(false);
    toast("Disconnected");
  });
  $("selVideo").addEventListener("change", populateFormats);
  $("selFormat").addEventListener("change", populateResolutions);
  $("selLens").addEventListener("change", () => {
    if (lastState.zoom != null) $("zoomPosVal").textContent = zoomLabel(lastState.zoom);
  });

  /* --- stage bar --- */
  wireButton("btnSnap", async () => {
    try {
      const res = await api("/api/capture", {});
      toast("Saved " + res.path.split("/").pop());
    } catch (e) { toast(e.message, true); }
  });
  wireButton("btnRec", async () => {
    const on = $("btnRec").classList.contains("on");
    try {
      const res = await api(`/api/record/${on ? "stop" : "start"}`, {});
      toast(on ? "Saved " + (res.path || "").split("/").pop() : "Recording started");
      refresh();
    } catch (e) { toast(e.message, true); }
  });
  wireButton("btnFullscreen", () => {
    const frame = $("frame");
    if (document.fullscreenElement) document.exitFullscreen();
    else frame.requestFullscreen();
  });
  $("selQuality").addEventListener("change", reloadStream);
  $("selScale").addEventListener("change", reloadStream);
  $("chkOsd").addEventListener("change", () => { if (!$("chkOsd").checked) $("osd").style.display = "none"; });

  /* --- tabs (deep-linkable as #lens, #network, ...) --- */
  function showTab(name) {
    const btn = document.querySelector(`#tabs button[data-tab="${name}"]`);
    if (!btn) return;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tabpage").forEach((p) =>
      p.classList.toggle("active", p.dataset.page === name));
  }
  $("tabs").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-tab]");
    if (!btn) return;
    showTab(btn.dataset.tab);
    history.replaceState(null, "", "#" + btn.dataset.tab);
  });
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
  if (location.hash.length > 1) showTab(location.hash.slice(1));

  /* --- keyboard --- */
  const keyHeld = {};
  document.addEventListener("keydown", (ev) => {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(ev.target.tagName)) return;
    const map = { "+": "zoomTele", "=": "zoomTele", "-": "zoomWide", _: "zoomWide",
                  ArrowRight: "focusFar", ArrowLeft: "focusNear" };
    const target = map[ev.key];
    if (target) {
      ev.preventDefault();
      if (!keyHeld[target]) { keyHeld[target] = true; $(target)._holdStart(new Event("x")); }
      return;
    }
    if (ev.key.toLowerCase() === "a") $("chkAf").click();
    else if (ev.key.toLowerCase() === "f") visca("focus_one_push");
    else if (ev.key.toLowerCase() === "s") $("btnSnap").click();
    else if (ev.key.toLowerCase() === "r") $("btnRec").click();
    else if (/^[0-9]$/.test(ev.key)) visca("preset_recall", { slot: Number(ev.key) });
  });
  document.addEventListener("keyup", (ev) => {
    const map = { "+": "zoomTele", "=": "zoomTele", "-": "zoomWide", _: "zoomWide",
                  ArrowRight: "focusFar", ArrowLeft: "focusNear" };
    const target = map[ev.key];
    if (target && keyHeld[target]) { keyHeld[target] = false; $(target)._holdStop(); }
  });

  updateAeEnabling();
  rescan().catch(() => {});
  loadNetwork().catch(() => {});
  refresh();
  setInterval(refresh, 1000);
}

async function refresh() {
  try {
    applyState(await api("/api/status"));
  } catch (err) {
    $("dotVideo").className = "dot";
    $("dotVisca").className = "dot";
  }
}

document.addEventListener("DOMContentLoaded", init);
