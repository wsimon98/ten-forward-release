/* Train tab: teach YuE2 a style (or an artist) from a folder of songs. Vanilla JS; talks to trainer.py's /api/train.
   Builds its own page inside #mode-train, so nothing here depends on app.js. */
(() => {
  const root = document.getElementById("mode-train");
  if (!root) return;
  const $ = (s, r = root) => r.querySelector(s);
  const $$ = (s, r = root) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get: (k, d) => { try { const v = localStorage.getItem("tf:train:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem("tf:train:" + k, JSON.stringify(v)); } catch {} },
  };
  async function api(path, opts = {}) {
    const o = { headers: {}, ...opts };
    if (o.body && !(o.body instanceof FormData)) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(o.body); }
    const r = await fetch("/api/train" + path, o);
    if (!r.ok) {
      let msg = r.statusText;
      try { const j = await r.json(); msg = j.detail || j.error || JSON.stringify(j); } catch {}
      throw new Error(r.status === 401 ? "Sign in first" : (typeof msg === "string" ? msg : JSON.stringify(msg)));
    }
    return r.json();
  }
  function toast(msg, err = false) {
    const box = document.getElementById("toasts"); if (!box) return;
    const t = document.createElement("div");
    t.className = "toast" + (err ? " err" : ""); t.textContent = msg; box.appendChild(t);
    setTimeout(() => t.remove(), err ? 7000 : 3500);
  }
  const fmtDur = (s) => { s = Math.round(s || 0); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
  const fmtLeft = (s) => {
    if (s == null) return "";
    if (s < 90) return "under 2 min";
    if (s < 5400) return "about " + Math.round(s / 60) + " min";
    return "about " + (s / 3600).toFixed(1) + " hours";
  };
  const when = (t) => t ? new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";

  const LENGTHS = { short: 8, medium: 14, long: 20 };
  const STRENGTHS = { subtle: [16, 5e-5], balanced: [32, 1e-4], strong: [48, 1.5e-4] };
  const ACTIVE = ["waiting", "freeing", "starting", "caching", "training"];
  const GUESS_S_PER_STEP = 2.2;    // measured on the RTX 3090: about 1.7 s a step for the sound alone, about 3.5 s with the planner too

  const S = { env: null, jobs: [], id: null, job: null, look: null, timer: null, showLog: false, log: [], busy: {}, settings: store.get("settings", { advanced: false, length: "medium", strength: "balanced", halves: "sound" }), capSig: "", loaded: false };

  // ------------------------------------------------------------------ skeleton
  root.innerHTML = `
    <div class="col main-col" id="tr-main">
      <div class="block tr-notready hidden" id="tr-notready"><div class="block-head"><span class="lbl">The trainer is not ready</span></div><div class="hint" id="tr-notready-why"></div></div>
      <div class="block" id="tr-intro">
        <div class="block-head"><span class="lbl">Train your own style</span></div>
        <div class="hint" style="line-height:1.5">Give it a folder of songs (15 to 50 is ideal) and it learns how they sound. Afterwards you can put that sound on any channel or song, like the other LoRAs. Training takes the graphics card for a while, so the radio stops making new songs until it is done.</div>
        <div class="row wrap" style="margin-top:10px"><button class="go small" id="tr-new">Start a new one</button></div>
      </div>
      <div id="tr-flow" class="hidden">
        <div class="block tr-step" id="tr-s1">
          <div class="block-head"><span class="lbl"><span class="tr-num">1</span>Your songs</span><span class="hint" id="tr-s1-sum"></span></div>
          <div class="tr-body">
            <div class="tr-row"><input id="tr-path" placeholder="A folder on this computer, like D:\\Music\\My Band"><button class="mini" id="tr-look">Look</button>
              <label class="filebtn mini">Upload songs<input type="file" id="tr-files" accept="audio/*,.m4a,.mp3,.wav,.flac" multiple></label></div>
            <div id="tr-lookout"></div>
            <div id="tr-songs" class="tr-songs"></div>
            <div id="tr-s1-warn"></div>
          </div>
        </div>
        <div class="block tr-step" id="tr-s2">
          <div class="block-head"><span class="lbl"><span class="tr-num">2</span>Name it</span></div>
          <div class="tr-body">
            <div class="grid2">
              <label>Name<input id="tr-name" maxlength="60" placeholder="Smoky Porch Band"></label>
              <label>Trigger word<span class="tr-trig"><input id="tr-trigger" maxlength="24" placeholder="a made-up word"><button class="mini ghost" id="tr-trig-new" title="Make up another word">New</button></span></label>
              <label class="full" style="grid-column:1/-1">What kind of music is it?<input id="tr-genre" maxlength="200" placeholder="for example: 70s outlaw country, gravelly male vocal, steel guitar"></label>
            </div>
            <div class="hint" style="margin-top:6px">The trigger word is a nonsense word that means "this style". Whenever the LoRA is on (a channel, Studio), the word is put in front of the style by itself. The kind of music helps write the captions.</div>
          </div>
        </div>
        <div class="block tr-step off" id="tr-s3">
          <div class="block-head"><span class="lbl"><span class="tr-num">3</span>Captions</span><span class="hint" id="tr-s3-sum"></span>
            <div class="tools"><button class="mini" id="tr-caps-go">Write the captions</button><button class="mini ghost hidden" id="tr-caps-stop">Stop</button><button class="mini ghost" id="tr-caps-all" title="Throw these away and write all of them again">Write all again</button></div></div>
          <div class="tr-body">
            <div class="hint">Each song needs its words and a line saying how it sounds. The words are heard from the singing (or read from a .txt beside the song). Fix anything that is wrong: this is what the model learns from.</div>
            <div id="tr-capprog"></div>
            <div id="tr-caps" class="tr-caps"></div>
          </div>
        </div>
        <div class="block tr-step off" id="tr-s4">
          <div class="block-head"><span class="lbl"><span class="tr-num">4</span>How to train it</span></div>
          <div class="tr-body">
            <div class="lbl" style="color:var(--lav);font-size:11px;margin-bottom:4px">How long</div>
            <div class="tr-seg" id="tr-length"></div>
            <div class="lbl" style="color:var(--lav);font-size:11px;margin:12px 0 4px">How strong</div>
            <div class="tr-seg" id="tr-strength"></div>
            <div class="hint" id="tr-plan" style="margin-top:8px"></div>
            <label class="check" style="margin-top:10px"><input type="checkbox" id="tr-adv"> Advanced</label>
            <div class="tr-adv hidden" id="tr-advbox"></div>
          </div>
        </div>
        <div class="block tr-step off" id="tr-s5">
          <div class="block-head"><span class="lbl"><span class="tr-num">5</span>Train</span></div>
          <div class="tr-body">
            <div class="go-row"><button class="go" id="tr-go">Train</button><button class="mini ghost danger hidden" id="tr-cancel">Cancel</button></div>
            <div class="hint" id="tr-go-hint" style="margin-top:6px"></div>
            <div id="tr-prog"></div>
          </div>
        </div>
        <div class="block tr-step off" id="tr-s6">
          <div class="block-head"><span class="lbl"><span class="tr-num">6</span>Results</span><span class="hint" id="tr-s6-sum"></span></div>
          <div class="tr-body">
            <div class="tr-row" style="margin-bottom:8px"><label style="display:flex;gap:8px;align-items:center;flex:1;min-width:200px;color:var(--lav);font-size:12px;text-transform:uppercase;letter-spacing:1.2px">Keep it as <input id="tr-keepname" maxlength="60" style="flex:1"></label></div>
            <div id="tr-cps" class="list"><div class="hint">Checkpoints show up here as they are saved.</div></div>
            <div class="hint" style="margin-top:8px">Try it makes a short test song with that checkpoint. Keep this one adds it to your LoRAs, ready to use on any channel or song.</div>
          </div>
        </div>
      </div>
    </div>
    <div class="col side-col">
      <div class="block"><div class="block-head"><span class="lbl">Your training runs</span><div class="tools"><button class="mini ghost" id="tr-refresh">Refresh</button></div></div>
        <div id="tr-runs" class="list tr-runs"><div class="hint">Nothing yet.</div></div></div>
      <div class="block"><div class="block-head"><span class="lbl">How it works</span></div>
        <div class="hint" style="line-height:1.5">1. Pick the songs. 2. Name the style. 3. It listens to each song and writes the words and a style line; you fix mistakes. 4. Pick how long and how strong. 5. Train: the radio pauses its song-making, the card is cleared, and the model practises. Checkpoints are saved along the way, so you can stop early. 6. Try the checkpoints and keep the one that sounds best.</div>
        <div class="hint" style="line-height:1.5;margin-top:8px">By default it learns only the <b>sound</b>: voices, instruments and production. That plays back at normal speed. The Advanced box can also teach the <b>planner</b> (how songs are written), which is much slower to play.</div></div>
    </div>`;

  // ------------------------------------------------------------------ small renderers
  function setSteps() {
    const j = S.job;
    const has = j && j.song_count > 0;
    const capd = j && j.song_count > 0 && j.captioned === j.song_count;
    const run = j && ACTIVE.includes(j.state);
    const cap = j && j.state === "captioning";
    $("#tr-s3").classList.toggle("off", !has || run);
    $("#tr-s4").classList.toggle("off", !capd || run || cap);
    $("#tr-s5").classList.toggle("off", !capd || cap);
    $("#tr-s1").classList.toggle("off", !!run || !!cap);
    $("#tr-s2").classList.toggle("off", !!run || !!cap);
    const hasCp = j && (j.checkpoints || []).length;
    $("#tr-s6").classList.toggle("off", !hasCp);
  }

  function renderEnv() {
    const e = S.env;
    const box = $("#tr-notready");
    if (!e) return;
    box.classList.toggle("hidden", !!e.ready);
    if (!e.ready) $("#tr-notready-why").textContent = (e.problems || []).join(". ") + ".";
  }

  function renderRuns() {
    const box = $("#tr-runs");
    if (!S.jobs.length) { box.innerHTML = `<div class="hint">Nothing yet.</div>`; return; }
    box.innerHTML = S.jobs.map((j) => `<div class="item${j.id === S.id ? " on" : ""}" data-id="${esc(j.id)}"><div class="grow"><b>${esc(j.name)}</b>
      <small>${esc(stateWords(j))} · ${j.song_count} songs · ${esc(when(j.updated || j.created))}</small></div>
      ${ACTIVE.includes(j.state) || j.state === "captioning" ? "" : `<button class="mini ghost danger" data-del="${esc(j.id)}" title="Remove this run and its checkpoints (kept LoRAs stay)">Remove</button>`}</div>`).join("");
    $$("[data-id]", box).forEach((el) => el.addEventListener("click", (e) => { if (e.target.closest("[data-del]")) return; openJob(el.dataset.id); }));
    $$("[data-del]", box).forEach((b) => b.addEventListener("click", async () => {
      if (!confirm("Remove this training run? Its checkpoints go too. LoRAs you already kept stay.")) return;
      try { await api("/jobs/" + b.dataset.del, { method: "DELETE" }); if (S.id === b.dataset.del) { S.id = null; S.job = null; store.set("job", null); showFlow(); } loadJobs(); } catch (e) { toast(e.message, true); }
    }));
  }

  function stateWords(j) {
    const k = Object.keys(j.kept || {}).length;
    return ({ draft: "getting ready", captioning: "writing captions", waiting: "waiting", freeing: "clearing the card", starting: "starting", caching: "preparing songs", training: "training",
      done: "finished" + (k ? ", " + k + " kept" : ""), failed: "stopped with a problem", cancelled: "cancelled", interrupted: "interrupted" }[j.state] || j.state);
  }

  function renderSongs() {
    const j = S.job; const box = $("#tr-songs");
    const songs = (j && j.songs) || [];
    $("#tr-s1-sum").textContent = songs.length ? `${songs.length} song${songs.length === 1 ? "" : "s"} · ${j.minutes} min` : "";
    const locked = ACTIVE.includes(j?.state) || j?.state === "captioning";
    box.innerHTML = songs.map((s) => `<div class="tr-song"><span class="t" title="${esc(s.orig)}">${esc(s.title)}</span><span class="d">${fmtDur(s.seconds)}${s.sidecar && s.sidecar.lyrics ? " · has words" : ""}</span>${locked ? "" : `<button class="x" data-rm="${s.n}" title="Take this song out">&times;</button>`}</div>`).join("");
    $$("[data-rm]", box).forEach((b) => b.addEventListener("click", async () => {
      try { S.job = await api(`/jobs/${S.id}/songs/${b.dataset.rm}`, { method: "DELETE" }); S.capSig = ""; renderAll(); } catch (e) { toast(e.message, true); }
    }));
    $("#tr-s1-warn").innerHTML = ((j && j.warnings) || []).map((w) => `<div class="tr-warn">${esc(w)}</div>`).join("");
  }

  function renderLook() {
    const L = S.look; const box = $("#tr-lookout");
    if (!L) { box.innerHTML = ""; return; }
    let h = `<div class="tr-look"><b>${L.count}</b> song${L.count === 1 ? "" : "s"}, <b>${L.minutes}</b> minutes in ${esc(L.path)}`;
    if (L.count) {
      const withWords = L.files.filter((f) => f.has_words).length;
      h += `<div class="names">${L.files.slice(0, 12).map((f) => esc(f.name)).join(" · ")}${L.count > 12 ? " ..." : ""}</div>`;
      if (withWords) h += `<div class="hint">${withWords} of them have a .txt beside them; their words will be used as written.</div>`;
      h += `<div class="row wrap" style="margin-top:6px"><button class="mini" id="tr-usefolder">Use these ${L.count} songs</button></div>`;
    } else if ((L.subfolders || []).length) {
      h += `<div class="hint" style="margin-top:4px">No songs directly in there. These folders inside have some:</div>` +
        L.subfolders.map((s) => `<button class="mini ghost tr-sub" data-sub="${esc(s.path)}">${esc(s.name)} (${s.count})</button>`).join("");
    } else h += `<div class="hint">No songs found in there.</div>`;
    h += (L.warnings || []).map((w) => `<div class="tr-warn">${esc(w)}</div>`).join("") + `</div>`;
    box.innerHTML = h;
    const use = $("#tr-usefolder");
    if (use) use.addEventListener("click", async () => {
      use.disabled = true; use.textContent = "Copying...";
      try { S.job = await api(`/jobs/${S.id}/songs/folder`, { method: "POST", body: { path: L.path } }); S.look = null; S.capSig = ""; toast("Songs added"); renderAll(); loadJobs(); }
      catch (e) { toast(e.message, true); use.disabled = false; use.textContent = `Use these ${L.count} songs`; }
    });
    $$("[data-sub]", box).forEach((b) => b.addEventListener("click", () => { $("#tr-path").value = b.dataset.sub; doLook(); }));
  }

  // captions table: rebuilt only when the set of songs or their states change, so typing is never interrupted
  function renderCaps() {
    const j = S.job; const box = $("#tr-caps");
    const songs = (j && j.songs) || [];
    const sig = songs.map((s) => `${s.n}:${s.state}:${s.lyrics == null ? 0 : 1}:${s.style == null ? 0 : 1}:${s.edited ? 1 : 0}`).join("|") + "|" + (j?.trigger || "");
    const run = ACTIVE.includes(j?.state);
    const cap = j?.state === "captioning";
    $("#tr-caps-go").classList.toggle("hidden", cap);
    $("#tr-caps-stop").classList.toggle("hidden", !cap);
    $("#tr-caps-all").classList.toggle("hidden", cap || !songs.some((s) => s.lyrics != null));
    const todo = songs.filter((s) => s.lyrics == null || s.style == null).length;
    $("#tr-caps-go").textContent = todo === songs.length ? "Write the captions" : (todo ? `Write the missing ${todo}` : "Write the captions");
    $("#tr-caps-go").disabled = !songs.length || run || (todo === 0);
    $("#tr-s3-sum").textContent = songs.length ? `${songs.length - todo} of ${songs.length} written` : "";
    const prog = $("#tr-capprog");
    if (cap) {
      const p = j.progress || {}; const pct = p.steps ? Math.round(100 * (p.step || 0) / p.steps) : 0;
      prog.innerHTML = `<div class="tr-prog"><div class="top"><span><span class="spinner"></span>${esc(j.message)}</span><span>${pct}%</span></div><div class="bar"><i style="width:${pct}%"></i></div><div class="sub">The first song takes longest while the listening models load.</div></div>`;
    } else prog.innerHTML = "";
    if (sig === S.capSig) {
      // same rows: only refresh fields nobody is editing
      songs.forEach((s) => {
        const ta = $(`#tr-lyr-${s.n}`), st = $(`#tr-sty-${s.n}`);
        if (ta && document.activeElement !== ta && !ta.dataset.dirty && ta.value !== (s.lyrics || "")) ta.value = s.lyrics || "";
        if (st && document.activeElement !== st && !st.dataset.dirty && st.value !== (s.style || "")) st.value = s.style || "";
      });
      return;
    }
    S.capSig = sig;
    if (!songs.length) { box.innerHTML = ""; return; }
    box.innerHTML = songs.map((s) => `<div class="tr-cap ${esc(s.state === "todo" ? "" : s.state)}" data-n="${s.n}"><div class="n">${s.n}</div><div>
      <div class="head"><b title="${esc(s.orig)}">${esc(s.title)}</b><span class="hint">${fmtDur(s.seconds)}${s.source ? " · words from " + esc(s.source) : ""}${s.instrumental ? " · no singing" : ""}</span>
        <button class="mini ghost" data-again="${s.n}"${run || cap ? " disabled" : ""}>Write again</button></div>
      ${s.error ? `<div class="tr-bad">${esc(s.error)}</div>` : ""}
      <div class="lab">Style line</div><div class="tr-style"><span class="pre">${esc(j.trigger)},</span><input id="tr-sty-${s.n}" data-n="${s.n}" data-f="style" value="${esc(s.style || "")}" placeholder="how it sounds, as tags"${run ? " disabled" : ""}></div>
      <div class="lab">Lyrics</div><textarea id="tr-lyr-${s.n}" data-n="${s.n}" data-f="lyrics" placeholder="[Verse 1]&#10;words..."${run ? " disabled" : ""}>${esc(s.lyrics || "")}</textarea></div></div>`).join("");
    $$("textarea[data-f],input[data-f]", box).forEach((el) => {
      el.addEventListener("input", () => { el.dataset.dirty = "1"; });
      el.addEventListener("change", async () => {
        try {
          const row = await api(`/jobs/${S.id}/songs/${el.dataset.n}`, { method: "PUT", body: { [el.dataset.f]: el.value } });
          delete el.dataset.dirty;
          const mine = S.job.songs.find((x) => x.n === row.n); if (mine) Object.assign(mine, row);
          if (el.dataset.f === "style" && el.value !== row.style) el.value = row.style;
          S.capSig = ""; renderSteps();
        } catch (e) { toast(e.message, true); }
      });
    });
    $$("[data-again]", box).forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "Listening...";
      try { await api(`/jobs/${S.id}/songs/${b.dataset.again}/again`, { method: "POST" }); await refreshJob(); S.capSig = ""; renderAll(); }
      catch (e) { toast(e.message, true); b.disabled = false; b.textContent = "Write again"; }
    }));
  }
  function renderSteps() { setSteps(); }

  // ------------------------------------------------------------------ the two knobs + advanced
  function planFor() {
    const s = S.settings; const n = S.job?.song_count || 0;
    const passes = LENGTHS[s.length] || 14;
    let [rank, lr] = STRENGTHS[s.strength] || STRENGTHS.balanced;
    let steps = Math.max(200, Math.min(1200, passes * Math.max(1, n)));
    if (s.advanced) { rank = +s.rank || rank; lr = +s.lr || lr; steps = +s.steps || steps; }
    const halves = s.advanced && s.halves === "both" ? "both" : "sound";
    return { passes, rank, lr, steps, halves, window: s.advanced && s.train_window_frames ? +s.train_window_frames : (halves === "both" ? 750 : 1500) };
  }
  function secPerStep(plan) {
    return GUESS_S_PER_STEP * (plan.halves === "both" ? 2 : 1);
  }
  function renderKnobs() {
    const s = S.settings; const n = S.job?.song_count || 0;
    const L = [["short", "Short", "8 passes over each song. A quick taste."], ["medium", "Medium", "14 passes. A good middle."], ["long", "Long", "20 passes. Learns more, a bit more risk of copying."]];
    $("#tr-length").innerHTML = L.map(([k, t, h]) => `<button class="chip-btn${s.length === k ? " on" : ""}" data-len="${k}">${t}<small>${h}</small></button>`).join("");
    const T = [["subtle", "Subtle", "A light touch. Safe to leave on everything."], ["balanced", "Balanced", "The usual choice."], ["strong", "Strong", "Takes over the sound. Try it and back off if it is too much."]];
    $("#tr-strength").innerHTML = T.map(([k, t, h]) => `<button class="chip-btn${s.strength === k ? " on" : ""}" data-str="${k}">${t}<small>${h}</small></button>`).join("");
    $$("[data-len]").forEach((b) => b.addEventListener("click", () => { s.length = b.dataset.len; saveSettings(); renderKnobs(); }));
    $$("[data-str]").forEach((b) => b.addEventListener("click", () => { s.strength = b.dataset.str; saveSettings(); renderKnobs(); }));
    const p = planFor();
    const mins = Math.round(p.steps * secPerStep(p) / 60);
    $("#tr-plan").innerHTML = `${p.steps} steps over ${n} song${n === 1 ? "" : "s"}. Rough time: ${mins < 90 ? mins + " minutes" : (mins / 60).toFixed(1) + " hours"} once it is going, plus about 5 minutes to load the model and get your songs ready. A checkpoint is saved every ${s.advanced ? (+s.save_every || 100) : 100} steps, so you can stop early and still keep one.`;
    $("#tr-adv").checked = !!s.advanced;
    $("#tr-advbox").classList.toggle("hidden", !s.advanced);
    if (s.advanced) renderAdv();
  }
  function renderAdv() {
    const s = S.settings; const p = planFor();
    const v = (k, d) => (s[k] ?? d);
    const both = s.halves === "both";
    $("#tr-advbox").innerHTML = `<div class="grid2">
      <label class="full">What to teach<select id="a-halves"><option value="sound"${both ? "" : " selected"}>The sound only (fast to play, recommended)</option><option value="both"${both ? " selected" : ""}>The planner and the sound (slow)</option></select>
        <small>${both ? "Also teaches how songs are written. Tight on a 24 GB card (it uses a shorter 30 second window to fit), and the result plays about 10 times slower. It needs a lot of songs to help rather than copy." : "Voices, instruments and production. Plays at normal speed."}</small></label>
      <label><span>Learning room (rank) <output id="o-rank">${v("rank", p.rank)}</output></span><input type="range" id="a-rank" min="8" max="64" step="8" value="${v("rank", p.rank)}"><small>How much it can learn. Higher means stronger, and more likely to copy.</small></label>
      <label><span>Learning speed <output id="o-lr">${(+v("lr", p.lr)).toExponential(1)}</output></span><input type="range" id="a-lr" min="2" max="20" step="1" value="${Math.round((+v("lr", p.lr)) / 1e-5)}"><small>Lower is gentler and slower. The slider runs 2e-5 to 2e-4.</small></label>
      <label><span>Steps <output id="o-steps">${v("steps", p.steps)}</output></span><input type="range" id="a-steps" min="100" max="2000" step="50" value="${v("steps", p.steps)}"><small>How many practice steps in all.</small></label>
      <label><span>Save a checkpoint every <output id="o-save">${v("save_every", 100)}</output> steps</span><input type="range" id="a-save" min="50" max="250" step="50" value="${v("save_every", 100)}"><small>More checkpoints means more to try.</small></label>
      <label><span>How much of a song per step <output id="o-win">${Math.round(p.window / 25)} s</output></span><input type="range" id="a-win" min="750" max="1500" step="250" value="${p.window}"><small>Shorter uses less graphics memory. With the planner on, keep it at 30 s or the card runs out of room and everything crawls.</small></label>
      <label>Lead sheet<select id="a-cot"><option value="full">Melody and chords</option><option value="melody">Melody only</option><option value="off">None</option></select><small>How songs are planned. Match how you will use it; melody and chords is the usual.</small></label>
      <label><span>Sometimes hide the lead sheet <output id="o-abc">${v("abc_dropout", 0.5)}</output></span><input type="range" id="a-abc" min="0" max="1" step="0.1" value="${v("abc_dropout", 0.5)}"><small>So it works with and without one.</small></label>
      <label>Song reader<select id="a-head"><option value="v9">v9 (newer)</option><option value="v4">v4 (the original)</option></select><small>Which version turns your songs into something the model reads.</small></label>
      <label><span>Average over <output id="o-acc">${v("grad_accum", 1)}</output> step${+v("grad_accum", 1) === 1 ? "" : "s"}</span><input type="range" id="a-acc" min="1" max="4" step="1" value="${v("grad_accum", 1)}"><small>Smoother, slower. 1 is normal.</small></label>
      <label class="check"><input type="checkbox" id="a-ema"${s.ema ? " checked" : ""}> Smooth the result (EMA 0.99)</label>
      <label class="check"><input type="checkbox" id="a-sep"${s.do_separation ? " checked" : ""}> Also learn the voice and the band apart<small>Slower to prepare and needs more memory.</small></label>
    </div>`;
    $("#a-cot").value = v("cot", "full"); $("#a-head").value = v("head", "v9");
    const bind = (id, key, conv, out, fmt) => {
      const el = $("#" + id); if (!el) return;
      el.addEventListener("input", () => { s[key] = conv(el); if (out) $("#" + out).textContent = fmt ? fmt(s[key]) : s[key]; saveSettings(); if (key === "steps" || key === "save_every") renderPlanOnly(); });
    };
    bind("a-rank", "rank", (e) => +e.value, "o-rank"); bind("a-lr", "lr", (e) => +e.value * 1e-5, "o-lr", (x) => x.toExponential(1));
    bind("a-steps", "steps", (e) => +e.value, "o-steps"); bind("a-save", "save_every", (e) => +e.value, "o-save");
    bind("a-win", "train_window_frames", (e) => +e.value, "o-win", (x) => Math.round(x / 25) + " s");
    bind("a-cot", "cot", (e) => e.value); bind("a-abc", "abc_dropout", (e) => +e.value, "o-abc"); bind("a-head", "head", (e) => e.value);
    bind("a-acc", "grad_accum", (e) => +e.value, "o-acc"); bind("a-ema", "ema", (e) => e.checked); bind("a-sep", "do_separation", (e) => e.checked);
    $("#a-halves").addEventListener("change", (e) => {
      s.halves = e.target.value;
      if (s.halves === "both") { s.rank = 32; s.lr = 5e-5; s.train_window_frames = 750; } else { delete s.rank; delete s.lr; delete s.train_window_frames; }
      saveSettings(); renderKnobs();
    });
  }
  function renderPlanOnly() {
    const p = planFor(); const n = S.job?.song_count || 0; const s = S.settings;
    const mins = Math.round(p.steps * secPerStep(p) / 60);
    $("#tr-plan").innerHTML = `${p.steps} steps over ${n} song${n === 1 ? "" : "s"}. Rough time: ${mins < 90 ? mins + " minutes" : (mins / 60).toFixed(1) + " hours"} once it is going, plus about 5 minutes to load the model and get your songs ready. A checkpoint is saved every ${s.advanced ? (+s.save_every || 100) : 100} steps, so you can stop early and still keep one.`;
  }
  let saveT = null;
  function saveSettings() {
    store.set("settings", S.settings);
    clearTimeout(saveT);
    saveT = setTimeout(() => { if (S.id) api(`/jobs/${S.id}/meta`, { method: "POST", body: { settings: S.settings } }).catch(() => {}); }, 600);
  }
  $("#tr-adv").addEventListener("change", (e) => { S.settings.advanced = e.target.checked; saveSettings(); renderKnobs(); });

  // ------------------------------------------------------------------ run + progress
  function sparkline(points) {
    if (!points || points.length < 2) return "";
    const xs = points.map((p) => p[0]), ys = points.map((p) => p[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const pts = points.map((p) => `${(((p[0] - x0) / Math.max(1e-9, x1 - x0)) * 300).toFixed(1)},${(52 - ((p[1] - y0) / Math.max(1e-9, y1 - y0)) * 48 - 2).toFixed(1)}`).join(" ");
    return `<svg class="tr-spark" viewBox="0 0 300 54" preserveAspectRatio="none"><polyline points="${pts}"/></svg>`;
  }
  function renderRun() {
    const j = S.job; if (!j) return;
    const run = ACTIVE.includes(j.state);
    const go = $("#tr-go"), cancel = $("#tr-cancel");
    const capd = j.song_count > 0 && j.captioned === j.song_count;
    go.disabled = run || !capd || j.state === "captioning" || (S.env && !S.env.ready);
    go.textContent = run ? "Training..." : (j.state === "done" || j.state === "failed" || j.state === "cancelled" || j.state === "interrupted" ? "Train again" : "Train");
    cancel.classList.toggle("hidden", !run);
    const p = planFor();
    $("#tr-go-hint").textContent = run ? "" : (!capd ? "Write the captions first." : (p.halves === "both" ? "Planner and sound: this is tight on a 24 GB card and may run out of memory. The radio stops making songs until it is done." : "The radio stops making new songs while this runs, and starts again when it ends. The song being made finishes first."));
    const box = $("#tr-prog"); const pr = j.progress || {};
    let h = "";
    if (run || ["done", "failed", "cancelled", "interrupted"].includes(j.state)) {
      const pct = pr.steps && pr.stage === "training" ? Math.round(100 * (pr.step || 0) / pr.steps) : (pr.stage === "caching" && pr.cache_total ? Math.round(100 * (pr.cache_done || 0) / pr.cache_total) : (j.state === "done" ? 100 : 0));
      const color = j.state === "failed" ? "var(--red)" : (j.state === "done" ? "var(--green)" : "var(--orange)");
      const detail = [];
      if (pr.stage === "training") { detail.push(`step ${pr.step} of ${pr.steps}`); if (pr.eta_s != null) detail.push(fmtLeft(pr.eta_s) + " left"); if (pr.loss != null) detail.push(`loss ${(+pr.loss).toFixed(3)}`); }
      else if (pr.stage === "caching") { detail.push(pr.cache_total ? `${pr.cache_done} of ${pr.cache_total} songs` : "getting your songs ready"); if (pr.eta_s != null) detail.push(fmtLeft(pr.eta_s) + " left"); }
      h = `<div class="tr-prog" style="border-left-color:${color}"><div class="top"><span>${run ? '<span class="spinner"></span>' : ""}${esc(j.message || "")}</span><span>${run ? pct + "%" : ""}</span></div>
        ${run ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}<div class="sub">${esc(detail.join(" · "))}</div>${pr.stage === "training" ? sparkline(pr.loss_points) : ""}
        ${pr.stage === "training" ? '<div class="hint" style="font-size:11.5px">Loss going down means it is learning. It wobbles; that is normal.</div>' : ""}
        <div class="row wrap" style="margin-top:8px"><button class="mini ghost" id="tr-showlog">${S.showLog ? "Hide details" : "Show details"}</button></div>${S.showLog ? `<div class="tr-log" id="tr-logbox">${esc((S.log || []).join("\n"))}</div>` : ""}</div>`;
    }
    box.innerHTML = h;
    const sl = $("#tr-showlog"); if (sl) sl.addEventListener("click", () => { S.showLog = !S.showLog; if (S.showLog) loadLog(); else renderRun(); });
    const lb = $("#tr-logbox"); if (lb) lb.scrollTop = lb.scrollHeight;
  }
  async function loadLog() { try { S.log = (await api(`/jobs/${S.id}/log?tail=120`)).lines; } catch {} renderRun(); }

  function renderCheckpoints() {
    const j = S.job; const box = $("#tr-cps");
    const cps = (j && j.checkpoints) || [];
    $("#tr-s6-sum").textContent = cps.length ? `${cps.length} saved` : "";
    if (!cps.length) { box.innerHTML = `<div class="hint">Checkpoints show up here as they are saved.</div>`; return; }
    const total = j.progress?.steps || 0;
    box.innerHTML = cps.slice().reverse().map((c) => `<div class="tr-cp${c.kept_as ? " kept" : ""}" data-step="${c.step}"><div class="st">${c.step}</div>
      <div><b>${c.final || (total && c.step === total) ? "End of the run" : "Step " + c.step}</b><small>${c.mb} MB · ${esc(when(c.saved))}${c.kept_as ? " · kept as " + esc(c.kept_as) : ""}${c.try ? " · test song queued" : (c.tested_song ? " · test song made (see the Library)" : "")}</small></div>
      <div class="acts"><button class="mini" data-try="${c.step}">Try it</button><button class="mini ghost" data-keep="${c.step}">${c.kept_as ? "Keep again" : "Keep this one"}</button></div></div>`).join("");
    $$("[data-try]", box).forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      try { const r = await api(`/jobs/${S.id}/checkpoints/${b.dataset.try}/try`, { method: "POST", body: {} }); toast(r.message); await refreshJob(); renderAll(); }
      catch (e) { toast(e.message, true); }
      b.disabled = false;
    }));
    $$("[data-keep]", box).forEach((b) => b.addEventListener("click", async () => {
      const name = ($("#tr-keepname").value || "").trim() || j.name;
      b.disabled = true;
      try {
        let r;
        try { r = await api(`/jobs/${S.id}/checkpoints/${b.dataset.keep}/keep`, { method: "POST", body: { name } }); }
        catch (e) {
          if (/already a LoRA/i.test(e.message) && confirm(e.message + "\n\nReplace it?")) r = await api(`/jobs/${S.id}/checkpoints/${b.dataset.keep}/keep`, { method: "POST", body: { name, replace: true } });
          else throw e;
        }
        toast(`Kept as ${r.name}. Find it in the LoRA lists. Its word "${r.trigger}" goes in front of the style by itself.`);
        await refreshJob(); renderAll();
      } catch (e) { toast(e.message, true); }
      b.disabled = false;
    }));
  }

  // ------------------------------------------------------------------ data flow
  function renderAll() {
    const j = S.job;
    $("#tr-intro").classList.toggle("hidden", !!j);
    $("#tr-flow").classList.toggle("hidden", !j);
    renderEnv(); renderRuns();
    if (!j) return;
    const act = document.activeElement;
    if (act !== $("#tr-name")) $("#tr-name").value = j.name || "";
    if (act !== $("#tr-trigger")) $("#tr-trigger").value = j.trigger || "";
    if (act !== $("#tr-genre")) $("#tr-genre").value = j.genre || "";
    if (act !== $("#tr-keepname") && !$("#tr-keepname").value) $("#tr-keepname").value = j.name || "";
    const locked = ACTIVE.includes(j.state) || j.state === "captioning";
    ["#tr-path", "#tr-look", "#tr-name", "#tr-trigger", "#tr-trig-new", "#tr-genre"].forEach((s) => { const e = $(s); if (e) e.disabled = locked; });
    $("#tr-files").disabled = locked;
    renderLook(); renderSongs(); renderCaps(); renderKnobs(); renderRun(); renderCheckpoints(); setSteps();
  }
  async function loadJobs() { try { S.jobs = await api("/jobs"); renderRuns(); } catch {} }
  async function loadEnv() { try { S.env = await api("/status"); renderEnv(); } catch (e) { /* not an admin, or the server is busy */ } }
  async function refreshJob() { if (!S.id) return; S.job = await api("/jobs/" + S.id); }
  function showFlow() { renderAll(); }
  async function openJob(id) {
    S.id = id; store.set("job", id); S.capSig = ""; S.look = null; S.log = [];
    try { await refreshJob(); if (S.job.settings && Object.keys(S.job.settings).length) S.settings = { ...S.settings, ...S.job.settings }; } catch (e) { toast(e.message, true); S.id = null; S.job = null; }
    renderAll(); schedule();
  }
  function schedule() {
    clearTimeout(S.timer);
    if (!root.classList.contains("on")) return;
    const busy = S.job && (ACTIVE.includes(S.job.state) || S.job.state === "captioning");
    S.timer = setTimeout(async () => {
      if (!root.classList.contains("on")) return;
      try {
        if (S.id) { await refreshJob(); if (S.showLog) { S.log = (await api(`/jobs/${S.id}/log?tail=120`)).lines; } renderAll(); }
        else await loadJobs();
        if (busy || !S.jobs.length) loadJobs();
      } catch (e) { /* the server may be restarting */ }
      schedule();
    }, busy ? 2000 : 8000);
  }

  // ------------------------------------------------------------------ events
  $("#tr-new").addEventListener("click", async () => {
    try {
      const j = await api("/jobs", { method: "POST", body: { name: "My style" } });
      S.settings = { ...S.settings, rank: undefined, lr: undefined };
      await loadJobs(); await openJob(j.id); $("#tr-name").select();
    } catch (e) { toast(e.message, true); }
  });
  $("#tr-refresh").addEventListener("click", () => { loadJobs(); loadEnv(); if (S.id) refreshJob().then(renderAll); });
  async function doLook() {
    const path = $("#tr-path").value.trim(); if (!path) return toast("Type a folder first", true);
    const b = $("#tr-look"); b.disabled = true; b.textContent = "Looking...";
    try { S.look = await api("/look", { method: "POST", body: { path } }); store.set("path", path); renderLook(); } catch (e) { S.look = null; renderLook(); toast(e.message, true); }
    b.disabled = false; b.textContent = "Look";
  }
  $("#tr-look").addEventListener("click", doLook);
  $("#tr-path").addEventListener("keydown", (e) => { if (e.key === "Enter") doLook(); });
  $("#tr-path").value = store.get("path", "");
  $("#tr-files").addEventListener("change", async (e) => {
    const files = [...e.target.files]; if (!files.length || !S.id) return;
    const fd = new FormData(); files.forEach((f) => fd.append("files", f));
    toast(`Uploading ${files.length} song${files.length === 1 ? "" : "s"}...`);
    try { S.job = await api(`/jobs/${S.id}/songs/upload`, { method: "POST", body: fd }); S.capSig = ""; renderAll(); loadJobs(); } catch (er) { toast(er.message, true); }
    e.target.value = "";
  });
  let metaT = null;
  const saveMeta = () => {
    clearTimeout(metaT);
    metaT = setTimeout(async () => {
      if (!S.id) return;
      try { const j = await api(`/jobs/${S.id}/meta`, { method: "POST", body: { name: $("#tr-name").value, trigger: $("#tr-trigger").value, genre: $("#tr-genre").value } }); S.job = j; S.capSig = ""; renderCaps(); loadJobs(); } catch (e) { toast(e.message, true); }
    }, 700);
  };
  ["#tr-name", "#tr-trigger", "#tr-genre"].forEach((s) => $(s).addEventListener("input", saveMeta));
  $("#tr-trig-new").addEventListener("click", async () => {
    try { const r = await api("/trigger?name=" + encodeURIComponent($("#tr-name").value + Math.random())); $("#tr-trigger").value = r.trigger; saveMeta(); } catch (e) { toast(e.message, true); }
  });
  $("#tr-caps-go").addEventListener("click", async () => {
    try { await flushMeta(); await api(`/jobs/${S.id}/captions/write`, { method: "POST", body: {} }); S.capSig = ""; await refreshJob(); renderAll(); schedule(); } catch (e) { toast(e.message, true); }
  });
  $("#tr-caps-all").addEventListener("click", async () => {
    if (!confirm("Throw away every caption (including your edits) and write them all again?")) return;
    try { await flushMeta(); await api(`/jobs/${S.id}/captions/write`, { method: "POST", body: { all: true } }); S.capSig = ""; await refreshJob(); renderAll(); schedule(); } catch (e) { toast(e.message, true); }
  });
  $("#tr-caps-stop").addEventListener("click", () => api(`/jobs/${S.id}/captions/stop`, { method: "POST" }).then(() => toast("Stopping after this song")).catch((e) => toast(e.message, true)));
  async function flushMeta() {
    clearTimeout(metaT);
    S.job = await api(`/jobs/${S.id}/meta`, { method: "POST", body: { name: $("#tr-name").value, trigger: $("#tr-trigger").value, genre: $("#tr-genre").value, settings: S.settings } });
  }
  $("#tr-go").addEventListener("click", async () => {
    const p = planFor();
    if (p.halves === "both" && !confirm("Planner and sound is tight on a 24 GB card and the result plays about 10 times slower. Go ahead?")) return;
    try {
      await flushMeta();
      S.job = await api(`/jobs/${S.id}/start`, { method: "POST", body: { settings: S.settings } });
      S.capSig = ""; renderAll(); schedule(); toast("Training is starting");
    } catch (e) { toast(e.message, true); }
  });
  $("#tr-cancel").addEventListener("click", async () => {
    if (!confirm("Stop training? Checkpoints already saved are kept.")) return;
    try { await api(`/jobs/${S.id}/cancel`, { method: "POST" }); toast("Stopping..."); } catch (e) { toast(e.message, true); }
  });

  // ------------------------------------------------------------------ tab life
  async function shown() {
    await loadEnv(); await loadJobs();
    if (!S.id) {
      const last = store.get("job", null);
      const act = S.jobs.find((j) => ACTIVE.includes(j.state) || j.state === "captioning");
      const pick = act ? act.id : (S.jobs.find((j) => j.id === last) ? last : null);
      if (pick) await openJob(pick); else renderAll();
    } else { try { await refreshJob(); } catch {} renderAll(); }
    schedule();
  }
  new MutationObserver(() => { if (root.classList.contains("on")) shown(); else clearTimeout(S.timer); }).observe(root, { attributes: true, attributeFilter: ["class"] });
  if (root.classList.contains("on")) shown();
  // keyboard: 7 opens this tab (1 to 6 are the other tabs)
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
    if (e.ctrlKey || e.metaKey || e.altKey || e.key !== "7") return;
    const nav = document.querySelector('.nav[data-mode="train"]'); if (nav) nav.click();
  });
})();
