// The draft room. The server says what is true; this only ever draws it.
// Every message carries the whole state, so there is no diffing to get
// wrong and a reconnect needs no catching up.
(() => {
  const room = document.getElementById("room");
  const data = document.getElementById("draft-data");
  if (!room || !data) return;
  const D = JSON.parse(data.textContent);
  const ME = D.me, MINE = D.mine || [], NAMES = D.names || {}, ADMIN = !!D.admin, BASE = D.base;
  const CSRF = room.dataset.csrf;
  const $ = id => document.getElementById(id);
  const PHASES = {lobby: "Waiting for captains", running: "Running", done: "Done"};

  const who = id => (NAMES[id] && NAMES[id].name) || ("#" + id);
  const face = id => {
    const a = NAMES[id] && NAMES[id].avatar_url;
    return a ? `<img class="avatar" src="${a}" alt="">` : "";
  };
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const chip = id => `<span class="chip">${face(id)}<a href="/player/${id}">${esc(who(id))}</a></span>`;

  let view = D.view || {};
  let closesAt = 0;                 // local clock, resynced on every message

  function say(why) {
    const box = $("why");
    box.textContent = why || "";
    box.hidden = !why;
    if (why) setTimeout(() => { box.hidden = true; }, 6000);
  }

  async function send(path, body) {
    const form = new URLSearchParams({csrf: CSRF, ...body});
    const r = await fetch(BASE + path, {method: "POST", body: form,
                                        headers: {"Content-Type": "application/x-www-form-urlencoded"}});
    if (!r.ok) {
      let why = "That didn't work.";
      try { why = (await r.json()).why || why; } catch (e) {}
      say(why);
    }
  }

  function drawSeats(v) {
    const ul = $("seats");
    ul.innerHTML = v.captains.map(c => `
      <li>
        <span class="dot ${c.seated ? "on" : "off"}"></span>${face(c.account_id)}
        <a href="/player/${c.account_id}">${esc(c.name)}</a>
        <span class="seat-note muted small">${
          c.surrogate ? (c.surrogate_present ? "seat filled for them" : "someone is sitting in, but isn't here")
                      : (c.present ? "" : "not here")}</span>
      </li>`).join("");
    $("seated").textContent = v.captains.length
      ? v.captains.filter(c => c.seated).length + " of " + v.captains.length + " here" : "";
    $("no-captains").hidden = v.captains.length > 0;
    const sur = $("sur-captain");
    if (sur && sur.options.length !== v.captains.length) {
      sur.innerHTML = v.captains.map(c => `<option value="${c.account_id}">${esc(c.name)}</option>`).join("");
    }
    const start = $("start");
    if (start) {
      start.disabled = !v.can_start;
      $("blocked").textContent = v.can_start ? "" : ("waiting on " + v.missing.join(", "));
    }
  }

  function drawLot(v) {
    const panel = $("lot-panel");
    panel.hidden = !v.lot;
    if (!v.lot) { $("bids").innerHTML = ""; return; }
    $("lot").innerHTML = `<div class="lot-player">${face(v.lot.player)}
        <a href="/player/${v.lot.player}">${esc(who(v.lot.player))}</a></div>
      <div class="lot-bid"><b>${v.lot.bid}</b> <span class="muted small">to ${esc(who(v.lot.bidder))}</span></div>`;
    // One button per team you may bid for, at the next price up.
    const next = v.lot.bid + 1;
    $("bids").innerHTML = MINE.filter(c => c !== v.lot.bidder).map(c => {
      const team = v.teams.find(t => t.captain === c) || {};
      const can = (team.slots_left > 0) && (team.max_bid >= next);
      return `<button class="primary" data-bid="${c}" ${can ? "" : "disabled"}>
        Bid ${next}${MINE.length > 1 ? " as " + esc(who(c)) : ""}</button>`;
    }).join("");
  }

  function drawTurn(v) {
    const panel = $("turn-panel");
    const running = v.phase === "running" && !v.done;
    panel.hidden = !running || !!v.lot;
    if (panel.hidden) return;
    const mineTurn = v.turn && (MINE.includes(v.turn) || ADMIN);
    $("turn-head").textContent = v.turn
      ? (mineTurn ? "Your nomination" : who(v.turn) + " is nominating")
      : "Nobody left to nominate";
    const box = $("nominate");
    box.hidden = !mineTurn;
    if (!mineTurn) return;
    const sel = $("nom-player");
    const keep = sel.value;
    sel.innerHTML = v.pool.map(p => `<option value="${p.account_id}">${esc(p.name)}</option>`).join("");
    if (keep && v.pool.some(p => String(p.account_id) === keep)) sel.value = keep;
    const team = v.teams.find(t => t.captain === v.turn) || {};
    $("nom-amount").min = v.rules.min_bid;
    $("nom-amount").max = team.max_bid || v.rules.min_bid;
  }

  function drawTeams(v) {
    $("running").hidden = v.phase === "lobby";
    $("teams").innerHTML = v.teams.map(t => `
      <div class="team-line${t.captain === v.turn ? " up" : ""}">
        <div class="team-who">${chip(t.captain)}</div>
        <div class="team-money muted small">${t.left} left · ${t.slots_left} to fill</div>
        <div class="team-roster">${t.roster.map(p => `${chip(p.account_id)}<span class="muted small">${p.cost}</span>`).join("")}</div>
      </div>`).join("");
    $("pool").innerHTML = v.pool.map(p => `<li>${chip(p.account_id)}</li>`).join("");
    $("pool-count").textContent = v.pool.length ? v.pool.length + " left" : "";
  }

  function paint(v) {
    view = v;
    $("phase").textContent = PHASES[v.phase] || v.phase;
    $("lobby-panel").hidden = v.phase !== "lobby";
    ["admin-lobby", "admin-rules"].forEach(id => { const e = $(id); if (e) e.hidden = v.phase !== "lobby"; });
    drawSeats(v);
    drawLot(v);
    drawTurn(v);
    drawTeams(v);
    closesAt = v.lot ? Date.now() + v.lot.closes_in * 1000 : 0;
    tick();
    if (v.stuck) say("Nobody is left to nominate and rosters aren't full — add more signups.");
  }

  function tick() {
    const clock = $("clock");
    if (!closesAt) { clock.textContent = ""; return; }
    const left = Math.max(0, (closesAt - Date.now()) / 1000);
    clock.textContent = left.toFixed(0) + "s";
    clock.classList.toggle("soon", left <= 5);
  }
  setInterval(tick, 200);

  document.addEventListener("click", e => {
    const bid = e.target.closest("[data-bid]");
    if (bid && view.lot) send("/bid", {captain: bid.dataset.bid, amount: view.lot.bid + 1});
  });
  const go = $("nom-go");
  if (go) go.addEventListener("click", () => {
    send("/nominate", {player: $("nom-player").value, amount: $("nom-amount").value});
  });

  let wait = 1000;
  function connect() {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://")
                             + location.host + location.pathname + "/ws");
    ws.onopen = () => { wait = 1000; };
    ws.onmessage = e => {
      const msg = JSON.parse(e.data);
      if (msg.type === "state") paint(msg);
    };
    ws.onclose = () => { setTimeout(connect, wait); wait = Math.min(wait * 2, 15000); };
    ws.onerror = () => ws.close();
  }
  paint(view);
  connect();
})();
