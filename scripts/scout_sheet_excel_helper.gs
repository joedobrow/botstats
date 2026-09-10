// Google Apps Script — syncs the RD2L scout sheet with new signups.
//
// SETUP (one time):
// 1. Create a Google Sheet, e.g. "Season 39 Scout Sheet".
// 2. Extensions > Apps Script, delete the boilerplate, paste this whole file.
// 3. Project Settings (gear icon) > Script Properties, add:
//      DRAFT_SHEET_URL = https://rd2l.gg/seasons/<season-id>/divisions/<division-id>/draftsheet
//      RATINGS_API_URL = https://dota-bot.fly.dev/api/ratings   (verify your fly hostname)
//      RATINGS_API_KEY = <the RATINGS_API_KEY value from the bot's .env / fly secrets>
// 4. In the Apps Script editor, select "installHourlyTrigger" from the function
//    dropdown and click Run once (it'll ask you to authorize — that's expected,
//    it needs permission to fetch external URLs and edit the sheet).
// 5. That's it — syncPlayers() will now run every hour automatically. You can
//    also just run syncPlayers() manually any time from the dropdown.
//
// Columns A-E are managed by this script and get overwritten on every sync —
// do not put scout notes there. Add your note columns starting at F; the
// script never reads or writes columns past E.
//
// There's no dedicated Account ID column — players are matched across syncs
// by the Steam account ID embedded in their Dotabuff link (column D), so
// renaming a player on rd2l.gg won't create a duplicate row.

const SHEET_NAME = 'Scout Sheet';
const DOTABUFF_COL = 4; // 1-indexed column letter D, used as the match key

const HEADERS = [
  'Name', 'Internal Rating', 'Self Statement', 'Dotabuff', 'Windrun',
];

function syncPlayers() {
  const props = PropertiesService.getScriptProperties();
  const csvUrl = props.getProperty('DRAFT_SHEET_URL');
  const ratingsUrl = props.getProperty('RATINGS_API_URL');
  const apiKey = props.getProperty('RATINGS_API_KEY');
  if (!csvUrl || !ratingsUrl || !apiKey) {
    throw new Error('Missing Script Property: DRAFT_SHEET_URL, RATINGS_API_URL, or RATINGS_API_KEY. See setup notes at the top of this file.');
  }

  const sheet = getOrCreateSheet_();
  ensureHeaders_(sheet);

  const players = fetchDraftSheet_(csvUrl);
  // Passing draft_sheet_url lets the bot backfill ratings for any signup it
  // hasn't seen yet, a few per call, so new players get rated automatically
  // within a couple of hourly syncs instead of needing a manual /player run.
  // guild_id + season_start make internal_rating match /player exactly (the
  // fantasy-adjusted rating, not the raw cached one) for anyone with match
  // history that season.
  const ratingsQuery = '?draft_sheet_url=' + encodeURIComponent(csvUrl) +
    '&guild_id=1481800158826991718&season_start=2026-04-28';
  const ratings = fetchRatings_(ratingsUrl + ratingsQuery, apiKey);

  const data = sheet.getDataRange().getValues();
  const existingRowByAccountId = {};
  for (let r = 1; r < data.length; r++) {
    const id = extractAccountId_(data[r][DOTABUFF_COL - 1]);
    if (id) existingRowByAccountId[id] = r + 1; // 1-indexed sheet row
  }

  let added = 0, updated = 0, skipped = 0;
  players.forEach(function (p) {
    const accountId = extractAccountId_(p.dotabuff) || extractAccountId_(p.opendota);
    if (!accountId) { skipped++; return; }

    const rating = ratings[accountId];
    const row = [
      p.name || '',
      rating ? rating.internal_rating : '',
      p.statement || '',
      p.dotabuff || '',
      'https://windrun.io/players/' + accountId,
    ];

    const existingRow = existingRowByAccountId[accountId];
    if (existingRow) {
      // Only overwrite A-E; scout note columns (F+) are left completely alone.
      sheet.getRange(existingRow, 1, 1, row.length).setValues([row]);
      updated++;
    } else {
      sheet.appendRow(row);
      added++;
    }
  });

  Logger.log('Sync complete: %s new, %s refreshed, %s skipped (no account id).', added, updated, skipped);
}

function getOrCreateSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) sheet = ss.insertSheet(SHEET_NAME);
  return sheet;
}

function ensureHeaders_(sheet) {
  const firstRow = sheet.getRange(1, 1, 1, HEADERS.length).getValues()[0];
  const hasHeaders = HEADERS.every(function (h, i) { return firstRow[i] === h; });
  if (!hasHeaders) {
    sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
    sheet.setFrozenRows(1);
  }
}

function fetchDraftSheet_(csvUrl) {
  const resp = UrlFetchApp.fetch(csvUrl, { muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) {
    throw new Error('Failed to fetch draft sheet CSV: HTTP ' + resp.getResponseCode());
  }
  const rows = Utilities.parseCsv(resp.getContentText());
  const header = rows[0];
  const idx = {};
  header.forEach(function (h, i) { idx[h] = i; });

  return rows.slice(1).map(function (r) {
    return {
      name: r[idx['name']],
      statement: r[idx['statement']],
      dotabuff: r[idx['dotabuff']],
      opendota: r[idx['opendota']],
    };
  });
}

function fetchRatings_(ratingsUrl, apiKey) {
  const resp = UrlFetchApp.fetch(ratingsUrl, {
    headers: { 'X-Api-Key': apiKey },
    muteHttpExceptions: true,
  });
  if (resp.getResponseCode() !== 200) {
    throw new Error('Failed to fetch ratings: HTTP ' + resp.getResponseCode() + ' ' + resp.getContentText());
  }
  return JSON.parse(resp.getContentText());
}

function extractAccountId_(url) {
  if (!url) return null;
  const m = String(url).match(/\/players\/(\d+)/);
  return m ? m[1] : null;
}

// Run this once to (re)install the hourly trigger. Safe to re-run — it
// removes any previous trigger for syncPlayers first.
function installHourlyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'syncPlayers') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('syncPlayers').timeBased().everyHours(1).create();
}
