/**
 * The whole thing, in a browser: upload, wait, click the job, pick bars, hear them.
 *
 *   npm run check:browser        (from service/frontend)
 *
 * It starts the service itself (uvicorn on a spare port, worker embedded), drives the
 * real page with Chrome, and prints what it measured. This is not a unit test -- it is
 * the check that the feature the service exists for actually happens in a browser, and
 * it is worth re-running after any change to the score page.
 *
 * Chrome must be installed; Playwright uses the one on this machine rather than
 * downloading its own (`channel: 'chrome'`).
 */

import { spawn } from 'node:child_process'
import { mkdtempSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'

import { chromium } from 'playwright'

const REPO = resolve(import.meta.dirname, '..', '..', '..')
const PORT = Number(process.env.CHECK_PORT ?? 8123)
const BASE = `http://127.0.0.1:${PORT}`
const SCORE = '/home/wei/sheet_music/chopin-waltz-in-a-minor-op-34-no-2.mxl'
const AUDIO = join(REPO, 'analyzer/cache/audio/chopin-waltz-in-a-minor-op-34-no-2.wav')
const FIRST_BAR = 40
const LAST_BAR = 83

function log(...args) {
  console.log(...args)
}

function fail(message) {
  console.error('\nFAILED: ' + message)
  process.exitCode = 1
}

async function waitForService(timeoutMs = 30000) {
  const until = Date.now() + timeoutMs
  while (Date.now() < until) {
    try {
      const response = await fetch(`${BASE}/api/v1/status`)
      if (response.ok) return await response.json()
    } catch {
      /* not up yet */
    }
    await sleep(300)
  }
  throw new Error('the service did not start')
}

const data = mkdtempSync(join(tmpdir(), 'note-seq-check-'))
log('data directory:', data)

const server = spawn(
  join(REPO, '.venv/bin/python'),
  ['-m', 'service.app', '--port', String(PORT), '--data', data],
  { cwd: REPO, stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, SERVICE_HOST: '127.0.0.1' } },
)
server.stdout.on('data', (block) => process.env.VERBOSE && process.stdout.write(block))
server.stderr.on('data', (block) => process.env.VERBOSE && process.stderr.write(block))

