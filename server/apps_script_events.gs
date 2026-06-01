/**
 * MRS PLC Programmer — flash-event receiver (Google Apps Script).
 *
 * Deployed as a Sheets-bound Web App. The MRS Programmer POSTs one JSON
 * event per flash; this script verifies a shared secret, computes
 * first-program-for-SN by scanning the existing rows, and appends the
 * event to the "Events" sheet. Styrestrøm HQ just opens the sheet to
 * watch flashes happen in real time.
 *
 * ── Deployment ─────────────────────────────────────────────────────────
 * 1. Open https://sheets.google.com → create a new sheet (rename it
 *    something like "MRS Flash Events HQ").
 * 2. Extensions → Apps Script. Delete the placeholder code.
 * 3. Paste this file's contents in. Replace SHARED_SECRET (line ~25)
 *    with a long random string — paste from a password manager, or
 *    generate one with: [Convert]::ToBase64String((1..32 | % {
 *    [byte](Get-Random -Max 256) })) in PowerShell.
 * 4. Save (Ctrl+S). Click Deploy → New deployment → gear icon → "Web app".
 *    Description: "MRS flash event receiver".
 *    Execute as: Me (your account).
 *    Who has access: Anyone.  ← required so the exe can POST without OAuth
 *    Click Deploy. Authorize when prompted (it's your own script).
 * 5. Copy the Web app URL (https://script.google.com/macros/s/…/exec).
 *    Paste it into mrs_protocol/config.py as EVENTS_URL.
 *    Paste your SHARED_SECRET into config.py as EVENTS_SECRET.
 * 6. On every re-deploy ("Manage deployments → Edit → New version"),
 *    the /exec URL stays the same. You only change config.py if you
 *    create a brand-new deployment.
 */

const SHARED_SECRET = 'pxie7lViWim4UzeTTcc34aERydEKX3yVyLf6r9reNh8';

const SHEET_NAME = 'Highbeam X Flash Events HQ';

const HEADER = [
  'Received (UTC)',
  'Client (UTC)',
  'Distributor',
  'Operator',
  'PLC Serial',
  'Part',
  'Module',
  'Channel',
  'Result',
  'First Program',
  'Error',
  'Flasher Exit',
  'Scan Label',
];


function doPost(e) {
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');

    if (String(body.shared_secret || '') !== SHARED_SECRET) {
      return _json({ ok: false, error: 'unauthorized' });
    }

    // plc_serial is intentionally optional — a flash that fails before
    // the SCAN line genuinely has no SN, and we still want HQ to see
    // "this distributor tried and it failed".
    const required = ['distributor', 'operator', 'part', 'result'];
    for (const k of required) {
      if (!String(body[k] || '').trim()) {
        return _json({ ok: false, error: 'missing field: ' + k });
      }
    }

    const sheet     = _sheet();
    const plcSerial = String(body.plc_serial).trim();
    const result    = String(body.result).trim().toUpperCase();

    // first_program_for_sn = no prior OK row for this SN, and this row is OK
    const firstProgram = result === 'OK' && !_priorOkExists(sheet, plcSerial);

    sheet.appendRow([
      new Date().toISOString(),
      String(body.timestamp_utc || ''),
      String(body.distributor   || ''),
      String(body.operator      || ''),
      plcSerial,
      String(body.part          || ''),
      String(body.module        || ''),
      String(body.channel       || ''),
      result,
      firstProgram ? 'FIRST' : '',
      String(body.error_message || ''),
      Number(body.flasher_exit) || 0,
      String(body.scan_label    || ''),
    ]);

    return _json({ ok: true, first_program_for_sn: firstProgram });
  } catch (err) {
    return _json({ ok: false, error: String(err) });
  }
}


function _sheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let s = ss.getSheetByName(SHEET_NAME);
  if (!s) {
    s = ss.insertSheet(SHEET_NAME);
    s.appendRow(HEADER);
    s.setFrozenRows(1);
    s.getRange(1, 1, 1, HEADER.length).setFontWeight('bold');
  }
  return s;
}


function _priorOkExists(sheet, plcSerial) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return false;

  // Columns: 5 = PLC Serial, 9 = Result. Read both in one batch.
  const serials = sheet.getRange(2, 5, lastRow - 1, 1).getValues();
  const results = sheet.getRange(2, 9, lastRow - 1, 1).getValues();
  for (let i = 0; i < serials.length; i++) {
    if (String(serials[i][0]).trim() === plcSerial &&
        String(results[i][0]).trim().toUpperCase() === 'OK') {
      return true;
    }
  }
  return false;
}


function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
