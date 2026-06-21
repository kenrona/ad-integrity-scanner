"""In-page JavaScript for the render tier.

Three phases:
  INIT_JS    — installed before page scripts: PerformanceObservers (LCP/CLS, with
               layout-shift sources retained) + GPT slotRenderEnded capture.
  SETUP_JS   — run after load+settle: identify ad elements from MULTIPLE sources
               (GPT runtime, ad-host iframes, google_ads_iframe_/adsbygoogle/
               data-google-query-id), dedupe nesting, tag them, and attach an
               IntersectionObserver (time-in-view → MRC viewability) and a
               MutationObserver (iframe swaps → refresh) to each.
  COLLECT_JS — run after the dwell: read geometry, viewability, density,
               interstitial, gaps, sizes, fold split, whitespace, CWV, prebid,
               cmp, video. (Bytes/requests/cookies/CPU come from CDP, not here.)
"""

INIT_JS = r"""
(() => {
  window.__ai = { lcp: 0, cls: 0, shifts: [], slotRenders: {}, ads: [], refreshCounts: {} };
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) window.__ai.lcp = e.startTime;
    }).observe({ type: 'largest-contentful-paint', buffered: true });
  } catch (e) {}
  try {
    let cls = 0;
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (e.hadRecentInput) continue;
        cls += e.value;
        const nodes = (e.sources || []).map(s => s.node).filter(Boolean);
        window.__ai.shifts.push({ value: e.value, nodes });
      }
      window.__ai.cls = cls;
    }).observe({ type: 'layout-shift', buffered: true });
  } catch (e) {}
  // Event Timing -> synthetic INP proxy (worst interaction latency we observe).
  window.__ai.maxEvent = 0;
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) {
        const d = e.duration || 0;
        if (d > window.__ai.maxEvent) window.__ai.maxEvent = d;
      }
    }).observe({ type: 'event', buffered: true, durationThreshold: 16 });
  } catch (e) {}
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) {
        const d = e.processingEnd - e.startTime;
        if (d > window.__ai.maxEvent) window.__ai.maxEvent = d;
      }
    }).observe({ type: 'first-input', buffered: true });
  } catch (e) {}
  window.googletag = window.googletag || { cmd: [] };
  window.googletag.cmd.push(function () {
    try {
      window.googletag.pubads().addEventListener('slotRenderEnded', function (ev) {
        const id = ev.slot.getSlotElementId();
        const t = (window.performance && performance.now) ? performance.now() : 0;
        (window.__ai.slotRenders[id] = window.__ai.slotRenders[id] || []).push(t);
      });
    } catch (e) {}
  });
})();
"""

# Behavioral sticky probe: an ad whose viewport-top barely moves when the page
# scrolls is anchored/sticky (catches JS-driven sticky CSS detection misses).
STICKY_PROBE_JS = r"""
() => new Promise(resolve => {
  const els = Array.from(document.querySelectorAll('[data-ai-ad]'));
  window.scrollTo(0, 0);
  const t0 = els.map(e => e.getBoundingClientRect().top);
  window.scrollTo(0, 900);
  requestAnimationFrame(() => {
    const sticky = [];
    els.forEach((e, i) => {
      const t1 = e.getBoundingClientRect().top;
      if (Math.abs(t1 - t0[i]) < 5) sticky.push(+e.getAttribute('data-ai-ad'));
    });
    window.scrollTo(0, 0);
    window.__ai.behavioralSticky = sticky;
    resolve(sticky.length);
  });
})
"""

