"""Shared vocabulary for the data a rating is built from.

A rating is a judgement about a player, and it is only as good as the data
behind it. Two things look alike at a fetch site and mean opposite things:

  windrun has no record of this account   — a fact, and a common one; the
                                            rating is computed without it
  windrun didn't answer                   — nothing is known, and a rating
                                            computed anyway is a guess

Both used to arrive as None, so the second was silently treated as the first:
a player whose game counts failed to load was rated as though their activity
were unknown, which for a pure Ability Draft player moved the weighting off
windrun and onto their badge — 135 points, from one timeout.

So a source that couldn't be reached raises Unavailable, and a source that
answered with nothing returns None. Nothing is rated from an incomplete
picture; the caller says so and tries again later.
"""

from __future__ import annotations


class Unavailable(Exception):
    """A data source couldn't be reached, so what it would have said is
    unknown. The message names the source, for showing to whoever asked."""
