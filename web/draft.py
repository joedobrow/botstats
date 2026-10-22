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
from dataclasses import dataclass, field

import db
from web import store

logger = logging.getLogger(__name__)

# Where a draft is. The log decides which: no start event means lobby.
LOBBY, RUNNING, DONE = "lobby", "running", "done"


@dataclass
class State:
    """A draft, replayed. Derived from the events and never written to."""
    phase: str = LOBBY
    surrogates: dict[int, int] = field(default_factory=dict)  # captain -> who may act for them
    started_at: str | None = None


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
        elif kind == "start":
            st.phase, st.started_at = RUNNING, e["at"]
        elif kind == "commit":
            st.phase = DONE
    return st


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
        rows.append({**c, "surrogate": sub, "present": here,
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


def room(season: dict) -> Room:
    key = (season["id"], season["division_id"])
    if key not in _ROOMS:
        _ROOMS[key] = Room()
    return _ROOMS[key]
