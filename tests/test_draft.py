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
# The auction is fixed at four picks a captain, so a draft that plays itself
# out needs that many players per captain and no fewer.
SLOTS = draft.DEFAULT_RULES["slots"]
MIN_BID = draft.DEFAULT_RULES["min_bid"]
PLAYERS = [200 + i for i in range(1, len(CAPS) * SLOTS + 1)]
STANDIN = 301
BUDGET = 100

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(label)


def refuses(label: str, fn) -> None:
    """An action the rules should turn away, with a reason."""
    try:
        fn()
    except draft.Refused:
        check(label, True)
        return
    check(label, False, "it was allowed")


def signups(season: dict, standin: bool = False) -> None:
    """Who turned up for this season: captains, players, maybe a stand-in."""
    now = "2026-01-01T00:00:00+00:00"
    levels = ([(c, "captain") for c in CAPS] + [(p, "player") for p in PLAYERS]
              + ([(STANDIN, "standin")] if standin else []))
    with store._conn() as conn:
        for aid, level in levels:
            conn.execute(
                "INSERT INTO site_signups (season_id, division_id, account_id, positions, "
                "captain, level, created_at, updated_at) VALUES (?, ?, ?, '1', 'no', ?, ?, ?)",
                (season["id"], season["division_id"], aid, level, now, now))


def setup() -> None:
    db.init_db()
    store.init_site_tables()
    signups(SEASON, standin=True)


def start(season: dict = SEASON, budgets: dict | None = None, **rules) -> draft.State:
    """Open a draft. Budgets are an admin's to set captain by captain; the
    clock is the only rule they can touch, and here it's off so lots close
    the moment they're asked to."""
    db.append_draft_event(season["id"], season["division_id"], "rules", 1,
                          {"rules": {"bid_seconds": 0, **rules}})
    db.append_draft_event(season["id"], season["division_id"], "budgets", 1,
                          {"budgets": budgets or {c: BUDGET for c in CAPS}})
    db.append_draft_event(season["id"], season["division_id"], "start", 1, {"order": CAPS})
    return draft.state(season)


def sell(season: dict, player: int, winner: int, price: int) -> None:
    """Nominate, let the winner take it, and close the lot."""
    st = draft.state(season)
    turn = draft.whose_turn(st)
    draft.nominate(season, st, actor=turn, is_admin=False, player=player, amount=MIN_BID)
    st = draft.state(season)
    if winner != st.lot.bidder:
        draft.bid(season, st, actor=winner, is_admin=False, captain=winner, amount=price)
        st = draft.state(season)
    draft.close_lot(season, st)


def play_out(season: dict) -> None:
    """Fill every roster at the minimum, whoever's turn it happens to be."""
    guard = 0
    while not draft.state(season).done() and guard < len(CAPS) * SLOTS + 5:
        guard += 1
        st = draft.state(season)
        pool = draft.draftees(season, st)
        if not pool:
            return
        sell(season, pool[0]["account_id"], draft.whose_turn(st), MIN_BID)


