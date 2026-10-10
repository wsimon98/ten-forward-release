/* Ten Forward front end. Vanilla JS, no build step. Talks to server.py's /api. */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const store = {
    get: (k, d) => { try { const v = localStorage.getItem("tf:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem("tf:" + k, JSON.stringify(v)); } catch {} },
  };
  const state = {
    ui: store.get("ui", "easy"), mode: store.get("mode", "create"), voiceId: store.get("voiceId", null), voices: [], loras: [], loraPlan: store.get("loraPlan", {}),
    presets: [], stations: [], songs: [], recent: [], jobs: [], selected: null, cover: null, instrumental: false,
    playing: null, queueMode: "library", radioStation: store.get("radioStation", null), waitingRadio: false, playedMarked: false, jobsTimer: null, statusTimer: null, radioTimer: null,
    recorder: null, recChunks: [], recBlob: null, recStart: 0, recTimer: null, uploadVoice: null, seenJobs: new Set(), libLiked: false,
    themes: [], nextUpTimer: null, queueTimer: null, reconnects: 0, uiBuild: null, reloadPending: false,
    history: [], sleepAt: null, sleepTick: null, stopAfter: false, impTimer: null, lyricFolder: store.get("lyricFolder", "my-songs"), lfTimer: null, lfLoaded: false,
    user: null, features: {}, settings: null, booted: false,
    primeSaid: store.get("primeSaid", []), primeGone: store.get("primeGone", []),
    upNext: store.get("upNext", []), fromQueue: false,
  };

  // ---------------------------------------------------------------- helpers
  async function api(path, opts = {}) {
    const o = { headers: {}, ...opts };
    if (o.body && !(o.body instanceof FormData)) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(o.body); }
    const r = await fetch(path, o);
    if (r.status === 401 && !path.startsWith("/api/auth/")) { showLogin(); throw new Error("Sign in first"); }
    if (!r.ok) {
      let msg = r.statusText;
      try { const j = await r.json(); msg = j.detail || j.error || JSON.stringify(j); } catch {}
      throw new Error(msg);
    }
    return r.json();
  }
  function toast(msg, err = false) {
    const t = document.createElement("div");
    t.className = "toast" + (err ? " err" : "");
    t.textContent = msg;
    $("#toasts").appendChild(t);
    setTimeout(() => t.remove(), err ? 6000 : 3200);
  }
  const fmtTime = (s) => { s = Math.max(0, Math.floor(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function stardate() {
    const d = new Date(); const start = new Date(d.getFullYear(), 0, 1);
    const doy = (d - start) / 86400000;
    $("#stardate").textContent = `SD ${((d.getFullYear() - 2000) * 1000 + doy * 1000 / 365).toFixed(1)}`;
  }

  // ---------------------------------------------------------------- navigation
  function switchMode(m) {
    if (m === "radio") m = "dial";  // 0.8: the Radio tab became Dial + Channels; phones still have "radio" stored
    if (m === "systems") m = "settings";  // 0.9: Systems grew up into Settings
    if (!$("#mode-" + m)) m = "create";
    state.mode = m; store.set("mode", m);
    $$(".mode").forEach((s) => s.classList.toggle("on", s.id === "mode-" + m));
    $$(".nav").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
    if (m === "library") { loadLibrary(); if (!state.lfLoaded) { state.lfLoaded = true; loadLyricFolders(); } }
    if (m === "dial") { loadStations(); loadHistory(); }
    if (m === "channels") loadStations();
    if (m === "settings") loadSettings();
    if (m === "queue") loadQueue(); else clearTimeout(state.queueTimer);
  }
  $$(".nav").forEach((b) => b.addEventListener("click", () => switchMode(b.dataset.mode)));
  $("#chip-queue").addEventListener("click", () => switchMode("queue"));
  function setUi(ui) {
    state.ui = ui; store.set("ui", ui); document.body.dataset.ui = ui;
    $$("#seg-mode .seg-btn").forEach((b) => b.classList.toggle("on", b.dataset.ui === ui));
  }
  $$("#seg-mode .seg-btn").forEach((b) => b.addEventListener("click", () => setUi(b.dataset.ui)));

  // ---------------------------------------------------------------- create: lyrics + style
  $("#btn-write").addEventListener("click", () => $("#writer").classList.toggle("hidden"));
  $("#btn-write-close").addEventListener("click", () => $("#writer").classList.add("hidden"));
  $("#btn-write-go").addEventListener("click", async () => {
    const b = $("#btn-write-go"); b.disabled = true; b.textContent = "Writing…";
    try {
      const r = await api("/api/lyrics/write", { method: "POST", body: { topic: $("#write-topic").value, mood: $("#write-mood").value, style: $("#style").value, length: $("#write-length").value, instrumental: state.instrumental } });
      $("#lyrics").value = r.lyrics; toast("Lyrics written");
    } catch (e) { toast("Lyric writer: " + e.message, true); }
    b.disabled = false; b.textContent = "Write";
  });
  $("#btn-improve").addEventListener("click", async () => {
    const lyrics = $("#lyrics").value.trim();
    if (!lyrics) return toast("Nothing to improve yet", true);
    const instruction = prompt("How should the lyrics change?", "tighter lines, stronger chorus, keep the story");
    if (instruction === null) return;
    try { const r = await api("/api/lyrics/improve", { method: "POST", body: { lyrics, instruction } }); $("#lyrics").value = r.lyrics; toast("Rewritten"); } catch (e) { toast(e.message, true); }
  });
  $("#btn-clear-lyrics").addEventListener("click", () => { $("#lyrics").value = ""; });
  $("#btn-cover").addEventListener("click", () => {
    const box = $("#coverer");
    box.classList.toggle("hidden");
    if (!box.classList.contains("hidden")) {
      const sel = $("#cover-station");
      sel.innerHTML = `<option value="">just put it in the box</option>` +
        (state.stations || []).filter((x) => !isFav(x) && !x.instrumental).map((x) => `<option value="${esc(x.id)}">sing it on ${esc(x.name)}</option>`).join("");
    }
  });
  $("#btn-cover-close").addEventListener("click", () => $("#coverer").classList.add("hidden"));
  $("#cover-q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#btn-cover-go").click(); });
  $("#btn-cover-go").addEventListener("click", async () => {
    const q = $("#cover-q").value.trim(), out = $("#cover-out");
    if (!q) return;
    out.innerHTML = `<div class="hint">Looking…</div>`;
    try {
      const rows = await api(`/api/lyrics/covers/search?q=${encodeURIComponent(q)}`);
      if (!rows.length) { out.innerHTML = `<div class="hint">Nothing with words came back for that. Try just the artist, or the artist and one word from the title.</div>`; return; }
      out.innerHTML = rows.slice(0, 30).map((r) => `<div class="item"><div class="grow"><b>${esc(r.track || "")}</b>
        <small>${esc(r.artist || "")}${r.album ? " · " + esc(r.album) : ""}${r.duration ? " · " + Math.round(r.duration / 60) + " min" : ""}</small></div>
        <button class="mini" data-cover="${r.id}">Use it</button></div>`).join("");
      $$("#cover-out [data-cover]").forEach((b) => b.addEventListener("click", async () => {
        const id = b.dataset.cover, station = $("#cover-station").value, interp = $("#cover-interp").checked;
        b.disabled = true; b.textContent = interp ? "Writing…" : "…";
        // new verses take the writer a minute or two; say so where the eye already is
        const note = document.createElement("div");
        if (interp) {
          const where = station ? `under ${esc($("#cover-station").selectedOptions[0].textContent.replace(/^sing it on /, ""))}'s rules` : "in the writer's own way";
          note.className = "hint"; note.innerHTML = `Writing new verses around the real chorus, ${where}. This takes a minute or two.`;
          b.closest(".item").after(note);
        }
        try {
          if (station && interp) {
            // write first, show it, and only Begin queues it
            const r = await api(`/api/lyrics/covers/${encodeURIComponent(id)}?interpolate=1&station=${encodeURIComponent(station)}`);
            coverDraft(b.closest(".item"), id, station, r);
          } else if (station) {
            const r = await api(`/api/radio/${encodeURIComponent(station)}/cover`, { method: "POST", body: { id: Number(id), interpolate: false } });
            toast(`${r.title} is being made`); loadQueue();
          } else {
            const q = interp ? `?interpolate=1&style=${encodeURIComponent($("#style").value.trim())}` : "";
            const r = await api(`/api/lyrics/covers/${encodeURIComponent(id)}${q}`);
            $("#lyrics").value = r.lyrics;
            toast(interp ? `${r.title}: the real chorus, new verses` : `${r.title} is in the box`); $("#coverer").classList.add("hidden");
          }
        } catch (e) { toast(e.message, true); }
        note.remove();
        b.disabled = false; b.textContent = "Use it";
      }));
    } catch (e) { out.innerHTML = `<div class="hint">${esc(e.message || "that did not work")}</div>`; }
  });
  // An interpolation on a channel: the words it is going to sing, to read (and change) before Begin queues them.
  function coverDraft(item, id, station, plan) {
    item.parentNode.querySelectorAll(".cover-draft").forEach((d) => d.remove());
    const chan = (state.stations || []).find((x) => x.id === station);
    const box = document.createElement("div");
    box.className = "writer cover-draft";
    box.innerHTML = `<div class="hint">These are the words it will sing on <b>${esc(chan ? chan.name : station)}</b>: the real chorus, and new verses
      written under the channel's rules. Change anything you like, then Begin.</div>
      <textarea rows="16"></textarea>
      <div class="row"><button class="mini" data-act="begin">Begin</button><button class="mini ghost" data-act="again">Write again</button>
      <button class="mini ghost" data-act="cancel">Cancel</button></div>`;
    const ta = box.querySelector("textarea");
    ta.value = plan.lyrics;
    item.after(box);
    const btns = box.querySelectorAll("button");
    const busy = (on) => btns.forEach((x) => (x.disabled = on));
    box.querySelector('[data-act="cancel"]').addEventListener("click", () => box.remove());
    box.querySelector('[data-act="again"]').addEventListener("click", async (e) => {
      busy(true); e.target.textContent = "Writing…";
      try {
        plan = await api(`/api/lyrics/covers/${encodeURIComponent(id)}?interpolate=1&station=${encodeURIComponent(station)}&style=${encodeURIComponent(plan.style || "")}`);
        ta.value = plan.lyrics;
      } catch (err) { toast(err.message, true); }
      busy(false); e.target.textContent = "Write again";
    });
    box.querySelector('[data-act="begin"]').addEventListener("click", async (e) => {
      busy(true); e.target.textContent = "…";
      try {
        const r = await api(`/api/radio/${encodeURIComponent(station)}/cover`, { method: "POST", body: { id: Number(id), interpolate: true, lyrics: ta.value, style: plan.style } });
        toast(`${r.title} is being made`); loadQueue(); box.remove();
      } catch (err) { toast(err.message, true); busy(false); e.target.textContent = "Begin"; }
    });
    ta.focus();
  }
  $("#btn-instrumental").addEventListener("click", () => {
    state.instrumental = !state.instrumental;
    $("#btn-instrumental").classList.toggle("on", state.instrumental);
    $("#lyrics").placeholder = state.instrumental ? "Instrumental: leave empty, or give section tags like [intro] [verse] [chorus] [outro] one per line." : "[Verse 1]\nWrite your lines here, or leave empty and hit Write for me.";
    const inst = state.loras.find((l) => l.kind === "ar" && /inst/i.test(l.file));
    if (inst) { if (state.instrumental) state.loraPlan[inst.file] = inst.default_scale || 1; else delete state.loraPlan[inst.file]; renderLoras(); }
  });
  $("#btn-style-suggest").addEventListener("click", async () => {
    try {
      const r = await api("/api/style/suggest", { method: "POST", body: { description: $("#write-topic").value || $("#style").value, lyrics: $("#lyrics").value } });
      if (r.style) { $("#style").value = r.style; toast("Style suggested"); }
    } catch (e) { toast(e.message, true); }
  });
  function renderPresets() {
    $("#style-presets").innerHTML = state.presets.map((p) => `<button class="chip-btn" data-style="${esc(p.style)}">${esc(p.name)}</button>`).join("");
    $$("#style-presets .chip-btn").forEach((b) => b.addEventListener("click", () => { $("#style").value = b.dataset.style; }));
  }

  // ---------------------------------------------------------------- create: studio controls
  const ranges = [["duration", "out-duration", (v) => v], ["steps", "out-steps", (v) => v], ["cfg", "out-cfg", (v) => Number(v).toFixed(2)], ["temperature", "out-temp", (v) => Number(v).toFixed(2)], ["top_k", "out-topk", (v) => v], ["top_p", "out-topp", (v) => Number(v).toFixed(2)]];
  ranges.forEach(([id, out, f]) => { const el = $("#" + id); const o = $("#" + out); const upd = () => (o.textContent = f(el.value)); el.addEventListener("input", upd); upd(); });
  let planMode = 0;
  $$("#mode-chips .chip-btn").forEach((b) => b.addEventListener("click", () => { planMode = Number(b.dataset.v); $$("#mode-chips .chip-btn").forEach((x) => x.classList.toggle("on", x === b)); }));

  async function uploadFile(file) {
    const fd = new FormData(); fd.append("file", file, file.name || "upload.wav");
    return api("/api/upload", { method: "POST", body: fd });
  }
  // A cover is as long as the original: the cap follows the source, with headroom (the server holds the same floor).
  function setDurationFor(seconds) {
    const s = Number(seconds) || 0; if (!s) return;
    const el = $("#duration"); el.value = Math.min(600, Math.max(30, Math.round((s * 1.35 + 10) / 5) * 5)); el.dispatchEvent(new Event("input"));
  }
  $("#cover-file").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    $("#cover-info").textContent = "Uploading " + f.name + "…";
    try {
      const info = await uploadFile(f);
      state.cover = { path: info.path, name: f.name, duration: info.duration };
      setDurationFor(info.duration);
      $("#cover-info").textContent = `Source: ${f.name} (${fmtTime(info.duration)}). Melody only planning is selected for you; paste the lyrics or transcribe them.`;
      $("#btn-cover-transcribe").disabled = false; $("#btn-cover-clear").disabled = false;
      planMode = 1; $$("#mode-chips .chip-btn").forEach((x) => x.classList.toggle("on", x.dataset.v === "1"));
    } catch (err) { $("#cover-info").textContent = "Upload failed: " + err.message; }
  });
  $("#btn-cover-clear").addEventListener("click", () => { state.cover = null; $("#cover-file").value = ""; $("#cover-info").textContent = "No source song."; $("#btn-cover-transcribe").disabled = true; $("#btn-cover-clear").disabled = true; });
  $("#btn-cover-transcribe").addEventListener("click", async () => {
    if (!state.cover || !state.cover.path) return toast("Covers of library songs already have their lyrics", true);
    const b = $("#btn-cover-transcribe"); b.disabled = true; b.textContent = "Listening…";
    try {
      const job = await api("/api/transcribe", { method: "POST", body: { path: state.cover.path } });
      const done = await waitJob(job.id);
      const lyr = done.params && done.params.result && done.params.result.lyrics;
      if (lyr) { $("#lyrics").value = lyr; toast("Lyrics transcribed. Check them against the song."); } else toast("No words found", true);
    } catch (e) { toast(e.message, true); }
    b.disabled = false; b.textContent = "Transcribe its lyrics";
  });
  async function waitJob(id, timeoutMs = 600000) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      const j = await api("/api/jobs/" + id);
      if (["done", "failed", "cancelled"].includes(j.status)) { if (j.status !== "done") throw new Error(j.message || j.status); return j; }
      await new Promise((r) => setTimeout(r, 2500));
    }
    throw new Error("timed out");
  }
  $("#abc-file").addEventListener("change", (e) => { const f = e.target.files[0]; if (!f) return; f.text().then((t) => ($("#abc").value = t)); });
  $("#btn-abc-clear").addEventListener("click", () => ($("#abc").value = ""));

  // ---------------------------------------------------------------- create: new words on a real tune (1.6)
  // An arrangement is a saved lead sheet (read once from a recording, a library song or a sheet file) with the
  // words it was sung with. The writer gets the melody's line and syllable counts; Sing it ships the sheet with the job.
  state.arr = null; state.arrs = [];
  async function loadArrangements(selectId) {
    try { state.arrs = await api("/api/arrangements"); } catch { return; }
    const sel = $("#arr-list");
    sel.innerHTML = `<option value="">Saved sheets…</option>` + state.arrs.map((a) => `<option value="${esc(a.id)}">${esc(a.name)} · ${fmtTime(a.duration_s)}${a.on_radio ? " · on the radio" : ""}</option>`).join("");
    if (selectId) sel.value = selectId;
  }
  function arrStations() {
    const sel = $("#arr-station"); if (!sel) return;
    const keep = sel.value;
    sel.innerHTML = `<option value="">the writer's own rules</option>` + (state.stations || []).filter((x) => !isFav(x) && !x.instrumental).map((x) => `<option value="${esc(x.id)}">under ${esc(x.name)}'s rules</option>`).join("");
    sel.value = keep;
  }
  function arrButtons(on) { ["#btn-arr-write", "#btn-arr-fit", "#btn-arr-sing", "#btn-arr-delete"].forEach((s) => ($(s).disabled = !on)); }
  function showArr(a) {
    state.arr = a;
    const info = a.info || {}; const rows = info.budget || [];
    $("#arr-info").textContent = `${a.name}: ${info.key || "?"}, ${info.meter || ""}, ${Math.round(info.bpm || 0)} BPM, ${info.bars || 0} bars, ${fmtTime(a.duration_s)}. Line and syllable counts from ${info.budget_source === "words" ? "the words it was sung with" : "the melody's notes"}.`;
    $("#arr-sections").innerHTML = rows.map((b) => `<b>${esc(b.tag)}</b><span>${b.lines ? b.lines + " line" + (b.lines === 1 ? "" : "s") + ": " + b.syllables.join(" / ") + " syllables" : "no singing"}</span>`).join("");
    $("#arr-sections").classList.toggle("hidden", !rows.length);
    $("#arr-radio").checked = !!a.on_radio;
    arrButtons(true);
    $("#btn-arr-original").disabled = !(a.words || "").trim();
    $("#arr-fit").textContent = "";
  }
  function showFit(fit) {
    if (!fit) return;
    const bad = (fit.lines || []).filter((l) => l.diff !== null && l.diff !== undefined && Math.abs(l.diff) > 1);
    $("#arr-fit").textContent = fit.ok ? "Every line sits on the melody." :
      `${bad.length} line${bad.length === 1 ? "" : "s"} off the melody${fit.missing ? ", " + fit.missing + " missing" : ""}${fit.extra ? ", " + fit.extra + " extra" : ""}` +
      (bad.length ? ": " + bad.slice(0, 6).map((l) => `[${l.section}] line ${l.n} has ${l.has}, wants ${l.want}`).join("; ") : "") + ". One off either way is fine.";
  }
  async function arrFromJob(job) {
    const done = await waitJob(job.id || job, 1200000);
    const aid = done.params && done.params.result && done.params.result.arrangement;
    if (!aid) throw new Error("the sheet did not come back");
    return api("/api/arrangements/" + aid);
  }
  function arrLoaded(a, how) { loadArrangements(a.id); showArr(a); if (a.words && a.words.trim()) $("#lyrics").value = a.words; setDurationFor(a.duration_s); toast(`${how}: ${a.name}`); }
  $("#arr-file").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    $("#arr-info").textContent = "Uploading " + f.name + "…";
    try {
      const info = await uploadFile(f);
      const r = await api("/api/arrangements", { method: "POST", body: { file: info.path, name: f.name.replace(/\.[^.]+$/, ""), instrumental: state.instrumental } });
      if (r.job) {
        $("#arr-info").textContent = "Reading the sheet: SheetSage2 reads the melody, then Whisper hears the words. A minute or two."; loadJobs();
        arrLoaded(await arrFromJob(r.job), "Sheet read");
      } else arrLoaded(r.arrangement, r.existing ? "Already had that sheet" : "Sheet loaded");
    } catch (err) { $("#arr-info").textContent = "Could not read that: " + err.message; }
    e.target.value = "";
  });
  // Uploads live in Studio; a fresh browser opens Create in Easy, so this gets there from either mode and opens the picker.
  $("#btn-upload").addEventListener("click", () => {
    if (state.ui !== "studio") { setUi("studio"); toast("Studio: the tune is read once, then sing it again or put new words on it"); }
    $("#rewrite-block").scrollIntoView({ behavior: "smooth", block: "start" });
    $("#arr-file").click();
  });
  // Swap the singer: a real recording in, its music kept, one of our voices singing it (an import and a re-voice in one go).
  let swapFile = null;
  function renderSwapVoices() {
    const sel = $("#swap-voice"); if (!sel) return;
    const cur = sel.value;
    const ready = (state.voices || []).filter((v) => v.ready);
    sel.innerHTML = `<option value="">Which voice…</option>` + ready.map((v) => `<option value="${v.id}">${esc(v.name)}${v.model ? " (trained)" : v.source === "stock" ? " (AI)" : ""}${v.pitch ? " · " + esc(v.pitch.low_note + "–" + v.pitch.high_note) : ""}</option>`).join("");
    if (cur && ready.some((v) => v.id === cur)) sel.value = cur;
    else if (ready.length === 1) sel.value = ready[0].id;
    swapReady();
  }
  $("#btn-swap").addEventListener("click", () => {
    const box = $("#swapper"); box.classList.toggle("hidden");
    if (!box.classList.contains("hidden")) { $("#writer").classList.add("hidden"); $("#coverer").classList.add("hidden"); renderSwapVoices(); }
  });
  $("#btn-swap-close").addEventListener("click", () => $("#swapper").classList.add("hidden"));
  $("#btn-ai-singers").addEventListener("click", async () => {
    const b = $("#btn-ai-singers"); b.disabled = true;
    try {
      const r = await api("/api/voices/stock", { method: "POST", body: {} });
      toast(r.voices.length ? `Making ${r.voices.map((v) => v.name).join(" and ")}: the vocal is lifted from a song of the radio's own.` : `The AI singers are already here (${(r.have || []).join(", ") || "none found to lift"}).`);
      loadJobs(); loadVoices();
    } catch (e) { toast(e.message, true); }
    b.disabled = false;
  });
  function swapReady() { $("#btn-swap-go").disabled = !(swapFile && $("#swap-voice").value); }
  $("#swap-file").addEventListener("change", (e) => { swapFile = e.target.files[0] || null; $("#swap-info").textContent = swapFile ? `${swapFile.name}: pick the voice and Swap it.` : "No song picked."; swapReady(); });
  $("#swap-voice").addEventListener("change", swapReady);
  $("#btn-swap-go").addEventListener("click", async () => {
    if (!swapFile) return;
    const b = $("#btn-swap-go"); b.disabled = true; b.textContent = "Uploading…";
    const vid = $("#swap-voice").value;
    const fd = new FormData(); fd.append("file", swapFile, swapFile.name); fd.append("voice_id", vid); fd.append("blend", $("#swap-blend").value); fd.append("pitch", $("#swap-pitch").value);
    try {
      const r = await api("/api/swap", { method: "POST", body: fd });
      const vname = ((state.voices || []).find((v) => v.id === vid) || {}).name || "that voice";
      $("#swap-info").textContent = `${r.song.title} is in My Songs${r.song.duplicate ? " (it was already there)" : ""}; ${vname} is singing it now. Watch the Queue: the new take lands next to it, tagged with the octave it moved.`;
      toast("Swapping the singer"); loadJobs(); swapFile = null; $("#swap-file").value = "";
    } catch (err) { toast(err.message, true); $("#swap-info").textContent = "Could not start that: " + err.message; }
    b.textContent = "Swap it"; swapReady();
  });
  $("#arr-list").addEventListener("change", async () => {
    const id = $("#arr-list").value; if (!id) return;
    try { arrLoaded(await api("/api/arrangements/" + id), "Sheet loaded"); } catch (e) { toast(e.message, true); }
  });
  $("#btn-arr-delete").addEventListener("click", async () => {
    if (!state.arr || !confirm(`Forget the sheet "${state.arr.name}"?`)) return;
    try { await api("/api/arrangements/" + state.arr.id, { method: "DELETE" }); } catch (e) { return toast(e.message, true); }
    state.arr = null; $("#arr-sections").classList.add("hidden"); $("#arr-info").textContent = "No sheet loaded."; arrButtons(false); $("#btn-arr-original").disabled = true; loadArrangements();
  });
  $("#btn-arr-original").addEventListener("click", () => { if (state.arr) $("#lyrics").value = state.arr.words || ""; });
  $("#arr-radio").addEventListener("change", async () => {
    if (!state.arr) return;
    try { state.arr = await api("/api/arrangements/" + state.arr.id, { method: "POST", body: { on_radio: $("#arr-radio").checked } }); toast($("#arr-radio").checked ? "Channels with a rewrite share may sing this tune" : "Taken off the radio"); loadArrangements(state.arr.id); }
    catch (e) { toast(e.message, true); }
  });
  $("#btn-arr-write").addEventListener("click", async () => {
    if (!state.arr) return;
    const b = $("#btn-arr-write"); b.disabled = true; b.textContent = "Writing…";
    const who = $("#arr-station").value ? `under ${$("#arr-station").selectedOptions[0].textContent.replace(/^under /, "")}` : "in the writer's own way";
    $("#arr-fit").textContent = `Writing new words to this tune ${who}, to its line and syllable counts. A minute or two.`;
    try {
      const r = await api(`/api/arrangements/${state.arr.id}/words`, { method: "POST", body: { station: $("#arr-station").value || null, style: $("#style").value.trim(), topic: $("#arr-topic").value.trim() } });
      $("#lyrics").value = r.lyrics; if (r.style && !$("#style").value.trim()) $("#style").value = r.style;
      showFit(r.fit); toast("New words are in the box. Read them, change what you like, then Sing it.");
    } catch (e) { toast(e.message, true); $("#arr-fit").textContent = ""; }
    b.disabled = false; b.textContent = "Write new words";
  });
  $("#btn-arr-fit").addEventListener("click", async () => {
    if (!state.arr) return;
    try { showFit(await api(`/api/arrangements/${state.arr.id}/fit`, { method: "POST", body: { lyrics: $("#lyrics").value } })); } catch (e) { toast(e.message, true); }
  });
  $("#btn-arr-sing").addEventListener("click", async () => {
    if (!state.arr) return;
    const b = $("#btn-arr-sing"); b.disabled = true;
    try {
      const body = { lyrics: $("#lyrics").value, style: $("#style").value.trim(), station: $("#arr-station").value || null, mode: planMode === 2 ? 0 : planMode,
        voice_id: state.voiceId || null, instrumental: state.instrumental, seed: Number($("#seed").value), steps: Number($("#steps").value), count: Math.max(1, Math.min(4, Number($("#count").value) || 1)) };
      if (Object.keys(state.loraPlan).length) body.loras = state.loraPlan;
      const r = await api(`/api/arrangements/${state.arr.id}/sing`, { method: "POST", body });
      toast(`${r.title} is being made${r.station ? " on " + r.station : ""}`); showFit(r.fit); loadJobs(); loadArrangements(state.arr.id);
    } catch (e) { toast(e.message, true); }
    b.disabled = false;
  });

  function renderLoras() {
    const el = $("#lora-list");
    if (!state.loras.length) { el.innerHTML = '<div class="hint">No LoRA files in the loras folder</div>'; return; }
    el.innerHTML = state.loras.map((l) => {
      const on = l.file in state.loraPlan; const scale = on ? state.loraPlan[l.file] : (l.default_scale || 1);
      return `<div class="lora" data-file="${esc(l.file)}">
        <input type="checkbox" class="lora-on" ${on ? "checked" : ""}>
        <div><div class="name">${esc(l.name)}</div><div class="kind">${esc(l.kind)} · ${l.size_mb} MB${l.trigger ? " · trigger " + esc(l.trigger) : ""}</div></div>
        <div class="row"><input type="range" class="lora-scale" min="0" max="1.5" step="0.05" value="${scale}"><span class="scale">${Number(scale).toFixed(2)}</span></div>
        <div class="blurb">${esc(l.blurb || "")}</div></div>`;
    }).join("");
    $$("#lora-list .lora").forEach((row) => {
      const file = row.dataset.file; const cb = $(".lora-on", row); const rg = $(".lora-scale", row); const sc = $(".scale", row);
      cb.addEventListener("change", () => { if (cb.checked) state.loraPlan[file] = Number(rg.value); else delete state.loraPlan[file]; store.set("loraPlan", state.loraPlan); });
      rg.addEventListener("input", () => { sc.textContent = Number(rg.value).toFixed(2); if (cb.checked) { state.loraPlan[file] = Number(rg.value); store.set("loraPlan", state.loraPlan); } });
    });
  }

  // ---------------------------------------------------------------- create: GO
  $("#btn-go").addEventListener("click", async () => {
    const lyrics = $("#lyrics").value.trim(); const style = $("#style").value.trim();
    const studio = state.ui === "studio";
    if (!lyrics && !style && !state.cover) return toast("Give me a style, some lyrics, or a source song", true);
    if ("Notification" in window && Notification.permission === "default") { try { Notification.requestPermission().catch(() => {}); } catch {} }
    const body = {
      lyrics, style, voice_id: state.voiceId || null, instrumental: state.instrumental, source: state.ui,
      mode: studio ? planMode : (state.cover ? 1 : 0),
      duration: studio ? Number($("#duration").value) : 240,  // a cap, not a target: YuE2 stops on its own end token; 150 chopped 3 minute songs
      steps: studio ? Number($("#steps").value) : 32,
      seed: studio ? Number($("#seed").value) : -1,
      cfg: studio ? Number($("#cfg").value) : 1.0,
      temperature: studio ? Number($("#temperature").value) : 1.0,
      top_k: studio ? Number($("#top_k").value) : 100,
      top_p: studio ? Number($("#top_p").value) : 0.95,
      count: studio ? Math.max(1, Math.min(4, Number($("#count").value) || 1)) : 1,
      write_lyrics: !lyrics && !state.instrumental && !state.cover,
      topic: $("#write-topic").value, mood: $("#write-mood").value,
    };
    if (studio && $("#abc").value.trim() && !state.cover) body.abc_text = $("#abc").value;
    if (state.cover) { if (state.cover.path) body.source_audio = state.cover.path; if (state.cover.songId) body.source_song_id = state.cover.songId; body.transcribe_lyrics = !lyrics; }
    if (studio && Object.keys(state.loraPlan).length) body.loras = state.loraPlan;
    const b = $("#btn-go"); b.disabled = true; $("#go-hint").textContent = body.write_lyrics ? "Writing lyrics first…" : "Queueing…";
    try {
      const r = await api("/api/songs", { method: "POST", body });
      if (r.lyrics && !lyrics) $("#lyrics").value = r.lyrics;
      if (r.style && !style) $("#style").value = r.style;
      toast(`Queued ${r.jobs.length} song${r.jobs.length > 1 ? "s" : ""}`);
      $("#go-hint").textContent = "Watch the Jobs panel. A 3 minute song takes a few minutes.";
      loadJobs();
    } catch (e) { toast(e.message, true); $("#go-hint").textContent = ""; }
    b.disabled = false;
  });

  // ---------------------------------------------------------------- jobs
  async function loadJobs() {
    try { state.jobs = await api("/api/jobs?limit=30"); } catch { return; }
    renderJobs();
    const active = state.jobs.some((j) => j.status === "running" || j.status === "queued");
    for (const j of state.jobs) {
      if (j.status === "done" && j.song_id && !state.seenJobs.has(j.id) && state.jobs.indexOf(j) < 5 && Date.now() / 1000 - (j.finished || 0) < 120) {
        state.seenJobs.add(j.id); if (((j.params || {}).requested_by || "studio") !== "auto") notifyReady(j); loadRecent();
        if (state.queueMode === "radio" && state.waitingRadio) radioNext();
      }
    }
    clearTimeout(state.jobsTimer);
    state.jobsTimer = setTimeout(loadJobs, active ? 2500 : 9000);
  }
  function renderJobs() {
    const el = $("#jobs");
    if (!state.jobs.length) { el.innerHTML = '<div class="hint">No jobs yet.</div>'; return; }
    el.innerHTML = state.jobs.slice(0, 12).map((j) => {
      const p = j.params || {}; const title = p.title || (p.lyrics ? p.lyrics.split("\n").find((l) => l.trim() && !l.trim().startsWith("[")) : "") || j.type;
      const pct = Math.round((j.progress || 0) * 100);
      const cancel = j.status === "queued" || j.status === "running" ? `<button class="mini ghost" data-cancel="${j.id}">Cancel</button>` : "";
      return `<div class="job ${j.status}"><div class="jt"><span>${esc(j.type === "song" || j.type === "revoice" ? title : j.type + ": " + title)}</span><span>${j.status === "running" ? pct + "%" : esc(j.status)}</span></div>
        <div class="jm">${esc(j.message || "")}${p.station_id ? " · " + esc(p.station_id) : ""}</div>${j.status === "running" ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}<div class="row" style="margin-top:6px">${cancel}</div></div>`;
    }).join("");
    $$("#jobs [data-cancel]").forEach((b) => b.addEventListener("click", async () => { try { await api("/api/jobs/" + b.dataset.cancel + "/cancel", { method: "POST" }); loadJobs(); } catch (e) { toast(e.message, true); } }));
  }
  $("#btn-jobs-clear").addEventListener("click", async () => { await api("/api/jobs/clear", { method: "POST" }); loadJobs(); });

  // ---------------------------------------------------------------- songs lists
  function keepLabel(s) {
    if (s.liked) return s.saved_path ? "♥ saved to " + s.saved_path : "♥ kept";
    if (!s.expires_at) return "";
    const d = (s.expires_at * 1000 - Date.now()) / 86400000;
    if (d < 0) return "expiring";
    if (d < 1) return "gone in " + Math.max(1, Math.round(d * 24)) + " h unless hearted";
    return "gone in " + Math.ceil(d) + " d unless hearted";
  }
  function heartToast(r) {
    if (r.liked) toast(r.saved_path ? "Saved: " + r.saved_path : "Hearted, kept forever");
    else toast("Un-hearted: copy removed from the songs folder" + (r.expires_at ? ", " + keepLabel(r) : ""));
  }
  /**
   * Somebody else's words. A "cover" is their lyrics sung in this channel's own sound; an
   * "interpolation" only borrows a piece. Either way it is not an original, and you should
   * never have to wonder which you are hearing - so it is labelled in every list and on the
   * player itself.
   */
  function coverOf(s) { const c = s && s.cover_of; return c && (c.track || c.artist) ? c : null; }
  function coverTag(s) {
    const c = coverOf(s); if (!c) return "";
    const interp = c.kind === "interpolation";
    const who = [c.track, c.artist].filter(Boolean).join(" · ");
    return `<span class="tag ${interp ? "interp" : "cov"}" title="${esc(interp ? "Borrows a piece of" : "Somebody else's words:")} ${esc(who)}">${interp ? "INTERPOLATION" : "COVER"}</span>`;
  }
  function coverLine(s) {
    const c = coverOf(s); if (!c) return "";
    const who = c.artist ? `${c.track || "a song"} — ${c.artist}` : (c.track || "another song");
    return `${c.kind === "interpolation" ? "Interpolates" : "Cover of"} ${who}`;
  }

  function songRow(s, cls = "") {
    const tags = [coverTag(s), s.station_id ? `<span class="tag st">${esc(s.station_id)}</span>` : "", s.voice_id ? `<span class="tag v">voice</span>` : "", s.liked ? `<span class="tag lk">♥</span>` : "", s.source === "revoice" ? `<span class="tag">revoice</span>` : "", (s.tags || []).includes("clipped") ? `<span class="tag exp" title="YuE2 hit the duration cap before its ending; the last seconds are faded">clipped</span>` : "", !s.liked && s.expires_at ? `<span class="tag exp">${esc(keepLabel(s))}</span>` : ""].join("");
    return `<div class="song ${cls} ${state.playing && state.playing.id === s.id ? "playing" : ""}" data-id="${s.id}">
      <button class="play" data-play="${s.id}">${state.playing && state.playing.id === s.id && !$("#audio").paused ? "❚❚" : "▶"}</button>
      <div><div class="t">${esc(s.title)}</div><div class="s">${esc(s.style)}</div><div class="tags">${tags}</div></div>
      <div class="dur">${fmtTime(s.duration_s)}</div></div>`;
  }
  function bindSongList(el, list, onSelect) {
    $$(".song", el).forEach((row) => {
      const s = list.find((x) => x.id === row.dataset.id);
      // right-click (or press and hold on a phone) opens the song menu; a hold must not also open the song
      let held = false, t = null, sx = 0, sy = 0;
      row.addEventListener("contextmenu", (e) => { e.preventDefault(); clearTimeout(t); row.classList.remove("hold"); held = e.pointerType === "touch" || held; openSongMenu(e.clientX, e.clientY, s, list); });
      row.addEventListener("pointerdown", (e) => {
        if (e.pointerType === "mouse" || e.target.dataset.play) return;
        held = false; sx = e.clientX; sy = e.clientY; row.classList.add("hold");
        t = setTimeout(() => { held = true; row.classList.remove("hold"); buzz(); openSongMenu(sx, sy, s, list); }, 500);
      });
      const end = () => { clearTimeout(t); row.classList.remove("hold"); };
      row.addEventListener("pointermove", (e) => { if (t && (Math.abs(e.clientX - sx) > 10 || Math.abs(e.clientY - sy) > 10)) end(); });
      ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => row.addEventListener(ev, end));
      row.addEventListener("click", (e) => { if (held) { held = false; e.preventDefault(); e.stopImmediatePropagation(); } }, true);
      row.addEventListener("click", (e) => { if (e.target.dataset.play) return; onSelect && onSelect(s); });
      $("[data-play]", row).addEventListener("click", (e) => { e.stopPropagation(); if (state.playing && state.playing.id === s.id) togglePlay(); else { state.queueMode = "library"; state.songs = list; playSong(s); } });
    });
  }
  async function loadRecent() {
    try { state.recent = await api("/api/songs?limit=8"); } catch { return; }
    $("#recent").innerHTML = state.recent.length ? state.recent.map((s) => songRow(s)).join("") : '<div class="hint">Nothing yet. Make something.</div>';
    bindSongList($("#recent"), state.recent, (s) => { switchMode("library"); selectSong(s); });
    $("#chip-songs").textContent = `${state.recent.length ? "" : ""}${(state.status && state.status.counts && state.status.counts.songs) || state.recent.length} SONGS`;
  }
  async function loadLibrary() {
    const q = $("#lib-search").value.trim();
    try { state.songs = await api(`/api/songs?limit=300${q ? "&q=" + encodeURIComponent(q) : ""}${state.libLiked ? "&liked=1" : ""}`); } catch { return; }
    $("#library").innerHTML = state.songs.length ? state.songs.map((s) => songRow(s)).join("") : '<div class="hint">Library is empty.</div>';
    bindSongList($("#library"), state.songs, selectSong);
  }
  $("#lib-search").addEventListener("input", () => { clearTimeout(state.libTimer); state.libTimer = setTimeout(loadLibrary, 300); });
  $("#lib-liked").addEventListener("click", () => { state.libLiked = !state.libLiked; $("#lib-liked").classList.toggle("on", state.libLiked); loadLibrary(); });

  function selectSong(s) {
    state.selected = s;
    const u = s.urls || {};
    const dl = ["mp3", "voiced", "master", "vocals", "instrumental", "abc", "mid"].filter((k) => u[k]).map((k) => `<a class="mini ghost" href="${u[k]}" download>${k}</a>`).join("");
    const voices = state.voices.filter((v) => v.ready);
    $("#lib-detail").innerHTML = `
      <div class="now"><div class="nt">${esc(s.title)}</div><div class="ns">${esc(s.style)}</div><div class="nm">${fmtTime(s.duration_s)} · seed ${esc(s.seed)} · ${["melody+chords", "melody", "direct"][s.mode] || ""}${s.station_id ? " · " + esc(s.station_id) : ""}</div></div>
      <div class="actions">
        <button class="mini" data-act="play">Play</button>
        <button class="mini ${s.liked ? "" : "ghost"}" data-act="like">${s.liked ? "♥ Liked" : "♡ Like"}</button>
        <button class="mini ghost" data-act="queue">Up next</button>
        <button class="mini ghost" data-act="rename">Rename</button>
        <button class="mini ghost" data-act="cover">Cover this</button>
        <button class="mini ghost" data-act="rewrite" title="Keep this song's sheet and sing new words to its tune">Rewrite this</button>
        <button class="mini ghost" data-act="stems">Stems</button>
        <button class="mini ghost danger" data-act="delete">Delete</button>
      </div>
      <div class="row wrap voice-only"><select id="revoice-voice"><option value="">Re-voice as…</option>${voices.map((v) => `<option value="${v.id}">${esc(v.name)}${v.model ? " (trained)" : v.source === "stock" ? " (AI)" : ""}</option>`).join("")}</select><input type="number" id="revoice-shift" value="0" min="-12" max="12" title="Pitch shift in semitones for the converted vocal (trained voices only): use it when the song sits outside your range" style="width:64px"><select id="revoice-blend" title="Trained voices only: your trained voice against the AI singer it was trained from; the original singer is gone either way. Less of you = the AI singer's steadier delivery, your timbre pulled in"><option value="1">100% mine</option><option value="0.75">75% mine · 25% AI singer</option><option value="0.5">50 / 50</option><option value="0.25">25% mine · 75% AI singer</option><option value="0">0% mine: the AI singer, nothing of my recording</option></select><button class="mini" data-act="revoice">Go</button></div>
      <div class="actions">${dl}</div>
      <div class="hint keep">${esc(keepLabel(s) || "Made by hand: kept until you delete it")}</div>
      <pre class="lyrics-view">${esc(s.lyrics || "")}</pre>`;
    $$("#lib-detail [data-act]").forEach((b) => b.addEventListener("click", () => songAction(b.dataset.act, s)));
  }
  async function songAction(act, s) {
    try {
      if (act === "play") { state.queueMode = "library"; clearTimeout(state.radioTimer); state.waitingRadio = false; playSong(s); }
      else if (act === "queue") queueSong(s);
      else if (act === "like") { const r = await api(`/api/songs/${s.id}/like`, { method: "POST" }); Object.assign(s, r); heartToast(r); selectSong(s); loadLibrary(); if (state.playing && state.playing.id === s.id) updateLike(); }
      else if (act === "rename") { const t = prompt("Title", s.title); if (t) { const r = await api(`/api/songs/${s.id}/title`, { method: "POST", body: { title: t } }); Object.assign(s, r); selectSong(s); loadLibrary(); } }
      else if (act === "cover") { state.cover = { songId: s.id, name: s.title }; $("#cover-info").textContent = `Source: ${s.title} (library). Lyrics are prefilled; change the style and go.`; $("#btn-cover-clear").disabled = false; $("#btn-cover-transcribe").disabled = true; $("#lyrics").value = s.lyrics || ""; setDurationFor(s.duration_s); planMode = 1; $$("#mode-chips .chip-btn").forEach((x) => x.classList.toggle("on", x.dataset.v === "1")); setUi("studio"); switchMode("create"); toast("Cover loaded into Studio"); }
      else if (act === "rewrite") {
        const r = await api("/api/arrangements", { method: "POST", body: { source_song_id: s.id, name: s.title } });
        setUi("studio"); switchMode("create");
        if (r.job) { toast("This song has no sheet of its own; reading one from its audio. It will be in Saved sheets when done."); loadJobs(); arrLoaded(await arrFromJob(r.job), "Sheet read"); }
        else arrLoaded(r.arrangement, "Sheet loaded into Studio");
      }
      else if (act === "stems") { await api(`/api/songs/${s.id}/stems`, { method: "POST" }); toast("Stem split queued"); loadJobs(); }
      else if (act === "delete") { if (confirm(`Delete "${s.title}"?`)) { await api(`/api/songs/${s.id}`, { method: "DELETE" }); toast("Deleted"); $("#lib-detail").innerHTML = '<div class="hint">Tap a song.</div>'; loadLibrary(); loadRecent(); } }
      else if (act === "revoice") { const vid = $("#revoice-voice").value; if (!vid) return toast("Pick a voice first", true); await api(`/api/songs/${s.id}/revoice`, { method: "POST", body: { voice_id: vid, semi_tone_shift: Number(($("#revoice-shift") || {}).value || 0), blend: Number(($("#revoice-blend") || {}).value || 1) } }); toast("Re-voice queued"); loadJobs(); }
    } catch (e) { toast(e.message, true); }
  }

  // ---------------------------------------------------------------- the song menu (right-click a song, or hold it)
  function songMenuItems(s) {
    const items = [["play", "▶ Play now"], ["queue", "Up next"], ["like", s.liked ? "♡ Take the heart off" : "♥ Heart it"], ["rename", "Rename"], null,
      ["cover", "Cover this: YuE2 sings it again"], ["rewrite", "Rewrite this: new words on its tune"]];
    if ((state.voices || []).some((v) => v.ready)) items.push(["swap", "Swap the singer: re-voice it"]);
    if (!(state.features && state.features.voices === false)) items.push(["voice", "Make a voice from this singer"]);
    items.push(["stems", "Split the stems"], ["download", "Download the mp3"]);
    if (s.station_id) items.push(["more", "More like this on " + s.station_id]);
    items.push(null, ["ban", s.banned ? "Let the radio play it again" : "Keep it off the radio"], ["delete", "Delete"]);
    return items;
  }
  function openSongMenu(x, y, s, list) {
    const m = $("#song-menu"); if (!m || !s) return;
    $("#sleep-menu").classList.add("hidden"); $("#queue-menu").classList.add("hidden");
    m.innerHTML = `<div class="ctx-title">${esc(s.title)}</div>` + songMenuItems(s).map((it) => it ? `<button data-act="${it[0]}" class="${it[0] === "delete" ? "danger" : ""}">${esc(it[1])}</button>` : `<div class="ctx-sep"></div>`).join("");
    m.classList.remove("hidden");
    const r = m.getBoundingClientRect();
    m.style.left = Math.max(6, Math.min(x, window.innerWidth - r.width - 6)) + "px";
    m.style.top = Math.max(6, Math.min(y, window.innerHeight - r.height - 6)) + "px";
    $$("button", m).forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); closeSongMenu(); menuAction(b.dataset.act, s, list); }));
    const first = $("button", m); if (first) first.focus({ preventScroll: true });
  }
  function closeSongMenu() { const m = $("#song-menu"); if (m) m.classList.add("hidden"); }
  async function menuAction(act, s, list) {
    try {
      if (act === "play") { state.queueMode = "library"; if (list) state.songs = list; clearTimeout(state.radioTimer); state.waitingRadio = false; playSong(s); }
      else if (act === "swap") { switchMode("library"); selectSong(s); const sel = $("#revoice-voice"); if (sel) { sel.scrollIntoView({ block: "center" }); sel.focus(); } toast("Pick the voice and the blend, then Go"); }
      else if (act === "voice") { const r = await api("/api/voices/from_song", { method: "POST", body: { song_id: s.id } }); toast(`Making a voice: ${r.voice.name}. It joins the voice pickers when ready.`); loadJobs(); loadVoices(); }
      else if (act === "download") { const u = bestUrl(s); if (!u) return toast("No file for that one", true); const a = document.createElement("a"); a.href = u; a.download = ""; document.body.appendChild(a); a.click(); a.remove(); }
      else if (act === "more") { const r = await api(`/api/songs/${s.id}/more`, { method: "POST", body: {} }); toast("Making one like it" + (r.title ? ": " + r.title : "")); loadJobs(); }
      else if (act === "ban") { const r = await api(`/api/songs/${s.id}/ban`, { method: "POST" }); Object.assign(s, r); toast(r.banned ? "Off the radio" : "Back on the radio"); loadLibrary(); if (state.selected && state.selected.id === s.id) selectSong(s); }
      else songAction(act, s);
    } catch (e) { toast(e.message, true); }
  }
  document.addEventListener("click", (e) => { if (!e.target.closest("#song-menu")) closeSongMenu(); });
  document.addEventListener("scroll", closeSongMenu, true);
  window.addEventListener("resize", closeSongMenu);

  // ---------------------------------------------------------------- player
  const audio = $("#audio");
  function bestUrl(s) { const u = s.urls || {}; return u.mp3 || u.voiced || u.master; }
  function playSong(s, queued = false) {
    if (!s) return;
    state.playing = s; state.playedMarked = false; state.reconnects = 0; state.fromQueue = queued;
    if (state.queueMode !== "radio") { clearTimeout(state.radioTimer); state.radioTimer = null; state.waitingRadio = false; }  // leaving the radio drops its retry timer
    audio.src = bestUrl(s); audio.play().catch(() => {});
    $("#p-title").textContent = s.title;
    $("#p-sub").textContent = (queued ? "UP NEXT · " : state.queueMode === "radio" ? "RADIO · " + (state.radioStation === "all" ? "All songs" : (state.stations.find((x) => x.id === state.radioStation) || {}).name || state.radioStation) + " · " : "") + (coverOf(s) ? coverLine(s).toUpperCase() + " · " : "") + (s.style || "");
    updateLike(); renderNow(s);
    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: s.title, artist: "Ten Forward", album: s.station_id || "Library", artwork: [{ src: "/static/icon-512.png", sizes: "512x512", type: "image/png" }] });
      navigator.mediaSession.setActionHandler("play", () => audio.play());
      navigator.mediaSession.setActionHandler("pause", () => audio.pause());
      navigator.mediaSession.setActionHandler("nexttrack", nextSong);
      try { navigator.mediaSession.setActionHandler("previoustrack", () => { audio.currentTime = 0; }); } catch {}
    }
    $$(".song").forEach((r) => r.classList.toggle("playing", r.dataset.id === s.id));
  }
  function togglePlay() { if (!audio.src) { nextSong(); return; } if (audio.paused) audio.play(); else audio.pause(); }
  $("#p-play").addEventListener("click", togglePlay);
  $("#p-next").addEventListener("click", nextSong);
  $("#p-like").addEventListener("click", async () => { if (!state.playing) return; try { const r = await api(`/api/songs/${state.playing.id}/like`, { method: "POST" }); Object.assign(state.playing, r); updateLike(); heartToast(r); if (state.mode === "dial" || state.mode === "channels") renderNow(state.playing); } catch (e) { toast(e.message, true); } });
  function updateLike() { const on = !!(state.playing && state.playing.liked); $("#p-like").classList.toggle("on", on); $("#p-like").textContent = on ? "♥" : "♡"; $("#dial-like").classList.toggle("on", on); $("#dial-like").textContent = on ? "♥" : "♡"; }
  audio.addEventListener("play", () => { $("#dial-play").textContent = "❚❚"; $("#p-play").textContent = "❚❚"; $$(".song .play").forEach((b) => (b.textContent = state.playing && b.dataset.play === state.playing.id ? "❚❚" : "▶")); });
  audio.addEventListener("pause", () => { $("#dial-play").textContent = "▶"; $("#p-play").textContent = "▶"; $$(".song .play").forEach((b) => (b.textContent = "▶")); });
  audio.addEventListener("timeupdate", () => {
    if (audio.duration) { $("#p-seek").value = Math.round((audio.currentTime / audio.duration) * 1000); $("#p-time").textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(audio.duration)}`; }
    if (state.playing && !state.playedMarked && audio.currentTime > 10) { state.playedMarked = true; api(`/api/songs/${state.playing.id}/played`, { method: "POST", body: { station: state.radioStation || null } }).then(() => { if (state.mode === "dial") setTimeout(loadHistory, 800); }).catch(() => {}); }
    if (state.sleepAt && Date.now() >= state.sleepAt && !state.fading) { state.fading = true; fadeOut(); }
  });
  function clog(event, detail) {
    try { fetch("/api/client/log", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ event, detail: detail || "", song_id: state.playing && state.playing.id, title: state.playing && state.playing.title, t: Math.round(audio.currentTime * 10) / 10, duration: Math.round(audio.duration || 0), mode: state.queueMode, station: state.radioStation, ua: navigator.userAgent.slice(0, 80) }) }); } catch {}
  }
  audio.addEventListener("ended", () => {
    // a stream that dies early looks like 'ended' to the browser: reconnect where we were instead of skipping
    if (isFinite(audio.duration) && audio.duration > 20 && audio.currentTime < audio.duration - 3 && state.reconnects < 2) {
      state.reconnects++; clog("ended-early", "reconnect " + state.reconnects);
      const at = audio.currentTime; audio.load(); audio.currentTime = at; audio.play().catch(() => {}); return;
    }
    if (audio.currentTime < 20) clog("ended-short");
    state.reconnects = 0;
    if (state.reloadPending) { store.set("resumeRadio", state.queueMode === "radio" ? state.radioStation : null); location.reload(); return; }
    if (state.stopAfter) { sleepOff(); toast("Stopped after the song, as asked"); return; }
    nextSong();
  });
  audio.addEventListener("error", () => { clog("error", audio.error && audio.error.code); if (state.playing) toast("Could not play " + state.playing.title, true); });
  audio.addEventListener("stalled", () => clog("stalled"));
  $("#p-seek").addEventListener("input", (e) => { if (audio.duration) audio.currentTime = (e.target.value / 1000) * audio.duration; });
  async function nextSong() {
    if (state.playing && audio.currentTime > 0 && !audio.ended) { clog("skip"); noteSkip(state.playing); }
    if (state.queueMode === "radio") return radioNext();   // the server hands out the line itself
    if (state.upNext.length) return playQueued();
    const list = state.songs.length ? state.songs : state.recent;
    if (!list.length) return;
    const i = state.playing ? list.findIndex((x) => x.id === state.playing.id) : -1;
    playSong(list[(i + 1) % list.length]);
  }

  /**
   * A skip early in a song on the radio is a vote against it. The server counts them and retires the song
   * after a few; this only tells it, and says so when one goes. Never for the library: clicking through it
   * is browsing, not an opinion.
   */
  async function noteSkip(s) {
    if (state.queueMode !== "radio" || !s || state.fromQueue) return;
    try {
      const r = await api(`/api/songs/${s.id}/skip`, { method: "POST", body: { at: Math.round(audio.currentTime * 10) / 10, duration: Math.round(audio.duration || s.duration_s || 0), mode: "radio" } });
      if (r.retired) toast(`Skipped early ${r.skips} times: "${s.title}" is retired from the radio`);
    } catch {}
  }

  // ---------------------------------------------------------------- the listening queue
  //
  // The line used to live in this browser's localStorage, which meant the phone app could never
  // see it. It is on the server now (/api/playqueue), so the desk, the phone browser and the
  // Android app are all looking at the same list: line three songs up indoors, walk out to the
  // car, they are what plays. state.upNext is only this page's copy of it.
  //
  // On the radio, the server hands queued songs out through /api/radio/next itself - the client
  // does not have to take them. In the Library the page takes them itself, because nothing else
  // is asking.
  async function loadQueue() {
    try { const r = await api("/api/playqueue"); state.upNext = r.queue || []; } catch { return; }
    renderQueue();
  }
  async function queueSong(s, next = false) {
    if (!s) return;
    if (state.upNext.some((x) => x.id === s.id)) { toast("That one is already lined up", true); return; }
    try {
      const r = await api("/api/playqueue", { method: "POST", body: { song_id: s.id, next: !!next } });
      state.upNext = r.queue || [];
      renderQueue();
      toast((next ? "Playing next: " : "Lined up: ") + s.title);
    } catch (e) { toast(e.message, true); }
  }
  async function dropQueued(id) {
    try { const r = await api(`/api/playqueue/${encodeURIComponent(id)}`, { method: "DELETE" }); state.upNext = r.queue || []; } catch (e) { return toast(e.message, true); }
    renderQueue();
  }
  async function playQueued() {
    let s = null;
    try { const r = await api("/api/playqueue/take", { method: "POST" }); s = r.song; state.upNext = r.queue || []; } catch (e) { return toast(e.message, true); }
    renderQueue();
    if (s) playSong(s, true);
  }
  function renderQueue() {
    const n = state.upNext.length;
    const badge = $("#p-queue-n");
    if (badge) { badge.textContent = n || ""; $("#p-queue").classList.toggle("on", !!n); }
    const foot = $("#ss-qn");
    if (foot) foot.textContent = n ? `${n} lined up` : "Nothing lined up yet";
    const el = $("#up-next");
    if (!el) return;
    el.innerHTML = n ? state.upNext.map((s, i) => `<div class="qn-row"><button class="qn-play" data-qnplay="${esc(s.queue_id || s.id)}" title="Play it now">${i + 1}</button><div class="qn-t">${esc(s.title)}${coverTag(s)}<span>${esc(s.style || "")}</span></div><button class="qn-x" data-qndrop="${esc(s.queue_id || s.id)}" title="Take it out">x</button></div>`).join("")
      : '<div class="hint">Nothing lined up. Open <b>SONGS</b> on the dial, then hold a song to line it up.</div>';
    $$("#up-next [data-qnplay]").forEach((b) => b.addEventListener("click", async () => {
      const s = state.upNext.find((x) => (x.queue_id || x.id) === b.dataset.qnplay);
      if (!s) return;
      await dropQueued(b.dataset.qnplay);
      playSong(s, true); $("#queue-menu").classList.add("hidden");
    }));
    $$("#up-next [data-qndrop]").forEach((b) => b.addEventListener("click", () => dropQueued(b.dataset.qndrop)));
  }
  $("#p-queue").addEventListener("click", () => { $("#sleep-menu").classList.add("hidden"); loadQueue(); $("#queue-menu").classList.toggle("hidden"); });
  $("#q-clear-next").addEventListener("click", async () => {
    try { await api("/api/playqueue/clear", { method: "POST" }); } catch (e) { return toast(e.message, true); }
    state.upNext = []; renderQueue(); toast("Queue cleared");
  });

  /** Anything left in the old browser-only queue moves to the server once, then stops existing. */
  async function migrateOldQueue() {
    const old = store.get("upNext", []);
    if (!Array.isArray(old) || !old.length) return;
    store.set("upNext", []);
    for (const s of old.slice(0, 50)) { try { await api("/api/playqueue", { method: "POST", body: { song_id: s.id } }); } catch {} }
    toast(`Moved ${old.length} lined-up song${old.length === 1 ? "" : "s"} onto the server, so the phone sees them too`);
  }

  // ---------------------------------------------------------------- the song sheet
  //
  // "Pick a song off this channel, right now." Opened by SONGS on the dial and by the Songs
  // button on every channel card. Tap plays it; press and hold lines it up. Both targets are
  // deliberately large: this gets used at 70mph.
  const sheet = { station: "all", name: "All songs", songs: [], liked: false, q: "", timer: null };

  async function openSongs(stationId, name) {
    sheet.station = stationId || "all";
    sheet.name = name || ((state.stations.find((x) => x.id === sheet.station) || {}).name) || "All songs";
    $("#ss-name").textContent = sheet.name;
    $("#ss-q").value = ""; sheet.q = "";
    $("#songsheet").classList.remove("hidden");
    $("#ss-list").innerHTML = '<div class="hint">Looking…</div>';
    loadQueue();
    await loadSheet();
  }
  async function loadSheet() {
    const p = new URLSearchParams({ station: sheet.station, limit: "300", brief: "1" });
    if (sheet.q) p.set("q", sheet.q);
    if (sheet.liked) p.set("liked", "1");
    let list;
    try { list = await api("/api/songs?" + p.toString()); } catch (e) { $("#ss-list").innerHTML = `<div class="hint">${esc(e.message)}</div>`; return; }
    sheet.songs = list || [];
    $("#ss-count").textContent = sheet.songs.length ? `${sheet.songs.length} song${sheet.songs.length === 1 ? "" : "s"}` : "";
    renderSheet();
  }
  function renderSheet() {
    const el = $("#ss-list");
    if (!sheet.songs.length) { el.innerHTML = `<div class="hint">${sheet.q || sheet.liked ? "Nothing here matches." : "No songs on this channel yet."}</div>`; return; }
    const inQ = new Set(state.upNext.map((x) => x.id));
    el.innerHTML = sheet.songs.map((s) => `<div class="ssr ${state.playing && state.playing.id === s.id ? "on" : ""}" data-id="${esc(s.id)}">
      <div class="ssm"><div class="sst">${esc(s.title || "Untitled")}</div><div class="sss">${coverOf(s) ? esc(coverLine(s)) : esc(s.style || "")}${s.liked ? " ♥" : ""}</div></div>
      <div class="ssd">${fmtTime(s.duration_s)}</div>
      <button class="ssq ${inQ.has(s.id) ? "in" : ""}" data-q="${esc(s.id)}" title="Line it up">${inQ.has(s.id) ? "✓" : "+"}</button></div>`).join("");
    $$(".ssr", el).forEach((row) => {
      const s = sheet.songs.find((x) => x.id === row.dataset.id);
      let held = false, t = null;
      const start = () => { held = false; row.classList.add("hold"); t = setTimeout(() => { held = true; row.classList.remove("hold"); buzz(); queueSong(s).then(renderSheet); }, 500); };
      const end = () => { clearTimeout(t); row.classList.remove("hold"); };
      row.addEventListener("pointerdown", start);
      row.addEventListener("pointerup", end);
      row.addEventListener("pointerleave", end);
      row.addEventListener("pointercancel", end);
      row.addEventListener("contextmenu", (e) => e.preventDefault());   // a long press must not open the browser menu
      row.addEventListener("click", (e) => {
        if (e.target.dataset.q) return;
        if (held) { held = false; return; }                             // the hold already lined it up
        playPicked(s);
      });
      $("[data-q]", row).addEventListener("click", async (e) => { e.stopPropagation(); await queueSong(s); renderSheet(); });
    });
  }
  function buzz() { try { navigator.vibrate && navigator.vibrate(18); } catch {} }

  /**
   * A song you chose yourself. It plays now, out of the radio's own order, and the channel it
   * came from keeps stocking itself behind it - so picking one song does not end the radio.
   */
  function playPicked(s) {
    if (!s) return;
    state.queueMode = "library"; state.fromQueue = true;
    clearTimeout(state.radioTimer); state.radioTimer = null; state.waitingRadio = false;
    state.songs = sheet.songs;
    playSong(s);
    $("#songsheet").classList.add("hidden");
    toast("Playing: " + s.title);
    if (sheet.station && sheet.station !== "all") topUp(sheet.station);
  }
  /** Keep the channel you just picked from stocked, quietly - one song, and only if it is thin. */
  async function topUp(stationId) {
    try {
      const st = state.stations.find((x) => x.id === stationId);
      if (!st || isFav(st)) return;
      if ((st.unplayed || 0) > 1 || (st.queued || 0) > 0) return;
      await api(`/api/radio/${encodeURIComponent(stationId)}/generate`, { method: "POST", body: { count: 1 } });
      loadJobs();
    } catch {}
  }

  $("#ss-close").addEventListener("click", () => $("#songsheet").classList.add("hidden"));
  $("#ss-liked").addEventListener("click", () => { sheet.liked = !sheet.liked; $("#ss-liked").classList.toggle("on", sheet.liked); loadSheet(); });
  $("#ss-q").addEventListener("input", (e) => { sheet.q = e.target.value.trim(); clearTimeout(sheet.timer); sheet.timer = setTimeout(loadSheet, 250); });
  $("#ss-clear").addEventListener("click", async () => {
    try { await api("/api/playqueue/clear", { method: "POST" }); } catch (e) { return toast(e.message, true); }
    state.upNext = []; renderQueue(); renderSheet(); toast("Queue cleared");
  });
  $("#dial-songs").addEventListener("click", () => openSongs(state.radioStation || "all"));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#songsheet").classList.contains("hidden")) $("#songsheet").classList.add("hidden"); });

  // ---------------------------------------------------------------- radio
  async function loadStations() {
    try { state.stations = await api("/api/stations"); } catch { return; }
    renderStations();
    arrStations();
  }
  /**
   * The one number worth watching: how many hours of fresh audio the radio is holding.
   * The card renders about 1.6x faster than a song plays, so a healthy radio banks audio while
   * nobody is listening and spends it while somebody is. Nothing showed this before, which is
   * why "the radio ran dry" left no trace at all.
   */
  function renderBuffer() {
    const el = $("#buffer-line");
    if (!el) return;
    const b = state.status && state.status.buffer;
    if (!b || b.hours === undefined) { el.textContent = ""; return; }
    const thin = b.thinnest;
    const bits = [`${b.hours} h ready`];
    if (b.coming_minutes) bits.push(`+${Math.round(b.coming_minutes)} min cooking`);
    if (thin && thin.minutes !== undefined) bits.push(`thinnest ${thin.name}: ${Math.round(thin.minutes)} min`);
    if (b.parked) bits.push(`${b.parked} channel${b.parked === 1 ? "" : "s"} stuck`);
    el.textContent = bits.join(" · ");
    el.className = "buffer" + (b.parked || (thin && thin.minutes < 5) ? " dry" : (thin && thin.minutes < 15 ? " thin" : ""));
  }

  function renderStations() {
    const all = { id: "all", name: "All songs", color: "gold", description: "Everything in the library, unplayed first.", ready: state.status && state.status.counts ? state.status.counts.songs : "", unplayed: "", queued: "" };
    const cards = [all, ...state.stations].map((st) => `<div class="station ${state.radioStation === st.id ? "on" : ""}" style="border-top-color:var(--${esc(st.color || "teal")})" data-id="${st.id}">
        <h3>${esc(st.name)}</h3><p>${esc(st.description || "")}</p>
        ${st.id !== "all" ? themeChips(st) : ""}
        <div class="counts">${st.id === "all" ? `${st.ready} songs` : isFav(st) ? `${st.ready} hearted` : `${st.ready} ready · ${st.unplayed} unplayed · ${st.queued} cooking${st.lyrics_files ? " · " + st.lyrics_files + " lyric files" : ""}`}</div>
        ${st.next_up ? `<div class="cook">${st.next_up.status === "running" ? "Cooking" : "Next in line"}: ${esc(st.next_up.title || "untitled")}${st.next_up.status === "running" ? ` · ${Math.round((st.next_up.progress || 0) * 100)}%` : ""}<div class="bar"><i style="width:${Math.round((st.next_up.progress || 0) * 100)}%"></i></div></div>` : ""}
        <div class="row wrap"><button class="mini" data-tune="${st.id}">Tune in</button><button class="mini ghost" data-songs="${st.id}">Songs</button>${st.id === "all" ? "" : `${isFav(st) ? "" : `<button class="mini ghost" data-make="${st.id}">Make one</button>`}<button class="mini ghost" data-edit="${st.id}">Edit</button>`}</div></div>`).join("");
    $("#stations").innerHTML = cards;
    $$("#stations [data-tune]").forEach((b) => b.addEventListener("click", () => tuneIn(b.dataset.tune)));
    $$("#stations [data-songs]").forEach((b) => b.addEventListener("click", () => openSongs(b.dataset.songs)));
    $$("#stations [data-make]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/api/radio/${b.dataset.make}/generate`, { method: "POST", body: { count: 1 } }); toast("Cooking one for " + b.dataset.make); loadJobs(); setTimeout(loadStations, 800); } catch (e) { toast(e.message, true); } }));
    $$("#stations [data-edit]").forEach((b) => b.addEventListener("click", () => editStation(state.stations.find((s) => s.id === b.dataset.edit))));
    renderBuffer();
    $("#btn-autofill").classList.toggle("on", !state.status || state.status.radio_autofill !== false);
    renderDial(); fillStationSelects();
  }
  const isFav = (st) => !!st && (st.kind === "favorites" || st.id === "favorites");
  function themeChips(st) {
    if (isFav(st)) return `<div class="tchips"><span class="tchip">every song you hearted</span><span class="tchip">never makes songs</span></div>`;
    if (st.instrumental) return `<div class="tchips"><span class="tchip">instrumental</span><span class="tchip">${st.variation === 0 ? "fixed style" : "style mutates every song"}</span></div>`;
    const names = st.theme_names || [];
    const shown = names.slice(0, 5).map((n) => `<span class="tchip">${esc(n)}</span>`).join("") + (names.length > 5 ? `<span class="tchip">+${names.length - 5}</span>` : "");
    const bans = (st.banned || []).map((b) => `<span class="tchip ban">no ${esc(b)}</span>`).join("");
    return `<div class="tchips">${shown}${bans}</div>`;
  }
  function renderNextUp(nu) {
    const el = $("#next-up");
    if (!nu) { el.classList.add("hidden"); el.innerHTML = ""; return; }
    el.classList.remove("hidden");
    const pct = Math.round((nu.progress || 0) * 100);
    el.innerHTML = `<div class="nl">${nu.status === "running" ? "Being made right now" : "Next in line"}</div><div>${esc(nu.title || "untitled")}${nu.topic && nu.topic !== "from lyrics folder" ? " · " + esc(String(nu.topic).slice(0, 90)) : ""}</div>${nu.status === "running" ? `<div class="bar"><i style="width:${pct}%"></i></div><div class="nl">${pct}% · ${esc(nu.message || "")}</div>` : ""}`;
  }
  async function pollNextUp() {
    clearTimeout(state.nextUpTimer);
    if (state.queueMode !== "radio" || !state.radioStation || state.radioStation === "all") { renderNextUp(null); return; }
    try {
      const sts = await api("/api/stations"); state.stations = sts;
      const st = sts.find((x) => x.id === state.radioStation);
      renderNextUp(st ? st.next_up : null);
      if (state.mode === "dial" || state.mode === "channels") renderStations();
    } catch {}
    state.nextUpTimer = setTimeout(pollNextUp, 8000);
  }
  async function tuneIn(id) {
    state.radioStation = id; store.set("radioStation", id); state.queueMode = "radio";
    renderStations();
    if (state.mode === "channels") { const s = dialStations().find((x) => x.id === id); toast("Tuned in: " + ((s && s.name) || id)); }
    await radioNext(true);
  }
  if ($("#ch-open-dial")) $("#ch-open-dial").addEventListener("click", () => switchMode("dial"));
  async function radioNext(fresh = false) {
    if (!state.radioStation) return;
    if (state.queueMode !== "radio") return;  // a stale timer must never hijack Library playback
    try {
      const exclude = !fresh && state.playing ? state.playing.id : "";
      const r = await api(`/api/radio/next?station=${encodeURIComponent(state.radioStation)}&exclude=${exclude}`);
      if (r.song) {
        state.waitingRadio = false; clearTimeout(state.radioTimer); state.radioTimer = null;
        state.fromQueue = !!r.from_queue;   // you asked for this one; a skip is not a vote against it
        playSong(r.song, !!r.from_queue);
        if (r.from_queue) { state.upNext = state.upNext.filter((x) => x.id !== r.song.id); renderQueue(); }
      } else {
        // nothing to play yet: show the cooking state and check again in 10 s (radio mode only)
        state.waitingRadio = true;
        $("#now").innerHTML = `<div class="nt">Cooking the first song…</div><div class="ns">${r.pending_jobs ? r.pending_jobs + " in the queue. A song takes a few minutes." : "No songs yet for this station. Hit Make one or wait for auto stock."}</div>`;
        $("#p-title").textContent = "Radio warming up"; $("#p-sub").textContent = "The first song is being generated";
        clearTimeout(state.radioTimer); state.radioTimer = setTimeout(() => { if (state.queueMode === "radio" && state.waitingRadio) radioNext(); }, 10000);
        if (!r.pending_jobs && state.radioStation !== "all") api(`/api/radio/${state.radioStation}/generate`, { method: "POST", body: { count: 1 } }).then(loadJobs).catch(() => {});
      }
      renderNextUp(r.next_up); pollNextUp();
      if (r.power === false) toast("Power is OFF: the radio plays what exists, nothing new is being made", true);
    } catch (e) { toast(e.message, true); }
  }
  function renderNow(s) {
    const st = state.stations.find((x) => x.id === s.station_id);
    const meta = [keepLabel(s), s.theme ? "theme: " + (state.themes.find((t) => t.id === s.theme) || {}).name || s.theme : "", s.singer ? s.singer + " singer" : "", s.topic && s.topic !== "from lyrics folder" && s.topic !== "instrumental" ? String(s.topic).slice(0, 140) : ""].filter(Boolean).join(" · ");
    $("#now").innerHTML = `<div class="nt">${esc(s.title)}${coverTag(s)}</div><div class="ns">${esc(s.style)}</div>${coverOf(s) ? `<div class="nowcover">${esc(coverLine(s))}</div>` : ""}<div class="nm">${st ? esc(st.name) + " · " : ""}${fmtTime(s.duration_s)}${s.voice_id ? " · voiced" : ""}</div>${meta ? `<div class="nmeta">${esc(meta)}</div>` : ""}
      <div class="row wrap" style="margin-top:8px"><button class="mini" id="now-skip">Skip</button><button class="mini ghost" id="now-like">${s.liked ? "♥ Liked" : "♡ Like"}</button>${s.station_id && s.source !== "import" ? `<button class="mini ghost" id="now-more" title="Another song with this theme, singer and sound, ahead of the auto stock">More like this</button>` : ""}<button class="mini ghost" id="now-cover">Cover this</button><button class="mini ghost danger" id="now-ban" title="Skip it and never play it on the radio again">Never again</button></div>`;
    if ($("#now-more")) $("#now-more").addEventListener("click", async () => { $("#now-more").disabled = true; try { const r = await api(`/api/songs/${s.id}/more`, { method: "POST", body: {} }); toast("Cooking another like it: " + (r.title || "")); loadJobs(); pollNextUp(); } catch (e) { toast(e.message, true); $("#now-more").disabled = false; } });
    $("#now-ban").addEventListener("click", async () => { try { await api(`/api/songs/${s.id}/ban`, { method: "POST" }); toast("Banned from the radio: " + s.title); nextSong(); } catch (e) { toast(e.message, true); } });
    $("#now-lyrics").textContent = s.lyrics || "";
    if ($("#ch-now")) $("#ch-now").innerHTML = `<div class="nt">${esc(s.title)}${coverTag(s)}</div><div class="ns">${esc(s.style)}</div>${coverOf(s) ? `<div class="nowcover">${esc(coverLine(s))}</div>` : ""}<div class="nm">${st ? esc(st.name) + " · " : ""}${fmtTime(s.duration_s)}</div>`;
    $("#now-skip").addEventListener("click", nextSong);
    $("#now-like").addEventListener("click", () => $("#p-like").click());
    $("#now-cover").addEventListener("click", () => songAction("cover", s));
  }
  $("#btn-autofill").addEventListener("click", async () => {
    const on = !$("#btn-autofill").classList.contains("on");
    try { await api("/api/settings", { method: "POST", body: { radio_autofill: on } }); $("#btn-autofill").classList.toggle("on", on); toast(on ? "Stations will restock themselves" : "Auto stock off"); } catch (e) { toast(e.message, true); }
  });
  // station editor
  function editStation(st, isNew) {
    // the defaults come from Settings, not from literals here: hard-coding 180/2 meant every channel
    // made in a browser ignored the configured defaults and disagreed with the phone's own editor
    const sd = (state.settings && state.settings.settings) || {};
    const dflt = (k, f) => { const v = sd[k]; const n = Number(v && v.value !== undefined ? v.value : v); return Number.isFinite(n) && n > 0 ? n : f; };
    st = st || { id: "", name: "", description: "", style_prompts: [], lyrics_folder: "", duration_s: dflt("default_duration_s", 300), keep_ahead: dflt("keep_ahead_default", 5), lyric_policy: "mixed", voice_id: "", mode: 0, instrumental: 0, auto_generate: 1 };
    $("#station-editor").classList.remove("hidden");
    $("#se-title").textContent = isNew ? "New channel: " + (st.name || "") : st.id ? "Station: " + st.name : "New station";
    if (!isNew) { $("#se-note").classList.add("hidden"); $("#se-note").textContent = ""; $("#se-note").classList.remove("built"); $("#se-save").classList.remove("flash"); }
    $("#station-editor").dataset.id = st.id || "";
    $("#se-name").value = st.name || ""; $("#se-folder").value = st.lyrics_folder || ""; $("#se-duration").value = st.duration_s || 180; $("#se-keep").value = st.keep_ahead ?? 2;
    $("#se-policy").value = st.lyric_policy || "mixed"; $("#se-mode").value = String(st.mode || 0); $("#se-steps").value = st.steps == null ? "" : st.steps; $("#se-instrumental").checked = !!st.instrumental; $("#se-auto").checked = st.auto_generate !== 0;
    $("#se-desc").value = st.description || ""; $("#se-styles").value = (st.style_prompts || []).join("\n");
    $("#station-editor").dataset.color = st.color || ""; $("#station-editor").dataset.fusion = st.fusion_set || "";
    fillFusionSelect(); $("#se-replay").value = st.replay_policy || "fresh"; $("#se-fusion").value = st.fusion_set || ""; $("#se-variation").checked = st.variation !== 0;
    $("#se-themes").value = (st.themes || []).join(", "); $("#se-banned").value = (st.banned_topics || []).join(", ");
    $("#se-retention").value = st.retention_days === null || st.retention_days === undefined ? "" : st.retention_days; $("#se-instchance").value = Math.round((st.instrumental_chance || 0) * 100);
    $("#se-male").value = st.male_ratio === null || st.male_ratio === undefined ? "" : Math.round(st.male_ratio * 100);
    $("#se-place").value = st.place_chance === null || st.place_chance === undefined ? "" : Math.round(st.place_chance * 100);
    $("#se-cover").value = st.cover_chance === null || st.cover_chance === undefined ? "" : Math.round(st.cover_chance * 100);
    $("#se-rewrite").value = st.rewrite_chance === null || st.rewrite_chance === undefined ? "" : Math.round(st.rewrite_chance * 100);
    $("#se-theme-hint").textContent = "Theme ids: " + state.themes.map((t) => t.id).join(", ");
    $("#se-mood").value = st.mood || ""; $("#se-explicit").checked = !!st.explicit;
    $("#theme-edit").classList.add("hidden"); renderThemePanel();
    $("#se-voice").innerHTML = `<option value="">YuE2's own singer</option>` + state.voices.filter((v) => v.ready).map((v) => `<option value="${v.id}" ${st.voice_id === v.id ? "selected" : ""}>${esc(v.name)}</option>`).join("");
    $("#se-delete").style.display = st.id && !isNew ? "" : "none";
    const fav = isFav(st);
    $("#se-fav-note").classList.toggle("hidden", !fav);
    [$("#station-editor .grid2"), $("#station-editor .mobile-only"), $("#se-theme-panel"), $("#se-banned").closest("label"), $("#se-styles").closest("label"), $("#se-delete")]
      .forEach((el) => el && el.classList.toggle("hidden", fav));
    $("#station-editor").scrollIntoView({ behavior: "smooth" });
  }
  // "Synthwave (Neon Static)": the channel in brackets only when this install has it
  const fusionLabel = (k) => { const l = FUSION_LABELS[k] || k; const m = l.match(/^(.*) \(([^)]+)\)$/); return m && !(state.stations || []).some((s) => s.name === m[2]) ? m[1] : l; };
  const FUSION_LABELS = { synthwave: "Synthwave (Neon Static)", darksynth: "Darksynth mash-ups (Warp Core)", classical: "Classical strings (Quartet Hall)", sleep: "Sleep ambient (Sleeping Sounds)", focus: "Focus electronic (Engineering)", lounge: "Lounge jazz and soul", porch: "Porch acoustic", summer: "Summer pop / country pop / pop punk (Summer Haze)", bluehour: "Moody pop after dark (Blue Hour)" };
  function fillFusionSelect() {
    const sel = $("#se-fusion"); if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = `<option value="">Auto from the description</option>` + (state.fusionSets || []).map((k) => `<option value="${esc(k)}">${esc(fusionLabel(k))}</option>`).join("");
    if (cur && (state.fusionSets || []).includes(cur)) sel.value = cur;
  }
  $("#btn-new-station").addEventListener("click", () => { $("#quick-create").classList.add("hidden"); editStation(null); });
  function qcCoverVisible() { $("#qc-cover-wrap").classList.toggle("hidden", $("#qc-inst").checked); }
  $("#btn-quick-station").addEventListener("click", () => {
    $("#station-editor").classList.add("hidden");
    $("#quick-create").classList.remove("hidden");
    $("#qc-out").textContent = ""; $("#qc-again").classList.add("hidden");
    qcCoverVisible(); $("#quick-create").scrollIntoView({ behavior: "smooth" }); $("#qc-text").focus();
  });
  $("#qc-close").addEventListener("click", () => $("#quick-create").classList.add("hidden"));
  $("#qc-inst").addEventListener("change", qcCoverVisible);
  async function quickBuild() {
    const text = $("#qc-text").value.trim();
    if (text.length < 8) return toast("Say a sentence or two about it first", true);
    askNotify();
    const mins = Number($("#qc-mins").value);
    const body = { text, instrumental: $("#qc-inst").checked ? 1 : 0, explicit: $("#qc-explicit").checked ? 1 : 0,
      cover_chance: $("#qc-inst").checked ? 0 : Math.max(0, Math.min(100, Number($("#qc-cover").value) || 0)) / 100,
      minutes: mins > 0 ? mins : null };
    $("#qc-go").disabled = true; $("#qc-again").disabled = true;
    const started = Date.now();
    const say = () => {
      const s = Math.round((Date.now() - started) / 1000);
      $("#qc-out").innerHTML = `<span class="spinner"></span><span class="working">GENERATING… ${s}s</span>` +
        (s > 45 ? " — it is waiting its turn behind a song being planned." : "");
    };
    say();
    const tick = setInterval(say, 1000);
    try {
      const r = await api("/api/stations/quick", { method: "POST", body });
      clearInterval(tick);
      $("#qc-out").innerHTML = `<span class="working">Built in ${Math.round((Date.now() - started) / 1000)}s.</span>`;
      $("#qc-again").classList.remove("hidden");
      editStation(r.station, true);
      const note = $("#se-note");
      note.textContent = ["Built from what you typed and filled in below — nothing is saved yet. Look it over, change anything, then press Save station."].concat(r.notes || []).join(" ");
      note.classList.remove("hidden");
      note.classList.add("built");
      $("#se-save").classList.remove("flash");
      void $("#se-save").offsetWidth;            // so the flash runs again on a second build
      $("#se-save").classList.add("flash");
      toast("Channel written — press Save station to keep it");
      if (r.needs === "lrclib_enabled") {
        const b = document.createElement("button");
        b.className = "mini"; b.textContent = "Turn it on"; b.style.marginLeft = "8px";
        b.addEventListener("click", async () => {
          try { await api("/api/settings", { method: "POST", body: { lrclib_enabled: true } }); b.remove(); toast("Learning from real songs is on"); } catch (e) { toast(e.message, true); }
        });
        note.appendChild(b);
      }
    } catch (e) {
      clearInterval(tick);
      $("#qc-out").textContent = e.message;
    } finally {
      $("#qc-go").disabled = false; $("#qc-again").disabled = false;
    }
  }
  $("#qc-go").addEventListener("click", quickBuild);
  $("#qc-again").addEventListener("click", quickBuild);
  $("#se-close").addEventListener("click", () => $("#station-editor").classList.add("hidden"));
  $("#se-save").addEventListener("click", async () => {
    askNotify();   // saving a channel is the click that earns the right to ask
    const body = { id: $("#station-editor").dataset.id || undefined, name: $("#se-name").value.trim(), lyrics_folder: $("#se-folder").value.trim() || undefined, duration_s: Number($("#se-duration").value), keep_ahead: Number($("#se-keep").value), lyric_policy: $("#se-policy").value, voice_id: $("#se-voice").value || null, mode: Number($("#se-mode").value), steps: $("#se-steps").value === "" ? null : Number($("#se-steps").value), instrumental: $("#se-instrumental").checked ? 1 : 0, auto_generate: $("#se-auto").checked ? 1 : 0, description: $("#se-desc").value, style_prompts: $("#se-styles").value.split("\n").map((s) => s.trim()).filter(Boolean), color: $("#station-editor").dataset.color || "teal",
      replay_policy: $("#se-replay").value, fusion_set: $("#se-fusion").value, fusion_set_cleared: !$("#se-fusion").value && !!$("#station-editor").dataset.fusion, variation: $("#se-variation").checked ? 1 : 0, themes: $("#se-themes").value, banned_topics: $("#se-banned").value,
      retention_days: $("#se-retention").value === "" ? null : Number($("#se-retention").value), instrumental_chance: Math.max(0, Math.min(100, Number($("#se-instchance").value) || 0)) / 100,
      mood: $("#se-mood").value.trim(), explicit: $("#se-explicit").checked ? 1 : 0,
      male_ratio: $("#se-male").value === "" ? null : Math.max(0, Math.min(100, Number($("#se-male").value))) / 100,
      place_chance: $("#se-place").value === "" ? null : Math.max(0, Math.min(100, Number($("#se-place").value))) / 100,
      cover_chance: $("#se-cover").value === "" ? null : Math.max(0, Math.min(100, Number($("#se-cover").value))) / 100,
      rewrite_chance: $("#se-rewrite").value === "" ? null : Math.max(0, Math.min(100, Number($("#se-rewrite").value))) / 100 };
    if (!body.name) return toast("Name the station", true);
    const fresh = !body.id || !state.stations.some((s) => s.id === body.id);
    try {
      await api("/api/stations", { method: "POST", body });
      toast(fresh && body.auto_generate ? "Saved — it is making its first songs now" : "Station saved");
      $("#station-editor").classList.add("hidden");
      loadStations(); loadStatus();
    } catch (e) { toast(e.message, true); }
  });
  $("#se-delete").addEventListener("click", async () => { const id = $("#station-editor").dataset.id; if (id && confirm("Delete this station? Songs stay in the library.")) { await api("/api/stations/" + id, { method: "DELETE" }); $("#station-editor").classList.add("hidden"); loadStations(); } });

  // ---------------------------------------------------------------- themes and mood (desktop station editor, 0.5)
  const isDesktop = () => window.matchMedia("(min-width: 821px)").matches;
  async function loadThemeCatalog() {
    try {
      const r = await api("/api/themes?full=1");
      state.themesFull = r.themes || []; state.themeKinds = r.kinds || [];
      state.themes = state.themesFull.filter((t) => !t.hidden).map((t) => ({ id: t.id, name: t.name, kind: t.kind }));
    } catch (e) { toast(e.message, true); }
    return state.themesFull || [];
  }
  function selectedThemeIds() { return $("#se-themes").value.split(/[,\n]/).map((s) => s.trim()).filter(Boolean); }
  function setSelectedThemes(ids) { $("#se-themes").value = ids.join(", "); }
  async function renderThemePanel() {
    if (!isDesktop() || !$("#se-theme-list")) return;
    const cat = state.themesFull && state.themesFull.length ? state.themesFull : await loadThemeCatalog();
    const sel = new Set(selectedThemeIds());
    const all = sel.size === 0 || sel.has("*");
    const kinds = [...new Set([...(state.themeKinds || []), ...cat.map((t) => t.kind)])];
    $("#se-theme-list").innerHTML = kinds.map((k) => {
      const rows = cat.filter((t) => t.kind === k);
      if (!rows.length) return "";
      const kindOn = sel.has(k);
      return `<div class="tkind"><div class="tkind-head"><label class="check"><input type="checkbox" data-kind="${k}" ${kindOn ? "checked" : ""}> ${esc(k)} <small>(whole group)</small></label></div>` +
        rows.map((t) => t.hidden
          ? `<div class="trow hidden-theme"><label class="check" title="Hidden from every station">${esc(t.name)}</label><span class="tw"></span><span class="tag exp">hidden</span><button class="mini ghost" data-restore="${t.id}">Restore</button></div>`
          : `<div class="trow ${all || kindOn || sel.has(t.id) ? "on" : ""}"><label class="check"><input type="checkbox" data-theme="${t.id}" ${sel.has(t.id) ? "checked" : ""} ${kindOn ? "disabled" : ""}> ${esc(t.name)}</label><span class="tw" title="weight: how often it comes up">${t.weight}</span>${t.custom ? `<span class="tag st">custom</span>` : t.edited ? `<span class="tag exp">edited</span>` : `<span></span>`}<button class="mini ghost" data-edit="${t.id}" title="Change what songs on this theme are about">Edit</button><div class="tbrief">${esc(t.brief || "")}</div></div>`).join("") + `</div>`;
    }).join("");
    const groups = [...sel].filter((x) => kinds.includes(x)), singles = [...sel].filter((x) => !kinds.includes(x) && x !== "*");
    $("#se-theme-count").textContent = all ? "every theme (nothing ticked)" : `${singles.length} theme${singles.length === 1 ? "" : "s"}${groups.length ? " + " + groups.length + " group" + (groups.length === 1 ? "" : "s") : ""} ticked`;
    $$("#se-theme-list input[data-theme], #se-theme-list input[data-kind]").forEach((cb) => cb.addEventListener("change", () => {
      const ids = new Set(selectedThemeIds());
      const key = cb.dataset.theme || cb.dataset.kind;
      if (cb.checked) ids.add(key); else ids.delete(key);
      setSelectedThemes([...ids]); renderThemePanel();
    }));
    $$("#se-theme-list .trow label.check").forEach((l) => l.addEventListener("click", (e) => { if (e.target.tagName !== "INPUT") { const row = l.closest(".trow"); if (row) row.classList.toggle("open"); } }));
    $$("#se-theme-list [data-edit]").forEach((b) => b.addEventListener("click", () => openThemeEdit(cat.find((t) => t.id === b.dataset.edit))));
    $$("#se-theme-list [data-restore]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/api/themes/${b.dataset.restore}/reset`, { method: "POST" }); toast("Theme restored"); await loadThemeCatalog(); renderThemePanel(); } catch (e) { toast(e.message, true); } }));
  }
  function openThemeEdit(t) {
    t = t || { id: "", name: "", brief: "", kind: "scene", weight: 5, custom: true };
    const ed = $("#theme-edit");
    ed.classList.remove("hidden"); ed.dataset.id = t.id || ""; ed.dataset.custom = t.custom ? "1" : "";
    $("#te-title").textContent = t.id ? (t.custom ? "Custom theme: " : "Built-in theme: ") + t.name : "New theme";
    $("#te-name").value = t.name || ""; $("#te-brief").value = t.brief || ""; $("#te-weight").value = t.weight || 5;
    const kinds = [...new Set([...(state.themeKinds || []), "love", "summer", "money", "scene", "street"])];
    $("#te-kind").innerHTML = kinds.map((k) => `<option value="${k}" ${k === t.kind ? "selected" : ""}>${esc(k)}</option>`).join("");
    $("#te-delete").textContent = t.custom ? "Delete" : "Reset to built-in"; $("#te-delete").style.display = t.id && (t.custom || t.edited) ? "" : "none";
    $("#te-hide").style.display = t.id && !t.custom ? "" : "none";
    ed.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  $("#se-theme-new").addEventListener("click", () => openThemeEdit(null));
  $("#te-close").addEventListener("click", () => $("#theme-edit").classList.add("hidden"));
  $("#te-save").addEventListener("click", async () => {
    const id = $("#theme-edit").dataset.id;
    const body = { id: id || undefined, name: $("#te-name").value.trim(), brief: $("#te-brief").value.trim(), kind: $("#te-kind").value, weight: Number($("#te-weight").value) || 5 };
    if (!body.name || !body.brief) return toast("A theme needs a name and a brief", true);
    try {
      const saved = await api("/api/themes", { method: "POST", body });
      toast("Theme saved: " + saved.name); $("#theme-edit").classList.add("hidden"); await loadThemeCatalog();
      if (!id) setSelectedThemes([...new Set([...selectedThemeIds(), saved.id])]);  // a brand new theme joins this station's list
      renderThemePanel();
    } catch (e) { toast(e.message, true); }
  });
  $("#te-delete").addEventListener("click", async () => {
    const id = $("#theme-edit").dataset.id, custom = $("#theme-edit").dataset.custom === "1";
    if (!id) return;
    if (custom && !confirm("Delete this custom theme? Stations that list it just skip it.")) return;
    try {
      if (custom) await api("/api/themes/" + id, { method: "DELETE" }); else await api(`/api/themes/${id}/reset`, { method: "POST" });
      toast(custom ? "Theme deleted" : "Theme reset to the built-in text"); $("#theme-edit").classList.add("hidden"); await loadThemeCatalog();
      if (custom) setSelectedThemes(selectedThemeIds().filter((x) => x !== id));
      renderThemePanel();
    } catch (e) { toast(e.message, true); }
  });
  $("#te-hide").addEventListener("click", async () => {
    const id = $("#theme-edit").dataset.id;
    if (!id || !confirm("Hide this built-in theme from every station? Restore brings it back.")) return;
    try { await api("/api/themes/" + id, { method: "DELETE" }); toast("Theme hidden"); $("#theme-edit").classList.add("hidden"); await loadThemeCatalog(); renderThemePanel(); } catch (e) { toast(e.message, true); }
  });

  // ---------------------------------------------------------------- voices + sheet
  async function loadVoices() {
    if (state.features && state.features.voices === false) { state.voices = []; return; }
    try { state.voices = await api("/api/voices"); } catch { return; }
    renderVoiceButton(); renderVoiceList(); renderSwapVoices();
    if (state.voiceId && !state.voices.some((v) => v.id === state.voiceId)) { state.voiceId = null; store.set("voiceId", null); renderVoiceButton(); }
    if (state.voices.some((v) => !v.ready || v.training)) setTimeout(loadVoices, 4000);
  }
  function renderVoiceButton() {
    const v = state.voices.find((x) => x.id === state.voiceId);
    $("#voice-name").textContent = v ? (v.ready ? v.name : v.name + " (processing)") : "YuE2's own singer";
    $("#btn-voice").classList.toggle("set", !!v);
  }
  function renderVoiceList() {
    const el = $("#vs-list");
    const playing = state.voicePlaying || null;
    el.innerHTML = `<div class="vitem ${!state.voiceId ? "on" : ""}" data-v=""><div class="grow"><b>YuE2's own singer</b><small>No voice conversion</small></div></div>` +
      state.voices.map((v) => `<div class="vitem ${state.voiceId === v.id ? "on" : ""}" data-v="${v.id}"><div class="grow"><b>${esc(v.name)}</b>${v.model ? `<span class="tag v" title="Fine-tuned on ${v.train_meta ? v.train_meta.seconds + "s of recordings, " + v.train_meta.steps + " steps" : "this voice"}">trained</span>` : ""}<small>${v.ready ? fmtTime(v.duration_s) + " sample" + (v.train_files && v.train_files.length ? ` + ${v.train_files.length} recording${v.train_files.length === 1 ? "" : "s"} (${Math.round(v.train_seconds / 60)} min total)` : "") + (v.pitch && v.pitch.low_note ? ` · range ${v.pitch.low_note}–${v.pitch.high_note}, ${v.pitch.gender} (${v.pitch.median_note} centre)` : "") : "processing…"}</small></div>
        ${v.ready ? `<div class="vtools">${v.training ? `<small class="training">Training… ${Math.round((v.training.progress || 0) * 100)}% · ${esc(v.training.message || "")}</small>` : `<button class="mini round" data-play="${v.id}" title="Play the sample">${playing === v.id ? "❚❚" : "▶"}</button><button class="mini" data-train="${v.id}" title="Fine-tune the singing model on this person (sample + added recordings). About 10 minutes on the GPU; more recordings = closer voice.">${v.model ? "Retrain" : "Train"}</button><label class="filebtn mini" title="More recordings of the same person: singing or talking, no music behind, any length">Add recordings<input type="file" multiple accept="audio/*" data-trainfiles="${v.id}"></label>${v.model ? `<button class="mini ghost" data-untrain="${v.id}" title="Go back to the plain 30 s sample conversion">Forget</button>` : ""}<button class="mini ghost" data-rename="${v.id}" title="Rename this voice">Rename</button><button class="mini ghost desktop-only-inline" data-folder="${v.id}" title="Add every audio file in a folder on this PC or the NAS (music behind the voice is removed before training)">Add a folder</button>`}</div>` : ""}</div>`).join("") ||
      "";
    $$("#vs-list .vitem").forEach((it) => it.addEventListener("click", (e) => { if (e.target.closest(".vtools")) return; state.voiceId = it.dataset.v || null; store.set("voiceId", state.voiceId); renderVoiceButton(); renderVoiceList(); closeSheet(); }));
    $$("#vs-list [data-play]").forEach((b) => b.addEventListener("click", (e) => {
      e.stopPropagation();
      const a = $("#vs-audio"), v = state.voices.find((x) => x.id === b.dataset.play);
      if (!a || !v) return;
      if (state.voicePlaying === v.id) { a.pause(); a.currentTime = 0; state.voicePlaying = null; renderVoiceList(); return; }
      a.src = v.url; state.voicePlaying = v.id; a.play().catch(() => { state.voicePlaying = null; renderVoiceList(); }); renderVoiceList();
    }));
    $$("#vs-list [data-folder]").forEach((b) => b.addEventListener("click", async (e) => {
      e.stopPropagation();
      const path = prompt("Folder with recordings of this person (a path this computer can read)", store.get("voiceFolder", ""));
      if (!path || !path.trim()) return;
      const ex = prompt("Skip files whose name contains any of these (comma separated), or leave blank", "");
      store.set("voiceFolder", path.trim());
      toast("Copying the folder's audio in…");
      try {
        const r = await api(`/api/voices/${b.dataset.folder}/train/folder`, { method: "POST", body: { path: path.trim(), exclude: (ex || "").split(",").map((s) => s.trim()).filter(Boolean) } });
        toast(`${(r.added || []).length} files added (${Math.round((r.seconds || 0) / 60)} min of recordings)${(r.skipped || []).length ? `, ${r.skipped.length} skipped` : ""}. Hit Train.`);
        loadVoices();
      } catch (err) { toast(err.message, true); }
    }));
    $$("#vs-list [data-rename]").forEach((b) => b.addEventListener("click", async (e) => { e.stopPropagation(); const v = state.voices.find((x) => x.id === b.dataset.rename); const n = prompt("Name this voice", v ? v.name : ""); if (n && n.trim()) { try { await api(`/api/voices/${b.dataset.rename}/rename`, { method: "POST", body: { name: n.trim() } }); toast("Renamed"); await loadVoices(); } catch (err) { toast(err.message, true); } } }));
    $$("#vs-list [data-train]").forEach((b) => b.addEventListener("click", async (e) => { e.stopPropagation(); b.disabled = true; try { await api(`/api/voices/${b.dataset.train}/train`, { method: "POST", body: {} }); toast("Training queued: about 10 minutes, the radio waits"); loadJobs(); setTimeout(loadVoices, 800); } catch (err) { toast(err.message, true); b.disabled = false; } }));
    $$("#vs-list [data-untrain]").forEach((b) => b.addEventListener("click", async (e) => { e.stopPropagation(); if (!confirm("Forget this voice's trained model?")) return; try { await api(`/api/voices/${b.dataset.untrain}/model`, { method: "DELETE" }); toast("Back to the plain sample"); loadVoices(); } catch (err) { toast(err.message, true); } }));
    $$("#vs-list [data-trainfiles]").forEach((inp) => inp.addEventListener("change", async (e) => {
      e.stopPropagation();
      const files = [...inp.files]; if (!files.length) return;
      const fd = new FormData(); files.forEach((f) => fd.append("files", f, f.name));
      toast(`Uploading ${files.length} recording${files.length === 1 ? "" : "s"}…`);
      try { const r = await fetch(`/api/voices/${inp.dataset.trainfiles}/train/files`, { method: "POST", body: fd }); if (!r.ok) throw new Error(await r.text()); const j = await r.json(); toast(`${j.added} added · ${Math.round(j.seconds)}s of recordings for training`); loadVoices(); } catch (err) { toast(err.message, true); }
    }));
  }
  if ($("#vs-audio")) { $("#vs-audio").addEventListener("ended", () => { state.voicePlaying = null; renderVoiceList(); }); $("#vs-audio").addEventListener("pause", () => { if ($("#vs-audio").ended || $("#vs-audio").currentTime === 0) { state.voicePlaying = null; renderVoiceList(); } }); }
  function nextVoiceName() { let n = state.voices.length + 1; while (state.voices.some((v) => v.name === "Voice " + n)) n++; return "Voice " + n; }
  function micAvailable() { return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder); }
  function openSheet() {
    $("#voice-sheet").classList.remove("hidden"); renderVoiceList(); positionSheet();
    const mic = micAvailable();
    $("#rec-insecure").classList.toggle("hidden", mic);
    $("#rec-btn").classList.toggle("hidden", !mic); $("#rec-time").classList.toggle("hidden", !mic);
    if (!mic) { const https = (state.status && state.status.urls && state.status.urls.https) || ""; $("#rec-https").href = https ? https + location.pathname : "#"; $("#rec-https").classList.toggle("hidden", !https); $("#rec-hint").textContent = https ? "" : "Set the off-network address in Settings to get a secure link."; }
  }
  function positionSheet() {
    // a dropdown under the Voice button (above it when the button sits low), never a sheet at the bottom of the screen
    const card = $("#voice-card"); const r = $("#btn-voice").getBoundingClientRect();
    const w = Math.min(460, window.innerWidth - 16); card.style.width = w + "px";
    const left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
    const below = window.innerHeight - r.bottom - 12; const above = r.top - 12;
    const useBelow = below >= 280 || below >= above;
    card.style.maxHeight = Math.max(200, Math.min(Math.floor(window.innerHeight * 0.72), useBelow ? below : above)) + "px";
    card.style.left = left + "px";
    if (useBelow) { card.style.top = (r.bottom + 6) + "px"; card.style.bottom = "auto"; } else { card.style.bottom = (window.innerHeight - r.top + 6) + "px"; card.style.top = "auto"; }
  }
  window.addEventListener("resize", () => { if (!$("#voice-sheet").classList.contains("hidden")) positionSheet(); });
  function closeSheet() { $("#voice-sheet").classList.add("hidden"); const a = $("#vs-audio"); if (a && state.voicePlaying) { a.pause(); a.currentTime = 0; state.voicePlaying = null; } }
  $("#btn-voice").addEventListener("click", openSheet);
  $("#vs-close").addEventListener("click", closeSheet);
  $("#voice-sheet").addEventListener("click", (e) => { if (e.target.id === "voice-sheet") closeSheet(); });
  $("#btn-voice-clear").addEventListener("click", () => { state.voiceId = null; store.set("voiceId", null); renderVoiceButton(); });
  $$("#vs-tabs .seg-btn").forEach((b) => b.addEventListener("click", () => { $$("#vs-tabs .seg-btn").forEach((x) => x.classList.toggle("on", x === b)); $$(".vs-tab").forEach((t) => t.classList.toggle("on", t.id === "vs-" + b.dataset.tab)); }));

  // recording
  $("#rec-btn").addEventListener("click", async () => {
    if (state.recorder && state.recorder.state === "recording") { state.recorder.stop(); return; }
    if (!micAvailable()) { $("#rec-insecure").classList.remove("hidden"); $("#rec-hint").textContent = "No microphone on a plain http page: use the https link or the phone recorder button."; return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: true } });
      const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"].find((m) => window.MediaRecorder.isTypeSupported(m)) || "";
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      state.recorder = rec; state.recChunks = []; state.recStart = Date.now();
      rec.ondataavailable = (e) => { if (e.data.size) state.recChunks.push(e.data); };
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        state.recBlob = new Blob(state.recChunks, { type: rec.mimeType || mime || "audio/webm" });
        $("#rec-preview").src = URL.createObjectURL(state.recBlob); $("#rec-preview").classList.remove("hidden");
        $("#rec-btn").classList.remove("on"); $("#rec-btn").textContent = "● Record again"; $("#rec-save").disabled = false;
        clearInterval(state.recTimer);
      };
      rec.start(250);
      $("#rec-btn").classList.add("on"); $("#rec-btn").textContent = "■ Stop"; $("#rec-save").disabled = true; $("#rec-hint").textContent = "";
      state.recTimer = setInterval(() => { const s = (Date.now() - state.recStart) / 1000; $("#rec-time").textContent = fmtTime(s); if (s >= 60) rec.stop(); }, 250);
    } catch (e) { $("#rec-hint").textContent = "Microphone blocked: " + e.message + ". On a phone this needs the https link."; }
  });
  $("#rec-save").addEventListener("click", async () => {
    if (!state.recBlob) return;
    const name = $("#rec-name").value.trim() || nextVoiceName();
    const ext = (state.recBlob.type || "").includes("mp4") ? "m4a" : (state.recBlob.type || "").includes("ogg") ? "ogg" : "webm";
    const fd = new FormData(); fd.append("file", state.recBlob, `voice.${ext}`); fd.append("name", name); fd.append("remove_music", "0"); fd.append("source", "record");
    await saveVoice(fd, name);
    $("#rec-save").disabled = true; $("#rec-name").value = "";
  });
  $("#rec-capture").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const name = $("#rec-name").value.trim() || nextVoiceName();
    const fd = new FormData(); fd.append("file", f, f.name || "voice.m4a"); fd.append("name", name); fd.append("remove_music", "0"); fd.append("source", "record");
    await saveVoice(fd, name); e.target.value = ""; $("#rec-name").value = "";
  });
  $("#vu-file").addEventListener("change", (e) => { const f = e.target.files[0]; state.uploadVoice = f || null; $("#vu-info").textContent = f ? `${f.name} (${Math.round(f.size / 1024)} KB)` : ""; $("#vu-save").disabled = !f; if (f && !$("#vu-name").value) $("#vu-name").value = f.name.replace(/\.[^.]+$/, ""); });
  $("#vu-save").addEventListener("click", async () => {
    if (!state.uploadVoice) return;
    const name = $("#vu-name").value.trim() || state.uploadVoice.name;
    const fd = new FormData(); fd.append("file", state.uploadVoice, state.uploadVoice.name); fd.append("name", name); fd.append("remove_music", $("#vu-remove").checked ? "1" : "0"); fd.append("source", "upload");
    await saveVoice(fd, name);
    $("#vu-file").value = ""; state.uploadVoice = null; $("#vu-save").disabled = true; $("#vu-info").textContent = "";
  });
  async function saveVoice(fd, name) {
    try {
      const r = await api("/api/voices", { method: "POST", body: fd });
      toast(`Saving voice "${name}"…`);
      state.voiceId = r.voice.id; store.set("voiceId", state.voiceId);
      await loadVoices(); loadJobs();
      $$("#vs-tabs .seg-btn")[0].click();
    } catch (e) { toast("Voice save failed: " + e.message, true); }
  }

  // ---------------------------------------------------------------- v0.4: dial, history, sleep, imports, lyric files, notifications, keys
  function dialStations() { return [{ id: "all", name: "All songs", color: "gold" }, ...state.stations]; }
  function renderDial() {
    const list = dialStations(); const r = $("#dial-range");
    r.max = Math.max(0, list.length - 1);
    const idx = Math.max(0, list.findIndex((s) => s.id === state.radioStation));
    r.value = idx;
    $("#dial-ticks").innerHTML = list.map((s, i) => `<span class="${i === idx ? "on" : ""}" style="--c:var(--${esc(s.color || "teal")})" data-i="${i}" title="${esc(s.name)}">${esc(s.name)}</span>`).join("");
    $$("#dial-ticks span").forEach((t) => t.addEventListener("click", () => dialTo(Number(t.dataset.i))));
    dialPreview(idx, false);
    if (!state.radioStation) $("#dial-name").textContent = "Tune in";
  }
  function dialPreview(i, buzz = true) {
    const s = dialStations()[i]; if (!s) return;
    $("#dial-name").textContent = s.name; $("#dial-name").style.color = `var(--${s.color || "gold"})`;
    $$("#dial-ticks span").forEach((t) => t.classList.toggle("on", Number(t.dataset.i) === i));
    if (buzz) { try { navigator.vibrate && navigator.vibrate(8); } catch {} }
  }
  let dialCommit = null;
  function dialTo(i) {
    const list = dialStations(); i = Math.max(0, Math.min(list.length - 1, i)); const s = list[i]; if (!s) return;
    $("#dial-range").value = i; dialPreview(i);
    clearTimeout(dialCommit);
    dialCommit = setTimeout(() => { if (s.id !== state.radioStation || state.queueMode !== "radio") tuneIn(s.id); }, 350);
  }
  $("#dial-range").addEventListener("input", (e) => dialPreview(Number(e.target.value)));
  $("#dial-range").addEventListener("change", (e) => dialTo(Number(e.target.value)));
  $("#dial-prev").addEventListener("click", () => dialTo(Number($("#dial-range").value) - 1));
  $("#dial-next").addEventListener("click", () => dialTo(Number($("#dial-range").value) + 1));
  $("#dial-play").addEventListener("click", () => { if (state.queueMode !== "radio" || !state.radioStation) tuneIn(state.radioStation || dialStations()[Number($("#dial-range").value)].id); else togglePlay(); });
  $("#dial-skip").addEventListener("click", nextSong);
  $("#dial-like").addEventListener("click", () => $("#p-like").click());
  (() => {  // swipe left / right on the dial face changes the station
    let x0 = null; const face = $("#dial-face");
    face.addEventListener("touchstart", (e) => { if (e.target.id === "dial-range") return; x0 = e.touches[0].clientX; }, { passive: true });
    face.addEventListener("touchend", (e) => { if (x0 === null) return; const dx = e.changedTouches[0].clientX - x0; x0 = null; if (Math.abs(dx) > 40) dialTo(Number($("#dial-range").value) + (dx < 0 ? 1 : -1)); }, { passive: true });
  })();
  function fillStationSelects() {
    const opts = state.stations.filter((s) => !isFav(s)).map((s) => `<option value="${esc(s.id)}" ${s.id === "my-songs" ? "selected" : ""}>${esc(s.name)}</option>`).join("");
    const sel = $("#imp-station"); const cur = sel.value; sel.innerHTML = opts; if (cur && state.stations.some((s) => s.id === cur)) sel.value = cur;
  }

  async function loadHistory() {
    try { state.history = await api("/api/history?limit=12"); } catch { return; }
    const el = $("#history");
    el.innerHTML = state.history.length ? state.history.map((s) => songRow(s)).join("") : '<div class="hint">Nothing played yet.</div>';
    bindSongList(el, state.history, (s) => { switchMode("library"); selectSong(s); });
  }

  function sleepOff() { state.sleepAt = null; state.stopAfter = false; state.fading = false; clearInterval(state.sleepTick); state.sleepTick = null; try { audio.volume = 1; } catch {} $("#p-sleep").classList.remove("on"); $("#p-sleep").textContent = "⏾"; }
  function sleepIn(min) {
    sleepOff(); state.sleepAt = Date.now() + min * 60000; $("#p-sleep").classList.add("on"); $("#p-sleep").textContent = min + "m";
    state.sleepTick = setInterval(() => { const left = state.sleepAt ? state.sleepAt - Date.now() : 0; if (left > 0) $("#p-sleep").textContent = Math.ceil(left / 60000) + "m"; }, 5000);
    toast("Sleep in " + min + " min");
  }
  function fadeOut() {
    const step = () => { let v = 1; try { v = audio.volume; } catch {} if (v > 0.07) { try { audio.volume = Math.max(0, v - 0.07); } catch {} setTimeout(step, 600); } else { audio.pause(); sleepOff(); toast("Good night"); } };
    step();
  }
  $("#p-sleep").addEventListener("click", (e) => { e.stopPropagation(); $("#sleep-menu").classList.toggle("hidden"); });
  $$("#sleep-menu [data-sleep]").forEach((b) => b.addEventListener("click", () => {
    const v = b.dataset.sleep; $("#sleep-menu").classList.add("hidden");
    if (v === "off") { sleepOff(); toast("Sleep timer off"); }
    else if (v === "song") { sleepOff(); state.stopAfter = true; $("#p-sleep").classList.add("on"); $("#p-sleep").textContent = "1♪"; toast("Stopping after this song"); }
    else sleepIn(Number(v));
  }));
  document.addEventListener("click", (e) => { if (!e.target.closest("#sleep-menu") && !e.target.closest("#p-sleep")) $("#sleep-menu").classList.add("hidden"); });

  $("#imp-files").addEventListener("change", async (e) => {
    const files = [...e.target.files]; if (!files.length) return;
    const st = $("#imp-station").value || "my-songs"; let ok = 0;
    for (const f of files) {
      $("#imp-progress").textContent = `Importing ${f.name} (${ok + 1} of ${files.length})…`;
      const fd = new FormData(); fd.append("file", f, f.name); fd.append("station_id", st);
      try { await api("/api/import/upload", { method: "POST", body: fd }); ok++; } catch (err) { toast(f.name + ": " + err.message, true); }
    }
    $("#imp-progress").textContent = `Imported ${ok} of ${files.length}. They are in the library and on the station, kept forever.`;
    e.target.value = ""; loadLibrary(); loadStations();
  });
  $("#imp-scan").addEventListener("click", async () => {
    const path = $("#imp-path").value.trim();
    if (!path) return toast("Type a folder this computer can see, like E:\\Music", true);
    try { await api("/api/import/folder", { method: "POST", body: { path, station_id: $("#imp-station").value || "my-songs" } }); pollImport(); } catch (err) { toast(err.message, true); }
  });
  async function pollImport() {
    clearTimeout(state.impTimer);
    let p; try { p = await api("/api/import/status"); } catch { return; }
    renderImport(p);
    if (p.running) state.impTimer = setTimeout(pollImport, 2000); else if (p.total !== undefined) { loadLibrary(); loadStations(); }
  }
  function renderImport(p) {
    const el = $("#imp-progress"); if (!p || p.total === undefined) return;
    el.innerHTML = `${p.running ? "Importing" : "Done"}: ${p.done} of ${p.total} · ${p.imported} new · ${p.matched_lyrics} with lyrics · ${p.duplicates} already in · ${p.failed} failed${p.current ? " · " + esc(p.current) : ""}${p.errors && p.errors.length ? `<div class="hint">${esc(p.errors.slice(-3).join(" | "))}</div>` : ""}`;
  }

  async function loadLyricFolders() {
    let folders; try { folders = await api("/api/lyrics/folders"); } catch { return; }
    const sel = $("#lf-folder"); const cur = sel.value || state.lyricFolder;
    sel.innerHTML = folders.map((f) => `<option value="${esc(f.folder)}">${esc(f.folder)} (${f.files - f.used} not sung of ${f.files})</option>`).join("");
    sel.value = folders.some((f) => f.folder === cur) ? cur : ((folders[0] || {}).folder || "");
    loadLyricFiles();
  }
  async function loadLyricFiles() {
    const folder = $("#lf-folder").value; if (!folder) return;
    state.lyricFolder = folder; store.set("lyricFolder", folder);
    let items; try { items = await api(`/api/lyrics/folders/${encodeURIComponent(folder)}`); } catch { return; }
    const q = $("#lf-search").value.trim().toLowerCase();
    items = items.filter((i) => !q || i.title.toLowerCase().includes(q));
    $("#lf-list").innerHTML = items.length ? items.map((i) => `<div class="lfrow ${i.used ? "used" : ""}" data-file="${esc(i.file)}"><div class="grow"><b>${esc(i.title)}</b><small>${i.chars} chars · ${i.used ? "sung" : "not sung yet"}</small></div><button class="mini" data-lfsing="${esc(i.file)}" title="Queue this one ahead of the auto stock">Sing next</button><button class="mini ghost danger" data-lfdel="${esc(i.file)}" title="Delete the lyric file">✕</button></div>`).join("") : '<div class="hint">No lyric files here.</div>';
    $$("#lf-list .lfrow").forEach((r) => r.addEventListener("click", (e) => { if (e.target.dataset.lfsing || e.target.dataset.lfdel) return; viewLyricFile(folder, r.dataset.file); }));
    $$("#lf-list [data-lfsing]").forEach((b) => b.addEventListener("click", async () => { b.disabled = true; try { const r = await api(`/api/lyrics/folders/${encodeURIComponent(folder)}/${encodeURIComponent(b.dataset.lfsing)}/render`, { method: "POST", body: {} }); toast("Queued ahead of the line: " + r.title); loadJobs(); } catch (err) { toast(err.message, true); } b.disabled = false; }));
    $$("#lf-list [data-lfdel]").forEach((b) => b.addEventListener("click", async () => { if (!confirm("Delete this lyric file? Songs already made from it stay.")) return; try { await api(`/api/lyrics/folders/${encodeURIComponent(folder)}/${encodeURIComponent(b.dataset.lfdel)}`, { method: "DELETE" }); loadLyricFolders(); } catch (err) { toast(err.message, true); } }));
  }
  async function viewLyricFile(folder, file) {
    let d; try { d = await api(`/api/lyrics/folders/${encodeURIComponent(folder)}/${encodeURIComponent(file)}`); } catch (err) { return toast(err.message, true); }
    $("#lf-detail").innerHTML = `<div class="now"><div class="nt">${esc(d.title)}</div><div class="nm">${esc(file)} · ${d.used ? "sung" : "not sung yet"}</div></div>
      ${d.songs.length ? `<div class="hint">Songs made from it:</div><div class="songlist compact" id="lf-songs">${d.songs.map((s) => songRow(s)).join("")}</div>` : ""}
      <pre class="lyrics-view">${esc(d.lyrics)}</pre>`;
    if (d.songs.length) bindSongList($("#lf-songs"), d.songs, selectSong);
  }
  $("#lf-folder").addEventListener("change", loadLyricFiles);
  $("#lf-search").addEventListener("input", () => { clearTimeout(state.lfTimer); state.lfTimer = setTimeout(loadLyricFiles, 250); });
  $("#lf-refresh").addEventListener("click", loadLyricFolders);

  // ---------------------------------------------------------------- a new channel filling up (0.9.7)
  /** Asked for on a click, because a browser will not hand out permission any other way. */
  function askNotify() {
    try { if ("Notification" in window && Notification.permission === "default") Notification.requestPermission(); } catch {}
  }
  function keepShort(list, key, v) { if (!list.includes(v)) { list.push(v); while (list.length > 30) list.shift(); store.set(key, list); } }
  function aboutMins(s) {
    if (!s || s < 45) return "under a minute";
    const m = Math.round(s / 60);
    return m <= 1 ? "about a minute" : "about " + m + " minutes";
  }
  /**
   * While a channel makes its first songs the server plans nothing else, so the sensible thing to do is
   * listen to something else. The card says so, and says again when the channel is ready.
   */
  function renderBuilding(b) {
    const el = $("#building");
    if (!el) return;
    const build = (b && b.stations) || [];
    const done = ((b && b.ready) || []).filter((r) => !state.primeGone.includes(r.id + ":" + Math.round(r.at)));
    for (const r of (b && b.ready) || []) {
      const key = r.id + ":" + Math.round(r.at);
      if (state.primeSaid.includes(key)) continue;
      keepShort(state.primeSaid, "primeSaid", key);
      toast(r.name + " is ready — " + r.songs + " song" + (r.songs === 1 ? "" : "s"));
      if ("Notification" in window && Notification.permission === "granted") {
        try { new Notification("Ten Forward", { body: r.name + " has made its first " + r.songs + " songs. Tune in whenever you like.", icon: "/static/icon-512.png", tag: "built:" + r.id }); } catch {}
      }
    }
    if (!build.length && !done.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }
    el.classList.remove("hidden");
    el.innerHTML = build.map((s) => {
      const pct = Math.round((s.progress || 0) * 100);
      const cook = s.next_up && s.next_up.status === "running" ? ` · making “${esc(s.next_up.title || "one more")}”` : "";
      return `<div class="bcard" style="border-left-color:var(--${esc(s.color || "gold")})">
        <div class="bhead"><b>Building ${esc(s.name)}</b><span>${s.ready} of ${s.target} songs · ${aboutMins(s.eta_s)} left${cook}</span></div>
        <div class="bar"><i style="width:${pct}%"></i></div>
        <div class="bmsg">Nothing else is being made until it has its first ${s.target} songs. Put another channel on meanwhile — it keeps playing, and you will be told here when this one is ready.</div>
        <div class="row wrap"><button class="mini" data-bdial="1">Pick another channel</button></div></div>`;
    }).join("") + done.map((r) => `<div class="bcard done" style="border-left-color:var(--${esc(r.color || "green")})">
        <div class="bhead"><b>${esc(r.name)} is ready</b><span>${r.songs} song${r.songs === 1 ? "" : "s"} · it keeps making more now</span></div>
        <div class="bmsg">It finished its first songs. Have a listen.</div>
        <div class="row wrap"><button class="mini" data-btune="${esc(r.id)}">Tune in</button><button class="mini ghost" data-bhide="${esc(r.id)}:${Math.round(r.at)}">Dismiss</button></div></div>`).join("");
    $$("#building [data-bdial]").forEach((x) => x.addEventListener("click", () => switchMode("dial")));
    $$("#building [data-btune]").forEach((x) => x.addEventListener("click", () => {
      const r = done.find((d) => d.id === x.dataset.btune);
      if (r) keepShort(state.primeGone, "primeGone", r.id + ":" + Math.round(r.at));
      switchMode("dial"); tuneIn(x.dataset.btune);
    }));
    $$("#building [data-bhide]").forEach((x) => x.addEventListener("click", () => { keepShort(state.primeGone, "primeGone", x.dataset.bhide); renderBuilding(state.status && state.status.building); }));
  }

  function notifyReady(j) {
    const title = j.message || "Song ready";
    toast("Ready: " + title);
    if ("Notification" in window && Notification.permission === "granted" && document.hidden) { try { new Notification("Ten Forward", { body: title, icon: "/static/icon-512.png", tag: j.id }); } catch {} }
  }

  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (e.code === "Space") { e.preventDefault(); togglePlay(); }
    else if (k === "n") nextSong();
    else if (k === "h") $("#p-like").click();
    else if (k === "b" && state.queueMode === "radio" && $("#now-ban")) $("#now-ban").click();
    else if (k === "arrowright") { if (audio.duration) audio.currentTime = Math.min(audio.duration, audio.currentTime + 10); }
    else if (k === "arrowleft") audio.currentTime = Math.max(0, audio.currentTime - 10);
    else if ((k === "arrowup" || k === "arrowdown") && state.mode === "dial") { e.preventDefault(); dialTo(Number($("#dial-range").value) + (k === "arrowdown" ? 1 : -1)); }
    else if (k.length === 1 && "123456".includes(k)) switchMode(["create", "dial", "channels", "library", "queue", "settings"][Number(k) - 1]);
    else if (k === "escape") { closeSheet(); closeSongMenu(); $("#sleep-menu").classList.add("hidden"); $("#queue-menu").classList.add("hidden"); }
  });

  // ---------------------------------------------------------------- queue tab (never touches the player)
  function who(j) { return `<span class="who ${esc(j.requested_by)}">${j.requested_by === "auto" ? "auto stock" : j.requested_by === "you" ? "you" : "studio"}</span>`; }
  function stchip(j) { return j.station ? `<span class="stchip" style="border-color:var(--${esc(j.color || "teal")})">${esc(j.station)}</span>` : ""; }
  async function loadQueue() {
    clearTimeout(state.queueTimer);
    let q; try { q = await api("/api/queue"); } catch { state.queueTimer = setTimeout(loadQueue, 5000); return; }
    $("#q-pause").classList.toggle("on", !!q.queue_paused); $("#q-pause").textContent = q.queue_paused ? "Paused: resume" : "Pause queue";
    $("#q-autofill").classList.toggle("on", q.radio_autofill !== false);
    const r = q.running;
    if (r) {
      const pct = Math.round((r.progress || 0) * 100); const el = Math.round((q.time - (r.started || q.time)) / 60);
      $("#q-now").innerHTML = `<div class="qt"><span>${esc(r.title)} ${stchip(r)} ${who(r)}</span><span>${pct}%</span></div><div class="qs">${esc(r.style || r.type)}</div>
        <div class="bar"><i style="width:${pct}%"></i></div><div class="qm">${esc(r.message || "")}${el ? " · " + el + " min in" : ""}</div>
        <div class="row wrap" style="margin-top:8px"><button class="mini ghost danger" data-qcancel="${r.id}">Stop this one, start the next</button></div>`;
    } else {
      $("#q-now").innerHTML = `<div class="hint">${q.power === false ? "Power is off: nothing renders until it is back on." : q.queue_paused ? "Paused. Songs wait here until you resume." : q.queued.length ? "Starting the next one…" : "Nothing is being made. The stations are stocked."}</div>`;
    }
    $("#q-count").textContent = q.queued.length ? q.queued.length + " waiting · about " + Math.round(q.queued.length * 3) + " min" : "";
    $("#q-list").innerHTML = q.queued.length ? q.queued.map((j, i) => `<div class="qrow queued"><div class="n">${i + 1}</div><div><div class="t">${esc(j.title)} ${stchip(j)} ${who(j)}</div><div class="s">${esc(j.style || j.type)}</div></div>
        <div class="acts">${i > 0 ? `<button class="mini ghost" data-qbump="${j.id}" title="Move to the front">Next</button>` : ""}${i < q.queued.length - 1 ? `<button class="mini ghost" data-qlater="${j.id}" title="Move to the back">Later</button>` : ""}<button class="mini ghost danger" data-qcancel="${j.id}">Remove</button></div></div>`).join("") : '<div class="hint">Queue is empty.</div>';
    $("#q-recent").innerHTML = q.recent.length ? q.recent.map((j) => `<div class="qrow ${esc(j.status)}"><div class="n">${j.status === "done" ? "✓" : j.status === "failed" ? "!" : "–"}</div><div><div class="t">${esc(j.title)} ${stchip(j)} ${who(j)}</div><div class="s">${esc(j.message || j.status)}</div></div>
        <div class="acts">${j.status === "done" && j.song_id ? `<button class="mini" data-qplay="${j.song_id}">Play</button>` : ""}</div></div>`).join("") : '<div class="hint">Nothing finished yet.</div>';
    $$("#mode-queue [data-qcancel]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/api/jobs/${b.dataset.qcancel}/cancel`, { method: "POST" }); toast("Removed from the line"); loadQueue(); loadJobs(); } catch (e) { toast(e.message, true); } }));
    $$("#mode-queue [data-qbump]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/api/jobs/${b.dataset.qbump}/bump`, { method: "POST" }); loadQueue(); } catch (e) { toast(e.message, true); } }));
    $$("#mode-queue [data-qlater]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/api/jobs/${b.dataset.qlater}/later`, { method: "POST" }); loadQueue(); } catch (e) { toast(e.message, true); } }));
    $$("#mode-queue [data-qplay]").forEach((b) => b.addEventListener("click", async () => { try { const s = await api(`/api/songs/${b.dataset.qplay}`); state.queueMode = "library"; playSong(s); } catch (e) { toast(e.message, true); } }));
    state.queueTimer = setTimeout(loadQueue, r ? 2500 : 6000);
  }
  $("#q-pause").addEventListener("click", async () => { const on = !$("#q-pause").classList.contains("on"); try { await api("/api/settings", { method: "POST", body: { queue_paused: on } }); toast(on ? "Queue paused after the current song" : "Queue running"); loadQueue(); } catch (e) { toast(e.message, true); } });
  $("#q-autofill").addEventListener("click", async () => { const on = !$("#q-autofill").classList.contains("on"); try { await api("/api/settings", { method: "POST", body: { radio_autofill: on } }); $("#btn-autofill").classList.toggle("on", on); toast(on ? "Stations will restock themselves" : "Auto stock off"); loadQueue(); } catch (e) { toast(e.message, true); } });
  $("#q-clear").addEventListener("click", async () => { try { await api("/api/jobs/clear", { method: "POST" }); loadQueue(); loadJobs(); } catch (e) { toast(e.message, true); } });

  // ---------------------------------------------------------------- systems
  async function loadSystems() {
    const s = state.status || {};
    const e = s.engine || {};
    $("#sys-engine").innerHTML = `<b>YuE2</b><span>${e.loaded ? (e.busy ? "busy" : "loaded, idle") : "not loaded (loads on first job, about 30 s)"}</span>
      <b>VRAM free</b><span>${e.vram && e.vram.free_mb != null ? Math.round(e.vram.free_mb / 1024 * 10) / 10 + " GB of " + Math.round(e.vram.total_mb / 1024) + " GB" : "unknown until loaded"}</span>
      <b>Power</b><span>${s.power === false ? "OFF: GPU released, jobs held, radio plays existing songs" : "ON"}</span>
      <b>GPU in use</b><span>${s.gpu_used_mb != null ? Math.round(s.gpu_used_mb / 1024 * 10) / 10 + " GB total (all apps)" : "unknown"}</span>
      <b>ComfyUI</b><span>${e.comfy_busy ? "rendering (we wait)" : "idle"}</span>
      <b>LM engine</b><span>${e.mode || "default (CUDA graphs)"}</span>
      <b>Queue</b><span>${(s.worker && s.worker.queued) || 0} waiting${s.worker && s.worker.current ? ", running: " + esc(s.worker.current.type) : ""}</span>
      <b>Hearted songs</b><span>${(s.counts && s.counts.liked) || 0}${s.songs_dir ? " saved as files in " + esc(s.songs_dir) : ""}</span>
      <b>Radio retention</b><span>unhearted radio songs are deleted after ${s.retention_days || 7} days, banned ones after ${s.banned_retention_h || 24} h</span>
      <b>Version</b><span>${esc(s.version || "")}</span>`;
    try { const h = await api("/api/llm/health"); $("#sys-llm").innerHTML = h.ok ? `<b>Status</b><span>online</span><b>Model</b><span>${esc(h.preferred || (h.models || [])[0] || "")}</span>` : `<b>Status</b><span>offline: ${esc(h.error)}</span>`; } catch {}
    $("#sys-voices").innerHTML = state.voices.map((v) => `<div class="item"><div class="grow"><b>${esc(v.name)}</b><small>${v.ready ? fmtTime(v.duration_s) + " sample · " + esc(v.source) : "processing"}</small></div><button class="mini ghost" data-vren="${v.id}">Rename</button><button class="mini ghost danger" data-vdel="${v.id}">Delete</button></div>`).join("") || '<div class="hint">No voices saved yet. Use the Voice button in Create.</div>';
    $$("#sys-voices [data-vren]").forEach((b) => b.addEventListener("click", async () => { const v = state.voices.find((x) => x.id === b.dataset.vren); const n = prompt("Voice name", v.name); if (n) { await api(`/api/voices/${v.id}/rename`, { method: "POST", body: { name: n } }); await loadVoices(); loadSystems(); } }));
    $$("#sys-voices [data-vdel]").forEach((b) => b.addEventListener("click", async () => { if (confirm("Delete this voice?")) { await api(`/api/voices/${b.dataset.vdel}`, { method: "DELETE" }); await loadVoices(); loadSystems(); } }));
    try {
      const folders = await api("/api/lyrics/folders");
      $("#sys-lyrics").innerHTML = folders.map((f) => `<div class="item"><div class="grow"><b>${esc(f.folder)}</b><small>${f.files} files · ${f.used} used</small></div><label class="filebtn mini">Add .txt<input type="file" accept=".txt,.md" data-folder="${esc(f.folder)}"></label></div>`).join("");
      $$("#sys-lyrics input[type=file]").forEach((inp) => inp.addEventListener("change", async (e) => { const f = e.target.files[0]; if (!f) return; const text = await f.text(); await api(`/api/lyrics/folders/${encodeURIComponent(inp.dataset.folder)}`, { method: "POST", body: { title: f.name.replace(/\.[^.]+$/, ""), lyrics: text } }); toast("Added to " + inp.dataset.folder); loadSystems(); }));
    } catch {}
    $("#sys-loras").innerHTML = state.loras.map((l) => `<div class="item"><div class="grow"><b>${esc(l.name)}</b><small>${esc(l.kind)} · ${l.size_mb} MB · ${l.pairs} weight pairs${l.unknown_keys ? " · " + l.unknown_keys + " unmapped keys" : ""}</small><small>${esc(l.blurb)}</small>${l.source ? `<a href="${esc(l.source)}" target="_blank" rel="noreferrer">source</a>` : ""}</div></div>`).join("") || '<div class="hint">No LoRAs.</div>';
  }
  $("#btn-power").addEventListener("click", async () => {
    const b = $("#btn-power"); const on = !b.classList.contains("on");
    if (!on && !confirm("Power OFF: hold every music job, stop a radio song that is rendering, and release the GPU for other apps?")) return;
    b.classList.add("busy");
    try {
      const r = await api("/api/power", { method: "POST", body: { on } });
      toast(on ? "Power ON: reclaiming the graphics card" + (r.comfy_freed ? ", ComfyUI dropped its models" : "") + (r.comfy_busy ? " (ComfyUI is rendering, we wait for it)" : "") : "Power OFF: releasing the GPU" + (r.cancelled_job ? ", stopped the radio render" : ""));
      await loadStatus();
    } catch (e) { toast(e.message, true); }
    b.classList.remove("busy");
  });
  $("#btn-unload").addEventListener("click", async () => { try { await api("/api/engine/unload", { method: "POST" }); toast("GPU released"); loadStatus(); } catch (e) { toast(e.message, true); } });

  // ---------------------------------------------------------------- who is listening (0.9)
  function applyFeatures(f) {
    if (!f) return;
    state.features = f;
    document.body.dataset.voices = f.voices === false ? "off" : "on";
    document.body.dataset.personal = f.personal === false ? "off" : "on";
  }
  function renderUser() {
    const c = $("#chip-user");
    if (!c) return;
    c.classList.toggle("hidden", !state.user);
    if (state.user) c.textContent = String(state.user.name).toUpperCase();
  }
  $("#chip-user").addEventListener("click", () => switchMode("settings"));

  let loginOpen = false;
  async function showLogin() {
    if (loginOpen) return;
    loginOpen = true;
    const sheet = $("#login-sheet");
    sheet.classList.remove("hidden");
    $("#login-pw-row").classList.add("hidden");
    let users = [];
    try { const r = await (await fetch("/api/auth/users-public")).json(); users = r.users || []; } catch {}
    if (!users.length) users = [{ name: "admin", needs_password: false }];
    $("#login-list").innerHTML = users.map((u) => `<button class="mini big" data-login="${esc(u.name)}" data-pw="${u.needs_password ? 1 : 0}">${esc(u.name)}${u.needs_password ? " 🔒" : ""}</button>`).join("");
    $$("#login-list [data-login]").forEach((b) => b.addEventListener("click", () => {
      const name = b.dataset.login;
      if (b.dataset.pw === "1") {
        $("#login-who").textContent = name; $("#login-go").dataset.name = name;
        $("#login-pw-row").classList.remove("hidden"); $("#login-pw").value = ""; $("#login-pw").focus();
      } else signIn(name, null);
    }));
  }
  async function signIn(name, password) {
    try {
      const r = await api("/api/auth/login", { method: "POST", body: { name, password, device: navigator.userAgent.slice(0, 90) } });
      state.user = r.user; loginOpen = false;
      $("#login-sheet").classList.add("hidden");
      renderUser(); toast(`Hello ${r.user.name}`);
      if (!state.booted) boot(); else { loadStatus(); if (state.mode === "settings") loadSettings(); }
    } catch (e) { $("#login-msg").textContent = e.message; }
  }
  $("#login-go").addEventListener("click", () => signIn($("#login-go").dataset.name, $("#login-pw").value));
  $("#login-pw").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#login-go").click(); });
  $("#login-back").addEventListener("click", () => $("#login-pw-row").classList.add("hidden"));

  // ---------------------------------------------------------------- settings tab (0.9)
  function setRow(x) {
    let ctl;
    if (x.type === "bool") ctl = `<label class="check"><input type="checkbox" data-key="${x.key}"${x.value ? " checked" : ""}> ${x.value ? "on" : "off"}</label>`;
    else if (x.type === "int" || x.type === "float") ctl = `<input type="number" step="${x.type === "float" ? "any" : "1"}" data-key="${x.key}" value="${esc(x.value)}">`;
    else if (x.type === "choice") ctl = `<select data-key="${x.key}">${(x.choices || []).map((c) => `<option value="${esc(c)}"${c === x.value ? " selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
    else ctl = `<input type="text" data-key="${x.key}" value="${esc(x.value)}" spellcheck="false">`;
    const reset = x.source !== "default" ? `<button class="mini ghost" data-reset="${x.key}" title="Back to ${esc(x.default)}">Default</button>` : "";
    return `<div class="setrow" data-row="${x.key}"><div class="setlbl"><b>${esc(x.label)}</b>${x.help ? `<small>${esc(x.help)}</small>` : ""}${x.restart ? '<small class="warn">Takes effect the next time the server starts</small>' : ""}</div><div class="setctl">${ctl}${reset}</div></div>`;
  }
  function groupExtra(key) {
    if (key === "songs") return '<div class="setnote">Changing the folder leaves the songs that were already copied where they are. <button class="mini ghost" id="set-reexport">Copy hearted songs there now</button></div>';
    if (key === "server") return '<div class="setnote">These two addresses are what you type into the phone app. Leave the second one empty if you do not use Tailscale.</div>';
    if (key === "features") return '<div class="setnote">Switching one off hides that part of the page as well.</div>';
    if (key === "lyrics") return `<div class="setnote">The radio reads back its own last songs before it writes the next one, so it stops reaching for the same
      objects, phrases and rhymes. Learning from real songs and covers both need the internet and are off until you switch them on.</div>
      <div class="setrow"><div class="setlbl"><b>What it keeps reaching for</b><small>The last songs on a channel, or on the whole dial.</small></div>
      <div class="setctl"><select id="hab-station"></select><button class="mini ghost" id="hab-refresh">Look</button></div></div>
      <div id="hab-out" class="hint">Pick a channel and press Look.</div>`;
    return "";
  }
  async function loadSettings() {
    loadSystems();
    let s;
    try { s = await api("/api/settings"); } catch (e) { $("#set-groups").innerHTML = `<div class="hint">${esc(e.message)}</div>`; return; }
    state.settings = s; applyFeatures(s.features);
    $("#set-who").textContent = s.admin ? "" : "Only an admin can change these.";
    const byGroup = {};
    (s.settings || []).forEach((x) => { (byGroup[x.group] = byGroup[x.group] || []).push(x); });
    $("#set-groups").innerHTML = (s.groups || []).map((g) => {
      const rows = byGroup[g.key] || [];
      if (!rows.length) return "";
      return `<div class="setgroup"><h4>${esc(g.label)}</h4>${rows.map(setRow).join("")}${groupExtra(g.key)}</div>`;
    }).join("");
    if (!s.admin) $$("#set-groups input, #set-groups select, #set-groups button").forEach((el) => { el.disabled = true; });
    bindSettings(s);
    renderAppBlock(s);
    renderLinks(s);
    loadUsers();
    loadProblems(s);
  }
  /** What the phones and browsers have reported lately. The server keeps two days of it. */
  async function loadProblems(s) {
    const box = $("#set-problems");
    if (!box) return;
    if (!s.admin) { box.innerHTML = `<div class="hint">An admin can see what the phones have reported.</div>`; return; }
    box.innerHTML = `<div class="hint">Looking…</div>`;
    try {
      const r = await api("/api/client/log?limit=60");
      const rows = r.entries || [];
      if (!rows.length) { box.innerHTML = `<div class="hint">Nothing reported in the last ${r.keep_days} days. Quiet is good.</div>`; return; }
      box.innerHTML = rows.map((e) => {
        const when = e.at ? new Date(e.at * 1000).toLocaleString() : "";
        const what = e.message || e.event || e.tag || "problem";
        const who = [e.source || "", e.device || "", e.who || "", e.app_version || ""].filter(Boolean).join(" · ");
        const more = [e.title, e.detail].filter(Boolean).join(" — ");
        return `<div class="item"><div class="grow"><b>${esc(String(what).slice(0, 120))}</b>
          <small>${esc(when)}${who ? " · " + esc(who) : ""}</small>
          ${more ? `<small>${esc(String(more).slice(0, 200))}</small>` : ""}</div></div>`;
      }).join("") + `<div class="hint">Kept for ${r.keep_days} days, here and on the phone.</div>`;
    } catch (e) {
      box.innerHTML = `<div class="hint">${esc(e.message || "could not read the problems")}</div>`;
    }
    const btn = $("#btn-problems-refresh");
    if (btn && !btn.dataset.bound) { btn.dataset.bound = "1"; btn.addEventListener("click", () => loadProblems(s)); }
  }
  /** The channel's own habits, read back from the songs it has written. */
  async function loadHabits(station) {
    const out = $("#hab-out");
    if (!out) return;
    out.innerHTML = `<div class="hint">Reading…</div>`;
    try {
      const r = await api(`/api/lyrics/habits${station ? "?station=" + encodeURIComponent(station) : ""}`);
      const h = r.habits || {}, m = r.memory || {};
      if (!h.songs) { out.innerHTML = `<div class="hint">Nothing written there yet.</div>`; return; }
      const row = (label, items, fmt) => items && items.length
        ? `<div class="item"><div class="grow"><b>${label}</b><small>${items.map(fmt).join(" · ")}</small></div></div>` : "";
      out.innerHTML =
        row("Words it keeps using", h.words, (x) => `${esc(x.word)} ${Math.round(x.share * 100)}%`) +
        row("Phrases written more than once", h.phrases, (x) => `"${esc(x.phrase)}" ×${x.songs}`) +
        row("Rhymes it leans on", h.rhymes, (x) => `${esc(x.pair)} ×${x.songs}`) +
        row("Where its pictures come from", h.buckets, (x) => `${esc(x.bucket)} ${Math.round(x.share * 100)}%`) +
        row("How songs open", h.openings, (x) => `${esc(x.kind)} ×${x.songs}`) +
        row("Who is speaking", h.perspectives, (x) => `${esc(x.kind)} ×${x.songs}`) +
        `<div class="hint">Over the last ${h.songs} song(s). Memory is ${esc(m.level || "?")}; ${m.remembered || 0} songs measured in all.</div>`;
    } catch (e) { out.innerHTML = `<div class="hint">${esc(e.message || "could not read it")}</div>`; }
  }
  function bindHabits() {
    const sel = $("#hab-station");
    if (!sel || sel.dataset.bound) return;
    sel.dataset.bound = "1";
    sel.innerHTML = `<option value="">every channel</option>` +
      (state.stations || []).map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("");
    $("#hab-refresh").addEventListener("click", () => loadHabits(sel.value));
    sel.addEventListener("change", () => loadHabits(sel.value));
  }
  function bindSettings(s) {
    bindHabits();
    $$("#set-groups [data-key]").forEach((el) => el.addEventListener("change", async () => {
      const key = el.dataset.key;
      const row = $(`.setrow[data-row="${key}"]`);
      const value = el.type === "checkbox" ? el.checked : el.value;
      row.classList.add("saving");
      try {
        const r = await api("/api/settings", { method: "POST", body: { [key]: value } });
        row.classList.remove("saving"); row.classList.add("saved");
        if (r[key] !== undefined && el.type !== "checkbox") el.value = r[key];
        setTimeout(() => row.classList.remove("saved"), 1500);
        toast("Saved");
        loadStatus();
      } catch (e) { row.classList.remove("saving"); toast(e.message, true); loadSettings(); }
    }));
    $$("#set-groups [data-reset]").forEach((b) => b.addEventListener("click", async () => {
      try { await api("/api/settings/reset", { method: "POST", body: { key: b.dataset.reset } }); toast("Back to the default"); loadSettings(); } catch (e) { toast(e.message, true); }
    }));
    const rx = $("#set-reexport");
    if (rx && s.admin) rx.addEventListener("click", async () => {
      rx.disabled = true;
      try { const r = await api("/api/settings/reexport", { method: "POST" }); toast(`${r.exported} hearted song(s) copied to ${r.folder}`); } catch (e) { toast(e.message, true); }
      rx.disabled = false;
    });
  }
  function renderAppBlock(s) {
    const lan = (s.urls || {}).lan || location.origin;
    const tail = (s.urls || {}).https || "";
    $("#set-app").innerHTML = `<a class="board" href="/app/TenForward.apk" download>${s.apk ? "DOWNLOAD THE ANDROID APP" : "ANDROID APP NOT BUILT YET"}</a>
      <div class="item"><div class="grow"><b>On your own wifi</b><small>${esc(lan)}</small></div></div>
      <div class="item"><div class="grow"><b>Away from home</b><small>${esc(tail || "not set — fill in the Tailscale address under Addresses")}</small></div></div>
      <div class="hint">Install it, open its settings and type those two addresses in: it uses whichever one answers. Channels are added and edited here on the web page; the app only switches between them.</div>`;
  }
  function renderLinks(s) {
    const box = $("#set-links");
    if (!box) return;
    const mine = ((s.settings || []).find((x) => x.key === "links") || {}).value || "";
    const rows = String(mine).split(",").map((p) => p.trim()).filter(Boolean).map((p) => {
      const i = p.indexOf("|");
      const name = i > 0 ? p.slice(0, i).trim() : p;
      const url = i > 0 ? p.slice(i + 1).trim() : p;
      return `<a class="board" href="${esc(url)}" target="_blank" rel="noreferrer">${esc(name.toUpperCase())}</a>`;
    }).join("");
    box.innerHTML = rows + `<a class="board" href="https://huggingface.co/m-a-p/YuE2-3B" target="_blank" rel="noreferrer">YUE2 MODEL CARD</a>`;
  }
  async function loadUsers() {
    const me = state.user || {};
    const admin = !!(state.settings && state.settings.admin);
    $("#set-adduser").classList.toggle("hidden", !admin);
    if (!admin) { $("#set-users").innerHTML = `<div class="hint">Signed in as ${esc(me.name || "?")}.</div>`; return; }
    let users = [];
    try { users = await api("/api/users"); } catch (e) { $("#set-users").innerHTML = `<div class="hint">${esc(e.message)}</div>`; return; }
    $("#set-users").innerHTML = users.map((u) => `<div class="item uitem"><div class="grow"><b>${esc(u.name)}</b> <span class="role">${esc(u.role)}</span><small>${u.has_password ? "password set" : "no password"}${u.last_seen ? " · last here " + new Date(u.last_seen * 1000).toLocaleDateString() : ""}${u.sessions ? " · " + u.sessions + " device(s) signed in" : ""}</small></div>
      <button class="mini ghost" data-upw="${u.id}">${u.has_password ? "Change password" : "Set password"}</button>${u.has_password ? `<button class="mini ghost" data-unopw="${u.id}">Remove password</button>` : ""}
      <button class="mini ghost" data-urole="${u.id}" data-role="${u.role === "admin" ? "user" : "admin"}">Make ${u.role === "admin" ? "listener" : "admin"}</button>${u.id === me.id ? "" : `<button class="mini ghost danger" data-udel="${u.id}">Remove</button>`}</div>`).join("");
    $$("#set-users [data-upw]").forEach((b) => b.addEventListener("click", async () => {
      const pw = prompt("Password for this person (leave empty to remove it)");
      if (pw === null) return;
      try { await api(`/api/users/${b.dataset.upw}/password`, { method: "POST", body: { password: pw || null } }); toast(pw ? "Password set" : "Password removed"); loadUsers(); } catch (e) { toast(e.message, true); }
    }));
    $$("#set-users [data-unopw]").forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/api/users/${b.dataset.unopw}/password`, { method: "POST", body: { password: null } }); toast("Password removed"); loadUsers(); } catch (e) { toast(e.message, true); }
    }));
    $$("#set-users [data-urole]").forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/api/users/${b.dataset.urole}/role`, { method: "POST", body: { role: b.dataset.role } }); loadUsers(); } catch (e) { toast(e.message, true); }
    }));
    $$("#set-users [data-udel]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm("Remove this person? Their devices are signed out.")) return;
      try { await api(`/api/users/${b.dataset.udel}`, { method: "DELETE" }); toast("Removed"); loadUsers(); } catch (e) { toast(e.message, true); }
    }));
  }
  $("#su-add").addEventListener("click", async () => {
    const name = $("#su-name").value.trim();
    if (!name) return toast("A name first", true);
    try {
      await api("/api/users", { method: "POST", body: { name, password: $("#su-pass").value || null, role: $("#su-role").value } });
      $("#su-name").value = ""; $("#su-pass").value = "";
      toast(`${name} can listen now`); loadUsers(); loadStatus();
    } catch (e) { toast(e.message, true); }
  });
  $("#su-mypass").addEventListener("click", async () => {
    if (!state.user) return;
    const pw = prompt("Your new password (leave empty to remove it)");
    if (pw === null) return;
    try { await api(`/api/users/${state.user.id}/password`, { method: "POST", body: { password: pw || null } }); toast(pw ? "Password set" : "Password removed"); loadUsers(); } catch (e) { toast(e.message, true); }
  });
  $("#su-signout").addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST" }); } catch {}
    location.reload();
  });
  $("#set-reload").addEventListener("click", () => loadSettings());
  $("#btn-llm-test").addEventListener("click", async () => {
    const b = $("#btn-llm-test"); b.disabled = true;
    try { const h = await api("/api/llm/health"); toast(h.ok ? `The writer answers: ${h.preferred || (h.models || [])[0] || ""}` : `The writer is offline: ${h.error}`, !h.ok); } catch (e) { toast(e.message, true); }
    b.disabled = false; loadSystems();
  });

  // ---------------------------------------------------------------- status polling
  async function loadStatus() {
    try { state.status = await api("/api/status"); } catch { $("#chip-engine").textContent = "SERVER OFFLINE"; $("#chip-engine").className = "chip r"; return; }
    applyFeatures(state.status.features);
    if (state.status.needs_login) { showLogin(); return; }
    renderBuilding(state.status.building);
    if (!state.queueDrawn) { state.queueDrawn = true; migrateOldQueue().then(loadQueue); }
    if (state.status.user) { state.user = state.status.user; renderUser(); }
    if (state.status.header_tagline && $("#tagline")) $("#tagline").textContent = state.status.header_tagline;
    const e = state.status.engine; const w = state.status.worker;
    if (state.status.ui_build) {
      if (!state.uiBuild) state.uiBuild = state.status.ui_build;
      else if (state.uiBuild !== state.status.ui_build) {
        state.reloadPending = true;
        if (audio.paused || !state.playing) { toast("Updating Ten Forward…"); setTimeout(() => location.reload(), 800); }
      }
    }
    const chip = $("#chip-engine");
    $("#btn-power").classList.toggle("on", state.status.power !== false);
    $("#btn-power").textContent = state.status.power === false ? "⏻ OFF" : "⏻ ON";
    if (state.status.power === false) { chip.textContent = "GPU RELEASED"; chip.className = "chip r"; }
    else if (e.busy) { chip.textContent = "ENGINE BUSY"; chip.className = "chip hot"; }
    else if (e.loaded) { chip.textContent = "ENGINE READY"; chip.className = "chip g"; }
    else { chip.textContent = "ENGINE COLD"; chip.className = "chip"; }
    $("#chip-queue").textContent = `QUEUE ${w.queued + (w.current ? 1 : 0)}`;
    $("#chip-queue").className = "chip " + (w.queued + (w.current ? 1 : 0) ? "o" : "");
    $("#chip-songs").textContent = `${state.status.counts.songs} SONGS`;
    if (state.mode === "settings") loadSystems();
    clearTimeout(state.statusTimer); state.statusTimer = setTimeout(loadStatus, 5000);
  }

  // ---------------------------------------------------------------- init
  async function init() {
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
    setUi(state.ui);
    stardate(); setInterval(stardate, 60000);
    let me = null;
    try { me = await (await fetch("/api/auth/me")).json(); } catch {}
    state.user = (me && me.user) || null;
    renderUser();
    if (!state.user) { showLogin(); return; }   // boot() runs as soon as somebody signs in
    boot();
  }
  async function boot() {
    state.booted = true;
    try { const d = await api("/api/defaults"); state.presets = d.style_presets || []; renderPresets(); } catch {}
    try { state.loras = await api("/api/loras"); } catch {}
    try { const th = await api("/api/themes"); state.themes = th.themes || []; state.fusionSets = th.fusion_sets || []; fillFusionSelect(); } catch {}
    renderLoras();
    await loadStatus();
    await Promise.all([loadVoices(), loadStations(), loadRecent()]);
    loadJobs();
    loadArrangements();
    switchMode(state.mode);
    const resume = store.get("resumeRadio", null);
    if (resume) { store.set("resumeRadio", null); tuneIn(resume); }
    $("#style").value = store.get("style", "");
    $("#style").addEventListener("input", () => store.set("style", $("#style").value));
  }
  init();
})();
