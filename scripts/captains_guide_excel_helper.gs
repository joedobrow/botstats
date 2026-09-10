// Google Apps Script — Captain's Guide. Pulls the RD2L players + captains
// CSVs into raw dump tabs, then builds analytics sheets for captains
// preparing for the player draft. First analysis: role demand.
//
// SETUP (one time):
// 1. Create a new Google Sheet for this project.
// 2. Extensions > Apps Script, delete the boilerplate, paste this whole file.
// 3. Project Settings (gear icon) > Script Properties, add:
//      PLAYERS_CSV_URL   = https://rd2l.gg/seasons/<season-id>/divisions/<division-id>/draftsheet
//      CAPTAINS_CSV_URL  = https://rd2l.gg/seasons/<season-id>/divisions/<division-id>/draftsheet?captains=1
//      CAPTAINS_PAGE_URL = https://rd2l.gg/seasons/<season-id>/divisions/<division-id>/captains
//      RATINGS_API_URL   = https://dota-bot.fly.dev/api/ratings
//      RATINGS_API_KEY   = <the RATINGS_API_KEY value from the bot's .env / fly secrets>
//      LAST_SEASON_COSTS_URL = https://dota-bot.fly.dev/api/costs?guild_id=<guild-id>&season_start=<last-season-start>
//      LAST_SEASON_STATS_URL = https://dota-bot.fly.dev/api/season_stats?guild_id=<guild-id>&season_start=<last-season-start>
// 4. In the Apps Script editor, select "installHourlyTrigger" from the
//    function dropdown and click Run once (authorize when prompted).
// 5. That's it — syncAll() runs hourly. Run it manually any time too.
//
// Sheets this creates:
//   Raw Player Data / Raw Captain Data — full CSV mirror, cleared + rewritten
//     every sync. Same captains-CSV-is-unreliable filtering as the other
//     project: Raw Captain Data is filtered down to only the account ids
//     the real captains PAGE confirms, since rd2l.gg's captains CSV export
//     has been observed returning far more rows than the real captain list.
//   Role Overrides (manual, visible) — Account ID / Name / Pos 1-5. Seeded
//     once with known corrections, then 100% yours to maintain — the script
//     never rewrites it after creation. Keyed by account id, not name: two
//     different captains are both named "Cam" this season, so name alone
//     isn't a safe identity key here.
//   Role Data (hidden helper) — one row per person, players + captains
//     unioned (deduped by account id; anyone appearing on either list as a
//     captain is flagged IsCaptain=true), with Role Overrides applied on
//     top of the raw self-reported 1-5 scores where an override exists,
//     plus Internal Rating and Last Season Cost (both fetched from the
//     bot's API — ratings use the same backfill-on-new-signup mechanism as
//     the other project; cost is blank for anyone not in last season's
//     draft). Fully rebuilt every sync — pure literal values, no formulas,
//     so there's no array-formula-spill staleness risk here.
//   Role Analysis — two side-by-side tables per role (Position 1 / 2 / 3 /
//     Support, where Support = Pos 4 and Pos 5 together), explained by the
//     text in row 1: "Plays this role" (self-report/override >= 3; Support
//     = Pos 4 OR Pos 5) on the left, "Maxed this role" (= 5; Support = Pos
//     4 AND Pos 5 both = 5, the literal "5/5") on the right. Both tables
//     count captains toward the % (they're still demand — they won't draft
//     their own role) but exclude them from the name lists, which are
//     sorted by Internal Rating, high to low.
//   Rank Analysis — non-captain player list (Name, Windrun, Internal
//     Rating, Last Season Bid) next to a Captain Name/Rating table, both
//     sorted by rating high to low, plus histograms of each group's rating
//     distribution. Chart data lives on the hidden "Rank Chart Data" sheet
//     so it never crowds or gets overlapped by the visible tables/charts.
//   Fantasy Data (hidden helper) / Fantasy Analysis — draft-prep valuation.
//     Fantasy Data merges Role Data with last season's per-player stats
//     (winrate, attendance, fantasy points, and the exact "deserved cost"
//     /leaderboard's Value ($ deserved − $ paid) is built from) fetched from
//     the bot's /api/season_stats, then computes a second suggested bid:
//     placed 0-300 by min-max scaling this season's Internal Rating across
//     the non-captain pool, then nudged +/-40 (capped, scaled by how big
//     last season's Value was relative to the pool's largest |Value|),
//     floored at 0. Both sheets are fully script-written (not formulas) —
//     this is a pure computed report with no manual-entry cells, so there's
//     nothing for a live formula to protect, and it sidesteps the
//     FILTER/SORT blank-cell quirks this file has hit elsewhere. Fantasy
//     Analysis shows just the non-captain pool, sorted by Internal Rating.