def main() -> int:
    setup()
    st = draft.state(SEASON)
    check("a draft with no events is a lobby", st.phase == draft.LOBBY)
    check("captains come from the signups", [c["account_id"] for c in draft.captains(SEASON)] == CAPS)

    st = start()
    check("starting fixes the nomination order", st.order == CAPS and st.phase == draft.RUNNING)
    check("with equal purses the first turn goes to the first captain",
          draft.whose_turn(st) == 101)
    check("budgets set captain by captain are what they get to spend",
          all(st.budget(c) == BUDGET for c in CAPS), str(st.budgets))
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
            lambda: draft.nominate(SEASON, st, actor=101, is_admin=False, player=201,
                                   amount=st.max_bid(101) + 1))

    # The rest of the roster still has to be fillable at the minimum price.
    check("the reserve keeps the other slots' worth back",
          st.max_bid(101) == BUDGET - MIN_BID * (SLOTS - 1), str(st.max_bid(101)))

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
            lambda: draft.bid(SEASON, st, actor=102, is_admin=False, captain=102,
                              amount=st.max_bid(102) + 1))

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
    check("the buyer drops behind the captains who still have their money",
          draft.whose_turn(st) == 101 and draft.nomination_order(st) == [101, 103, 102],
          str(draft.nomination_order(st)))
    check("a bought player is out of the pool",
          201 not in {p["account_id"] for p in draft.draftees(SEASON, st)})

    # Play it out and see that nothing the rules forbid has happened.
    play_out(SEASON)
    st = draft.state(SEASON)
    check("every roster filled", all(st.slots_left(c) == 0 for c in CAPS), str(st.rosters))
    check("nobody was sold twice",
          len(st.taken()) == sum(len(r) for r in st.rosters.values()))
    check("nobody overspent", all(st.spent(c) <= st.budget(c) for c in CAPS))
    check("the draft knows it's finished", st.done())
    refuses("nominating after the draft is done is refused",
            lambda: draft.nominate(SEASON, draft.state(SEASON), actor=101, is_admin=False,
                                   player=PLAYERS[-1], amount=1))

    # The order is the money: richest first, every round.
    rich = {"id": 99003, "division_id": 9}
    signups(rich)
    start(rich, budgets={101: 10, 102: 90, 103: 50})
    r = draft.state(rich)
    check("the richest captain nominates first, whatever order they were entered in",
          draft.nomination_order(r) == [102, 103, 101], str(draft.nomination_order(r)))
    db.append_draft_event(rich["id"], rich["division_id"], "sold", None,
                          {"player": 201, "captain": 102, "cost": 60})
    r = draft.state(rich)
    check("spending moves you down the order",
          draft.nomination_order(r) == [103, 102, 101], str(draft.nomination_order(r)))
    db.append_draft_event(rich["id"], rich["division_id"], "sold", None,
                          {"player": 202, "captain": 103, "cost": 48})
    r = draft.state(rich)
    check("and the order keeps following the money",
          draft.nomination_order(r) == [102, 101, 103], str(draft.nomination_order(r)))
    for player in PLAYERS[2:2 + SLOTS - 1]:
        db.append_draft_event(rich["id"], rich["division_id"], "sold", None,
                              {"player": player, "captain": 102, "cost": 1})
    r = draft.state(rich)
    check("a captain with every slot filled drops out of the order",
          r.slots_left(102) == 0 and 102 not in draft.nomination_order(r),
          str(draft.nomination_order(r)))
    check("and can't bid for a roster that's full", r.max_bid(102) == 0)

    # Budgets differ, and have to survive the round trip through JSON —
    # which has no integer keys.
    other = {"id": 99002, "division_id": 9}
    signups(other)
    start(other, budgets={101: 500, 102: 50, 103: 200})
    o = draft.state(other)
    check("a captain's own budget is read back as a number, not a string",
          o.budget(101) == 500 and o.budget(102) == 50, str(o.budgets))
    check("the rich captain can bid more", o.max_bid(101) == 500 - MIN_BID * (SLOTS - 1))
    check("the poor captain can't", o.max_bid(102) == 50 - MIN_BID * (SLOTS - 1))
    check("a log from before budgets were set falls back to the house default",
          draft.replay([{"kind": "start", "at": "", "payload": {"order": CAPS}}]).budget(101)
          == draft.DEFAULT_RULES["budget"])
    draft.nominate(other, o, actor=101, is_admin=False, player=201, amount=60)
    o = draft.state(other)
    refuses("a captain can't bid past their own purse",
            lambda: draft.bid(other, o, actor=102, is_admin=False, captain=102, amount=61))
    draft.bid(other, o, actor=103, is_admin=False, captain=103, amount=61)
    o = draft.state(other)
    check("a captain who can afford it takes the lot", o.lot.bid == 61 and o.lot.bidder == 103)
    draft.close_lot(other, draft.state(other))
    o = draft.state(other)
    check("spending comes off their own budget, not another captain's",
          o.budget(103) - o.spent(103) == 200 - 61, f"{o.budget(103)} - {o.spent(103)}")

    # The clock: a captain who sits on their turn doesn't hold the room up,
    # and a paused draft does nothing at all until it's resumed.
    clock = {"id": 99005, "division_id": 9}
    signups(clock)
    db.append_draft_event(clock["id"], clock["division_id"], "rules", 1,
                          {"rules": {"bid_seconds": 0, "nominate_seconds": 30}})
    db.append_draft_event(clock["id"], clock["division_id"], "budgets", 1,
                          {"budgets": {c: BUDGET for c in CAPS}})
    db.append_draft_event(clock["id"], clock["division_id"], "start", 1,
                          {"order": CAPS, "nominate_by": time.time() - 1})
    c = draft.state(clock)
    check("a turn whose time is up nominates for itself", draft.auto_nominate(clock, c))
    c = draft.state(clock)
    check("the auto-nomination belongs to the captain whose turn it was, at the minimum",
          c.lot and c.lot.nominated_by == 101 and c.lot.bid == MIN_BID, str(c.lot))
    check("and it isn't nominated twice over", not draft.auto_nominate(clock, c))

    draft.pause(clock, c, actor=None)
    c = draft.state(clock)
    check("a paused draft is paused", c.paused)
    refuses("bidding while paused is refused",
            lambda: draft.bid(clock, c, actor=102, is_admin=False, captain=102, amount=50))
    refuses("pausing twice is refused", lambda: draft.pause(clock, c, actor=None))
    check("and a paused lot doesn't sell", not draft.close_lot(clock, c))
    draft.resume(clock, c, actor=None)
    c = draft.state(clock)
    check("resuming unpauses", not c.paused)
    check("and nobody is sold the instant the room comes back",
          not draft.close_lot(clock, c) and c.lot.closes_at > time.time())
    refuses("resuming a running draft is refused", lambda: draft.resume(clock, c, actor=None))
    draft.close_lot(clock, draft.replay(
        db.get_draft_events(clock["id"], clock["division_id"])
        + [{"kind": "resume", "id": 0, "at": "", "payload": {"closes_at": time.time() - 1}}]))
    c = draft.state(clock)
    check("once its time really is up it sells", c.lot is None and c.rosters[101], str(c.rosters))
    check("and the next turn gets a clock of its own",
          c.nominate_by and c.nominate_by > time.time(), str(c.nominate_by))

    # Taking something back. This is the draft-night button, so it gets
    # checked properly: the player comes back, so does the money, and so
    # does the captain's place in the order.
    undo_s = {"id": 99004, "division_id": 9}
    signups(undo_s)
    u = start(undo_s)
    draft.nominate(undo_s, u, actor=101, is_admin=False, player=201, amount=30)
    u = draft.state(undo_s)
    check("taking back a nomination closes the lot",
          draft.undo(undo_s, u) == "the nomination" and draft.state(undo_s).lot is None)
    u = draft.state(undo_s)
    check("and the player goes back in the pool",
          201 in {p["account_id"] for p in draft.draftees(undo_s, u)})

    draft.nominate(undo_s, u, actor=101, is_admin=False, player=201, amount=30)
    u = draft.state(undo_s)
    draft.bid(undo_s, u, actor=102, is_admin=False, captain=102, amount=40)
    u = draft.state(undo_s)
    draft.close_lot(undo_s, u)
    u = draft.state(undo_s)
    check("a sale puts the player on the team", u.rosters[102][0]["account_id"] == 201)
    check("and takes the money", u.budget(102) - u.spent(102) == BUDGET - 40)
    draft.undo(undo_s, draft.state(undo_s))
    u = draft.state(undo_s)
    check("taking back a sale empties the roster slot", u.rosters[102] == [], str(u.rosters))
    check("gives the money back", u.budget(102) - u.spent(102) == BUDGET)
    check("puts the player back in the pool",
          201 in {p["account_id"] for p in draft.draftees(undo_s, u)})
    check("and puts the captain back up the order",
          draft.nomination_order(u) == [101, 102, 103], str(draft.nomination_order(u)))
    refuses("undoing twice over doesn't reach further back",
            lambda: draft.undo(undo_s, draft.state(undo_s))
                    if draft.undoable(undo_s) is None else (_ for _ in ()).throw(
                        draft.Refused("there was still something to undo")))

    # Committing writes the shape the rest of the league already reads.
    refuses("an unfinished draft can't be committed",
            lambda: draft.commit(undo_s, draft.state(undo_s)))
    play_out(undo_s)
    wrote = draft.commit(undo_s, draft.state(undo_s))
    check("committing writes every pick", wrote["players"] == len(CAPS) * SLOTS, str(wrote))
    costs = db.get_player_costs(undo_s["id"], undo_s["division_id"])
    check("player_costs has the account, the price and the captain",
          len(costs) == len(CAPS) * SLOTS
          and all(c.get("captain") for c in costs.values()), str(costs)[:120])
    check("a team exists per captain",
          len(db.get_season_teams(undo_s["id"], undo_s["division_id"])) == len(CAPS))
    check("and the draft is done", draft.state(undo_s).phase == draft.DONE)

    # The log is the draft: reading it twice gives the same answer.
    st = draft.state(SEASON)
    again = draft.replay(db.get_draft_events(SEASON["id"], SEASON["division_id"]))
    check("replaying the log is deterministic",
          again.rosters == st.rosters and again.phase == st.phase
          and draft.nomination_order(again) == draft.nomination_order(st))

    # A lot left open by a server that went away is sold on the next look,
    # not lost and not sold twice.
    before = len(db.get_draft_events(SEASON["id"], SEASON["division_id"]))
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
