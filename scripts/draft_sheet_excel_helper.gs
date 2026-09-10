// Google Apps Script — pulls the RD2L players + captains CSVs into raw dump
// tabs, then builds a derived "Players" sheet from them via formulas, on an
// hourly schedule.
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
// 4. In the Apps Script editor, select "installHourlyTrigger" from the function
//    dropdown and click Run once (it'll ask you to authorize — expected, it
//    needs permission to fetch external URLs and edit the sheet).
// 5. That's it — syncAll() now runs hourly. Run it manually any time too.
//
// Sheets this creates:
//   Raw Player Data / Raw Captain Data — full CSV mirror, cleared + rewritten
//     every sync. Every CSV column, in CSV order. Not for hand-editing.
//     NOTE: rd2l.gg's captains CSV export (?captains=1) has been observed
//     returning far more rows than the real captains list (looks like it's
//     unioning in most of the player pool). The actual captains PAGE is
//     reliable, so Raw Captain Data is the CSV filtered down to only the
//     account ids that page confirms are real captains — same CSV columns,
//     just the correct row set. If rd2l.gg fixes the CSV export, this
//     filter becomes a no-op and can be removed.
//   Ratings Lookup (hidden) — Account ID -> Internal Rating -> Discord Name.
//     Helper sheet the Players sheet's formulas VLOOKUP into. Internal
//     Rating is refreshed every sync; Discord Name is only scraped once per
//     account (from their rd2l.gg profile page) and then cached here, since
//     it rarely changes and scraping is the slow part.
//   Drafted Players (hidden helper column A) — identity-keyed, append-only:
//     one row per account id ever seen in Raw Player Data, in whatever
//     order they were first appended (never reordered, never removed).
//     Account ID + Name are script-written once per player and then left
//     alone; Winner (dropdown of current captain names) and Cost are the
//     actual manual-entry cells. Because alignment is by account id, not
//     row position, you can freely insert, delete, or reorder rows in Raw
//     Player Data without desyncing anyone's Winner/Cost — type Winner/Cost
//     here, not in Players, since Players's row order changes.
//   Players — Name / Discord ID / Internal MMR / Dotabuff / Windrun come
//     from one SORT()+FILTER() formula (sorted by Internal MMR, high to
//     low) derived from Raw Player Data + Ratings Lookup. Winner / Cost are
//     read-only formulas pulling from Drafted Players by account id, so
//     they stay attached to the right player even as the sort order shifts.
//   Captains — same idea, derived from Raw Captain Data: Name / Discord ID /
//     Windrun / Internal Rating are formulas. CORNN (starting draft budget)
//     is manual entry. Remaining is a formula: CORNN minus the sum of Cost
//     (from Drafted Players) for every row whose Winner is this captain.
//   Teams — Captain/Player/Player/Player/Player blocks, one per captain.
//     Role + captain Name are script-written but APPEND-ONLY: existing
//     blocks are never rewritten or reordered (though contents get repaired/
//     re-sorted every sync — see sortTeamBlocksByRating_), only new blocks
//     get added for captains beyond however many blocks already exist.
//     Player Name cells are LIVE FORMULAS pulling from Drafted Players'
//     Winner column (see sortTeamBlocksByRating_) — updates instantly as
//     Winner/Cost are typed, no script run needed. Drafted Players is the
//     one place you log who drafted whom; don't type player names directly
//     into Teams, they'll be overwritten on the next sync. KNOWN
//     LIMITATION: matched by captain name, so two captains sharing a
//     display name (there are two "Cam"s this season) can't be
//     auto-attributed correctly — check that pair by hand. Windrun /
//     Internal Rating are formulas keyed off whatever name is in that row,
//     looked up against Players then Captains. Average is set once per
//     captain row at block-creation time.

const PLAYERS_SHEET_NAME = 'Raw Player Data';
const CAPTAINS_SHEET_NAME = 'Raw Captain Data';
const RATINGS_LOOKUP_SHEET_NAME = 'Ratings Lookup';
const PLAYERS_DERIVED_SHEET_NAME = 'Players';
const PLAYER_OVERRIDES_SHEET_NAME = 'Drafted Players';
const CAPTAINS_DERIVED_SHEET_NAME = 'Captains';
const TEAMS_SHEET_NAME = 'Teams';
const ADMIN_GUIDE_SHEET_NAME = 'Admin Guide';
const DOTABUFF_COL_IN_RAW = 8; // 1-indexed column H in the raw CSV dumps

function syncAll() {
  ensureAdminGuideSheet_();
  syncRawData();
  // Force Sheets to fully commit + recalculate before anything downstream
  // reads Raw Player Data / Raw Captain Data. Without this, array-formula
  // spills that reference those ranges (Players, Captains, Drafted Players)
  // can go stale after a script rewrite — a row removed from the source
  // sometimes leaves a ghost value behind, or a row added doesn't appear —
  // even though the raw data itself is correct.
  SpreadsheetApp.flush();

  syncRatingsLookup_();
  SpreadsheetApp.flush();

  ensurePlayerOverridesSheet_();
  ensurePlayersSheet_();
  ensurePlayersWithRolesSheet_();
  ensureCaptainsSheet_();
  ensureTeamsSheet_();
  Logger.log('Full sync complete.');
}

// Recomputes Drafted Players / Players / Captains / Teams from whatever is
// CURRENTLY sitting in Raw Player Data / Raw Captain Data — without
// re-fetching from rd2l.gg. Use this after lockSheet() once signups are
// closed and you're hand-editing the raw sheets (add/delete/reorder rows,
// switch someone to captain, etc.): running syncAll instead would
// immediately overwrite your manual edits with fresh data from the
// still-open rd2l.gg site.
function recomputeOnly() {
  ensureAdminGuideSheet_();
  syncRatingsLookup_();
  SpreadsheetApp.flush();

  ensurePlayerOverridesSheet_();
  ensurePlayersSheet_();
  ensurePlayersWithRolesSheet_();
  ensureCaptainsSheet_();
  ensureTeamsSheet_();
  Logger.log('Recompute complete — Raw Player Data / Raw Captain Data were not touched.');
}