const PLAYERS_SHEET_NAME = 'Raw Player Data';
const CAPTAINS_SHEET_NAME = 'Raw Captain Data';
const ROLE_OVERRIDES_SHEET_NAME = 'Role Overrides';
const ROLE_DATA_SHEET_NAME = 'Role Data';
const ROLE_ANALYSIS_SHEET_NAME = 'Role Analysis';
const RANK_ANALYSIS_SHEET_NAME = 'Rank Analysis';
const RANK_CHART_DATA_SHEET_NAME = 'Rank Chart Data';
const FANTASY_DATA_SHEET_NAME = 'Fantasy Data';
const FANTASY_ANALYSIS_SHEET_NAME = 'Fantasy Analysis';
const DOTABUFF_COL_IN_RAW = 8; // 1-indexed column H in the raw CSV dumps
// 1-indexed columns K-O in the raw CSV dumps: Role: Position 1-5.
const ROLE_COLS_IN_RAW = [11, 12, 13, 14, 15];

// Known role-report corrections, seeded once when Role Overrides is first
// created. [account_id, display name (reference only), pos1, pos2, pos3, pos4, pos5]
const SEED_ROLE_OVERRIDES = [
  [45626568, 'ExO', 1, 1, 1, 5, 5],
  [178434754, 'Cam', 3, 5, 3, 1, 1],
  [372541122, 'Jynx', 5, 3, 1, 1, 1],
];

function syncAll() {
  syncRawData();
  SpreadsheetApp.flush();

  ensureRoleOverridesSheet_();
  syncRoleData_();
  ensureRoleAnalysisSheet_();
  ensureRankAnalysisSheet_();
  syncFantasyData_();
  Logger.log('Full sync complete.');
}

// --- Raw CSV dumps ---------------------------------------------------------

function syncRawData() {
  const props = PropertiesService.getScriptProperties();
  const playersUrl = props.getProperty('PLAYERS_CSV_URL');
  const captainsUrl = props.getProperty('CAPTAINS_CSV_URL');
  const captainsPageUrl = props.getProperty('CAPTAINS_PAGE_URL');
  if (!playersUrl || !captainsUrl || !captainsPageUrl) {
    throw new Error('Missing Script Property: PLAYERS_CSV_URL, CAPTAINS_CSV_URL, or CAPTAINS_PAGE_URL. See setup notes at the top of this file.');
  }

  writeRawSheet_(PLAYERS_SHEET_NAME, fetchCsv_(playersUrl));
  writeRawSheet_(CAPTAINS_SHEET_NAME, fetchRealCaptainsCsv_(captainsUrl, captainsPageUrl));
}

// Filters the (currently unreliable) captains CSV down to only the account
// ids the real captains page confirms.
function fetchRealCaptainsCsv_(captainsCsvUrl, captainsPageUrl) {
  const allRows = fetchCsv_(captainsCsvUrl);
  const header = allRows[0];
  const realIds = fetchCaptainAccountIdsFromPage_(captainsPageUrl);

  const filtered = allRows.slice(1).filter(function (row) {
    const id = extractAccountId_(row[DOTABUFF_COL_IN_RAW - 1]);
    return id && realIds[id];
  });
  return [header].concat(filtered);
}

function fetchCaptainAccountIdsFromPage_(url) {
  const resp = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) {
    throw new Error('Failed to fetch captains page (' + url + '): HTTP ' + resp.getResponseCode());
  }
  const html = resp.getContentText();
  const re = /<a href="\/profile\/(\d+)">/g;
  const ids = {};
  let m;
  while ((m = re.exec(html)) !== null) {
    ids[m[1]] = true;
  }
  return ids;
}

function fetchCsv_(url) {
  const resp = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) {
    throw new Error('Failed to fetch CSV (' + url + '): HTTP ' + resp.getResponseCode());
  }
  return Utilities.parseCsv(resp.getContentText());
}

function writeRawSheet_(sheetName, rows) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(sheetName);
  if (!sheet) sheet = ss.insertSheet(sheetName);

  sheet.clearContents();
  if (rows.length === 0) return;

  sheet.getRange(1, 1, rows.length, rows[0].length).setValues(rows);
  sheet.setFrozenRows(1);
}

// --- "Role Overrides" sheet (manual) ---------------------------------------

function ensureRoleOverridesSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(ROLE_OVERRIDES_SHEET_NAME);
  if (sheet) return; // already exists — never touched again after creation

  sheet = ss.insertSheet(ROLE_OVERRIDES_SHEET_NAME);
  const headers = ['Account ID', 'Name', 'Pos 1', 'Pos 2', 'Pos 3', 'Pos 4', 'Pos 5'];
  sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  sheet.setFrozenRows(1);
  sheet.getRange(2, 1, SEED_ROLE_OVERRIDES.length, headers.length).setValues(SEED_ROLE_OVERRIDES);
}

// --- "Role Data" hidden helper sheet ---------------------------------------

