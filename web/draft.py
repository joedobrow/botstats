"""The auction draft: a log of what happened, and the room watching it.

Everything anyone sees is replayed from `draft_events`, so the server can be
restarted in the middle of a draft and the room rebuilds exactly where it
was. Nothing is stored twice and nothing is updated in place — an undo will
be its own event, not a row that changes.

Presence is the one thing that isn't in the log. Who has a socket open is
only true right now, so it lives in memory and dies with the process, which
is the correct lifetime for it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import db
from web import store

logger = logging.getLogger(__name__)

# Where a draft is. The log decides which: no start event means lobby.
LOBBY, RUNNING, DONE = "lobby", "running", "done"

# What the auction is played with. An admin can change these before it
# starts; they go into the log, so what the rules were is part of the record.
DEFAULT_RULES = {
    "budget": 1000,        # what a captain has to spend unless they're given their own
    "slots": 4,            # players each captain drafts, on top of themselves
    "min_bid": 1,          # the smallest a player can go for
    "bid_seconds": 20,     # how long a lot stays open after the last bid
}


@dataclass
class Lot:
    """The player currently up, and the best bid on them."""
    player: int
    nominated_by: int
    bid: int
    bidder: int
    closes_at: float                     # unix seconds, the server's clock


@dataclass
class State:
    """A draft, replayed. Derived from the events and never written to."""
    phase: str = LOBBY
    surrogates: dict[int, int] = field(default_factory=dict)  # captain -> who may act for them
    started_at: str | None = None
    rules: dict = field(default_factory=lambda: dict(DEFAULT_RULES))
    budgets: dict[int, int] = field(default_factory=dict)     # captain -> their own purse
    order: list[int] = field(default_factory=list)            # whose turn to nominate, in turn
    turn: int = 0                                             # index into order
    lot: Lot | None = None
    rosters: dict[int, list[dict]] = field(default_factory=dict)   # captain -> [{account_id, cost}]

    # -- what the rules say, given what has happened ---------------------
    def budget(self, captain: int) -> int:
        """What this captain has to spend. Admins set these one by one, so a
        captain with no figure of their own falls back to the house default
        rather than to nothing."""
        return self.budgets.get(captain, self.rules["budget"])

    def spent(self, captain: int) -> int:
        return sum(p["cost"] for p in self.rosters.get(captain, ()))

    def slots_left(self, captain: int) -> int:
        return self.rules["slots"] - len(self.rosters.get(captain, ()))

    def max_bid(self, captain: int) -> int:
        """The most a captain can put on this player and still be able to
        fill the rest of their roster at the minimum price. A draft that
        leaves someone unable to field five is a broken draft."""
        left = self.slots_left(captain)
        if left <= 0:
            return 0
        reserve = self.rules["min_bid"] * (left - 1)
        return max(0, self.budget(captain) - self.spent(captain) - reserve)

    def taken(self) -> set[int]:
        return {p["account_id"] for roster in self.rosters.values() for p in roster}

    def done(self) -> bool:
        return bool(self.order) and all(self.slots_left(c) <= 0 for c in self.order)


def replay(events: list[dict]) -> State:
    """Fold the log into the state it describes."""
    st = State()
    for e in events:
        kind, p = e["kind"], e["payload"]
        if kind == "surrogate":
            captain, who = p.get("captain"), p.get("surrogate")
            if captain is None:
                continue
            if who:
                st.surrogates[captain] = who
            else:
                st.surrogates.pop(captain, None)
        elif kind == "rules":
            st.rules = {**DEFAULT_RULES, **(p.get("rules") or {})}
        elif kind == "budgets":
            # JSON has no integer keys, so they come back as strings.
            for captain, amount in (p.get("budgets") or {}).items():
                st.budgets[int(captain)] = int(amount)
        elif kind == "start":
            st.phase, st.started_at = RUNNING, e["at"]
            st.order = list(p.get("order") or [])
            st.rosters = {c: [] for c in st.order}
            st.turn = 0
        elif kind == "nominate":
            st.lot = Lot(player=p["player"], nominated_by=p["by"], bid=p["bid"],
                         bidder=p["by"], closes_at=p["closes_at"])
        elif kind == "bid":
            if st.lot:
                st.lot.bid, st.lot.bidder = p["amount"], p["captain"]
                st.lot.closes_at = p["closes_at"]
        elif kind == "sold":
            st.rosters.setdefault(p["captain"], []).append(
                {"account_id": p["player"], "cost": p["cost"]})
            st.lot = None
            st.turn = _next_turn(st, p.get("after_turn", st.turn))
        elif kind == "commit":
            st.phase = DONE
    return st


def _next_turn(st: State, from_turn: int) -> int:
    """The next captain with a slot to fill. Everyone full ends the draft,
    and the index stops moving."""
    if not st.order:
        return 0
    for step in range(1, len(st.order) + 1):
        i = (from_turn + step) % len(st.order)
        if st.slots_left(st.order[i]) > 0:
            return i
    return from_turn


def whose_turn(st: State) -> int | None:
    """The captain who nominates next, or None when nobody can."""
    if st.phase != RUNNING or not st.order:
        return None
    if st.slots_left(st.order[st.turn]) > 0:
        return st.order[st.turn]
    i = _next_turn(st, st.turn)
    return st.order[i] if st.slots_left(st.order[i]) > 0 else None


def state(season: dict) -> State:
    return replay(db.get_draft_events(season["id"], season["division_id"]))


def captains(season: dict) -> list[dict]:
    """Who drafts a team. Before a draft there are no teams yet, so this is
    the signup list — whoever an admin has settled on as a captain."""
    out = []
    for s in store.list_signups(season):
        if s["level_is"] != "captain":
            continue
        out.append({"account_id": s["account_id"], "name": s["name"],
                    "avatar_url": s.get("avatar_url"), "discord": s.get("discord")})
    out.sort(key=lambda c: c["name"].lower())
    return out


class Refused(Exception):
    """Why an action didn't happen, in words the room can show."""


