"""Self-contained checks on the auction: python3 tests/test_draft.py

Runs against a throwaway database in a temp directory, so it never touches
real data. The draft is the one part of this site where being wrong is
expensive and hard to undo, so the rules that protect it — whose turn,
what a captain can afford, who has already been bought — are checked here
rather than in a room full of captains.
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="draft-test-")
os.environ["DB_PATH"] = os.path.join(TMP, "test.db")
os.environ.setdefault("DISCORD_TOKEN", "dev")

import db                       # noqa: E402
from web import draft, store    # noqa: E402

SEASON = {"id": 99001, "division_id": 9}
CAPS = [101, 102, 103]
PLAYERS = [201, 202, 203, 204, 205, 206, 207]
STANDIN = 301

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(label)


def refuses(label: str, fn) -> None:
    """An action the rules should turn away, with a reason."""
    try:
        fn()
    except draft.Refused as e:
        check(label, True)
        return
    check(label, False, "it was allowed")


def setup() -> None:
    db.init_db()
    store.init_site_tables()
    now = "2026-01-01T00:00:00+00:00"
    with store._conn() as conn:
        for aid, level in ([(c, "captain") for c in CAPS]
                           + [(p, "player") for p in PLAYERS]
                           + [(STANDIN, "standin")]):
            conn.execute(
                "INSERT INTO site_signups (season_id, division_id, account_id, positions, "
                "captain, level, created_at, updated_at) VALUES (?, ?, ?, '1', 'no', ?, ?, ?)",
                (SEASON["id"], SEASON["division_id"], aid, level, now, now))


def start(**rules) -> draft.State:
    db.append_draft_event(SEASON["id"], SEASON["division_id"], "rules", 1,
                          {"rules": {**{"budget": 100, "slots": 2, "min_bid": 1,
                                        "bid_seconds": 0}, **rules}})
    db.append_draft_event(SEASON["id"], SEASON["division_id"], "start", 1, {"order": CAPS})
    return draft.state(SEASON)


def sell(player: int, winner: int, price: int) -> None:
    """Nominate, let the winner take it, and close the lot."""
    st = draft.state(SEASON)
    turn = draft.whose_turn(st)
    draft.nominate(SEASON, st, actor=turn, is_admin=False, player=player, amount=1)
    st = draft.state(SEASON)
    if winner != st.lot.bidder:
        draft.bid(SEASON, st, actor=winner, is_admin=False, captain=winner, amount=price)
        st = draft.state(SEASON)
    draft.close_lot(SEASON, st)


def main() -> int:
    setup()
    st = draft.state(SEASON)
    check("a draft with no events is a lobby", st.phase == draft.LOBBY)
    check("captains come from the signups", [c["account_id"] for c in draft.captains(SEASON)] == CAPS)

    st = start()
    check("starting fixes the nomination order", st.order == CAPS and st.phase == draft.RUNNING)
    check("the first turn is the first captain", draft.whose_turn(st) == 101)
    check("a stand-in isn't in the draft",
          STANDIN not in {p["account_id"] for p in draft.draftees(SEASON, st)})
    check("neither is a captain",
          not ({c for c in CAPS} & {p["account_id"] for p in draft.draftees(SEASON, st)}))

    refuses("nominating out of turn is refused",
            lambda: draft.nominate(SEASON, st, actor=102, is_admin=False, player=201, amount=1))
    refuses("nominating a stand-in is refused",
            lambda: draft.nominate(SEASON, st, actor=101, is_admin=False, player=STANDIN, amount=1))
    refuses("nominating below the minimum is refused",
            lambda: draft.nominate(SEASON, st, actor=101, is_admin=False, player=201, amount=0))
    refuses("nominating more than the budget allows is refused",
            lambda: draft.nominate(SEASON, st, actor=101, is_admin=False, player=201, amount=100))

    # 100 to spend, 2 slots, minimum 1: the most for the first is 99.
    check("the reserve keeps a slot's worth back", st.max_bid(101) == 99)

    draft.nominate(SEASON, st, actor=101, is_admin=False, player=201, amount=5)
    st = draft.state(SEASON)
    check("a nomination opens a lot at that price",
          st.lot and st.lot.player == 201 and st.lot.bid == 5 and st.lot.bidder == 101)
    refuses("two lots at once is refused",
            lambda: draft.nominate(SEASON, st, actor=101, is_admin=False, player=202, amount=1))
    refuses("bidding under the standing bid is refused",
            lambda: draft.bid(SEASON, st, actor=102, is_admin=False, captain=102, amount=5))
    refuses("bidding against yourself is refused",
            lambda: draft.bid(SEASON, st, actor=101, is_admin=False, captain=101, amount=9))
    refuses("bidding for someone else's team is refused",
            lambda: draft.bid(SEASON, st, actor=103, is_admin=False, captain=102, amount=9))
    refuses("bidding past the reserve is refused",
            lambda: draft.bid(SEASON, st, actor=102, is_admin=False, captain=102, amount=100))

    draft.bid(SEASON, st, actor=102, is_admin=False, captain=102, amount=9)
    st = draft.state(SEASON)
    check("a bid takes the lot", st.lot.bid == 9 and st.lot.bidder == 102)
    check("a surrogate may bid for their captain",
          draft.may_act_for(draft.replay([{"kind": "surrogate", "at": "", "payload":
                                           {"captain": 103, "surrogate": 999}}]), 103, 999))

    check("the lot doesn't close early",
          not draft.close_lot(SEASON, draft.replay(
              [{"kind": "start", "at": "", "payload": {"order": CAPS}},
               {"kind": "nominate", "at": "", "payload": {"player": 201, "by": 101, "bid": 1,
                                                          "closes_at": time.time() + 60}}])))
    check("the lot closes when its time is up", draft.close_lot(SEASON, st))
    st = draft.state(SEASON)
    check("the winner gets the player at their bid",
          st.rosters[102] == [{"account_id": 201, "cost": 9}] and st.spent(102) == 9)
    check("the lot is over", st.lot is None)
    check("the turn moves on", draft.whose_turn(st) == 102)
    check("a bought player is out of the pool",
          201 not in {p["account_id"] for p in draft.draftees(SEASON, st)})

    # Play it out and see that nothing the rules forbid has happened.
    guard = 0
    while not draft.state(SEASON).done() and guard < 50:
        guard += 1
        st = draft.state(SEASON)
        pool = draft.draftees(SEASON, st)
        if not pool:
            break
        sell(pool[0]["account_id"], draft.whose_turn(st), 1)
    st = draft.state(SEASON)
    check("every roster filled", all(st.slots_left(c) == 0 for c in CAPS), str(st.rosters))
    check("nobody was sold twice",
          len(st.taken()) == sum(len(r) for r in st.rosters.values()))
    check("nobody overspent", all(st.spent(c) <= st.budget(c) for c in CAPS))
    check("the draft knows it's finished", st.done())
    refuses("nominating after the draft is done is refused",
            lambda: draft.nominate(SEASON, draft.state(SEASON), actor=101, is_admin=False,
                                   player=PLAYERS[-1], amount=1))

    # Budgets are set per captain, by hand, and have to survive the round
    # trip through JSON — which has no integer keys.
    other = {"id": 99002, "division_id": 9}
    with store._conn() as conn:
        for aid, level in [(c, "captain") for c in CAPS] + [(p, "player") for p in PLAYERS]:
            conn.execute(
                "INSERT INTO site_signups (season_id, division_id, account_id, positions, "
                "captain, level, created_at, updated_at) VALUES (?, ?, ?, '1', 'no', ?, 'x', 'x')",
                (other["id"], other["division_id"], aid, level))
    db.append_draft_event(other["id"], other["division_id"], "rules", 1,
                          {"rules": {"budget": 100, "slots": 2, "min_bid": 1, "bid_seconds": 0}})
    db.append_draft_event(other["id"], other["division_id"], "budgets", 1,
                          {"budgets": {101: 500, 102: 50}})
    db.append_draft_event(other["id"], other["division_id"], "start", 1, {"order": CAPS})
    o = draft.state(other)
    check("a captain's own budget is read back as a number, not a string",
          o.budget(101) == 500 and o.budget(102) == 50, str(o.budgets))
    check("a captain with no budget of their own gets the default", o.budget(103) == 100)
    check("the rich captain can bid more", o.max_bid(101) == 499)
    check("the poor captain can't", o.max_bid(102) == 49)
    draft.nominate(other, o, actor=101, is_admin=False, player=201, amount=60)
    o = draft.state(other)
    refuses("a captain can't bid past their own purse",
            lambda: draft.bid(other, o, actor=102, is_admin=False, captain=102, amount=61))
    draft.bid(other, o, actor=103, is_admin=False, captain=103, amount=61)
    o = draft.state(other)
    check("a captain on the default budget can", o.lot.bid == 61 and o.lot.bidder == 103)
    draft.close_lot(other, draft.state(other))
    o = draft.state(other)
    check("spending comes off their own budget, not the house one",
          o.budget(103) - o.spent(103) == 39, f"{o.budget(103)} - {o.spent(103)}")

    # The log is the draft: reading it twice gives the same answer.
    again = draft.replay(db.get_draft_events(SEASON["id"], SEASON["division_id"]))
    check("replaying the log is deterministic",
          again.rosters == st.rosters and again.turn == st.turn and again.phase == st.phase)

    # A lot left open by a server that went away is sold on the next look,
    # not lost and not sold twice.
    before = len(db.get_draft_events(SEASON["id"], SEASON["division_id"]))
    st = draft.state(SEASON)
    check("a finished draft has no lot to resolve", not draft.close_lot(SEASON, st))
    check("and resolving it added nothing",
          len(db.get_draft_events(SEASON["id"], SEASON["division_id"])) == before)

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