// Removes a captain entirely: deletes their row from Raw Captain Data (so
// they aren't re-added by the next recompute) and deletes their exact
// 5-row block from Teams, then recomputes everything else. Teams is
// append-only by design (protects hand-typed player names in every OTHER
// block), so this is the supported way to remove one — don't hand-delete
// rows in Teams directly, it's easy to miscount and delete the wrong 5.
// Prompts for the captain's Account ID (find it via their Dotabuff/Windrun
// link on Raw Captain Data or Captains, or the hidden Account ID column F
// on Teams) rather than taking a name, since two captains can share a
// display name (confirmed real this season) and a name-based lookup would
// risk deleting the wrong one.
function removeCaptain() {
  const ui = SpreadsheetApp.getUi();
  const response = ui.prompt(
    'Remove a captain',
    "Enter the captain's Account ID (from their Dotabuff/Windrun link, or the hidden Account ID column on Teams):",
    ui.ButtonSet.OK_CANCEL
  );
  if (response.getSelectedButton() !== ui.Button.OK) return;
  const accountId = Number(response.getResponseText().trim());
  if (!accountId) {
    ui.alert('Invalid account ID — no changes made.');
    return;
  }

  const ss = SpreadsheetApp.getActiveSpreadsheet();

  let removedFromRaw = false;
  const captainRawSheet = ss.getSheetByName(CAPTAINS_SHEET_NAME);
  if (captainRawSheet) {
    const values = captainRawSheet.getDataRange().getValues();
    for (let r = values.length - 1; r >= 1; r--) {
      const id = extractAccountId_(values[r][DOTABUFF_COL_IN_RAW - 1]);
      if (id && Number(id) === accountId) {
        captainRawSheet.deleteRow(r + 1); // 1-indexed sheet row
        removedFromRaw = true;
      }
    }
  }

  let removedBlock = false;
  const teamsSheet = ss.getSheetByName(TEAMS_SHEET_NAME);
  if (teamsSheet) {
    const values = teamsSheet.getRange(2, 1, Math.max(teamsSheet.getMaxRows() - 1, 0), 6).getValues();
    for (let i = 0; i < values.length; i++) {
      if (values[i][0] === 'Captain' && values[i][5] !== '' && Number(values[i][5]) === accountId) {
        teamsSheet.deleteRows(i + 2, 5); // the Captain row + its 4 Player rows
        removedBlock = true;
        break;
      }
    }
  }

  recomputeOnly();
  ui.alert(
    'Removed captain ' + accountId + ': ' +
    (removedFromRaw ? 'row deleted from Raw Captain Data. ' : 'no row found in Raw Captain Data. ') +
    (removedBlock ? 'Teams block deleted.' : 'No matching Teams block found.')
  );
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
// ids the real captains page confirms — see the note above CAPTAINS_SHEET_NAME.
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

// --- Ratings Lookup helper sheet -------------------------------------------

function syncRatingsLookup_() {
  const props = PropertiesService.getScriptProperties();
  const ratingsUrl = props.getProperty('RATINGS_API_URL');
  const apiKey = props.getProperty('RATINGS_API_KEY');
  const playersCsvUrl = props.getProperty('PLAYERS_CSV_URL');
  if (!ratingsUrl || !apiKey || !playersCsvUrl) {
    throw new Error('Missing Script Property: RATINGS_API_URL, RATINGS_API_KEY, or PLAYERS_CSV_URL.');
  }

  // Every account id currently in the player + captain pools.
  const accountIds = {};
  [PLAYERS_SHEET_NAME, CAPTAINS_SHEET_NAME].forEach(function (name) {
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
    if (!sheet) return;
    const values = sheet.getDataRange().getValues();
    for (let r = 1; r < values.length; r++) {
      const id = extractAccountId_(values[r][DOTABUFF_COL_IN_RAW - 1]);
      if (id) accountIds[id] = true;
    }
  });

  // This call also backfills internal ratings for any never-before-seen
  // account in the current draft sheet (a few per call — see the bot's
  // RATINGS_BACKFILL_LIMIT), so Internal MMR fills in on its own over time.
  // guild_id + season_start make internal_rating match /player exactly (the
  // fantasy-adjusted rating, not the raw cached one) for anyone with match
  // history that season.
  const ratingsQuery = '?draft_sheet_url=' + encodeURIComponent(playersCsvUrl) +
    '&guild_id=1481800158826991718&season_start=2026-04-28';
  const ratingsResp = UrlFetchApp.fetch(
    ratingsUrl + ratingsQuery,
    { headers: { 'X-Api-Key': apiKey }, muteHttpExceptions: true }
  );
  if (ratingsResp.getResponseCode() !== 200) {
    throw new Error('Failed to fetch ratings: HTTP ' + ratingsResp.getResponseCode() + ' ' + ratingsResp.getContentText());
  }
  const ratings = JSON.parse(ratingsResp.getContentText());

  // Keep previously-scraped Discord names — only scrape accounts we've
  // never seen before, since a Discord username rarely changes and the
  // scrape (one profile-page fetch per account) is the slow part.
  const lookupSheet = getOrCreateHiddenSheet_(RATINGS_LOOKUP_SHEET_NAME);
  const existingRows = lookupSheet.getDataRange().getValues();
  const knownDiscordNames = {};
  for (let r = 1; r < existingRows.length; r++) {
    if (existingRows[r][0]) knownDiscordNames[String(existingRows[r][0])] = existingRows[r][2];
  }

  const rows = [['Account ID', 'Internal Rating', 'Discord Name']];
  Object.keys(accountIds).forEach(function (id) {
    const rating = ratings[id] ? ratings[id].internal_rating : null;
    let discordName = knownDiscordNames[id];
    if (discordName === undefined) {
      discordName = scrapeDiscordName_(id);
    }
    // Store as a real Number, not the string Object.keys() gives us — a
    // text-vs-number mismatch against REGEXEXTRACT's output is what silently
    // breaks the Players sheet's VLOOKUPs (IFERROR swallows the #N/A).
    rows.push([Number(id), rating != null ? rating : '', discordName || '']);
  });

  lookupSheet.clearContents();
  lookupSheet.getRange(1, 1, rows.length, 3).setValues(rows);
  lookupSheet.setFrozenRows(1);
}

function scrapeDiscordName_(accountId) {
  try {
    const resp = UrlFetchApp.fetch('https://rd2l.gg/profile/' + accountId, { muteHttpExceptions: true });
    if (resp.getResponseCode() !== 200) return '';
    const m = resp.getContentText().match(/<p class="title">Discord Name<\/p><p class="subtitle">([^<]*)<\/p>/);
    return m ? m[1].trim() : '';
  } catch (e) {
    Logger.log('Discord scrape failed for %s: %s', accountId, e);
    return '';
  }
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

// --- "Drafted Players" helper sheet ----------------------------------------

function ensurePlayerOverridesSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(PLAYER_OVERRIDES_SHEET_NAME);
  const isNew = !sheet;
  if (isNew) sheet = ss.insertSheet(PLAYER_OVERRIDES_SHEET_NAME);

  const headers = ['Account ID', 'Name', 'Winner', 'Cost'];
  const firstRow = sheet.getRange(1, 1, 1, headers.length).getValues()[0];
  const hasHeaders = headers.every(function (h, i) { return firstRow[i] === h; });
  if (!hasHeaders) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.setFrozenRows(1);
  }
  sheet.hideColumns(1);

  const playersSheet = ss.getSheetByName(PLAYERS_SHEET_NAME);
  const playerRowCount = playersSheet ? Math.max(playersSheet.getLastRow() - 1, 0) : 0;

  // One-time migration: this sheet used to have A/B as ArrayFormulas
  // row-aligned to Raw Player Data — which broke the moment a row got
  // inserted/deleted there, since Winner/Cost (C/D, manual) are anchored to
  // a fixed row position while the formula-driven A/B would shift to match
  // the new row content. Bake the current formula results into plain
  // values (correct as of right now, before any raw-data reordering
  // happens), then switch permanently to the identity-keyed model below —
  // an append-only {Account ID, Name} table that doesn't care what row
  // anyone is on in Raw Player Data. Runs at most once: after this, A2 has
  // no formula, so this block never triggers again.
  if (playerRowCount > 0 && sheet.getRange('A2').getFormula() !== '') {
    const baked = sheet.getRange(2, 1, playerRowCount, 2).getValues();
    sheet.getRange(2, 1, playerRowCount, 2).clearContent();
    sheet.getRange(2, 1, playerRowCount, 2).setValues(baked);
  }

  // Identity-keyed, append-only: every account id currently in Raw Player
  // Data that doesn't already have a row here gets one appended (Winner/
  // Cost blank). Existing rows are never touched — deleting, inserting, or
  // reordering rows in Raw Player Data can't desync anyone's Winner/Cost,
  // since alignment is by account id, not row position.
  const playerValues = playersSheet ? playersSheet.getDataRange().getValues() : [];
  const currentPlayers = [];
  const seen = {};
  for (let r = 1; r < playerValues.length; r++) {
    const name = playerValues[r][0];
    const accountId = extractAccountId_(playerValues[r][DOTABUFF_COL_IN_RAW - 1]);
    if (!name || !accountId || seen[accountId]) continue;
    seen[accountId] = true;
    currentPlayers.push({ accountId: Number(accountId), name: name });
  }

  const existingRows = sheet.getRange(2, 1, Math.max(sheet.getLastRow() - 1, 0), 1).getValues();
  const existingIds = {};
  existingRows.forEach(function (row) {
    if (row[0] !== '') existingIds[Number(row[0])] = true;
  });

  const newRows = currentPlayers
    .filter(function (p) { return !existingIds[p.accountId]; })
    .map(function (p) { return [p.accountId, p.name, '', '']; });
  if (newRows.length > 0) {
    const startRow = sheet.getLastRow() + 1;
    sheet.getRange(startRow, 1, newRows.length, 4).setValues(newRows);
  }

  // Winner column gets a dropdown of current captain names. Sourced from
  // Raw Captain Data directly (plain pasted values, exactly as many rows as
  // real captains) rather than the derived Captains sheet, since Captains's
  // formula columns spill "" down the whole sheet depth and would pad the
  // dropdown with blank options.
  const captainRawSheet = ss.getSheetByName(CAPTAINS_SHEET_NAME);
  const captainCount = captainRawSheet ? captainRawSheet.getLastRow() - 1 : 0;
  if (captainCount > 0) {
    const captainNameRange = captainRawSheet.getRange(2, 1, captainCount, 1);
    const rule = SpreadsheetApp.newDataValidation()
      .requireValueInRange(captainNameRange, true)
      .setAllowInvalid(false)
      .build();
    sheet.getRange(2, 3, 1000, 1).setDataValidation(rule);
  }
}