def draftees(season: dict, st: State) -> list[dict]:
    """Who is still there to be bought: signed up to play, not a captain,
    not already on a team, and not the one currently under the hammer."""
    taken = st.taken()
    if st.lot:
        taken = taken | {st.lot.player}
    out = []
    for sg in store.list_signups(season):
        if sg["level_is"] != "player" or sg["account_id"] in taken:
            continue
        out.append({"account_id": sg["account_id"], "name": sg["name"],
                    "avatar_url": sg.get("avatar_url"),
                    "positions": sg.get("positions") or []})
    out.sort(key=lambda p: p["name"].lower())
    return out


def names(season: dict) -> dict[int, dict]:
    """{account_id: {name, avatar_url}} for everyone in this season's draft,
    so the room can talk in ids and still show faces."""
    out = {}
    for sg in store.list_signups(season):
        out[sg["account_id"]] = {"name": sg["name"], "avatar_url": sg.get("avatar_url")}
    return out


def _actor_for(st: State, captain: int, actor: int | None, is_admin: bool) -> None:
    if is_admin or may_act_for(st, captain, actor):
        return
    raise Refused("That isn't your team to bid for.")


def nominate(season: dict, st: State, *, actor: int | None, is_admin: bool,
             player: int, amount: int) -> None:
    """Put a player up, at an opening price. Only the captain whose turn it
    is — the order is fixed when the draft starts and never jumps."""
    if st.phase != RUNNING:
        raise Refused("The draft isn't running.")
    if st.lot is not None:
        raise Refused("Someone is already up for auction.")
    turn = whose_turn(st)
    if turn is None:
        raise Refused("Every roster is full.")
    _actor_for(st, turn, actor, is_admin)
    if player not in {p["account_id"] for p in draftees(season, st)}:
        raise Refused("That player isn't in the draft.")
    low, high = st.rules["min_bid"], st.max_bid(turn)
    if amount < low:
        raise Refused(f"The opening bid is at least {low}.")
    if amount > high:
        raise Refused(f"That leaves too little to fill the roster — {high} is the most you can bid.")
    closes_at = time.time() + st.rules["bid_seconds"]
    db.append_draft_event(season["id"], season["division_id"], "nominate", actor,
                          {"player": player, "by": turn, "bid": amount, "closes_at": closes_at})


def bid(season: dict, st: State, *, actor: int | None, is_admin: bool,
        captain: int, amount: int) -> None:
    """Raise. Every bid puts the clock back to the full countdown, so the
    last one always has the same chance to be answered as the first."""
    if st.phase != RUNNING or st.lot is None:
        raise Refused("Nobody is up for auction.")
    _actor_for(st, captain, actor, is_admin)
    if captain not in st.rosters:
        raise Refused("That isn't a captain in this draft.")
    if st.slots_left(captain) <= 0:
        raise Refused("That roster is full.")
    if st.lot.bidder == captain:
        raise Refused("You're already the high bid.")
    if amount <= st.lot.bid:
        raise Refused(f"The bid is {st.lot.bid}; you have to beat it.")
    high = st.max_bid(captain)
    if amount > high:
        raise Refused(f"That leaves too little to fill the roster — {high} is the most you can bid.")
    closes_at = time.time() + st.rules["bid_seconds"]
    db.append_draft_event(season["id"], season["division_id"], "bid", actor,
                          {"captain": captain, "amount": amount, "closes_at": closes_at})


def close_lot(season: dict, st: State) -> bool:
    """Sell, if the clock has run out. The server decides this, never a
    client — and it's safe to ask as often as you like, because a lot that
    has already been sold isn't a lot any more."""
    if st.phase != RUNNING or st.lot is None or time.time() < st.lot.closes_at:
        return False
    db.append_draft_event(season["id"], season["division_id"], "sold", None,
                          {"player": st.lot.player, "captain": st.lot.bidder,
                           "cost": st.lot.bid, "after_turn": st.turn})
    return True


def may_act_for(st: State, captain_id: int, account_id: int | None) -> bool:
    """Whether this account can bid for that team: the captain, or whoever
    an admin put in their seat."""
    if account_id is None:
        return False
    return account_id == captain_id or st.surrogates.get(captain_id) == account_id


