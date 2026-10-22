"""Season 38's awards, as Mr. Loves Muffins announced them in Discord.

Winners are matched to accounts by name where the name is one the site knows.
Several were announced by Discord handle, which isn't a name anyone plays
under — those are stored as text and show as plain names until an admin fixes
them from the season page. Run once after deploying; re-running replaces.

    DB_PATH=... python3 scripts/seed_season38_awards.py --apply
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
from web import store  # noqa: E402

SEASON, DIVISION = 19546, 2

AWARDS = [
    ("breakout", "@Effjai",
     "One of the biggest steals of the auction draft, FJ went for just 50 gold before "
     "proceeding to completely outperform that price tag. Taking over the Position 1 role, "
     "he became a major driving force behind his team's run to the #1 seed, consistently "
     "delivering when they needed him most.\n\n"
     "From bargain-bin draft pick to one of the most impactful carries in the division, "
     "FJ's breakout season was impossible to ignore."),
    ("support", "@grey poupon",
     "Grey Poupon was a crucial piece of his team's championship run from start to finish. "
     "Consistently providing value in every game, he established himself as arguably the most "
     "important player on the roster and when the championship was on the line, he delivered.\n\n"
     "His clutch plays in the Finals (and the whole season) helped push his team over the "
     "finish line and secure the title."),
    ("core", "@Verve",
     "Verve was the engine behind his team's run to the championship, consistently taking over "
     "games when they needed him most. Time and time again, he turned losing positions into wins "
     "and almost singlehandedly kept his team alive through some of the toughest moments of "
     "their run.\n\n"
     "Although his team ultimately fell just short of winning it all, that does nothing to "
     "diminish the season he had."),
    # Announced as a Discord handle; he captained "Yokozuna Hoshoryu" in this
    # season, which is the 0-8 turnaround the write-up is about, so by id.
    ("leader", "@218077154",
     "Few players embodied leadership this season more than Yokozuna. After an 0-8 start, it "
     "would have been easy for the season to completely unravel. Instead, he kept the team "
     "together, rallied the boys, and helped spark one of the biggest turnarounds of the season.\n\n"
     "From there, they won every game they needed to stay alive, battled their way through a "
     "dramatic tiebreaker to secure a playoff spot, and followed it up with a strong postseason "
     "showing."),
    ("mvp", "@Not Exo",
     "Few players were more important to their team's success this season than Jynx. In many "
     "ways, his team lived and died with his builds and his ability to take over games when they "
     "needed him most. When Jynx was on, his team always had a chance to win.\n\n"
     "Throughout the season, he carried his team to victories time and time again, often being "
     "the deciding factor in games that could have easily gone the other way."),
    # Not voted on: the top average fantasy points at each position.
    ("all_ad_team", "@redground @notoxicore @LovesMuffins @ruN @Ratman", None),
]


def main() -> None:
    season = next((s for s in store.all_seasons()
                   if s["id"] == SEASON and s["division_id"] == DIVISION), None)
    if not season:
        raise SystemExit(f"no season {SEASON} in division {DIVISION}")
    unresolved = []
    for award, winners, citation in AWARDS:
        n = store.set_season_award(season, award, winners, citation or "")
        got = [w for a in store.season_awards(season) if a["key"] == award for w in a["winners"]]
        unresolved += [(award, w["name"]) for w in got if not w["account_id"]]
        print(f"  {store.AWARD_LABELS[award]:<32} {n} winner(s): "
              + ", ".join(f"{w['name']}{'' if w['account_id'] else ' (text only)'}" for w in got))
    if unresolved:
        print("\nAnnounced by Discord handle, so stored as text — fix from the season page:")
        for award, name in unresolved:
            print(f"   {store.AWARD_LABELS[award]}: {name}")


if __name__ == "__main__":
    if "--apply" not in sys.argv:
        raise SystemExit("pass --apply to write")
    main()