# Identify + tag + observe ad elements. Returns the count tagged.
SETUP_JS = r"""
() => {
  const adHostRe = /(googlesyndication|doubleclick|amazon-adsystem|adnxs|criteo|rubiconproject|pubmatic|adsrvr|3lift|sharethrough|smartadserver|teads|adform|openx|casalemedia|33across)/i;
  const set = new Set();
  try {
    const gt = window.googletag;
    if (gt && gt.pubads && gt.pubads().getSlots) {
      for (const s of gt.pubads().getSlots()) {
        const el = document.getElementById(s.getSlotElementId());
        if (el) { el.__aiGpt = true; set.add(el); }
      }
    }
  } catch (e) {}
  document.querySelectorAll('iframe[src]').forEach(f => {
    try { if (adHostRe.test(new URL(f.src, location.href).hostname)) set.add(f); } catch (e) {}
  });
  document.querySelectorAll('[id^="google_ads_iframe_"], ins.adsbygoogle, [data-google-query-id]')
    .forEach(e => set.add(e));

  // Keep outermost only (a slot div containing an ad iframe counts once).
  const els = [...set];
  const outer = els.filter(e => !els.some(o => o !== e && o.contains(e)));

  window.__ai.ads = [];
  window.__ai.refreshCounts = {};
  outer.forEach((el, i) => {
    el.setAttribute('data-ai-ad', String(i));
    const rec = { i, cum_in_view_ms: 0, in_start: null, max_ratio: 0, gpt: !!el.__aiGpt };
    window.__ai.ads.push(rec);
    try {
      new IntersectionObserver((ents) => {
        for (const e of ents) {
          const now = performance.now();
          if (e.intersectionRatio > rec.max_ratio) rec.max_ratio = e.intersectionRatio;
          const vis = e.intersectionRatio >= 0.5;
          if (vis && rec.in_start === null) rec.in_start = now;
          else if (!vis && rec.in_start !== null) { rec.cum_in_view_ms += now - rec.in_start; rec.in_start = null; }
        }
      }, { threshold: [0, 0.3, 0.5, 1] }).observe(el);
    } catch (e) {}
    try {
      new MutationObserver((muts) => {
        let swap = 0;
        muts.forEach(m => m.addedNodes && m.addedNodes.forEach(n => { if (n.tagName === 'IFRAME') swap++; }));
        if (swap) window.__ai.refreshCounts[i] = (window.__ai.refreshCounts[i] || 0) + swap;
      }).observe(el, { childList: true, subtree: true });
    } catch (e) {}
  });

  // Video viewability: time each <video> spends >=50% in view (MRC video = >=2s).
  window.__ai.videos = [];
  document.querySelectorAll('video').forEach((v, i) => {
    v.setAttribute('data-ai-vid', String(i));
    const rec = { i, cum_in_view_ms: 0, in_start: null, max_ratio: 0 };
    window.__ai.videos.push(rec);
    try {
      new IntersectionObserver((ents) => {
        for (const e of ents) {
          const now = performance.now();
          if (e.intersectionRatio > rec.max_ratio) rec.max_ratio = e.intersectionRatio;
          const vis = e.intersectionRatio >= 0.5;
          if (vis && rec.in_start === null) rec.in_start = now;
          else if (!vis && rec.in_start !== null) { rec.cum_in_view_ms += now - rec.in_start; rec.in_start = null; }
        }
      }, { threshold: [0, 0.5, 1] }).observe(v);
    } catch (e) {}
  });
  return window.__ai.ads.length;
}
"""