def lobby(season: dict, st: State, present: set[int]) -> dict:
    """What the lobby shows: every captain, whether their seat is filled,
    and whether that's all of them.

    A seat is filled by the captain or by their surrogate, which is the same
    question the draft will ask before accepting a bid — so "everyone is
    here" and "everyone can act" can't drift apart.
    """
    rows, missing = [], []
    for c in captains(season):
        sub = st.surrogates.get(c["account_id"])
        here = c["account_id"] in present
        sub_here = bool(sub and sub in present)
        rows.append({**c, "surrogate": sub, "present": here, "budget": st.budget(c["account_id"]),
                     "surrogate_present": sub_here, "seated": here or sub_here})
        if not (here or sub_here):
            missing.append(c["name"])
    return {"phase": st.phase, "captains": rows, "missing": missing,
            "can_start": bool(rows) and not missing, "started_at": st.started_at}


# ---------------------------------------------------------------------------
# The room: who is watching, right now
# ---------------------------------------------------------------------------

class Room:
    """The sockets open on one division's draft."""

    def __init__(self) -> None:
        self.sockets: dict[object, int | None] = {}   # ws -> the account watching
        self._lock = asyncio.Lock()

    def present(self) -> set[int]:
        return {aid for aid in self.sockets.values() if aid is not None}

    def join(self, ws, account_id: int | None) -> None:
        """One entry per socket, so a captain with two tabs open is one
        person present, and closing one tab doesn't empty their seat."""
        self.sockets[ws] = account_id

    def leave(self, ws) -> None:
        self.sockets.pop(ws, None)

    async def broadcast(self, message: dict) -> None:
        """Tell everyone watching. A socket that has gone away is dropped
        rather than retried — the client reconnects and asks for a snapshot."""
        async with self._lock:
            for ws in list(self.sockets):
                try:
                    await ws.send_json(message)
                except Exception:
                    self.sockets.pop(ws, None)


_ROOMS: dict[tuple[int, int], Room] = {}
_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
_TIMERS: dict[tuple[int, int], asyncio.Task] = {}


def _key(season: dict) -> tuple[int, int]:
    return (season["id"], season["division_id"])


def lock(season: dict) -> asyncio.Lock:
    """Held across read-state → check → append, so two bids landing together
    can't both be measured against the same standing bid and then applied in
    the other order. The log decides what happened; this decides what gets
    into the log."""
    return _LOCKS.setdefault(_key(season), asyncio.Lock())


async def push(season: dict) -> None:
    """Tell the room what the draft looks like now. One message carrying the
    whole picture: it's small, and a client that reconnects gets the same
    thing it would have got by listening the whole time."""
    r = room(season)
    await r.broadcast({"type": "state", **view(season, state(season), r.present())})


async def _run_clock(season: dict) -> None:
    """Sell the open lot when its time is up.

    Sleeps in short steps rather than once until the deadline, because every
    bid moves the deadline — re-reading is simpler than cancelling and
    rescheduling, and a second of drift on a twenty-second clock is nothing.
    """
    try:
        while True:
            st = state(season)
            if st.phase != RUNNING or st.lot is None:
                return
            wait = st.lot.closes_at - time.time()
            if wait > 0:
                await asyncio.sleep(min(wait, 0.5))
                continue
            async with lock(season):
                if close_lot(season, state(season)):
                    await push(season)
                    return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("draft clock failed for %s", _key(season))
    finally:
        _TIMERS.pop(_key(season), None)


def start_clock(season: dict) -> None:
    """Make sure a lot that is open has something counting it down. Safe to
    call whenever: an already-running clock is left alone, and one that has
    finished is replaced."""
    key = _key(season)
    task = _TIMERS.get(key)
    if task and not task.done():
        return
    _TIMERS[key] = asyncio.create_task(_run_clock(season))


def view(season: dict, st: State, present: set[int]) -> dict:
    """Everything the room draws, in one object."""
    out = lobby(season, st, present)
    out["rules"] = st.rules
    out["turn"] = whose_turn(st)
    out["teams"] = [{"captain": c, "spent": st.spent(c), "budget": st.budget(c),
                     "left": st.budget(c) - st.spent(c),
                     "slots_left": st.slots_left(c), "max_bid": st.max_bid(c),
                     "roster": st.rosters.get(c, [])} for c in st.order]
    out["pool"] = draftees(season, st) if st.phase == RUNNING else []
    out["lot"] = (None if not st.lot else
                  {"player": st.lot.player, "bid": st.lot.bid, "bidder": st.lot.bidder,
                   "closes_in": max(0.0, st.lot.closes_at - time.time())})
    out["done"] = st.done()
    out["stuck"] = bool(st.phase == RUNNING and not st.done() and not out["pool"] and not st.lot)
    return out




def room(season: dict) -> Room:
    key = (season["id"], season["division_id"])
    if key not in _ROOMS:
        _ROOMS[key] = Room()
    return _ROOMS[key]