// --- Derived "Players" sheet -------------------------------------------

function ensurePlayersSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(PLAYERS_DERIVED_SHEET_NAME);
  const isNew = !sheet;
  if (isNew) sheet = ss.insertSheet(PLAYERS_DERIVED_SHEET_NAME);

  const headers = ['Name', 'Discord ID', 'Internal MMR', 'Dotabuff', 'Windrun', 'Winner', 'Cost'];
  const firstRow = sheet.getRange(1, 1, 1, headers.length).getValues()[0];
  const hasHeaders = headers.every(function (h, i) { return firstRow[i] === h; });
  if (!hasHeaders) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.setFrozenRows(1);
  }

  // One combined FILTER+SORT array spilling across A:E, sorted by Internal
  // MMR (this virtual array's column 3) high to low. Rows with no name are
  // filtered out first so blank Raw Player Data padding never enters the
  // sort. Only set if A2 is empty, so this never clobbers a hand-edited
  // formula — if the sheet already has the OLD per-column formulas in
  // B2:E2 from before, those must be cleared by hand first or this will
  // fail to spill ("would overwrite existing data").
  setFormulaIfEmpty_(sheet, 'A2',
    '=ARRAYFORMULA(SORT(FILTER({' +
    "'" + PLAYERS_SHEET_NAME + "'!A2:A," +
    "IFERROR(VLOOKUP(VALUE(REGEXEXTRACT('" + PLAYERS_SHEET_NAME + "'!H2:H,\"\\d+\")),'" +
    RATINGS_LOOKUP_SHEET_NAME + "'!$A:$C,3,FALSE),\"\")," +
    "IFERROR(VLOOKUP(VALUE(REGEXEXTRACT('" + PLAYERS_SHEET_NAME + "'!H2:H,\"\\d+\")),'" +
    RATINGS_LOOKUP_SHEET_NAME + "'!$A:$C,2,FALSE),\"\")," +
    "'" + PLAYERS_SHEET_NAME + "'!H2:H," +
    "IFERROR(\"https://windrun.io/players/\"&REGEXEXTRACT('" + PLAYERS_SHEET_NAME + "'!H2:H,\"\\d+\"),\"\")" +
    "}, '" + PLAYERS_SHEET_NAME + "'!A2:A<>\"\"), 3, FALSE))");

  // Winner/Cost: read-only, looked up by this row's own account id (from
  // its own Dotabuff column, D) against Player Overrides — stays correct
  // no matter how the sort above reorders rows.
  setFormulaIfEmpty_(sheet, 'F2',
    "=ARRAYFORMULA(IF($D2:D=\"\",\"\",IFERROR(VLOOKUP(VALUE(REGEXEXTRACT($D2:D,\"\\d+\")),'" +
    PLAYER_OVERRIDES_SHEET_NAME + "'!$A:$D,3,FALSE),\"\")))");
  setFormulaIfEmpty_(sheet, 'G2',
    "=ARRAYFORMULA(IF($D2:D=\"\",\"\",IFERROR(VLOOKUP(VALUE(REGEXEXTRACT($D2:D,\"\\d+\")),'" +
    PLAYER_OVERRIDES_SHEET_NAME + "'!$A:$D,4,FALSE),\"\")))");

  if (isNew) {
    sheet.activate();
    ss.moveActiveSheet(1);
  }
}