let browser
try {
  const status = await waitForService()
  log('service up:', JSON.stringify(status.capabilities))
  if (!status.capabilities.analyze || !status.capabilities.ffmpeg) {
    throw new Error('this machine cannot run the pipeline: ' + JSON.stringify(status.capabilities))
  }
  if (!status.frontend_built) {
    throw new Error('the frontend is not built: run npm run build in service/frontend')
  }

  browser = await chromium.launch({
    channel: 'chrome',
    args: ['--autoplay-policy=no-user-gesture-required'],
  })
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } })
  page.on('console', (message) => {
    if (message.type() === 'error') log('  [browser error]', message.text())
  })

  // ---------------------------------------------------------------- upload
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  log('title:', await page.title())
  await page.setInputFiles('#score', SCORE)
  await page.setInputFiles('#audio', AUDIO)
  await page.fill('#title', 'Chopin, Waltz in A minor (browser check)')
  const started = Date.now()
  await page.click('button:has-text("Analyse")')

  // ---------------------------------------------------------------- the job
  const row = page.locator('[data-testid="job"]').first()
  await row.waitFor({ timeout: 60000 })
  log('job queued after %d ms', Date.now() - started)
  await row.locator('[data-testid="job-link"]').waitFor({ timeout: 300000 })
  const seconds = ((Date.now() - started) / 1000).toFixed(1)
  log('job finished after %s s; the link is %s', seconds, await row.locator('[data-testid="job-link"]').innerText())

  await row.locator('[data-testid="job-link"]').click()
  await page.waitForURL('**/pieces/**', { timeout: 30000 })
  log('landed on:', new URL(page.url()).pathname)

  // ---------------------------------------------------------------- the score
  await page.waitForSelector('[data-testid="measure-overlay"]', { timeout: 60000 })

  const slug = new URL(page.url()).pathname.split('/').pop()
  const pieceData = await (await fetch(`${BASE}/api/v1/pieces/${slug}`)).json()
  const pageOf = (bar) =>
    pieceData.geometry.pages.find((candidate) => candidate.measures.some((m) => m.number === bar))

  async function showPage(number) {
    for (let guard = 0; guard < 20; guard += 1) {
      const label = await page.locator('text=/page \\d+ \\/ \\d+/').first().innerText()
      const shown = Number(label.match(/page (\d+)/)?.[1])
      if (shown === number) return
      await page.click(shown < number ? 'button[title="next page"]' : 'button[title="previous page"]')
      await page.waitForTimeout(250)
    }
    throw new Error(`could not reach page ${number}`)
  }

  await showPage(pageOf(FIRST_BAR).number)
  const bars = await page.locator('[data-testid^="bar-"]').count()
  const pageBox = await page.locator('[data-testid="score-page"]').boundingBox()
  const geometry = pageOf(FIRST_BAR)
  log('page %d shows %d clickable bars; the engraving is %sx%s px',
      geometry.number, bars, Math.round(pageBox.width), Math.round(pageBox.height))
  if (bars !== geometry.measures.length) {
    fail(`${bars} click targets for ${geometry.measures.length} bars on this page`)
  }

  // The click targets are placed in percent of the container, and the container is given
  // the engraving's own aspect ratio -- but the engraving is two nested SVGs, the outer
  // one with a fixed pixel size and no viewBox. If the outer and inner disagree, the
  // targets sit perfectly in their own coordinate system and beside the music. The
  // SVG's own screen matrix is what decides: user units -> pixels, in the browser.
  const matrix = await page.locator('.score-page .definition-scale').evaluate((element) => {
    const ctm = element.getScreenCTM()
    return { a: ctm.a, d: ctm.d, e: ctm.e, f: ctm.f }
  })
  const scaleY = matrix.d
  const expectedHeight = geometry.height * scaleY
  log('the engraving maps %s px per unit; %d units tall = %s px against a %s px box',
      scaleY.toFixed(5), geometry.height, Math.round(expectedHeight), Math.round(pageBox.height))
  if (Math.abs(expectedHeight - pageBox.height) > 3) {
    fail(`the music is scaled by ${scaleY} but the box is ${pageBox.height} px tall`)
  }

  // And where a bar actually is on screen, against where its click target is.
  let worst = 0
  for (const bar of geometry.measures.slice(0, 12)) {
    const target = await page.locator(`[data-testid="bar-${bar.number}"]`).boundingBox()
    const drawn = { x: matrix.e + bar.x0 * matrix.a, y: matrix.f + bar.y0 * scaleY }
    worst = Math.max(worst, Math.abs(drawn.x - target.x), Math.abs(drawn.y - target.y))
  }
  log('click targets land within %s px of where the engraving draws those bars', worst.toFixed(3))
  if (worst > 3) fail(`the overlay is ${worst.toFixed(2)} px out from the engraving`)

  const barBox = await page.locator(`[data-testid="bar-${FIRST_BAR}"]`).boundingBox()
  log('bar %d is at %s,%s (%sx%s px)',
      FIRST_BAR, Math.round(barBox.x), Math.round(barBox.y),
      Math.round(barBox.width), Math.round(barBox.height))
  if (!barBox || barBox.width < 8) fail(`bar ${FIRST_BAR} has no usable click target`)

  // ---------------------------------------------------------------- the range
  await page.click(`[data-testid="bar-${FIRST_BAR}"]`)
  const hint = (await page.locator('[data-testid="range-readout"]').innerText()).trim()
  log('after the first click:', hint)
  if (!hint.includes(String(FIRST_BAR))) fail('the first click did not set the start bar')

  await showPage(pageOf(LAST_BAR).number)
  await page.click(`[data-testid="bar-${LAST_BAR}"]`)
  // The clip appearing is the real signal that the range closed: the readout alone
  // would accept "start at bar 83", which is what a half-set range says.
  await page.locator('[data-testid="clip-audio"]').waitFor({ timeout: 60000 })
  const readout = (await page.locator('[data-testid="range-readout"]').innerText()).replace(/\s+/g, ' ')
  log('range set:', readout.trim())
  if (!readout.includes(String(FIRST_BAR)) || !readout.includes(String(LAST_BAR))) {
    fail('the readout does not show both ends of the range')
  }

  // ---------------------------------------------------------------- the audio
  const audio = page.locator('[data-testid="clip-audio"]')
  const src = await audio.getAttribute('src')
  log('clip source:', src?.slice(0, 24), '…')
  if (!src?.startsWith('blob:')) fail('the clip was not fetched as a blob')

  await page.waitForFunction(
    () => {
      const element = document.querySelector('[data-testid="clip-audio"]')
      return element instanceof HTMLAudioElement && element.readyState >= 1 && element.duration > 0
    },
    undefined,
    { timeout: 60000 },
  )
  const duration = await audio.evaluate((element) => element.duration)
  log('clip duration: %s s', duration.toFixed(3))

  const clipInfo = await page.locator('[data-testid="clip-info"]').innerText()
  log('clip info:', clipInfo.trim())

  await audio.evaluate((element) => element.play())
  await sleep(2500)
  const played = await audio.evaluate((element) => element.currentTime)
  log('after 2.5 s of playing, currentTime is %s s', played.toFixed(3))
  if (played < 1.0) fail(`the clip is not playing (currentTime ${played})`)

  // The bar under the playhead should now be one of the selected bars.
  const now = (await page.locator('[data-testid="range-readout"]').innerText()).match(/now bar (\d+)/)
  log('the page says the playhead is in %s', now ? `bar ${now[1]}` : '(no bar reported)')
  if (!now) fail('the page did not report which bar is sounding')
  else if (Number(now[1]) < FIRST_BAR || Number(now[1]) > LAST_BAR + 1) {
    fail(`the playhead reports bar ${now[1]}, outside the selected range`)
  }

  // The bar that is sounding may be on another page than the one on screen; the page
  // offers a link to it, which is also how a person gets there.
  const elsewhere = page.locator('button:has-text("is on page")')
  if (await elsewhere.count()) {
    log('the page offers:', (await elsewhere.innerText()).trim())
    await elsewhere.first().click()
    await page.waitForTimeout(600)
  }
  const highlighted = await page.locator('[data-testid^="bar-"][data-state="current"]').count()
  log('bars drawn as the playhead: %d', highlighted)
  if (!highlighted) fail('no bar is shown as the current one')
  const outline = await page.locator('[data-testid^="bar-"][data-state="current"]').first().getAttribute('data-testid')
  const outlineBar = Number((outline ?? '').replace('bar-', ''))
  log('the amber outline is on %s (the readout said bar %s a moment earlier)',
      outline, now?.[1])
  if (!(outlineBar >= FIRST_BAR && outlineBar <= LAST_BAR)) {
    fail(`the outline is on ${outline}, outside the selected range`)
  }

  await page.screenshot({ path: join(REPO, 'service/.scratch/browser-check.png'), fullPage: false })
  log('screenshot: service/.scratch/browser-check.png')
  log('\nOK: upload -> job -> score page -> bars %d-%d -> %s s clip -> playing',
      FIRST_BAR, LAST_BAR, duration.toFixed(2))
} catch (error) {
  fail(error.stack ?? String(error))
} finally {
  if (browser) await browser.close()
  server.kill('SIGTERM')
}