function syncRoleData_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  // Overrides, keyed by account id (a Number — see the note on Ratings
  // Lookup's Account ID column in the other project's script for why this
  // matters: a text/number mismatch silently breaks VLOOKUP-style lookups).
  const overridesSheet = ss.getSheetByName(ROLE_OVERRIDES_SHEET_NAME);
  const overrideRows = overridesSheet ? overridesSheet.getDataRange().getValues() : [];
  const overrides = {};
  for (let r = 1; r < overrideRows.length; r++) {
    const aid = overrideRows[r][0];
    if (aid === '' || aid == null) continue;
    overrides[Number(aid)] = [
      overrideRows[r][2], overrideRows[r][3], overrideRows[r][4],
      overrideRows[r][5], overrideRows[r][6],
    ];
  }

  // Union players + captains, deduped by account id. Captains are flagged
  // even if they only appear on the captains list.
  const people = {}; // account_id -> {name, isCaptain, roles: [p1..p5]}
  function ingest(sheetName, isCaptainSource) {
    const sheet = ss.getSheetByName(sheetName);
    if (!sheet) return;
    const values = sheet.getDataRange().getValues();
    for (let r = 1; r < values.length; r++) {
      const name = values[r][0];
      const accountId = extractAccountId_(values[r][DOTABUFF_COL_IN_RAW - 1]);
      if (!name || !accountId) continue;
      const aid = Number(accountId);
      const roles = ROLE_COLS_IN_RAW.map(function (col) { return values[r][col - 1]; });
      if (people[aid]) {
        if (isCaptainSource) people[aid].isCaptain = true;
      } else {
        people[aid] = { name: name, isCaptain: isCaptainSource, roles: roles };
      }
    }
  }
  ingest(PLAYERS_SHEET_NAME, false);
  ingest(CAPTAINS_SHEET_NAME, true);

  const ratings = fetchRatings_();
  const lastSeasonCosts = fetchLastSeasonCosts_();

  const rows = [[
    'Account ID', 'Name', 'Is Captain', 'Pos 1', 'Pos 2', 'Pos 3', 'Pos 4', 'Pos 5',
    'Internal Rating', 'Last Season Cost',
  ]];
  Object.keys(people).forEach(function (aidStr) {
    const aid = Number(aidStr);
    const p = people[aid];
    const roles = overrides[aid] || p.roles;
    const rating = ratings[aid] != null ? ratings[aid] : '';
    const cost = lastSeasonCosts[aid] != null ? lastSeasonCosts[aid] : '';
    rows.push([aid, p.name, p.isCaptain, roles[0], roles[1], roles[2], roles[3], roles[4], rating, cost]);
  });

  const sheet = getOrCreateHiddenSheet_(ROLE_DATA_SHEET_NAME);
  sheet.clearContents();
  sheet.getRange(1, 1, rows.length, 10).setValues(rows);
  sheet.setFrozenRows(1);
}

// Fetches {account_id: cost} for last season's draft. Blank/absent for
// anyone who didn't play last season — that's the intended behavior, not
// an error, so this never throws on a missing account, only on a genuine
// config/fetch problem.
function fetchLastSeasonCosts_() {
  const props = PropertiesService.getScriptProperties();
  const costsUrl = props.getProperty('LAST_SEASON_COSTS_URL');
  const apiKey = props.getProperty('RATINGS_API_KEY');
  if (!costsUrl || !apiKey) {
    throw new Error('Missing Script Property: LAST_SEASON_COSTS_URL or RATINGS_API_KEY.');
  }
  const resp = UrlFetchApp.fetch(costsUrl, { headers: { 'X-Api-Key': apiKey }, muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) {
    throw new Error('Failed to fetch last season costs: HTTP ' + resp.getResponseCode() + ' ' + resp.getContentText());
  }
  const raw = JSON.parse(resp.getContentText());
  const out = {};
  Object.keys(raw).forEach(function (aid) {
    out[Number(aid)] = raw[aid].cost;
  });
  return out;
}

// Fetches {account_id: internal_rating}. Also backfills ratings for any
// never-before-seen account in the current draft sheet (a few per call —
// see the bot's RATINGS_BACKFILL_LIMIT), same mechanism as the other
// project's Ratings Lookup sheet.
function fetchRatings_() {
  const props = PropertiesService.getScriptProperties();
  const ratingsUrl = props.getProperty('RATINGS_API_URL');
  const apiKey = props.getProperty('RATINGS_API_KEY');
  const playersCsvUrl = props.getProperty('PLAYERS_CSV_URL');
  if (!ratingsUrl || !apiKey || !playersCsvUrl) {
    throw new Error('Missing Script Property: RATINGS_API_URL, RATINGS_API_KEY, or PLAYERS_CSV_URL.');
  }
  // guild_id + season_start make internal_rating match /player exactly (the
  // fantasy-adjusted rating, not the raw cached one) for anyone with match
  // history that season.
  const ratingsQuery = '?draft_sheet_url=' + encodeURIComponent(playersCsvUrl) +
    '&guild_id=1481800158826991718&season_start=2026-04-28';
  const resp = UrlFetchApp.fetch(
    ratingsUrl + ratingsQuery,
    { headers: { 'X-Api-Key': apiKey }, muteHttpExceptions: true }
  );
  if (resp.getResponseCode() !== 200) {
    throw new Error('Failed to fetch ratings: HTTP ' + resp.getResponseCode() + ' ' + resp.getContentText());
  }
  const raw = JSON.parse(resp.getContentText());
  const out = {};
  Object.keys(raw).forEach(function (aid) {
    out[Number(aid)] = raw[aid].internal_rating;
  });
  return out;
}

function getOrCreateHiddenSheet_(name) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.hideSheet();
  }
  return sheet;
}

