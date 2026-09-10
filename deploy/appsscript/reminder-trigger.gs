/**
 * Fires the reminder pass on a timer.
 *
 * Vercel's Hobby plan only allows a cron job once per day, so the schedule
 * lives here instead. This file is deliberately dumb: it makes one HTTP call
 * and logs the answer. Deciding what is due, claiming it and placing the call
 * all happen in Python, in the repo, under test.
 *
 * Setup, once:
 *   1. Open the appointments spreadsheet, Extensions > Apps Script.
 *   2. Paste this file in.
 *   3. Project Settings > Script Properties, add:
 *        REMINDER_ENDPOINT  https://<your-webhook>.vercel.app/tasks/reminders
 *        REMINDER_SECRET    the same value as REMINDER_TRIGGER_SECRET on Vercel
 *   4. Run installTrigger once and accept the permissions prompt.
 *   5. Check Executions after a few minutes.
 *
 * Every five minutes is 288 runs a day, well inside the 90 minutes of daily
 * trigger runtime a consumer Gmail account gets. Once a minute would not be.
 */

var INTERVAL_MINUTES = 5;

function installTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'runReminders') {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }
  ScriptApp.newTrigger('runReminders')
    .timeBased()
    .everyMinutes(INTERVAL_MINUTES)
    .create();
  Logger.log('Trigger installed: runReminders every ' + INTERVAL_MINUTES + ' minutes.');
}

function removeTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'runReminders') {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }
  Logger.log('Trigger removed.');
}

function runReminders() {
  var properties = PropertiesService.getScriptProperties();
  var endpoint = properties.getProperty('REMINDER_ENDPOINT');
  var secret = properties.getProperty('REMINDER_SECRET');

  if (!endpoint || !secret) {
    Logger.log('REMINDER_ENDPOINT and REMINDER_SECRET must both be set in Script Properties.');
    return;
  }

  try {
    var response = UrlFetchApp.fetch(endpoint, {
      method: 'post',
      headers: { 'X-Reminder-Secret': secret },
      muteHttpExceptions: true,
      followRedirects: true
    });
    var code = response.getResponseCode();
    var body = response.getContentText();

    // Anything other than 200 is worth seeing in Executions rather than
    // failing silently every five minutes forever.
    if (code === 200) {
      Logger.log('ok ' + body);
    } else {
      Logger.log('reminder endpoint returned ' + code + ': ' + body);
    }
  } catch (error) {
    // Swallowed on purpose: a thrown error here just means the next run tries
    // again, and a stuck reminder is picked up by the claim timeout anyway.
    Logger.log('reminder trigger failed: ' + error);
  }
}
