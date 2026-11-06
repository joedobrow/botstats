# Working on dota-bot

The Discord bot (`bot.py`) and the league site (`web/`, served at adcl.gg) are one
app on one SQLite database. The rule above all others: **the bot and the site never
compute the same thing two ways.** When a page and a command show the same number,
they call the same function. If you find yourself writing a second version, move the
first one somewhere both can reach (usually `db.py`) and call it from both places.

## The single sources

| Question | Ask | Not |
|---|---|---|
| Which season is a division playing? | `db.playing_season_id`, i.e. `division["current_season_id"]` from any `db` division getter; `store.current_seasons()` on the site | the raw `divisions.current_season_id` column (that's `division["season_pointer"]`, which only the weekly fetch reads) |
| What rating do we show? | `db.displayed_ratings(division)`, `db.displayed_rating(aid, division)`, `store.player_ratings()` | `player_ratings_cache.internal_rating` plus your own fantasy adjustment |
| Who has enough games to rank? | `db.qualified` / `db.games_needed` | a fixed number in a template or route |
| Standings, tiebreaks | `store.season_teams` (the bot's `/standings` calls it) | re-sorting teams elsewhere |
| A player's season stats | `db.get_all_time_stats(division, season)` | `store.aggregate` (only for totals the bot has no view of) |
| Fantasy points | `fantasy.py` (`calculate_fantasy_points`, `breakdown`, `describe`) | arithmetic in templates |
| Who is a person? | `db.person_of`, `db.accounts_of_people`, `store.alt_links()`: one row per person, filed under their main account. Anything matching a person to a season (drafts, captains, teams, titles) checks all of their accounts | comparing raw account ids |
| How do I reach them on Discord? | `store.discord_handles()` / `store.player_discord()` | reading `site_users.discord` or signups directly |
| The site doing something in Discord | `web.notify.client` (the bot) and `notify.owner()`, set by `bot.py` at startup | importing `bot` from the site |
| Display names, avatars, badges | `store.player_identities` (bot's `db.get_display_names` rule) | `players.name` |

## Seasons are (id, division)

A season's id is its Dota league id, and **divisions share ids**: NA and EU both play
ADCL Season 1 (20508). Anything that picks a season takes the division too:
- URLs: `/season/<id>/<division slug>/…`; `?season=<id>&division=<id>` in a query
  (`routes._query_season`, which also takes `?season=<id>/<division id>` from one picker).
- Admin forms: hidden `season_id` + `division_id`, read with `routes._form_season`.
- Never `next(s for s in seasons if s["id"] == …)` without the division.

## Site handlers (web/routes.py)

- **After a form, redirect with `_go(url, notice)`.** `base.html` shows `?notice=` in
  one banner for every page, so templates never render notices themselves. Pass
  extra query with keywords: `_go(season_url(s), "Saved.", tab="champions")`.
- **Bad input is a 400, not a crash.** `raise web.HTTPBadRequest(text="Human sentence.")`;
  `error_middleware` turns 400/403 into a site page for a browser and keeps the bare
  status for scripts (the draft room's fetches). Unexpected exceptions are logged and
  shown as a "Something broke" page.
- Read form ids with `_form_season`, `_form_account`, or `re.sub(r"\D", "", …)`, never
  a bare `int(data[...])`. Numeric URL parts get a pattern: `{match_id:-?\d+}`
  (in an `r"…"` string).
- Permission checks: `_require_user`, `_require_admin`, `_require_uberadmin`; team
  edits use `_can_edit_team` (admins, or the captain on any of their accounts).
- `render()` supplies `user`, `is_admin`, `csrf`, `path`, `login_url` (comes back to
  this page with its query), `notice`, `theme`. Every POST form carries
  `<input type="hidden" name="csrf" value="{{ csrf }}">`.

## Templates (web/templates)

- Use the macros in `macros.html` rather than hand-rolling markup:
  `player_link` (every name that stands for a player; handles private account 0
  and adds title rings), `result_word` (Win/Loss linked to the match),
  `hero_icon(h, alone=True)` (icon only, name in alt/title), `pos`/`positions`
  (numbered position circles), `team_cell`, `signup_button`.
- Lists people compare are `<table class="sortable">` with `data-sort-type`,
  `data-sort-first="desc"` on numbers and `data-sort` on cells whose text isn't the
  sort key. Numbers are `class="num"`. Empty tables get a muted
  "Nobody/No … yet" row spanning every column.
- Never put a template value inside an `on…="…"` handler: the browser
  HTML-unescapes the attribute before running it, so a player's name becomes
  code. Ask "are you sure?" with `data-confirm="…"` on the form or button
  (handled in `base.html`); pass data to scripts as `|tojson` in a
  `<script type="application/json">` block.
- No explainer text. Say it in a label, a column header's `title`, or not at all.
- American spelling in anything a user reads (favorite, color).

## Look (web/static/style.css)

- The paper look: every color is a token in `:root`. The night palette is written
  twice: `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) }`
  and `:root[data-theme="dark"]`. A new token goes in all three blocks.
- The theme lives only in the `theme` cookie; the server renders `data-theme`.
  Themed images are `<thing>-light.png` / `<thing>-dark.png` in a
  `<picture class="themed">`; the script in `base.html` swaps them.
- `[hidden]` is `display: none !important`, so `hidden` always wins over a class
  that sets `display`.
- Discord embeds and cards use `formatters.EMBED_COLOUR`, the day `--accent` brick.
  No other embed colors.

## Database

- Schema changes are numbered migrations in `db._MIGRATIONS`. Append; never edit one.
- `db._derived(key, build)` caches derived data against the database's version. Use
  it for anything a page asks for repeatedly.
- Hand-entered games have negative match ids, no player rows, and count only for
  teams, never for a player's record.

## Checking work

- Tests are scripts: `python3 tests/test_draft.py`, `tests/test_people.py`,
  `tests/test_seasons.py`. Each prints `all checks passed`.
- Preview the site with the `site` entry in `.claude/launch.json` (a copy of the
  prod db). Copy prod with sqlite's backup on the machine, then sftp the snapshot:
  a plain copy misses the WAL.
- Deploy: `fly deploy -a dota-bot`, then check `fly logs -a dota-bot --no-tail`
  for `Logged in as`.