// --- "Role Analysis" sheet ---------------------------------------------

function ensureRoleAnalysisSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(ROLE_ANALYSIS_SHEET_NAME);
  if (!sheet) sheet = ss.insertSheet(ROLE_ANALYSIS_SHEET_NAME);

  const rd = "'" + ROLE_DATA_SHEET_NAME + "'";
  const headers = ['Position 1', 'Position 2', 'Position 3', 'Support'];

  // Row 1: explanation for each table. Table 1 = A:D, gap at E, table 2 = F:I.
  // Only written if currently empty, so any manual edits (text, color fills,
  // etc.) are permanent from here on — the script never touches a non-empty
  // cell in this sheet, only fills in what's missing.
  if (sheet.getRange('A1').getValue() === '') {
    sheet.getRange('A1:D1').merge();
    sheet.getRange('A1').setValue('Plays this role');
  }
  if (sheet.getRange('F1').getValue() === '') {
    sheet.getRange('F1:I1').merge();
    sheet.getRange('F1').setValue('Loves this role');
  }
  sheet.getRange('A1:I1')
    .setFontStyle('italic')
    .setFontWeight('bold')
    .setHorizontalAlignment('center')
    .setWrap(true);

  setValueIfEmpty_(sheet, 'A2', headers[0]);
  setValueIfEmpty_(sheet, 'B2', headers[1]);
  setValueIfEmpty_(sheet, 'C2', headers[2]);
  setValueIfEmpty_(sheet, 'D2', headers[3]);
  setValueIfEmpty_(sheet, 'F2', headers[0]);
  setValueIfEmpty_(sheet, 'G2', headers[1]);
  setValueIfEmpty_(sheet, 'H2', headers[2]);
  setValueIfEmpty_(sheet, 'I2', headers[3]);
  sheet.setFrozenRows(2);

  // Row 3: % of the combined player+captain pool meeting each table's bar.
  // Captains count toward this — they're still demand, since they won't
  // draft their own role.
  setFormulaIfEmpty_(sheet, 'A3', '=COUNTIF(' + rd + '!D2:D,">=3")/COUNTA(' + rd + '!A2:A)');
  setFormulaIfEmpty_(sheet, 'B3', '=COUNTIF(' + rd + '!E2:E,">=3")/COUNTA(' + rd + '!A2:A)');
  setFormulaIfEmpty_(sheet, 'C3', '=COUNTIF(' + rd + '!F2:F,">=3")/COUNTA(' + rd + '!A2:A)');
  setFormulaIfEmpty_(sheet, 'D3',
    '=SUMPRODUCT((' + rd + '!G2:G>=3)+(' + rd + '!H2:H>=3)>0)/COUNTA(' + rd + '!A2:A)');
  setFormulaIfEmpty_(sheet, 'F3', '=COUNTIF(' + rd + '!D2:D,5)/COUNTA(' + rd + '!A2:A)');
  setFormulaIfEmpty_(sheet, 'G3', '=COUNTIF(' + rd + '!E2:E,5)/COUNTA(' + rd + '!A2:A)');
  setFormulaIfEmpty_(sheet, 'H3', '=COUNTIF(' + rd + '!F2:F,5)/COUNTA(' + rd + '!A2:A)');
  setFormulaIfEmpty_(sheet, 'I3',
    '=SUMPRODUCT((' + rd + '!G2:G=5)*(' + rd + '!H2:H=5))/COUNTA(' + rd + '!A2:A)');
  sheet.getRange('A3:D3').setNumberFormat('0%');
  sheet.getRange('F3:I3').setNumberFormat('0%');

  // Row 4 intentionally left blank (used to hold a "% of pool" label).
  sheet.getRange('A4:D4').clearContent();
  sheet.getRange('F4:I4').clearContent();

  // Row 5+: live list of non-captain players meeting each bar, sorted by
  // Internal Rating high to low. SORT's sort-key array has to line up
  // row-for-row with the name array, so it's the same FILTER conditions
  // applied a second time to the rating column, rather than a plain range.
  setFormulaIfEmpty_(sheet, 'A5', ratingSortedRoleFilter_(rd, 'D2:D>=3'));
  setFormulaIfEmpty_(sheet, 'B5', ratingSortedRoleFilter_(rd, 'E2:E>=3'));
  setFormulaIfEmpty_(sheet, 'C5', ratingSortedRoleFilter_(rd, 'F2:F>=3'));
  setFormulaIfEmpty_(sheet, 'D5', ratingSortedRoleFilter_(rd, '((' + rd + '!G2:G>=3)+(' + rd + '!H2:H>=3))>0'));
  setFormulaIfEmpty_(sheet, 'F5', ratingSortedRoleFilter_(rd, 'D2:D=5'));
  setFormulaIfEmpty_(sheet, 'G5', ratingSortedRoleFilter_(rd, 'E2:E=5'));
  setFormulaIfEmpty_(sheet, 'H5', ratingSortedRoleFilter_(rd, 'F2:F=5'));
  setFormulaIfEmpty_(sheet, 'I5', ratingSortedRoleFilter_(rd, rd + '!G2:G=5,' + rd + '!H2:H=5'));
}