const PLAYERS_WITH_ROLES_SHEET_NAME = 'Players With Roles';

// Same as Players, plus Pos 1-5 (straight pass-through from Raw Player
// Data columns K-O, 1-5 scale) spliced in before Winner/Cost.
function ensurePlayersWithRolesSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(PLAYERS_WITH_ROLES_SHEET_NAME);
  if (!sheet) sheet = ss.insertSheet(PLAYERS_WITH_ROLES_SHEET_NAME);

  const headers = ['Name', 'Discord ID', 'Internal MMR', 'Dotabuff', 'Windrun',
    'Pos 1', 'Pos 2', 'Pos 3', 'Pos 4', 'Pos 5', 'Winner', 'Cost'];
  const firstRow = sheet.getRange(1, 1, 1, headers.length).getValues()[0];
  const hasHeaders = headers.every(function (h, i) { return firstRow[i] === h; });
  if (!hasHeaders) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.setFrozenRows(1);
  }

  setFormulaIfEmpty_(sheet, 'A2',
    '=ARRAYFORMULA(SORT(FILTER({' +
    "'" + PLAYERS_SHEET_NAME + "'!A2:A," +
    "IFERROR(VLOOKUP(VALUE(REGEXEXTRACT('" + PLAYERS_SHEET_NAME + "'!H2:H,\"\\d+\")),'" +
    RATINGS_LOOKUP_SHEET_NAME + "'!$A:$C,3,FALSE),\"\")," +
    "IFERROR(VLOOKUP(VALUE(REGEXEXTRACT('" + PLAYERS_SHEET_NAME + "'!H2:H,\"\\d+\")),'" +
    RATINGS_LOOKUP_SHEET_NAME + "'!$A:$C,2,FALSE),\"\")," +
    "'" + PLAYERS_SHEET_NAME + "'!H2:H," +
    "IFERROR(\"https://windrun.io/players/\"&REGEXEXTRACT('" + PLAYERS_SHEET_NAME + "'!H2:H,\"\\d+\"),\"\")," +
    "'" + PLAYERS_SHEET_NAME + "'!K2:K," +
    "'" + PLAYERS_SHEET_NAME + "'!L2:L," +
    "'" + PLAYERS_SHEET_NAME + "'!M2:M," +
    "'" + PLAYERS_SHEET_NAME + "'!N2:N," +
    "'" + PLAYERS_SHEET_NAME + "'!O2:O" +
    "}, '" + PLAYERS_SHEET_NAME + "'!A2:A<>\"\"), 3, FALSE))");

  setFormulaIfEmpty_(sheet, 'K2',
    "=ARRAYFORMULA(IF($D2:D=\"\",\"\",IFERROR(VLOOKUP(VALUE(REGEXEXTRACT($D2:D,\"\\d+\")),'" +
    PLAYER_OVERRIDES_SHEET_NAME + "'!$A:$D,3,FALSE),\"\")))");
  setFormulaIfEmpty_(sheet, 'L2',
    "=ARRAYFORMULA(IF($D2:D=\"\",\"\",IFERROR(VLOOKUP(VALUE(REGEXEXTRACT($D2:D,\"\\d+\")),'" +
    PLAYER_OVERRIDES_SHEET_NAME + "'!$A:$D,4,FALSE),\"\")))");
}

