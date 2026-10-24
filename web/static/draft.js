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
  const PROFILES = D.profiles || {};
  let CAPTAINS = new Set(((D.view || {}).captains || []).map(c => String(c.account_id)));

  // The rating, as a number on the pill. Blank for someone never rated.
  const ratingTag = id => {
    const r = PROFILES[id] && PROFILES[id].rating;
    return r != null ? `<span class="pill-rating">${r}</span>` : "";
  };
  // Every name is a pill: captains in the league's red, players plain. A
  // pill opens that player's card under the pool rather than leaving the
  // room; everything on the card came with the page, so it opens at once.
  // price: what they sold for, written into the pill once they're bought.
  const pill = (id, text, extra = "", price = null) =>
    `<button type="button" class="pill ${CAPTAINS.has(String(id)) ? "captain" : "player"} ${extra}"
      data-player="${id}">${face(id)}<span class="pill-name">${
      esc(text == null ? who(id) : text)}</span>${ratingTag(id)}${
      price != null ? `<span class="pill-price">– $${price}</span>` : ""}</button>`;
  const POSITIONS = {1: "Safe lane", 2: "Mid", 3: "Off lane", 4: "Soft support", 5: "Hard support"};

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
        <span class="dot ${c.seated ? "on" : "off"}"></span>${pill(c.account_id, c.name)}
        <span class="muted small">${c.budget == null ? "no budget" : c.budget}</span>
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
      const force = $("force-start");
      if (force) force.hidden = v.can_start || !v.can_force;
      $("blocked").textContent = v.can_start ? ""
        : v.unbudgeted && v.unbudgeted.length ? "no budget yet: " + v.unbudgeted.join(", ")
        : "waiting on " + v.missing.join(", ");
    }
  }

  // The amount box survives every repaint: other people's bids arrive
  // while you're typing, and a box that reset itself would eat your number.
  // It only moves on its own when what's in it is no longer a raise.
  let lotKey = null, typed = false;

  function drawLot(v) {
    const panel = $("lot-panel");
    panel.hidden = !v.lot;
    if (!v.lot) { lotKey = null; return; }
    $("lot").innerHTML = `<div class="lot-player">${pill(v.lot.player, null, "big")}</div>
      <div class="lot-bid"><b>${v.lot.bid}</b> <span class="muted small">to ${esc(who(v.lot.bidder))}</span></div>`;
    const amount = $("bid-amount");
    if (lotKey !== v.lot.player) { lotKey = v.lot.player; typed = false; }
    if (!typed || !(Number(amount.value) > v.lot.bid)) amount.value = v.lot.bid + 1;
    amount.min = v.lot.bid + 1;

    const ours = MINE.filter(c => v.teams.some(t => t.captain === c));
    $("bid-buttons").innerHTML = ours.map(c => `<button class="primary" data-bid="${c}">
        Bid${ours.length > 1 ? " as " + esc(who(c)) : ""}</button>`).join("");
    // An admin can bid for any team: the captain who lost their connection,
    // or every team at once when the draft is being tried out.
    const adminBox = $("admin-bid");
    if (adminBox) {
      const sel = $("admin-bid-as"), keep = sel.value;
      const others = v.teams.map(t => t.captain).filter(c => !ours.includes(c) && c !== v.lot.bidder);
      adminBox.hidden = !others.length;
      sel.innerHTML = others.map(c => `<option value="${c}"${String(c) === keep ? " selected" : ""}>${esc(who(c))}</option>`).join("");
    }
    $("bid-row").hidden = !(ours.length || (adminBox && !adminBox.hidden));
    checkBids();
  }

  // Grey out a bid button when that team can't make the bid in the box.
  function checkBids() {
    const v = view;
    if (!v.lot) return;
    const amount = Number($("bid-amount").value);
    const ok = c => {
      const team = v.teams.find(t => String(t.captain) === String(c)) || {};
      return !v.paused && String(c) !== String(v.lot.bidder) && team.slots_left > 0
        && amount > v.lot.bid && team.max_bid >= amount;
    };
    document.querySelectorAll("#bid-buttons [data-bid]").forEach(b => { b.disabled = !ok(b.dataset.bid); });
    const go = $("admin-bid-go");
    if (go) go.disabled = !ok($("admin-bid-as").value);
  }

  function drawTurn(v) {
    const panel = $("turn-panel");
    const running = v.phase === "running" && !v.done;
    panel.hidden = !running || !!v.lot;
    if (panel.hidden) return;
    const mineTurn = v.turn && (MINE.includes(v.turn) || ADMIN) && !v.paused;
    $("turn-head").textContent = v.paused ? "Paused" : v.turn
      ? (MINE.includes(v.turn) ? "Your nomination"
         : ADMIN ? "Nominate for " + who(v.turn) : who(v.turn) + " is nominating")
      : "Nobody left to nominate";
    const box = $("nominate");
    box.hidden = !mineTurn;
    if (!mineTurn) return;
    const sel = $("nom-player");
    const keep = sel.value;
    sel.innerHTML = v.pool.map(p => `<option value="${p.account_id}">${esc(p.name)}</option>`).join("");
    if (keep && v.pool.some(p => String(p.account_id) === keep)) sel.value = keep;
    const team = v.teams.find(t => t.captain === v.turn) || {};
    $("nom-amount").min = 1;
    $("nom-amount").max = team.max_bid || 1;
  }

  let posFilter = 0;
  function drawTeams(v) {
    $("running").hidden = v.phase === "lobby";
    $("teams").innerHTML = v.teams.map(t => `
      <div class="team-line${t.captain === v.turn ? " up" : ""}">
        <div class="team-who">${pill(t.captain)}</div>
        <div class="team-money muted small">${t.left} left</div>
        <div class="team-roster">${t.roster.map(p => pill(p.account_id, null, "", p.cost)).join("")}</div>
      </div>`).join("");
    // A position picked in the header narrows the pool to those who'll play it.
    const shown = posFilter ? v.pool.filter(p => (p.positions || []).includes(posFilter)) : v.pool;
    $("pool").innerHTML = shown.map(p => `<li>${pill(p.account_id)}</li>`).join("");
    $("pool-count").textContent = !v.pool.length ? ""
      : posFilter ? `${shown.length} of ${v.pool.length}` : v.pool.length + " left";
    document.querySelectorAll("#pos-filter [data-pos]").forEach(b => {
      const on = Number(b.dataset.pos) === posFilter;
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.classList.toggle("off", !!posFilter && !on);
    });
    drawBreakdown(v);
  }

  // Each team at a glance: its average rating and every position its people
  // said they'd play, captain included, so the roles still missing show.
  const posDot = n => `<span class="pos pos-${n}" title="${POSITIONS[n] || ""}">${n}</span>`;
  function drawBreakdown(v) {
    // Strongest team on average at the top; a team nobody has a rating for
    // goes to the bottom.
    const rows = v.teams.map(t => {
      const people = [t.captain, ...t.roster.map(p => p.account_id)];
      const ratings = people.map(id => PROFILES[id] && PROFILES[id].rating).filter(r => r != null);
      const avg = ratings.length ? Math.round(ratings.reduce((a, b) => a + b, 0) / ratings.length) : null;
      const dots = people.flatMap(id => (PROFILES[id] && PROFILES[id].positions) || [])
        .sort((a, b) => a - b).map(posDot).join("");
      return {avg, html: `<div class="tb-row"><div>${pill(t.captain)}</div>
        <div class="tb-avg"><span class="label">AVG:</span> ${avg == null ? "—" : avg}</div>
        <div class="tb-dots pos-list">${dots}</div></div>`};
    });
    rows.sort((a, b) => (b.avg ?? -1) - (a.avg ?? -1));
    $("breakdown").innerHTML = rows.map(r => r.html).join("");
  }

  function drawUndo(v) {
    const row = $("undo-row");
    if (!row) return;
    row.hidden = !(ADMIN && v.phase === "running" && !v.done);
    $("undo").hidden = !v.undoable;
    if (v.undoable) $("undo-what").textContent = v.undoable;
    const pause = $("pause");
    pause.textContent = v.paused ? "Resume" : "Pause";
    pause.classList.toggle("primary", !!v.paused);
  }

  function drawCommit(v) {
    const panel = $("commit-panel");
    if (!panel) return;
    panel.hidden = !(ADMIN && v.done && v.phase === "running");
    if (!panel.hidden) {
      const n = v.teams.reduce((a, t) => a + t.roster.length, 0);
      $("commit-note").textContent = v.teams.length + " teams, " + n + " players drafted.";
    }
    if (v.phase === "done") {
      const note = $("commit-note");
      if (note) { panel.hidden = false; $("commit-form").hidden = true;
                  note.textContent = "Written into the season."; }
    }
  }

  function showCard(id) {
    const p = PROFILES[id], card = $("player-card");
    if (!card) return;
    const row = (label, value) => value === null || value === undefined || value === "" ? ""
      : `<div class="pc-row"><span class="label">${label}</span><span>${value}</span></div>`;
    const won = p && p.titles ? `<span class="rings" title="Won ${p.titles} season${p.titles === 1 ? "" : "s"}">${"\u{1F48D}".repeat(p.titles)}</span>` : "";
    $("pc-name").innerHTML = face(id) + esc(who(id)) + won;
    if (!p) {
      $("pc-body").innerHTML = `<p class="muted">Not signed up for this season.</p>
        <p class="small"><a href="/player/${id}" target="_blank" rel="noopener">Full profile</a></p>`;
    } else {
      const record = p.games
        ? `${p.games} games · ${p.wins}–${p.games - p.wins} · ${Math.round(100 * p.wins / p.games)}%` : "No league games";
      const history = p.history.length ? `<table class="compact pc-history"><tbody>${p.history.map(h => `
          <tr><td>${esc(h.season)}</td><td>${esc(h.team || "")}</td>
              <td class="num">${h.captain ? '<span class="tag">C</span>' : (h.cost != null ? "$" + h.cost : "")}</td></tr>`).join("")}
        </tbody></table>` : "";
      $("pc-body").innerHTML =
        row("Rating", p.rating != null ? `<b>${p.rating}</b>` : "") +
        row("Windrun", p.windrun != null ? `${p.windrun}${p.windrun_games ? ` <span class="muted small">· ${p.windrun_games} games</span>` : ""}` : "") +
        row("Rank", p.badge ? esc(p.badge) : "") +
        row("Positions", `<span class="pos-list">${p.positions.map(n =>
          `<span class="pos pos-${n}" title="${POSITIONS[n] || ""}">${n}</span>`).join("")}</span>`) +
        row("League", record) +
        (p.games ? row("Per game", `${p.fantasy} fantasy · ${p.kda} KDA · ${p.gpm} GPM`) : "") +
        row("Also signed up", p.other_divisions.length ? esc(p.other_divisions.join(", ")) : "") +
        (p.comment ? `<p class="pc-comment">“${esc(p.comment)}”</p>` : "") +
        history +
        `<p class="small"><a href="/player/${id}" target="_blank" rel="noopener">Full profile</a></p>`;
    }
    card.hidden = false;
  }

  function paint(v) {
    view = v;
    CAPTAINS = new Set((v.captains || []).map(c => String(c.account_id)));
    $("phase").textContent = v.paused ? "Paused" : (PHASES[v.phase] || v.phase);
    $("phase").classList.toggle("paused", !!v.paused);
    $("lobby-panel").hidden = v.phase !== "lobby";
    ["admin-lobby", "admin-rules", "admin-budgets"].forEach(id => { const e = $(id); if (e) e.hidden = v.phase !== "lobby"; });
    drawSeats(v);
    drawLot(v);
    drawTurn(v);
    drawTeams(v);
    drawUndo(v);
    drawCommit(v);
    closesAt = v.lot && !v.paused ? Date.now() + v.lot.closes_in * 1000 : 0;
    nominateAt = v.nominate_in != null && !v.paused ? Date.now() + v.nominate_in * 1000 : 0;
    nominateFrozen = v.nominate_in != null && v.paused ? v.nominate_in : null;
    frozen = v.lot && v.paused ? v.lot.closes_in : null;
    tick();
    if (v.stuck) say("Nobody is left to nominate and rosters aren't full — add more signups.");
  }

  let frozen = null;              // seconds left on a paused lot
  let nominateAt = 0, nominateFrozen = null;
  function tick() {
    const nc = $("nom-clock");
    if (nominateFrozen !== null) nc.textContent = "paused · " + nominateFrozen.toFixed(0) + "s";
    else if (nominateAt) {
      const n = Math.max(0, (nominateAt - Date.now()) / 1000);
      nc.textContent = n.toFixed(0) + "s";
      nc.classList.toggle("soon", n <= 5);
    } else nc.textContent = "";
    const clock = $("clock");
    if (frozen !== null) { clock.textContent = "paused · " + frozen.toFixed(0) + "s"; clock.classList.remove("soon"); return; }
    if (!closesAt) { clock.textContent = ""; return; }
    const left = Math.max(0, (closesAt - Date.now()) / 1000);
    clock.textContent = left.toFixed(0) + "s";
    clock.classList.toggle("soon", left <= 5);
  }
  setInterval(tick, 200);

  document.addEventListener("click", e => {
    const posBtn = e.target.closest("#pos-filter [data-pos]");
    if (posBtn) {
      const n = Number(posBtn.dataset.pos);
      posFilter = posFilter === n ? 0 : n;
      drawTeams(view);
      return;
    }
    const name = e.target.closest("[data-player]");
    if (name) { showCard(name.dataset.player); return; }
    const bid = e.target.closest("[data-bid]");
    if (bid && view.lot) placeBid(bid.dataset.bid);
    if (e.target.id === "admin-bid-go" && view.lot) placeBid($("admin-bid-as").value);
    if (e.target.id === "pause") send(view.paused ? "/resume" : "/pause", {});
  });
  function placeBid(captain) {
    send("/bid", {captain, amount: $("bid-amount").value});
    typed = false;
  }
  const amountBox = $("bid-amount");
  amountBox.addEventListener("input", () => { typed = true; checkBids(); });
  // Enter bids for your own team, or for the one picked in the admin box.
  amountBox.addEventListener("keydown", e => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const own = document.querySelector("#bid-buttons [data-bid]:not([disabled])");
    if (own) own.click();
    else if ($("admin-bid-go") && !$("admin-bid-go").disabled) placeBid($("admin-bid-as").value);
  });
  const adminAs = $("admin-bid-as");
  if (adminAs) adminAs.addEventListener("change", checkBids);
  const undoBtn = $("undo");
  if (undoBtn) undoBtn.addEventListener("click", () => {
    if (confirm("Take back " + $("undo-what").textContent + "?")) send("/undo", {});
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