// Builds a SORT(FILTER(name, <roleCondition>, notCaptain), FILTER(rating,
// <roleCondition>, notCaptain), FALSE) formula. roleCondition is either a
// bare "<col><range><op><value>" fragment (gets the rd!  prefix applied) or,
// for compound conditions, the fully-qualified fragment(s) already including
// the sheet prefix — pass those as-is (comma-separated for multiple FILTER
// conditions, or wrapped in its own parens for a single boolean expression).
function ratingSortedRoleFilter_(rd, roleCondition) {
  const cond = /^[A-Z]/.test(roleCondition) ? rd + '!' + roleCondition : roleCondition;
  // Blank cells coerce to FALSE for equality comparisons in Sheets (but not
  // for >=/<=), so "C2:C=FALSE" alone also matches every blank phantom row
  // down the sheet's open-ended extent — the A2:A<>"" clause excludes them.
  // See the matching note in ensureRankAnalysisSheet_ for the full story.
  const notCaptain = rd + '!C2:C=FALSE,' + rd + '!A2:A<>""';
  return '=SORT(FILTER(' + rd + '!B2:B,' + cond + ',' + notCaptain + '),' +
    'FILTER(' + rd + '!I2:I,' + cond + ',' + notCaptain + '),FALSE)';
}

// --- "Rank Analysis" sheet --------------------------------------------

function ensureRankAnalysisSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(RANK_ANALYSIS_SHEET_NAME);
  const isNew = !sheet;
  if (isNew) sheet = ss.insertSheet(RANK_ANALYSIS_SHEET_NAME);

  const rd = "'" + ROLE_DATA_SHEET_NAME + "'";
  // NOTE on the extra A2:A<>"" clause below: 'Role Data'!C2:C is an
  // open-ended range, and Sheets coerces a genuinely blank cell to FALSE
  // for equality comparisons (but NOT for >=/<= ones) — so "C2:C=FALSE"
  // alone matches every real non-captain row *plus* every blank row all
  // the way down the sheet's extent, which is what was producing the huge
  // tail of dead windrun links below the real players. Requiring the
  // Account ID to be non-blank excludes those phantom rows. This bites
  // every "=FALSE"/"=TRUE" filter condition in this file, not just here —
  // same fix applied in ratingSortedRoleFilter_ for Role Analysis.
  const notCaptain = rd + '!C2:C=FALSE,' + rd + '!A2:A<>""';
  const isCaptain = rd + '!C2:C=TRUE,' + rd + '!A2:A<>""';
  const ratingKey = 'FILTER(' + rd + '!I2:I,' + notCaptain + ')';
  const captainRatingKey = 'FILTER(' + rd + '!I2:I,' + isCaptain + ')';

  setValueIfEmpty_(sheet, 'A1', 'Name');
  setValueIfEmpty_(sheet, 'B1', 'Windrun');
  setValueIfEmpty_(sheet, 'C1', 'Internal Rating');
  setValueIfEmpty_(sheet, 'D1', 'Last Season Bid');
  sheet.getRange('A1:D1').setFontWeight('bold');
  sheet.setFrozenRows(1);

  // Non-captain players, sorted by Internal Rating high to low. Every
  // column uses the identical filter+sort-key as A, so they all stay
  // row-aligned no matter how FILTER internally orders things.
  setFormulaIfEmpty_(sheet, 'A2', '=SORT(FILTER(' + rd + '!B2:B,' + notCaptain + '),' + ratingKey + ',FALSE)');
  setFormulaIfEmpty_(sheet, 'B2',
    '=ARRAYFORMULA("https://windrun.io/players/"&SORT(FILTER(' + rd + '!A2:A,' + notCaptain + '),' + ratingKey + ',FALSE))');
  setFormulaIfEmpty_(sheet, 'C2', '=SORT(FILTER(' + rd + '!I2:I,' + notCaptain + '),' + ratingKey + ',FALSE)');
  setFormulaIfEmpty_(sheet, 'D2', '=SORT(FILTER(' + rd + '!J2:J,' + notCaptain + '),' + ratingKey + ',FALSE)');

  // Captains: name + rating, sorted by rating high to low. Right next to
  // the player table (one gap column) now that chart data lives on its own
  // hidden sheet instead of cluttering columns here.
  setValueIfEmpty_(sheet, 'F1', 'Captain Name');
  setValueIfEmpty_(sheet, 'G1', 'Rating');
  sheet.getRange('F1:G1').setFontWeight('bold');
  setFormulaIfEmpty_(sheet, 'F2', '=SORT(FILTER(' + rd + '!B2:B,' + isCaptain + '),' + captainRatingKey + ',FALSE)');
  setFormulaIfEmpty_(sheet, 'G2', '=SORT(FILTER(' + rd + '!I2:I,' + isCaptain + '),' + captainRatingKey + ',FALSE)');

  // Chart data lives on a hidden sheet, not here — keeps this sheet free of
  // helper columns that could get overlapped by the charts or crowd the
  // visible tables as they grow.
  const chartData = getOrCreateHiddenSheet_(RANK_CHART_DATA_SHEET_NAME);
  chartData.getRange('A1').setValue('Player Ratings');
  chartData.getRange('B1').setValue('Captain Ratings');
  chartData.getRange('A2').setFormula('=FILTER(' + rd + '!I2:I,' + rd + '!C2:C=FALSE,' + rd + '!I2:I<>"")');
  chartData.getRange('B2').setFormula('=FILTER(' + rd + '!I2:I,' + rd + '!C2:C=TRUE,' + rd + '!I2:I<>"")');

  // Charts anchor well to the right of both visible tables so they can
  // never overlap regardless of how long either table grows.
  ensureHistogramChart_(sheet, 'Player Internal Rating Distribution', chartData.getRange('A2:A500'), 1, 10);
  ensureHistogramChart_(sheet, 'Captain Internal Rating Distribution', chartData.getRange('B2:B500'), 21, 10);

  // Mean rating, pinned at a fixed row well below any realistic list length
  // rather than literally the last row — a dynamic SORT/FILTER spill has no
  // fixed "last row" to anchor to, and this pool is nowhere near 200 people.
  // References the hidden chart-data columns (already blank-filtered) so
  // there's no self-reference risk from averaging a range that includes
  // itself.
  setValueIfEmpty_(sheet, 'B200', 'Mean:');
  setFormulaIfEmpty_(sheet, 'C200', '=AVERAGE(' + "'" + RANK_CHART_DATA_SHEET_NAME + "'" + '!A2:A)');
  setValueIfEmpty_(sheet, 'F200', 'Mean:');
  setFormulaIfEmpty_(sheet, 'G200', '=AVERAGE(' + "'" + RANK_CHART_DATA_SHEET_NAME + "'" + '!B2:B)');
  sheet.getRange('B200:C200').setFontWeight('bold');
  sheet.getRange('F200:G200').setFontWeight('bold');
}

// Removes any existing chart with this title, then recreates it fresh —
// keeps chart options (bucket size, etc.) in sync with the code on every
// sync, rather than freezing them at first creation. The data range is a
// live cell reference either way, so this is purely about picking up
// option changes; if you ever manually drag/resize a chart, that resets
// on the next sync.
function ensureHistogramChart_(sheet, title, dataRange, anchorRow, anchorCol) {
  sheet.getCharts().forEach(function (c) {
    if (c.getOptions().get('title') === title) sheet.removeChart(c);
  });

  const chart = sheet.newChart()
    .setChartType(Charts.ChartType.HISTOGRAM)
    .addRange(dataRange)
    .setOption('title', title)
    .setOption('legend', { position: 'none' })
    .setOption('histogram.bucketSize', 200)
    .setPosition(anchorRow, anchorCol, 0, 0)
    .build();
  sheet.insertChart(chart);
}

function setFormulaIfEmpty_(sheet, a1, formula) {
  const range = sheet.getRange(a1);
  if (range.getFormula() === '') {
    range.setFormula(formula);
  }
}

function setValueIfEmpty_(sheet, a1, value) {
  const range = sheet.getRange(a1);
  if (range.getValue() === '') {
    range.setValue(value);
  }
}

function extractAccountId_(url) {
  if (!url) return null;
  const m = String(url).match(/\/players\/(\d+)/);
  return m ? m[1] : null;
}

// --- "Fantasy Data" / "Fantasy Analysis" -----------------------------------