// --- Derived "Captains" sheet --------------------------------------------

function ensureCaptainsSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(CAPTAINS_DERIVED_SHEET_NAME);
  const isNew = !sheet;
  if (isNew) sheet = ss.insertSheet(CAPTAINS_DERIVED_SHEET_NAME);

  const headers = ['Name', 'Discord ID', 'Windrun', 'Internal Rating', 'CORNN', 'Remaining'];
  const firstRow = sheet.getRange(1, 1, 1, headers.length).getValues()[0];
  const hasHeaders = headers.every(function (h, i) { return firstRow[i] === h; });
  if (!hasHeaders) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.setFrozenRows(1);
  }

  // A–D are one combined SORT+FILTER array spilling across the sheet, sorted
  // by Internal Rating (this virtual array's column 4) high to low. Blank
  // captain rows are filtered out so no padding lands mid-sort. Columns A–D
  // are entirely formula-managed here (nothing hand-entered), so we clear
  // and rewrite whenever the sheet's still holding the pre-sort per-column
  // shape — one spill can't overwrite existing formulas.
  const dotabuffRef = "'" + CAPTAINS_SHEET_NAME + "'!H2:H";
  const ratingsLookup = "'" + RATINGS_LOOKUP_SHEET_NAME + "'!$A:$C";
  const sortFormula =
    '=ARRAYFORMULA(SORT(FILTER({' +
    "'" + CAPTAINS_SHEET_NAME + "'!A2:A," +
    "IFERROR(VLOOKUP(VALUE(REGEXEXTRACT(" + dotabuffRef + ",\"\\d+\")), " + ratingsLookup + ", 3, FALSE), \"\")," +
    "IFERROR(\"https://windrun.io/players/\" & REGEXEXTRACT(" + dotabuffRef + ", \"\\d+\"), \"\")," +
    "IFERROR(VLOOKUP(VALUE(REGEXEXTRACT(" + dotabuffRef + ",\"\\d+\")), " + ratingsLookup + ", 2, FALSE), \"\")" +
    "}, '" + CAPTAINS_SHEET_NAME + "'!A2:A<>\"\"), 4, FALSE))";
  if (sheet.getRange('A2').getFormula() !== sortFormula) {
    sheet.getRange('A2:D').clearContent();
    sheet.getRange('A2').setFormula(sortFormula);
  }

  // Remaining = CORNN minus the Cost of every Drafted Players row whose
  // Winner is this captain. Winner is dropdown-validated against these same
  // captain names, so the SUMIF match is always exact.
  setFormulaIfEmpty_(sheet, 'F2',
    "=ARRAYFORMULA(IF($E2:E=\"\",\"\",$E2:E-SUMIF('" + PLAYER_OVERRIDES_SHEET_NAME +
    "'!$C:$C,$A2:A,'" + PLAYER_OVERRIDES_SHEET_NAME + "'!$D:$D)))");
}

// --- Derived "Teams" sheet ------------------------------------------------

function ensureTeamsSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(TEAMS_SHEET_NAME);
  const isNew = !sheet;
  if (isNew) sheet = ss.insertSheet(TEAMS_SHEET_NAME);

  // Column F is a hidden helper: the captain's account id, for captain rows
  // only. Two different real people can share a display name (confirmed —
  // rd2l.gg currently has two distinct captains both named "Cam", different
  // Dotabuff accounts) so a captain row must be resolved by account id, not
  // by name — VLOOKUP-by-name would silently return whichever row matches
  // first and give both "Cam" blocks identical (wrong) data.
  const headers = ['Role', 'Name', 'Windrun', 'Internal Rating', 'Average', 'Account ID'];
  const firstRow = sheet.getRange(1, 1, 1, headers.length).getValues()[0];
  const hasHeaders = headers.every(function (h, i) { return firstRow[i] === h; });
  if (!hasHeaders) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.setFrozenRows(1);
  }
  sheet.hideColumns(6);

  // Row shading by Role, via conditional formatting so it automatically
  // covers every future block — no re-coloring needed as captains get
  // added. Re-applied every sync; if you add your own conditional
  // formatting rules to this sheet elsewhere, this will overwrite them.
  const shadingRange = sheet.getRange('A2:E1000');
  sheet.setConditionalFormatRules([
    SpreadsheetApp.newConditionalFormatRule()
      .whenFormulaSatisfied('=$A2="Captain"')
      .setBackground('#FCE8B2')
      .setRanges([shadingRange])
      .build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenFormulaSatisfied('=$A2="Player"')
      .setBackground('#D9EAD3')
      .setRanges([shadingRange])
      .build(),
  ]);

  // Captain rows (F populated): resolve by account id directly against
  // Ratings Lookup — unambiguous even when two captains share a name.
  // Player rows (F blank): fall back to a name-based lookup against
  // Players then Captains, since a human-typed name is all we have there.
  // Open-ended $B2:B / $F2:F already span the whole sheet depth, so newly
  // appended rows below just recalculate automatically.
  setFormulaIfEmpty_(sheet, 'C2',
    '=ARRAYFORMULA(IF($F2:F<>"","https://windrun.io/players/"&$F2:F,IF($B2:B="","",IFERROR(VLOOKUP($B2:B,' +
    PLAYERS_DERIVED_SHEET_NAME + '!$A:$E,5,FALSE),IFERROR(VLOOKUP($B2:B,' + CAPTAINS_DERIVED_SHEET_NAME +
    '!$A:$C,3,FALSE),"")))))');
  setFormulaIfEmpty_(sheet, 'D2',
    '=ARRAYFORMULA(IF($F2:F<>"",IFERROR(VLOOKUP(VALUE($F2:F),\'' + RATINGS_LOOKUP_SHEET_NAME +
    '\'!$A:$C,2,FALSE),""),IF($B2:B="","",IFERROR(VLOOKUP($B2:B,' + PLAYERS_DERIVED_SHEET_NAME +
    '!$A:$E,3,FALSE),IFERROR(VLOOKUP($B2:B,' + CAPTAINS_DERIVED_SHEET_NAME + '!$A:$D,4,FALSE),"")))))');

  // Captains in Raw Captain Data order, deduped by account id (NOT name —
  // see the header comment above). A true duplicate (same account id twice)
  // is dropped; two different people sharing a name are both kept.
  const captainRawSheet = ss.getSheetByName(CAPTAINS_SHEET_NAME);
  const captainRawValues = captainRawSheet ? captainRawSheet.getDataRange().getValues() : [];
  const seenAccountIds = {};
  const captains = []; // { name, accountId }
  for (let r = 1; r < captainRawValues.length; r++) {
    const name = captainRawValues[r][0];
    const accountId = extractAccountId_(captainRawValues[r][DOTABUFF_COL_IN_RAW - 1]);
    if (!name || !accountId || seenAccountIds[accountId]) continue;
    seenAccountIds[accountId] = true;
    captains.push({ name: name, accountId: accountId });
  }

  // Append-only: a block, once created, is never rewritten or reordered —
  // so player names typed into it are permanently safe. Only captains not
  // already represented ANYWHERE in the sheet get a new block appended.
  //
  // Identity-aware, not count-based: sortTeamBlocksByRating_ (below)
  // reorders blocks by rating every run, so "N blocks exist" no longer means
  // "the first N captains in Raw Captain Data order are covered" after the
  // first sort — captains[N] could easily be someone already present at a
  // different position. Comparing counts caused this to blindly re-append
  // (and then dedupe away) already-present captains while genuinely new
  // ones at an earlier array index never got added. Scanning for which
  // account ids already have a "Captain" row sidesteps that entirely.
  //
  // NOTE: can't use sheet.getLastRow() for the row-count part — the C2/D2
  // ArrayFormulas above spill "" all the way down the sheet's full row
  // extent (open-ended $B2:B/$F2:F ranges), and Sheets counts
  // formula-produced blank cells as "having content", so getLastRow() would
  // report ~1000 instead of the real last row. Column A is pure literal
  // text the script writes, never touched by a formula, so scan it directly.
  const existingRows = sheet.getRange(2, 1, sheet.getMaxRows() - 1, 6).getValues(); // A:F
  let lastRoleRow = 1;
  const existingCaptainIds = {};
  for (let i = 0; i < existingRows.length; i++) {
    const role = existingRows[i][0];
    if (role !== '') lastRoleRow = i + 2;
    if (role === 'Captain' && existingRows[i][5] !== '') {
      existingCaptainIds[String(existingRows[i][5])] = true;
    }
  }
  const newCaptains = captains.filter(function (c) { return !existingCaptainIds[String(c.accountId)]; });
  Logger.log('ensureTeamsSheet_: captains.length=%s, lastRoleRow=%s, existingCaptainCount=%s, newCaptains=%s',
    captains.length, lastRoleRow, Object.keys(existingCaptainIds).length, newCaptains.length);

  if (newCaptains.length > 0) {
    // Only columns A (Role) and B (Name) are written here — C/D are left to
    // the ArrayFormulas above, which already cover these rows once the
    // formula's open-ended range picks up the new B/F values.
    const newRows = [];
    newCaptains.forEach(function (c) {
      newRows.push(['Captain', c.name]);
      for (let p = 0; p < 4; p++) newRows.push(['Player', '']);
    });
    const startRow = lastRoleRow + 1;
    sheet.getRange(startRow, 1, newRows.length, 2).setValues(newRows);

    // Per new captain row: the hidden Account ID (F), and an Average formula
    // (E) over that captain's row + the 4 player rows below it.
    for (let i = 0; i < newCaptains.length; i++) {
      const captainRow = startRow + i * 5;
      sheet.getRange(captainRow, 6).setValue(Number(newCaptains[i].accountId));
      sheet.getRange(captainRow, 5).setFormula(
        '=IFERROR(AVERAGE(D' + captainRow + ':D' + (captainRow + 4) + '),"")'
      );
    }
  }

  // Force the append above to fully commit before the repair function reads
  // the sheet back — without this, its scan can see stale data that doesn't
  // yet include the rows just written (same class of bug as the Raw Data ->
  // derived-sheet staleness fixed elsewhere in this file).
  SpreadsheetApp.flush();

  // Always re-sort + repair — a syncAll run with no new captains should still
  // fix duplicated / misaligned blocks left by earlier buggy appends. This
  // also (re)installs each block's 4 live player-name formulas — see the
  // function's own header comment.
  sortTeamBlocksByRating_(sheet);
}

