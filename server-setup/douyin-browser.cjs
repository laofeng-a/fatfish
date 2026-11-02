'use strict';
// Private, one-shot helper. Its stdout is consumed by douyin_reader.py only.
// Fresh browser context: never opens an existing browser profile or loads cookies.
const [modulePath, id] = process.argv.slice(2);
const MAX_DETAIL = 1000000;

function formatsFromVideo(video) {
  const formats = [];
  const seen = new Set();
  function audio(node, depth = 0) {
    if (!node || typeof node !== 'object' || depth > 6) return;
    if (node.url_list && typeof node.url_list === 'object') {
      // Current long-video audio_meta uses {main_url, backup_url, fallback_url};
      // older responses use an array. Both describe this video's soundtrack.
      const urls = Array.isArray(node.url_list) ? node.url_list : Object.values(node.url_list);
      for (const url of urls) {
        if (typeof url !== 'string' || !url.startsWith('https://') || seen.has(url)) continue;
        seen.add(url);
        formats.push({format_id: `audio-${formats.length}`, url, ext: 'm4a',
          vcodec: 'none', acodec: 'aac', filesize: Number(node.data_size || node.size) || undefined});
      }
    }
    for (const [key, value] of Object.entries(node)) {
      if (key !== 'url_list' && value && typeof value === 'object') audio(value, depth + 1);
    }
  }
  // These are the video's own soundtrack; music.play_url can be unrelated BGM.
  audio(video.bit_rate_audio);
  audio(video.audio);
  if (!formats.length) {
    const candidates = [video.play_addr, ...(video.bit_rate || []).map(x => x.play_addr)]
      .filter(Boolean).sort((a, b) => (a.data_size || Infinity) - (b.data_size || Infinity));
    for (const address of candidates) {
      for (const url of address.url_list || []) {
        if (typeof url !== 'string' || !url.startsWith('https://') || seen.has(url)) continue;
        seen.add(url);
        formats.push({format_id: `video-${formats.length}`, url, ext: 'mp4',
          filesize: Number(address.data_size) || undefined});
      }
    }
  }
  return formats;
}

async function main() {
  if (!/^\d{10,24}$/.test(id || '') || !modulePath) throw new Error('invalid_input');
  const {chromium} = require(modulePath);
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  let timer;
  try {
    const context = await browser.newContext({viewport: {width: 1280, height: 800}});
    const page = await context.newPage();
    await page.route('**/*', route => ['image', 'font', 'media'].includes(route.request().resourceType())
      ? route.abort() : route.continue());
    let resolve;
    const detail = new Promise(r => { resolve = r; });
    page.on('response', async response => {
      try {
        const address = new URL(response.url());
        if (address.hostname !== 'www.douyin.com' || address.pathname !== '/aweme/v1/web/aweme/detail/') return;
        if (Number(response.headers()['content-length']) > MAX_DETAIL) return;
        const body = await response.body();
        if (body.length > MAX_DETAIL) return;
        const item = JSON.parse(body).aweme_detail;
        if (String(item?.aweme_id) === id && item.video) resolve(item);
      } catch { /* Only a matching, valid response may satisfy the request. */ }
    });
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('public_page_unavailable')), 30000);
    });
    await page.goto(`https://www.douyin.com/video/${id}`, {waitUntil: 'domcontentloaded', timeout: 25000});
    const item = await Promise.race([detail, timeout]);
    const formats = formatsFromVideo(item.video);
    if (!formats.length) throw new Error('media_not_found');
    return {id, title: String(item.desc || '').slice(0, 500),
      description: String(item.desc || '').slice(0, 4000),
      uploader: String(item.author?.nickname || '').slice(0, 200),
      duration: Number(item.video.duration || item.duration) / 1000,
      is_live: false, formats, extractor: 'DouyinPublicPage',
      webpage_url: `https://www.douyin.com/video/${id}`,
      http_headers: {'User-Agent': await page.evaluate(() => navigator.userAgent),
        Referer: 'https://www.douyin.com/'}};
  } finally {
    clearTimeout(timer);
    await browser.close();
  }
}

if (require.main === module) {
  main().then(info => process.stdout.write(JSON.stringify({state: 'ok', info}) + '\n'))
    .catch(() => { process.stdout.write('{"state":"failed","reason":"public_page_unavailable"}\n'); process.exitCode = 1; });
}
module.exports = {formatsFromVideo};