function syncFantasyData_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const roleDataSheet = ss.getSheetByName(ROLE_DATA_SHEET_NAME);
  const roleDataValues = roleDataSheet ? roleDataSheet.getDataRange().getValues() : [];
  const seasonStats = fetchLastSeasonStats_();

  // One row per person, pulling identity/rating/last-season-bid straight
  // from the already-built Role Data sheet, and joining last season's
  // performance stats (blank for anyone who didn't play last season).
  const people = [];
  for (let r = 1; r < roleDataValues.length; r++) {
    const aid = roleDataValues[r][0];
    if (aid === '' || aid == null) continue;
    const stats = seasonStats[Number(aid)] || {};
    const winrate = stats.games_played ? stats.wins / stats.games_played : '';
    people.push({
      accountId: Number(aid),
      name: roleDataValues[r][1],
      isCaptain: roleDataValues[r][2],
      pos1: roleDataValues[r][3],
      pos2: roleDataValues[r][4],
      pos3: roleDataValues[r][5],
      internalRating: roleDataValues[r][8],
      lastSeasonBid: roleDataValues[r][9],
      winrate: winrate,
      attendance: stats.attendance != null ? stats.attendance : '',
      fantasyPoints: stats.fantasy_points != null ? Math.round(stats.fantasy_points * 10) / 10 : '',
      botSuggestedBid: stats.deserved_cost != null ? stats.deserved_cost : '',
      value: stats.value != null ? stats.value : '',
    });
  }

  // Redground Suggested Bid:
  //  1. Baseline -40 to 360 by min-max placement of this season's Internal
  //     Rating within the non-captain (draftable) pool.
  //  2. +/-40, scaled by how big last season's Value was relative to the
  //     pool's largest |Value| (no last-season data = no adjustment).
  //  3. +/-10, scaled by League Winrate relative to a 50% neutral point —
  //     20*(winrate-0.5), which is naturally bounded to +/-10 since winrate
  //     itself is bounded [0,1] (no last-season games = no adjustment).
  //  4. If self-report/override is <=2 on ALL THREE core roles (Pos 1-3) —
  //     i.e. support-only — reduce the running total by a percentage that
  //     scales with the same pool position used for the baseline: 0% at
  //     the bottom of the pool, 30% at the top, linear in between. A cheap
  //     support-only player gets no penalty; an expensive one gets docked
  //     the most, since their price already reflects being unable to flex.
  //  5. Across-the-board reduction, everyone: 0% at the top of the pool,
  //     40% at the bottom, linear in between — the inverse slope of step 4,
  //     stacked as a second multiplier (so a low-rated support-only player
  //     still gets the full 40% here even though step 4 gave them a pass).
  //  6. A flat +(400 / pool size) added to everyone, after the reductions
  //     above — so the total suggested bid across all players increases by
  //     exactly 400, spread evenly, without disturbing the relative
  //     spread/ordering the rest of the formula already produces. Sized
  //     dynamically off the current pool, so it stays exactly +400 total
  //     even as the pool grows/shrinks between syncs. (If anyone's total is
  //     still floored at 0 after this, the realized total increase is
  //     marginally less than 400 for them specifically — not expected to
  //     matter unless many players are deep in negative territory.)
  //  7. Floor at 0.
  const draftPool = people.filter(function (p) { return !p.isCaptain && p.internalRating !== ''; });
  const ratings = draftPool.map(function (p) { return p.internalRating; });
  const poolMin = ratings.length ? Math.min.apply(null, ratings) : 0;
  const poolMax = ratings.length ? Math.max.apply(null, ratings) : 0;
  const absValues = draftPool
    .filter(function (p) { return p.value !== ''; })
    .map(function (p) { return Math.abs(p.value); });
  const maxAbsValue = absValues.length ? Math.max.apply(null, absValues) : 0;
  const flatShiftPerPlayer = draftPool.length > 0 ? 400 / draftPool.length : 0;

  people.forEach(function (p) {
    if (p.isCaptain || p.internalRating === '') { p.redgroundBid = ''; return; }
    const poolPosition = poolMax > poolMin ? (p.internalRating - poolMin) / (poolMax - poolMin) : 0.5;
    const baseline = -40 + 400 * poolPosition;
    const valueAdj = (p.value !== '' && maxAbsValue > 0) ? 40 * (p.value / maxAbsValue) : 0;
    const winrateAdj = p.winrate !== '' ? Math.max(-10, Math.min(10, 20 * (p.winrate - 0.5))) : 0;
    let total = baseline + valueAdj + winrateAdj;
    const supportOnly = p.pos1 <= 2 && p.pos2 <= 2 && p.pos3 <= 2;
    if (supportOnly) total *= (1 - 0.30 * poolPosition);
    total *= (1 - 0.40 * (1 - poolPosition));
    total += flatShiftPerPlayer;
    p.redgroundBid = Math.max(0, Math.round(total));
  });

  const dataHeaders = [
    'Account ID', 'Name', 'Is Captain', 'Internal Rating', 'Last Season Bid',
    'Winrate', 'Attendance', 'Fantasy Points', 'Bot Suggested Bid', 'Value',
    'Redground Suggested Bid',
  ];
  const dataRows = [dataHeaders];
  people.forEach(function (p) {
    dataRows.push([
      p.accountId, p.name, p.isCaptain, p.internalRating, p.lastSeasonBid,
      p.winrate, p.attendance, p.fantasyPoints, p.botSuggestedBid, p.value, p.redgroundBid,
    ]);
  });

  const dataSheet = getOrCreateHiddenSheet_(FANTASY_DATA_SHEET_NAME);
  dataSheet.clearContents();
  dataSheet.getRange(1, 1, dataRows.length, dataHeaders.length).setValues(dataRows);
  dataSheet.setFrozenRows(1);

  ensureFantasyAnalysisSheet_(people);
}

