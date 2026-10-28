// TEN FORWARD — the public dial. Every url is relative, so this page works at /listen/ or behind any proxy prefix.
(() => {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const audio = $("#audio");
  const strip = $("#strip");
  const state = { stations: [], station: null, song: null, playedMarked: false, timer: null, poll: null, settle: null, centred: null };
  try { state.station = localStorage.getItem("tf_listen_station"); } catch {}

  async function api(path, opts = {}) {
    const o = { headers: {}, cache: "no-store", ...opts };
    if (o.body) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(o.body); }
    const r = await fetch(path, o);
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
    return r.json();
  }
  const coverLine = (s) => { const c = s && s.cover_of; if (!c || !(c.track || c.artist)) return ""; const who = c.artist ? `${c.track || "a song"} — ${c.artist}` : (c.track || "another song"); return `${c.kind === "interpolation" ? "Interpolates" : "Cover of"} ${who}`; };
  const byId = (id) => state.stations.find((x) => x.id === id);

  // ------------------------------------------------------------------ the dial
  // A strip of channel names that scrolls sideways (finger, wheel, or the arrows) and snaps one under the
  // needle. The channel under the needle after the strip settles is the one tuned in; scrolling past three
  // channels on the way to a fourth tunes only the fourth.
  function renderStrip() {
    const items = state.stations.map((s) => `<button class="ch ${s.id === state.station ? "on" : ""}" data-id="${esc(s.id)}" style="--c:var(--${esc(s.color || "dim")})"><i></i>${esc(s.name)}<small>${s.cooking ? "cooking" : s.ready + " songs"}</small></button>`).join("");
    strip.innerHTML = `<div class="pad"></div>${items || '<div class="hint">No channels are open right now.</div>'}<div class="pad"></div>`;
    strip.querySelectorAll(".ch").forEach((b) => b.addEventListener("click", () => { scrollToId(b.dataset.id, true); tuneIn(b.dataset.id); }));
    markCentred();
  }
  function chips() { return [...strip.querySelectorAll(".ch")]; }
  function centred() {
    const mid = strip.scrollLeft + strip.clientWidth / 2;
    let best = null, d = Infinity;
    for (const b of chips()) { const c = b.offsetLeft + b.offsetWidth / 2; const dd = Math.abs(c - mid); if (dd < d) { d = dd; best = b; } }
    return best;
  }
  function markCentred() {
    const b = centred();
    state.centred = b ? b.dataset.id : null;
    chips().forEach((x) => x.classList.toggle("on", x === b));
    $("#dial-name").textContent = b ? (byId(b.dataset.id) || {}).name || "" : "";
  }
  function scrollToId(id, smooth) {
    const b = chips().find((x) => x.dataset.id === id);
    if (!b) return;
    strip.scrollTo({ left: b.offsetLeft + b.offsetWidth / 2 - strip.clientWidth / 2, behavior: smooth ? "smooth" : "instant" });
  }
  function settled() {
    markCentred();
    if (state.centred && state.centred !== state.station) tuneIn(state.centred);
  }
  strip.addEventListener("scroll", () => { markCentred(); clearTimeout(state.settle); state.settle = setTimeout(settled, 450); }, { passive: true });
  function step(dir) {
    const list = chips(); if (!list.length) return;
    const i = Math.max(0, list.findIndex((x) => x.dataset.id === (state.centred || state.station)));
    const j = Math.min(list.length - 1, Math.max(0, i + dir));
    scrollToId(list[j].dataset.id, true);
  }
  $("#dial-left").addEventListener("click", () => step(-1));
  $("#dial-right").addEventListener("click", () => step(1));
  document.addEventListener("keydown", (e) => { if (e.key === "ArrowLeft") step(-1); if (e.key === "ArrowRight") step(1); if (e.key === " " && e.target === document.body) { e.preventDefault(); $("#btn-play").click(); } });
  strip.addEventListener("wheel", (e) => { if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { e.preventDefault(); strip.scrollLeft += e.deltaY; } }, { passive: false });

  async function loadStations() {
    try { state.stations = await api("api/stations"); renderStrip(); } catch (e) { strip.innerHTML = `<div class="hint">${esc(e.message)}</div>`; }
  }

  // ------------------------------------------------------------------ now playing
  function renderNow(s) {
    const st = byId(s.station_id);
    $("#now").innerHTML = `<div class="nt">${esc(s.title)}</div>${coverLine(s) ? `<div class="nc">${esc(coverLine(s))}</div>` : ""}`;
    document.title = `${s.title} · Ten Forward Radio`;
    $("#btn-dl").href = s.download; $("#btn-dl").hidden = false;
    $("#btn-next").disabled = false; $("#btn-play").disabled = false;
    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: s.title, artist: "Ten Forward Radio", album: st ? st.name : "", artwork: [{ src: "icon.png", sizes: "192x192", type: "image/png" }] });
      navigator.mediaSession.setActionHandler("play", () => audio.play());
      navigator.mediaSession.setActionHandler("pause", () => audio.pause());
      navigator.mediaSession.setActionHandler("nexttrack", () => next(false));
    }
  }
  function renderCooking(r) {
    const el = $("#cooking");
    const c = r.station && r.station.cooking;
    if (!c && !r.pending_jobs) { el.hidden = true; return; }
    const pct = Math.round(((c && c.progress) || 0) * 100);
    el.hidden = false;
    el.innerHTML = c ? `<div>${c.status === "running" ? "BEING MADE RIGHT NOW" : "NEXT IN LINE"}: ${esc(c.title || "untitled")}</div>${c.status === "running" ? `<div class="bar"><i style="width:${pct}%"></i></div><div class="hint">${pct}%</div>` : ""}`
                     : `<div>${r.pending_jobs} song${r.pending_jobs === 1 ? "" : "s"} in the line. One takes a few minutes.</div>`;
  }

  async function tuneIn(id) {
    if (!byId(id)) return;
    state.station = id; try { localStorage.setItem("tf_listen_station", id); } catch {}
    markCentred();
    await next(true);
  }
  async function next(fresh) {
    if (!state.station) return;
    clearTimeout(state.timer);
    const asked = state.station;
    try {
      const exclude = !fresh && state.song ? state.song.id : "";
      const r = await api(`api/next?station=${encodeURIComponent(asked)}&exclude=${encodeURIComponent(exclude)}`);
      if (asked !== state.station) return;        // the dial moved on while this was in flight
      renderCooking(r);
      if (r.song) {
        state.song = r.song; state.playedMarked = false;
        renderNow(r.song);
        audio.src = r.song.url; audio.play().catch(() => { $("#btn-play").textContent = "PLAY"; });
      } else {
        state.song = null; audio.removeAttribute("src");
        $("#now").innerHTML = `<div class="nt">Warming up…</div><div class="hint">${r.power === false ? "The radio is powered down right now; nothing new is being made." : r.asked ? "Nothing on this channel yet. A song was just asked for; it takes a few minutes." : "The first song is on its way."}</div>`;
        $("#btn-play").disabled = true; $("#btn-next").disabled = true; $("#btn-dl").hidden = true;
        state.timer = setTimeout(() => next(true), 15000);
      }
    } catch (e) {
      // the radio did not answer (restarting, or the line dropped): keep what is on screen and try again shortly
      $("#cooking").hidden = false; $("#cooking").innerHTML = `<div>Reconnecting to the radio… (${esc(e.message)})</div>`;
      state.timer = setTimeout(() => next(fresh), 5000);
    }
    pollCooking();
  }
  function pollCooking() {
    clearTimeout(state.poll);
    state.poll = setTimeout(async () => {
      try {
        const fresh = await api("api/stations");
        const same = fresh.length === state.stations.length && fresh.every((s, i) => s.id === state.stations[i].id);
        state.stations = fresh;
        if (same) chips().forEach((b) => { const s = byId(b.dataset.id); if (s) b.querySelector("small").textContent = s.cooking ? "cooking" : s.ready + " songs"; });
        else { renderStrip(); scrollToId(state.station, false); }
        const st = byId(state.station); if (st) renderCooking({ station: st, pending_jobs: st.cooking ? 1 : 0 });
      } catch {}
      pollCooking();
    }, 15000);
  }

  $("#btn-play").addEventListener("click", () => { if (!audio.src) { if (!state.station && state.centred) state.station = state.centred; next(true); return; } if (audio.paused) audio.play(); else audio.pause(); });
  $("#btn-next").addEventListener("click", () => next(false));
  audio.addEventListener("play", () => { $("#btn-play").textContent = "PAUSE"; });
  audio.addEventListener("pause", () => { $("#btn-play").textContent = "PLAY"; });
  audio.addEventListener("ended", () => next(false));
  audio.addEventListener("error", () => { if (audio.src) state.timer = setTimeout(() => next(false), 3000); });
  audio.addEventListener("timeupdate", () => {
    if (state.song && !state.playedMarked && audio.currentTime > 10) {
      state.playedMarked = true;
      api("api/played", { method: "POST", body: { id: state.song.id } }).catch(() => {});
    }
  });

  (async () => {
    try { const h = await api("api/hello"); if (h.tagline) $("#tagline").textContent = h.tagline; } catch {}
    await loadStations();
    if (state.station && !byId(state.station)) state.station = null;
    // park the needle on the remembered channel (or the first one) without starting sound: browsers want a tap first
    const start = state.station || (state.stations[0] || {}).id;
    if (start) { state.station = start; scrollToId(start, false); markCentred(); $("#btn-play").disabled = false; $("#now").innerHTML = `<div class="hint">Press PLAY to tune in to ${esc((byId(start) || {}).name || "")}, or turn the dial.</div>`; }
  })();
})();