COLLECT_JS = r"""
() => {
  const out = { cwv: {}, gpt: {}, prebid: {}, cmp: {}, video: {}, resources: {}, layout: {} };
  const vw = window.innerWidth || 1366, vh = window.innerHeight || 768;
  const now = performance.now();

  out.cwv = {
    lcp_ms: (window.__ai && window.__ai.lcp) ? Math.round(window.__ai.lcp) : null,
    cls: (window.__ai && window.__ai.cls != null) ? Math.round(window.__ai.cls * 1000) / 1000 : null,
    inp_ms: (window.__ai && window.__ai.maxEvent) ? Math.round(window.__ai.maxEvent) : null,  // synthetic
  };

  const IAB = {
    '300x250':'Medium Rectangle','336x280':'Large Rectangle','728x90':'Leaderboard',
    '970x90':'Large Leaderboard','970x250':'Billboard','300x600':'Half Page',
    '160x600':'Wide Skyscraper','120x600':'Skyscraper','320x50':'Mobile Banner',
    '320x100':'Large Mobile Banner','300x50':'Mobile Banner','468x60':'Full Banner',
    '234x60':'Half Banner','250x250':'Square','200x200':'Small Square','180x150':'Rectangle',
    '300x1050':'Portrait','970x550':'Panorama','480x320':'Mobile Interstitial','970x66':'Pushdown'
  };
  const med = a => { if (!a.length) return null; const s=[...a].sort((x,y)=>x-y);
    const m=Math.floor(s.length/2); return s.length%2?s[m]:Math.round((s[m-1]+s[m])/2); };

  try {
    const recs = (window.__ai && window.__ai.ads) || [];
    const beh = new Set((window.__ai && window.__ai.behavioralSticky) || []);
    const pageH = document.documentElement.scrollHeight || vh;
    const pageArea = pageH * vw;
    let adArea = 0; const slots = [];
    const docW = document.documentElement.scrollWidth || vw;
    document.querySelectorAll('[data-ai-ad]').forEach(el => {
      const idx = +el.getAttribute('data-ai-ad');
      const rec = recs[idx] || {};
      const r = el.getBoundingClientRect();
      const w = Math.round(r.width), h = Math.round(r.height);
      const top = Math.round(r.top + window.scrollY);
      const cs = getComputedStyle(el);
      const pos = cs.position;
      // GIVT-style geometric validity checks (served-but-not-viewable signals).
      const hidden = cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') === 0;
      const tiny = w > 0 && h > 0 && w <= 2 && h <= 2;            // 1x1 tracking pixels
      const offscreen = (r.right <= 0) || (r.left >= docW) || (top + h < -50);  // off-canvas
      let cum = rec.cum_in_view_ms || 0;
      if (rec.in_start != null) cum += now - rec.in_start;
      adArea += w * h;
      slots.push({ idx, w, h, area: w*h, top, left: Math.round(r.left),
        above_fold: top < vh, in_view: r.top < vh && r.bottom > 0,
        sticky: pos === 'fixed' || pos === 'sticky' || beh.has(idx),
        hidden, tiny, offscreen,
        size_name: IAB[w+'x'+h] || (w*h===0 ? 'Unfilled' : 'Custom'),
        viewable_ms: Math.round(cum),
        max_ratio: Math.round((rec.max_ratio || 0) * 100) / 100,
        gpt: !!rec.gpt });
    });

    const filled = slots.filter(s => s.area > 0);
    // Fold counts exclude hidden/off-screen ads (not part of the visible fold);
    // they're reported separately as GIVT signals.
    const placed = filled.filter(s => !s.hidden && !s.offscreen);
    const interIdx = new Set(filled.filter(s => s.w >= 0.9*vw && s.h >= 0.9*vh).map(s => s.idx));
    const bySize = {};
    filled.forEach(s => { const k = s.w+'x'+s.h; bySize[k] = (bySize[k]||0)+1; });
    const sorted = [...filled].sort((a,b) => a.top - b.top);
    const gaps = [];
    for (let i=1; i<sorted.length; i++) gaps.push(Math.round(sorted[i].top - (sorted[i-1].top + sorted[i-1].h)));
    let fsAd = 0;
    filled.forEach(s => { if (s.top < vh) fsAd += s.w * Math.max(0, Math.min(s.top+s.h, vh) - s.top); });

    // GIVT: ad stacking — pairs of filled slots overlapping >50% of the smaller.
    const stacked = new Set();
    for (let i=0; i<filled.length; i++) for (let j=i+1; j<filled.length; j++) {
      const a = filled[i], b = filled[j];
      const ox = Math.max(0, Math.min(a.left+a.w, b.left+b.w) - Math.max(a.left, b.left));
      const oy = Math.max(0, Math.min(a.top+a.h, b.top+b.h) - Math.max(a.top, b.top));
      const inter = ox * oy;
      if (inter > 0.5 * Math.min(a.area, b.area)) { stacked.add(a.idx); stacked.add(b.idx); }
    }
    // Only count GIVT signals on FILLED slots (an ad actually rendered into a
    // hidden/offscreen/tiny slot) — empty placeholders are benign.
    const hidden = filled.filter(s => s.hidden).length;
    const tiny = filled.filter(s => s.tiny).length;
    const offscreen = filled.filter(s => s.offscreen).length;
    const suspicious = filled.filter(s => s.hidden || s.tiny || s.offscreen || stacked.has(s.idx)).length;

    // Refresh: GPT slotRenderEnded timestamps + MutationObserver iframe swaps.
    const gptR = (window.__ai && window.__ai.slotRenders) || {};
    const moR = (window.__ai && window.__ai.refreshCounts) || {};
    let refreshing = false, minInterval = null, events = 0;
    for (const id in gptR) { const ts = gptR[id]; if (ts.length > 1) { refreshing = true; events += ts.length-1;
      for (let i=1;i<ts.length;i++){ const d=(ts[i]-ts[i-1])/1000; if (minInterval==null||d<minInterval) minInterval=d; } } }
    for (const k in moR) { if (moR[k] > 1) { refreshing = true; events += moR[k]-1; } }

    const adsInView = filled.filter(s => s.max_ratio >= 0.5).length;
    const adsViewable = filled.filter(s => s.viewable_ms >= 1000).length;
    const interstitial = filled.some(s => s.w >= 0.9*vw && s.h >= 0.9*vh);

    out.gpt = {
      present: slots.length > 0,
      detected_via_gpt: filled.filter(s => s.gpt).length,
      slot_count: slots.length,
      filled_count: filled.length,
      empty_count: slots.length - filled.length,
      above_fold_count: placed.filter(s => s.above_fold).length,
      below_fold_count: placed.filter(s => !s.above_fold).length,
      sticky_count: slots.filter(s => s.sticky && !interIdx.has(s.idx)).length,
      interstitial: interstitial,
      hidden_ad_count: hidden,
      tiny_ad_count: tiny,
      offscreen_ad_count: offscreen,
      stacked_ad_count: stacked.size,
      suspicious_ad_count: suspicious,
      ads_in_view: adsInView,
      ads_viewable_1s: adsViewable,
      sizes: bySize,
      total_ad_area: Math.round(adArea),
      page_area: Math.round(pageArea),
      a2cr: pageArea > 0 ? Math.round((adArea/pageArea)*1000)/1000 : null,
      first_screen_ad_coverage: vw*vh>0 ? Math.round((fsAd/(vw*vh))*1000)/1000 : null,
      first_ad_offset_px: sorted.length ? sorted[0].top : null,
      ads_per_screen: pageH>0 ? Math.round(filled.length*vh/pageH*100)/100 : null,
      ads_per_1000px: pageH>0 ? Math.round(filled.length/(pageH/1000)*100)/100 : null,
      gap_min_px: gaps.length ? Math.min(...gaps) : null,
      gap_median_px: med(gaps),
      gap_max_px: gaps.length ? Math.max(...gaps) : null,
      refreshing, refresh_events: events,
      min_refresh_seconds: minInterval != null ? Math.round(minInterval*10)/10 : null,
      slots: slots.slice(0, 60),
    };
  } catch (e) { out.gpt = { present: false, error: String(e) }; }

  // Ad-attributable CLS: shift values whose source nodes sit within an ad element.
  try {
    const shifts = (window.__ai && window.__ai.shifts) || [];
    let adShift = 0, total = 0;
    for (const s of shifts) {
      total += s.value;
      const fromAd = (s.nodes || []).some(n => n && n.nodeType === 1 &&
        (n.closest && (n.closest('[data-ai-ad]') || (n.hasAttribute && n.hasAttribute('data-ai-ad')))));
      if (fromAd) adShift += s.value;
    }
    out.cwv.ad_cls_share = total > 0 ? Math.round((adShift/total)*100)/100 : 0;
  } catch (e) {}

  // First-screen whitespace ('unused space') via grid sampling.
  try {
    let hit = 0, samples = 0;
    const stepX = Math.max(24, vw/24), stepY = Math.max(24, vh/24);
    for (let y=2; y<vh; y+=stepY) for (let x=2; x<vw; x+=stepX) {
      samples++;
      const el = document.elementFromPoint(x, y);
      if (!el || el === document.body || el === document.documentElement) continue;
      const hasText = (el.textContent || '').trim().length > 0;
      const isMedia = /^(IMG|VIDEO|CANVAS|IFRAME|SVG|PICTURE)$/.test(el.tagName);
      if (hasText || isMedia) hit++;
    }
    out.layout = {
      dom_node_count: document.getElementsByTagName('*').length,
      first_screen_fill: samples ? Math.round(hit/samples*1000)/1000 : null,
      first_screen_whitespace: samples ? Math.round((1 - hit/samples)*1000)/1000 : null,
    };
  } catch (e) { out.layout = {}; }

  // ---- Prebid ----
  try {
    const pb = window.pbjs;
    let bidders = [];
    if (pb && pb.getBidResponses) {
      const resp = pb.getBidResponses() || {};
      const s = new Set();
      for (const u in resp) (resp[u].bids || []).forEach(b => b.bidderCode && s.add(b.bidderCode));
      bidders = Array.from(s);
    }
    // SupplyChain (schain) declared in Prebid config — validate asi vs ads.txt.
    let schain = null;
    try {
      let sc = pb && pb.getConfig ? (pb.getConfig('schain') || pb.getConfig().schain) : null;
      // Normalise the various Prebid shapes to an object with .nodes.
      let node = sc && (sc.nodes ? sc : (sc.config || (sc.ordered && sc.ordered.config) || sc.ordered));
      if (node && node.nodes) {
        schain = { complete: node.complete === 1 || node.complete === true,
                   nodes: node.nodes.map(n => ({ asi: (n.asi || '').toLowerCase(), sid: String(n.sid || '') })) };
      }
    } catch (e) {}
    out.prebid = { present: !!(pb && pb.version), version: pb && pb.version ? String(pb.version) : null,
      bidders, bidder_count: bidders.length, schain };
  } catch (e) { out.prebid = { present: false }; }

  // ---- Consent / CMP ----
  // Detect a CMP even when its live API didn't initialise (e.g. an EU TCF prompt
  // that doesn't fire for our non-EU vantage point): check the spec locator
  // iframes and known CMP vendor scripts, not just window.__tcfapi.
  try {
    const q = sel => !!document.querySelector(sel);
    const VENDORS = [
      [/fundingchoicesmessages\.google|fundingchoices/, 'google-funding-choices'],
      [/cookielaw\.org|onetrust|cookiepro|otsdkstub/, 'onetrust'],
      [/consensu\.org|quantcast|cmp\.inmobi/, 'quantcast/iab'],
      [/sp-prod\.net|sourcepoint|sp-prod|message[0-9]*\.sp-/, 'sourcepoint'],
      [/privacy-center\.org|didomi/, 'didomi'],
      [/cookiebot/, 'cookiebot'],
      [/usercentrics/, 'usercentrics'],
      [/trustarc|truste/, 'trustarc'],
      [/osano/, 'osano'], [/termly/, 'termly'], [/sirdata/, 'sirdata'],
    ];
    let hay = '';
    document.querySelectorAll('script[src], iframe[src], link[href]').forEach(e => {
      hay += ' ' + (e.src || e.href || '');
    });
    hay = hay.toLowerCase();
    let vendor = null;
    for (const [re, name] of VENDORS) { if (re.test(hay)) { vendor = name; break; } }

    const tcf = typeof window.__tcfapi === 'function' || q('iframe[name="__tcfapiLocator"]');
    const gpp = typeof window.__gpp === 'function' || q('iframe[name="__gppLocator"]');
    const usp = typeof window.__uspapi === 'function' || q('iframe[name="__uspapiLocator"]');
    out.cmp = {
      tcf, gpp, usp,
      tcf_api_live: typeof window.__tcfapi === 'function',
      gpc: navigator.globalPrivacyControl === true,
      vendor,
      cmp_present: !!(tcf || gpp || usp || vendor),
    };
  } catch (e) { out.cmp = {}; }

  // ---- Video / OLV ----
  try {
    const vids = Array.from(document.querySelectorAll('video'));
    const vrecs = (window.__ai && window.__ai.videos) || [];
    const vnow = performance.now();
    const player = (typeof window.jwplayer !== 'undefined') || (typeof window.videojs !== 'undefined');
    let maxArea = 0, viewable2s = 0, instream = 0, outstream = 0;
    vids.forEach(v => {
      const r = v.getBoundingClientRect();
      maxArea = Math.max(maxArea, Math.round(r.width * r.height));
      const rec = vrecs[+v.getAttribute('data-ai-vid')] || {};
      let cum = rec.cum_in_view_ms || 0;
      if (rec.in_start != null) cum += vnow - rec.in_start;
      if (cum >= 2000) viewable2s++;
      // instream = in a content player; outstream = injected/ad-slot/muted-autoplay unit.
      const inAd = !!v.closest('[data-ai-ad]');
      if (inAd || (v.autoplay && v.muted && !player && !v.controls)) outstream++;
      else if (player || v.controls) instream++;
      else outstream++;
    });
    out.video = {
      jwplayer: typeof window.jwplayer !== 'undefined',
      videojs: typeof window.videojs !== 'undefined',
      video_tag_count: vids.length,
      autoplay_count: vids.filter(v => v.autoplay).length,
      muted_autoplay_count: vids.filter(v => v.autoplay && v.muted).length,
      viewable_2s: viewable2s,
      instream_count: instream,
      outstream_count: outstream,
      max_player_area_px: maxArea,
      large_player: maxArea >= 242500,        // MRC large-ad threshold
      has_video: vids.length > 0 || player,
    };
  } catch (e) { out.video = {}; }

  // Rendered content (used to classify when the static fetch was bot-blocked).
  try {
    const raw = (document.body ? document.body.innerText : '') || '';
    const txt = raw.replace(/\s+/g, ' ').trim().slice(0, 200000);
    out.content_render = {
      title: document.title || null,
      lang: document.documentElement.getAttribute('lang'),
      text: txt.slice(0, 8000),
      word_count: txt ? txt.split(' ').length : 0,
      paragraph_count: document.querySelectorAll('p').length,
      heading_count: document.querySelectorAll('h1,h2,h3').length,
      link_count: document.querySelectorAll('a').length,
    };
  } catch (e) {}

  // In-page resource timing (fallback only — CDP is authoritative, set in Python).
  try {
    const res = performance.getEntriesByType('resource') || [];
    let bytes = 0; for (const r of res) bytes += r.transferSize || 0;
    out.resources_inpage = { request_count: res.length, page_weight_bytes: bytes };
  } catch (e) {}

  return out;
}
"""
