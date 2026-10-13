/**
 * Does a piece's page actually work? Point it at a URL.
 *
 *   npm run check:page                                  # http://127.0.0.1:8100/
 *   npm run check:page -- http://piano.local:8100/pieces/rachmaninov
 *
 * The other check (`browser_check.mjs`) drives the whole flow from an upload; this one
 * starts from a page that already exists — which is the case for a piece published by a
 * broker job, where nothing was uploaded here at all. It measures the same things that
 * matter: the click targets sit on the bars, a range can be chosen, and the clip of that
 * range is fetched and plays.
 *
 * Needs Chrome installed; Playwright drives the one on this machine.
 */

import { chromium } from 'playwright'

const target = process.argv[2] ?? 'http://127.0.0.1:8100/'
const base = new URL(target).origin

function fail(message) {
  console.error('\nFAILED: ' + message)
  process.exitCode = 1
}

const browser = await chromium.launch({
  channel: 'chrome',
  args: ['--autoplay-policy=no-user-gesture-required'],
})
try {
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } })
  page.on('console', (message) => {
    if (message.type() === 'error') console.log('  [browser error]', message.text())
  })

  await page.goto(target, { waitUntil: 'domcontentloaded' })
  console.log('opened:', target)
  console.log('title :', await page.title())

  await page.waitForSelector('[data-testid="measure-overlay"]', { timeout: 60000 })
  const slug = new URL(page.url()).pathname.split('/').pop()
  const piece = await (await fetch(`${base}/api/v1/pieces/${slug}`)).json()
  const first = piece.geometry.pages[0]
  const firstBar = first.measures[0].number
  const lastBar = first.measures[first.measures.length - 1].number
  console.log('piece : %s — %d bars, %d pages, %s map',
              piece.meta.title, piece.meta.bars, piece.geometry.pages.length,
              piece.map.method)

  const targets = await page.locator('[data-testid^="bar-"]').count()
  console.log('page 1: %d click targets for %d bars', targets, first.measures.length)
  if (targets !== first.measures.length) {
    fail(`${targets} click targets for ${first.measures.length} bars on this page`)
  }

  // The click targets are placed by percentage, and the engraving is two nested SVGs;
  // the SVG's own screen matrix is what says whether they landed on the music.
  const matrix = await page.locator('.score-page .definition-scale').evaluate((element) => {
    const ctm = element.getScreenCTM()
    return { a: ctm.a, d: ctm.d, e: ctm.e, f: ctm.f }
  })
  let worst = 0
  for (const bar of first.measures.slice(0, 12)) {
    const box = await page.locator(`[data-testid="bar-${bar.number}"]`).boundingBox()
    worst = Math.max(worst, Math.abs(matrix.e + bar.x0 * matrix.a - box.x),
                     Math.abs(matrix.f + bar.y0 * matrix.d - box.y))
  }
  console.log('click targets land within %s px of the engraving', worst.toFixed(3))
  if (worst > 3) fail(`the overlay is ${worst.toFixed(2)} px out from the bars`)

  // A range, the way a person sets one: first bar, then the last bar of the page.
  await page.click(`[data-testid="bar-${firstBar}"]`)
  await page.click(`[data-testid="bar-${lastBar}"]`)
  const audio = page.locator('[data-testid="clip-audio"]')
  await audio.waitFor({ timeout: 60000 })
  const readout = (await page.locator('[data-testid="range-readout"]').innerText()).replace(/\s+/g, ' ')
  console.log('range : %s', readout.trim())
  if (!readout.includes(String(firstBar)) || !readout.includes(String(lastBar))) {
    fail('the readout does not show both ends of the range')
  }

  await page.waitForFunction(() => {
    const element = document.querySelector('[data-testid="clip-audio"]')
    return element instanceof HTMLAudioElement && element.readyState >= 1 && element.duration > 0
  }, undefined, { timeout: 60000 })
  const duration = await audio.evaluate((element) => element.duration)
  console.log('clip  : %s s — %s', duration.toFixed(2),
              (await page.locator('[data-testid="clip-info"]').innerText()).trim())

  await audio.evaluate((element) => element.play())
  await new Promise((resolve) => setTimeout(resolve, 2500))
  const played = await audio.evaluate((element) => element.currentTime)
  console.log('play  : currentTime %s s after 2.5 s', played.toFixed(2))
  if (played < 1) fail(`the clip is not playing (currentTime ${played})`)

  const sounding = (await page.locator('[data-testid="range-readout"]').innerText())
    .match(/now bar (\d+)/)
  const outline = await page.locator('[data-testid^="bar-"][data-state="current"]').count()
  console.log('playhead: %s, %d bar outlined', sounding ? `bar ${sounding[1]}` : 'none', outline)
  if (!sounding || !outline) fail('the page does not show which bar is sounding')

  console.log('\nOK: %s renders, bars are selectable, and the clip plays', slug)
} catch (error) {
  fail(error.stack ?? String(error))
} finally {
  await browser.close()
}