// Rebuild the Teams sheet as clean 5-row blocks — one per unique captain —
// sorted by the captain's internal rating (high to low). Repairs any
// pre-existing damage: duplicated captains (from historical append bugs)
// are merged and misaligned blocks are re-anchored to consistent 5-row
// offsets. Called at the end of every ensureTeamsSheet_.
//
// Player Name cells are LIVE FORMULAS, not stored values: each one pulls
// whoever has that block's captain as their Winner on Drafted Players, via
// FILTER+INDEX. That's what makes Teams update instantly as Winner/Cost are
// typed — no script run needed for that part. It also means player
// membership is never "read back" from the sheet here; only captain
// identity (name + account id) needs to survive a re-sort, since a
// player's row is 100% derived from Drafted Players at formula-evaluation
// time. KNOWN LIMITATION: matched by captain name, so if two captains share
// a display name (there are two "Cam"s this season), a drafted player whose
// Winner is that name can't be attributed to the right one automatically —
// check that pair by hand during the draft.
function sortTeamBlocksByRating_(sheet) {
  const colAValues = sheet.getRange(2, 1, sheet.getMaxRows() - 1, 1).getValues();
  let lastRoleRow = 1;
  for (let i = 0; i < colAValues.length; i++) {
    if (colAValues[i][0] !== '') lastRoleRow = i + 2;
  }
  if (lastRoleRow < 2) return;

  // Only Captain rows matter here now — walk A:F and pull out {name,
  // accountId} for each one. Tolerates whatever mess is currently in
  // between (short/misaligned blocks from earlier bugs); those get
  // discarded and rebuilt clean below regardless.
  const dataHeight = lastRoleRow - 1;
  const rows = sheet.getRange(2, 1, dataHeight, 6).getValues();
  const rawBlocks = []; // { name, accountId }
  for (let i = 0; i < rows.length; i++) {
    if (rows[i][0] === 'Captain') {
      rawBlocks.push({ name: rows[i][1] || '', accountId: Number(rows[i][5]) || 0 });
    }
  }

  // Dedupe by account_id — a captain who somehow ended up in the sheet
  // multiple times (from earlier append bugs) collapses to one block.
  const byId = {};
  rawBlocks.forEach(function (b) {
    if (!b.accountId) return; // skip blocks with no account id — can't reason about identity
    byId[b.accountId] = b;
  });
  const blocks = Object.keys(byId).map(function (k) { return byId[k]; });
  if (blocks.length === 0) return;

  // Rating lookup — same source the D column formula uses.
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const ratingsSheet = ss.getSheetByName(RATINGS_LOOKUP_SHEET_NAME);
  const ratingsMap = {};
  if (ratingsSheet) {
    const rv = ratingsSheet.getDataRange().getValues();
    for (let r = 1; r < rv.length; r++) {
      const aid = rv[r][0]; const rating = rv[r][1];
      if (aid !== '' && rating !== '') ratingsMap[Number(aid)] = Number(rating);
    }
  }
  blocks.forEach(function (b) {
    b.sortKey = ratingsMap[b.accountId];
    if (b.sortKey === undefined) b.sortKey = -Infinity;
  });
  blocks.sort(function (a, b) {
    if (b.sortKey !== a.sortKey) return b.sortKey - a.sortKey;
    return String(a.name).localeCompare(String(b.name));
  });

  // Emit clean 5-row blocks. A/F are plain values for every row (player-row
  // Name is left blank here — a formula gets installed below instead). C/D
  // self-recompute via their ARRAYFORMULA; E and each player-row Name get a
  // fresh per-row formula.
  const outAB = [];
  const outF  = [];
  const eFormulas = [];
  const playerNameFormulas = [];
  for (let t = 0; t < blocks.length; t++) {
    const captainRow = 2 + t * 5;
    outAB.push(['Captain', blocks[t].name]);
    outF.push([blocks[t].accountId]);
    for (let p = 0; p < 4; p++) {
      outAB.push(['Player', '']);
      outF.push(['']);
      playerNameFormulas.push({
        row: captainRow + 1 + p,
        // Whoever has this block's captain as their Winner on Drafted
        // Players, p-th one in Drafted-Players row order. Blank if fewer
        // than p+1 have been drafted so far.
        formula: '=IFERROR(INDEX(FILTER(\'' + PLAYER_OVERRIDES_SHEET_NAME + '\'!$B:$B,\'' +
          PLAYER_OVERRIDES_SHEET_NAME + '\'!$C:$C=$B$' + captainRow + '),' + (p + 1) + '),"")',
      });
    }
    eFormulas.push({
      row: captainRow,
      formula: '=IFERROR(AVERAGE(D' + captainRow + ':D' + (captainRow + 4) + '),"")',
    });
  }
  const totalRows = blocks.length * 5;

  // Blow away A/B/E/F first so any trailing dupe/garbage rows beyond the new
  // clean layout disappear. C/D are deliberately skipped — they hold the
  // Windrun/Internal Rating ArrayFormulas (set earlier in ensureTeamsSheet_),
  // and clearContent() on a formula cell deletes the formula itself, not
  // just its value.
  const clearHeight = Math.max(dataHeight, totalRows);
  sheet.getRange(2, 1, clearHeight, 2).clearContent(); // A:B
  sheet.getRange(2, 5, clearHeight, 2).clearContent(); // E:F

  sheet.getRange(2, 1, totalRows, 2).setValues(outAB);
  sheet.getRange(2, 6, totalRows, 1).setValues(outF);
  eFormulas.forEach(function (e) {
    sheet.getRange(e.row, 5).setFormula(e.formula);
  });
  playerNameFormulas.forEach(function (pf) {
    sheet.getRange(pf.row, 2).setFormula(pf.formula);
  });
}

// --- Admin Guide ------------------------------------------------------

