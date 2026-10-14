/**
 * Does the patched job dashboard work in a browser? Drives the real app against the real
 * broker, with a token from the superuser account.
 *
 *   node check_dashboard.mjs            # needs FIRST_SUPERUSER / FIRST_SUPERUSER_PASSWORD
 *
 * This is verification for the two patches in the directory above -- the artifact link
 * and the progress timeline -- not something the service ships.
 */

import { chromium } from 'playwright'

const APP = process.env.APP_URL ?? 'http://127.0.0.1:5199'
// the API directly, not through the dev proxy: their list route answers 307 to the
// trailing slash, and a redirect between origins drops the Authorization header
const API = process.env.API_URL ?? 'http://127.0.0.1:8000'
const USER = process.env.FIRST_SUPERUSER
const PASSWORD = process.env.FIRST_SUPERUSER_PASSWORD

function fail(message) {
  console.error('\nFAILED: ' + message)
  process.exitCode = 1
}

if (!USER || !PASSWORD) fail('FIRST_SUPERUSER / FIRST_SUPERUSER_PASSWORD are not set')
if (process.exitCode) process.exit(1)

// 1. a token, the same way the login page gets one
const body = new URLSearchParams({ username: USER, password: PASSWORD })
const response = await fetch(`${API}/api/v1/login/access-token`, {
  body,
  headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  method: 'POST',
})
if (!response.ok) {
  fail(`login failed: ${response.status} ${await response.text()}`)
  process.exit(1)
}
const { access_token: token } = await response.json()
console.log('logged in as %s', USER)

// 2. what the API says the jobs are, so the browser check has something to compare with
const jobs = await (
  await fetch(`${API}/api/v1/jobs/?limit=20`, { headers: { Authorization: `Bearer ${token}` } })
).json()
const rows = jobs.data ?? []
console.log('the broker has %d job(s)', rows.length)
const succeeded = rows.filter((job) => job.status === 'succeeded')
if (succeeded.length === 0) fail('no succeeded job to look at')

let withLink = 0
for (const job of succeeded.slice(0, 5)) {
  const artifacts = await (
    await fetch(`${API}/api/v1/jobs/${job.id}/artifacts`, {
      headers: { Authorization: `Bearer ${token}` },
    })
  ).json()
  const kinds = artifacts.map((a) => `${a.kind}(${a.mime_type})`)
  console.log('  job %s: %s', job.id.slice(0, 8), kinds.join(', ') || 'no artifacts')
  if (artifacts.some((a) => a.kind.endsWith('_url') || a.mime_type === 'text/uri-list')) {
    withLink += 1
  }
}
if (withLink === 0) {
  console.log('note: no artifact of this job set is a link, so (a) cannot be seen here')
}

// 3. the browser
const browser = await chromium.launch({ channel: 'chrome' })
try {
  const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } })
  page.on('console', (message) => {
    if (message.type() === 'error') console.log('  [browser error]', message.text())
  })
  // the app keeps its session in localStorage (hooks/useAuth.ts)
  await page.addInitScript((value) => {
    window.localStorage.setItem('access_token', value)
  }, token)

  await page.goto(`${APP}/jobs`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('table tbody tr', { timeout: 60000 })
  const count = await page.locator('table tbody tr').count()
  console.log('\nthe page rendered %d row(s)', count)

  // (a) an artifact rendered as a link rather than a download
  const links = page.locator('table a[href^="http"]')
  const linkCount = await links.count()
  console.log('(a) artifact links on the page: %d', linkCount)
  for (let index = 0; index < Math.min(linkCount, 4); index += 1) {
    const link = links.nth(index)
    console.log('    %s -> %s', (await link.innerText()).trim(), await link.getAttribute('href'))
  }
  if (linkCount === 0) {
    fail('no artifact was rendered as a link (expected one for a page_url artifact)')
  } else {
    const href = await links.first().getAttribute('href')
    if (!/^https?:\/\//.test(href ?? '')) fail(`the link href is not a URL: ${href}`)
    // and the link is the piece, i.e. the page really answers
    const opened = await fetch(href)
    console.log('    the linked page answers: %s %s', opened.status, opened.headers.get('content-type'))
    if (!opened.ok) fail(`the linked page returned ${opened.status}`)
  }

  // (b) the progress timeline, behind the row's chevron
  const toggles = page.locator('button[title="progress log"]')
  const toggleCount = await toggles.count()
  console.log('\n(b) rows with a progress-log toggle: %d', toggleCount)
  if (toggleCount === 0) fail('no progress-log toggle was rendered')
  await toggles.first().click()
  await page.waitForTimeout(1500)
  const timeline = page.locator('table tbody tr td[colspan="6"]')
  const shown = await timeline.count()
  if (shown === 0) {
    fail('the timeline row did not appear')
  } else {
    const text = (await timeline.first().innerText()).trim().split('\n').filter(Boolean)
    console.log('    the timeline has %d line(s); first few:', text.length)
    for (const line of text.slice(0, 6)) console.log('      %s', line.slice(0, 140))
    const inside = timeline.first().locator('a[href^="http"]')
    const insideCount = await inside.count()
    console.log('    links inside the timeline: %d', insideCount)
    for (let index = 0; index < Math.min(insideCount, 3); index += 1) {
      console.log('      %s', await inside.nth(index).getAttribute('href'))
    }
    if (text.length === 0) fail('the timeline rendered nothing')
  }

  if (process.exitCode !== 1) {
    console.log('\nOK: the dashboard renders an artifact link and the progress timeline')
  }
} catch (error) {
  fail(error.stack ?? String(error))
} finally {
  await browser.close()
}