function fetchLastSeasonStats_() {
  const props = PropertiesService.getScriptProperties();
  const statsUrl = props.getProperty('LAST_SEASON_STATS_URL');
  const apiKey = props.getProperty('RATINGS_API_KEY');
  if (!statsUrl || !apiKey) {
    throw new Error('Missing Script Property: LAST_SEASON_STATS_URL or RATINGS_API_KEY.');
  }
  const resp = UrlFetchApp.fetch(statsUrl, { headers: { 'X-Api-Key': apiKey }, muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) {
    throw new Error('Failed to fetch last season stats: HTTP ' + resp.getResponseCode() + ' ' + resp.getContentText());
  }
  const raw = JSON.parse(resp.getContentText());
  const out = {};
  Object.keys(raw).forEach(function (aid) { out[Number(aid)] = raw[aid]; });
  return out;
}

// Non-captain pool, sorted by Internal Rating high to low. Fully
// script-written like Fantasy Data: this is a pure computed report with no
// manual-entry cells, so a live formula buys nothing here and only
// reintroduces the FILTER/SORT blank-cell risk this file has hit before.
// Only the header row is protected (setValueIfEmpty_), in case you rename
// a column — everything below it is safe to fully rebuild every sync.
function ensureFantasyAnalysisSheet_(people) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(FANTASY_ANALYSIS_SHEET_NAME);
  if (!sheet) sheet = ss.insertSheet(FANTASY_ANALYSIS_SHEET_NAME);

  const headers = [
    'Name', 'Rating', 'League Winrate', 'Attendance %', 'Fantasy Points',
    'Last Season Bid', 'Bot Suggested Bid', "Redground's Suggested Bid",
  ];
  headers.forEach(function (h, i) {
    setValueIfEmpty_(sheet, sheet.getRange(1, i + 1).getA1Notation(), h);
  });
  sheet.getRange(1, 1, 1, headers.length).setFontWeight('bold');
  sheet.setFrozenRows(1);

  const draftPool = people
    .filter(function (p) { return !p.isCaptain; })
    .sort(function (a, b) {
      const ra = a.internalRating === '' ? -Infinity : a.internalRating;
      const rb = b.internalRating === '' ? -Infinity : b.internalRating;
      return rb - ra;
    });

  const rows = draftPool.map(function (p) {
    return [
      p.name, p.internalRating, p.winrate, p.attendance, p.fantasyPoints,
      p.lastSeasonBid, p.botSuggestedBid, p.redgroundBid,
    ];
  });

  // Reset number format on the whole body before reapplying — a column's
  // format (e.g. '0%') otherwise survives a reorder even though its data
  // doesn't, so a later column that used to hold a percentage keeps
  // rendering its new (non-percentage) values as one.
  const maxRows = Math.max(sheet.getLastRow() - 1, rows.length);
  if (maxRows > 0) {
    sheet.getRange(2, 1, maxRows, headers.length).clearContent();
    sheet.getRange(2, 1, maxRows, headers.length).setNumberFormat('General');
  }
  if (rows.length > 0) {
    sheet.getRange(2, 1, rows.length, headers.length).setValues(rows);
    sheet.getRange(2, 3, rows.length, 2).setNumberFormat('0%'); // League Winrate, Attendance %
  }
}

// --- Trigger ---------------------------------------------------------------

// Run this once to (re)install the hourly trigger. Safe to re-run.
function installHourlyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'syncAll') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('syncAll').timeBased().everyHours(1).create();
}

// Freeze the sheet: remove the hourly trigger. Run at draft-lock time.
// Re-enable later by running installHourlyTrigger() again.
function lockSheet() {
  let removed = 0;
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'syncAll') {
      ScriptApp.deleteTrigger(t);
      removed++;
    }
  });
  Logger.log('Sheet locked: removed %s hourly sync trigger(s). Run installHourlyTrigger() to unlock.', removed);
}