// Plain-English documentation for whoever's running this next season. Kept
// as short as possible on purpose — always fully rewritten every sync, so
// it stays in sync with the code. Don't add personal notes to this tab,
// they will be wiped on the next sync; use a separate tab for that.
function ensureAdminGuideSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(ADMIN_GUIDE_SHEET_NAME);
  if (!sheet) sheet = ss.insertSheet(ADMIN_GUIDE_SHEET_NAME, 0);
  sheet.clear();
  sheet.setColumnWidth(1, 780);
  sheet.setTabColor('#4285F4');

  const TITLE = 'title', SECTION = 'section', BODY = 'body', SPACER = 'spacer';
  const rows = [
    [TITLE, '📘 Admin Guide'],
    [SPACER, ''],

    [SECTION, 'Tabs'],
    [BODY, 'Raw Player/Captain Data: live rd2l.gg mirror — don\'t hand-edit while syncing is on. Players/Captains: auto-generated, read-only. Drafted Players: type Winner + Cost here (not in Players). Teams: auto-built; Player Name cells update live from Drafted Players — don\'t type into them.'],
    [SPACER, ''],

    [SECTION, 'Closing signups & editing after'],
    [BODY, 'Run lockSheet() once, right before the draft, to stop the hourly rd2l.gg refresh. After that, freely add/delete/reorder rows in Raw Player/Captain Data — order doesn\'t matter. To add a captain, copy their row into Raw Captain Data. After any manual edit, run recomputeOnly() — NOT syncAll, which would re-pull from the still-open rd2l.gg site and undo your edits.'],
    [SPACER, ''],

    [SECTION, 'Removing a captain'],
    [BODY, 'Run removeCaptain() (asks for their Account ID) — don\'t hand-delete Teams rows, the 5-row blocks are easy to miscount.'],
    [SPACER, ''],

    [SECTION, 'Gotchas'],
    [BODY, 'A fully-manual player (not from rd2l.gg) needs one /player run in Discord to get rated. A Dotabuff link must match https://www.dotabuff.com/players/ID exactly. Two captains are both named "Cam" this season — name-based matching (Winner, Teams) can\'t tell them apart, check that pair by hand.'],
    [SPACER, ''],

    [SECTION, 'Functions (Extensions > Apps Script > pick from dropdown > Run)'],
    [BODY, 'syncAll — normal/hourly operation.  lockSheet — freeze before the draft.  installHourlyTrigger — resume syncing.  recomputeOnly — rebuild from current raw data without re-fetching.  removeCaptain — remove one captain + their Teams block.'],
  ];

  sheet.getRange(1, 1, rows.length, 1).setValues(rows.map(function (r) { return [r[1]]; }));

  rows.forEach(function (r, i) {
    const row = i + 1;
    const range = sheet.getRange(row, 1);
    const type = r[0];
    if (type === TITLE) {
      range.setFontSize(18).setFontWeight('bold');
      sheet.setRowHeight(row, 36);
    } else if (type === SECTION) {
      range.setFontSize(12).setFontWeight('bold').setBackground('#E8F0FE');
      sheet.setRowHeight(row, 28);
    } else if (type === BODY) {
      range.setFontSize(10).setWrap(true);
      sheet.setRowHeight(row, 42);
    } else {
      sheet.setRowHeight(row, 10);
    }
  });
}

function setFormulaIfEmpty_(sheet, a1, formula) {
  const range = sheet.getRange(a1);
  if (range.getFormula() === '') {
    range.setFormula(formula);
  }
}

function extractAccountId_(url) {
  if (!url) return null;
  const m = String(url).match(/\/players\/(\d+)/);
  return m ? m[1] : null;
}

// --- Debug -------------------------------------------------------------

// Temporary: functions ending in _ are hidden from the "Select function"
// dropdown, so this public wrapper lets you run + inspect ensureTeamsSheet_
// directly. After running, check View > Logs (or Ctrl+Enter) for output.
function debugTeamsSync() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(TEAMS_SHEET_NAME);
  Logger.log('Teams sheet exists: %s, lastRow: %s', !!sheet, sheet ? sheet.getLastRow() : 'n/a');

  const captainRawSheet = ss.getSheetByName(CAPTAINS_SHEET_NAME);
  Logger.log('Raw Captain Data sheet exists: %s', !!captainRawSheet);
  const captainRawValues = captainRawSheet ? captainRawSheet.getDataRange().getValues() : [];
  Logger.log('Raw Captain Data row count (incl header): %s', captainRawValues.length);
  for (let r = 1; r < captainRawValues.length; r++) {
    const name = captainRawValues[r][0];
    const dotabuffCell = captainRawValues[r][DOTABUFF_COL_IN_RAW - 1];
    const accountId = extractAccountId_(dotabuffCell);
    Logger.log('row %s: name=%s dotabuffCell=%s accountId=%s', r, name, dotabuffCell, accountId);
  }

  ensureTeamsSheet_();
  Logger.log('ensureTeamsSheet_ finished. Teams lastRow now: %s', sheet ? sheet.getLastRow() : 'n/a');
}

// --- Trigger ---------------------------------------------------------------

// Run this once to (re)install the hourly trigger. Safe to re-run — it
// removes any previous trigger for syncAll (or the older syncRawData-only
// trigger) first.
function installHourlyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    const fn = t.getHandlerFunction();
    if (fn === 'syncAll' || fn === 'syncRawData') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('syncAll').timeBased().everyHours(1).create();
}

// Freeze the sheet: remove the hourly trigger so the derived tabs stop
// receiving updates from rd2l.gg or the ratings API. Run this at draft-lock
// time. Re-enable later by running installHourlyTrigger() again.
function lockSheet() {
  let removed = 0;
  ScriptApp.getProjectTriggers().forEach(function (t) {
    const fn = t.getHandlerFunction();
    if (fn === 'syncAll' || fn === 'syncRawData') {
      ScriptApp.deleteTrigger(t);
      removed++;
    }
  });
  Logger.log('Sheet locked: removed %s hourly sync trigger(s). Run installHourlyTrigger() to unlock.', removed);
}
