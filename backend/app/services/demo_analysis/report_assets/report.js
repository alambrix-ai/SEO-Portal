/* Interactive website-analysis report. Self-contained: no network, no HTML parsing of data.
   Every string from the report is model output or site text, so it is only ever
   set through textContent / setAttribute on elements built here. */
(function () {
  'use strict';

  var doc = document, root = doc.documentElement;
  var app = doc.getElementById('app');
  var D;
  try { D = JSON.parse(doc.getElementById('report-data').textContent || '{}'); } catch (e) { D = null; }
  if (!app) return;
  if (!D || typeof D !== 'object') { fallback(); return; }

  // ── Small utilities ──────────────────────────────────────────────────────
  function arr(v) { return Array.isArray(v) ? v : []; }
  function obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }
  function str(v) { return typeof v === 'string' ? v : (typeof v === 'number' && isFinite(v) ? String(v) : ''); }
  function num(v) { return typeof v === 'number' && isFinite(v) ? v : null; }
  function chars(s) { return Array.from ? Array.from(s).length : s.length; }
  function cap(s) { s = str(s); return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''; }
  function safeUrl(u) { u = str(u); return /^https?:\/\//i.test(u) ? u : ''; }
  function hostOf(u) { return str(u).replace(/^https?:\/\//i, '').replace(/\/$/, ''); }
  function pathOf(u) { var h = hostOf(u), i = h.indexOf('/'); return i < 0 ? '' : h.slice(i); }
  function domainOf(u) { var h = hostOf(u), i = h.indexOf('/'); return i < 0 ? h : h.slice(0, i); }
  var store = {
    get: function (k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* file:// may refuse */ } }
  };

  function add(el, kids) {
    if (kids == null || kids === false) return el;
    if (!Array.isArray(kids)) kids = [kids];
    for (var i = 0; i < kids.length; i++) {
      var k = kids[i];
      if (k == null || k === false || k === '') continue;
      if (Array.isArray(k)) add(el, k);
      else el.appendChild(typeof k === 'string' || typeof k === 'number' ? doc.createTextNode(String(k)) : k);
    }
    return el;
  }
  function h(tag, attrs, kids) {
    var el = doc.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
      var v = attrs[k];
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    return add(el, kids);
  }
  var SVGNS = 'http://www.w3.org/2000/svg';
  function s(tag, attrs, kids) {
    var el = doc.createElementNS(SVGNS, tag);
    for (var k in attrs) if (Object.prototype.hasOwnProperty.call(attrs, k)) el.setAttribute(k, String(attrs[k]));
    (kids || []).forEach(function (c) { el.appendChild(c); });
    return el;
  }
  var ICONS = {
    copy: ['M9 9h10v10H9z', 'M5 15V5h10'],
    check: ['M5 12.5l4.5 4.5L19 7.5'],
    chev: ['M6 9l6 6 6-6'],
    search: ['M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14z', 'M20 20l-4-4'],
    sun: ['M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4'],
    moon: ['M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z'],
    print: ['M7 9V3h10v6', 'M7 17H4v-7h16v7h-3', 'M7 14h10v7H7z'],
    arrow: ['M5 12h14M13 6l6 6-6 6'],
    shuffle: ['M4 7h3c4 0 6 10 10 10h3', 'M4 17h3c1.5 0 2.7-1.4 3.8-3.2M13.2 10.2C14.3 8.4 15.5 7 17 7h3', 'M18 4l3 3-3 3M18 14l3 3-3 3'],
    expand: ['M8 4H4v4M16 4h4v4M8 20H4v-4M16 20h4v-4']
  };
  function icon(name, cls) {
    return s('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', 'class': cls || '' },
      (ICONS[name] || []).map(function (d) { return s('path', { d: d }); }));
  }
  var uid = 0;
  function id(prefix) { uid += 1; return (prefix || 'x') + '-' + uid; }

  // ── Live region, toast and copy ──────────────────────────────────────────
  var live = h('div', { 'class': 'sr', 'aria-live': 'polite', role: 'status' });
  var toast = h('div', { 'class': 'toast', 'aria-hidden': 'true' });
  var toastTimer;
  function announce(msg) {
    live.textContent = ''; setTimeout(function () { live.textContent = msg; }, 30);
    toast.textContent = msg; toast.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { toast.classList.remove('show'); }, 1600);
  }
  function copyText(text, btn) {
    function done() { flash(btn); announce('Copied to clipboard'); }
    function legacy() {
      var ta = h('textarea', { readonly: true, 'aria-hidden': 'true', style: 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0' });
      ta.value = text; doc.body.appendChild(ta); ta.select();
      var ok = false;
      try { ok = doc.execCommand('copy'); } catch (e) { ok = false; }
      doc.body.removeChild(ta);
      if (btn && btn.focus) btn.focus();
      if (ok) done(); else manual(text, btn);
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext !== false) {
        navigator.clipboard.writeText(text).then(done, legacy);
      } else legacy();
    } catch (e) { legacy(); }
  }
  // Last resort when the browser blocks clipboard access: show the text selected, ready for Ctrl+C.
  var panel = null;
  function manual(text, btn) {
    if (panel) panel.remove();
    var ta = h('textarea', { 'class': 'input', readonly: true, rows: 4, 'aria-label': 'Text to copy' });
    ta.value = text;
    var close = h('button', { type: 'button', 'class': 'btn btn-sm', text: 'Done' });
    panel = h('div', { 'class': 'manual-copy', role: 'dialog', 'aria-label': 'Copy text' }, [
      h('p', { 'class': 'small', text: 'Your browser blocked automatic copying. Press Ctrl+C (Cmd+C on Mac) to copy.' }), ta, h('div', { style: 'text-align:right' }, close)]);
    function shut() { if (panel) { panel.remove(); panel = null; } if (btn && btn.focus) btn.focus(); }
    close.addEventListener('click', shut);
    panel.addEventListener('keydown', function (e) { if (e.key === 'Escape') shut(); });
    ta.addEventListener('copy', function () { setTimeout(function () { shut(); announce('Copied to clipboard'); }, 0); });
    doc.body.appendChild(panel);
    ta.focus(); ta.select();
  }
  function flash(btn) {
    if (!btn) return;
    var label = btn.querySelector('.lbl');
    var prev = label ? label.textContent : '';
    btn.classList.add('ok');
    var ic = btn.querySelector('svg');
    if (ic) ic.replaceWith(icon('check'));
    if (label) label.textContent = 'Copied';
    clearTimeout(btn._t);
    btn._t = setTimeout(function () {
      btn.classList.remove('ok');
      var ic2 = btn.querySelector('svg'); if (ic2) ic2.replaceWith(icon('copy'));
      if (label) label.textContent = prev;
    }, 1600);
  }
  function copyBtn(getText, label, what) {
    // label '' gives an icon-only button (its accessible name comes from aria-label).
    var b = h('button', { type: 'button', 'class': 'copy no-print' + (label === '' ? ' icon-only' : ''), 'aria-label': 'Copy ' + (what || label || 'text'), title: 'Copy' },
      [icon('copy'), label === '' ? null : h('span', { 'class': 'lbl', text: label || 'Copy' })]);
    b.addEventListener('click', function () {
      var t = typeof getText === 'function' ? getText() : getText;
      if (t) copyText(t, b);
    });
    return b;
  }
  // Hard limits (ads) are refused by Google when exceeded; soft ones (titles, descriptions) only risk truncation.
  function counter(text, limit, soft) {
    var n = chars(str(text)), over = n > limit;
    return h('span', { 'class': 'count' + (over ? (soft ? ' soft' : ' over') : ''),
      title: over ? (soft ? 'Longer than about ' + limit + ' characters; Google may cut it off' : 'Over the ' + limit + '-character limit') : limit + '-character guide' },
      n + '/' + limit + (over ? (soft ? ' · may be cut off' : ' · too long') : ''));
  }

  // ── Scores ───────────────────────────────────────────────────────────────
  function tone(score) { return score == null ? 'none' : score >= 80 ? 'good' : score >= 50 ? 'mid' : 'bad'; }
  function ring(score, size, stroke, opts) {
    opts = opts || {};
    score = num(score);
    var r = (size - stroke) / 2, c = 2 * Math.PI * r;
    var target = score == null ? c : c * (1 - Math.max(0, Math.min(100, score)) / 100);
    // The final value is set up front; a CSS animation draws it in, so a ring is never left empty.
    var val = s('circle', { cx: size / 2, cy: size / 2, r: r, fill: 'none', 'stroke-width': stroke, 'stroke-linecap': 'round', 'class': 'val c-' + tone(score),
      'stroke-dasharray': c.toFixed(2), 'stroke-dashoffset': target.toFixed(2), style: '--c:' + c.toFixed(2) });
    var svg = s('svg', { width: size, height: size, viewBox: '0 0 ' + size + ' ' + size, 'aria-hidden': 'true' }, [
      s('circle', { cx: size / 2, cy: size / 2, r: r, fill: 'none', 'stroke-width': stroke, 'class': 'track' }), val]);
    var numEl = h('span', { 'class': 'num c-' + tone(score), style: 'font-size:' + Math.round(size * (score == null ? 0.2 : 0.3)) + 'px' },
      [h('span', null, [score == null ? '–' : String(Math.round(score)), opts.sub ? h('small', { text: opts.sub }) : null])]);
    var wrap = h('span', { 'class': 'ring', role: 'img', 'aria-label': opts.label || (score == null ? 'Not measured' : 'Score ' + Math.round(score) + ' out of 100') }, [svg, numEl]);
    return wrap;
  }

  // ── Section scaffolding ──────────────────────────────────────────────────
  var BADGES = { measured: ['b-measured', 'Measured'], ai: ['b-ai', 'AI suggestion'] };
  function badge(kind, text) {
    var b = BADGES[kind];
    return b ? h('span', { 'class': 'badge ' + b[0], text: text || b[1] }) : h('span', { 'class': 'badge b-neutral', text: text || kind });
  }
  function head(eyebrow, kind, title, desc, right, badgeText) {
    return h('div', { 'class': 'sec-head' }, [
      h('div', null, [
        h('div', { 'class': 'eyebrow' }, [eyebrow, kind ? badge(kind, badgeText) : null]),
        h('h2', { text: title }),
        desc ? h('p', { text: desc }) : null
      ]),
      right || null
    ]);
  }
  function card(kids, cls) { return h('div', { 'class': 'card ' + (cls || '') }, kids); }
  function gap(el) { el.style.marginTop = '16px'; return el; }
  function bullets(items, cls) { items = arr(items).filter(function (x) { return str(x); }); return items.length ? h('ul', { 'class': 'list-tight ' + (cls || '') }, items.map(function (x) { return h('li', { text: str(x) }); })) : null; }
  function expander(btn, body, openText, closeText) {
    var bid = id('panel');
    body.id = bid; body.classList.add('collapsed');
    btn.setAttribute('aria-expanded', 'false'); btn.setAttribute('aria-controls', bid);
    function set(open) {
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      body.classList.toggle('collapsed', !open);
      var l = btn.querySelector('.lbl'); if (l && openText) l.textContent = open ? closeText : openText;
    }
    btn.addEventListener('click', function () { set(btn.getAttribute('aria-expanded') !== 'true'); });
    return set;
  }

  var AI = obj(D.ai), M = obj(D.measured), X = obj(D.extra), BIZ = obj(AI.business);
  var CATS = arr(D.categories), CHECKS = arr(D.checks);
  var catLabel = {}; CATS.forEach(function (c) { catLabel[c.key] = c.label; });
  var agentName = {}; arr(D.automation).forEach(function (a) { if (a.agent) agentName[a.agent] = a.name; });
  var STATUS = { fail: 'Fix', warn: 'Improve', info: 'Note', pass: 'Good' };
  var STATUS_LONG = { fail: 'To fix', warn: 'To improve', info: 'Notes', pass: 'Good' };
  var RANK = { fail: 0, warn: 1, info: 2, pass: 3 }, PRANK = { high: 0, medium: 1, low: 2 };

  // ── Sections ─────────────────────────────────────────────────────────────
  function secOverview() {
    var ov = obj(D.overall), sm = obj(D.summary);
    var name = str(BIZ.name) || D.site;
    var site = safeUrl(D.website);
    var meta = h('div', { 'class': 'hero-meta' }, [
      h('span', null, site ? h('a', { href: site, target: '_blank', rel: 'noopener noreferrer', text: D.site }) : D.site),
      BIZ.industry ? h('span', { text: BIZ.industry }) : null,
      M.platform ? h('span', { text: 'Built on ' + M.platform }) : null,
      D.generated_label ? h('span', { text: D.generated_label }) : null,
      X.workspace ? h('span', { text: 'Workspace: ' + X.workspace }) : null
    ]);
    function stat(n, label, cls) { return h('div', { 'class': 'stat ' + (n ? cls || '' : '') }, [h('b', { text: String(n || 0) }), h('span', { text: label })]); }
    var hero = card([
      h('div', null, [
        h('div', { 'class': 'eyebrow' }, ['Website growth analysis']),
        h('h1', { text: name }),
        meta,
        h('div', { 'class': 'stats' }, [
          stat(sm.failed, 'To fix', 's-fail'), stat(sm.warnings, 'To improve', 's-warn'), stat(sm.passed, 'Good', 's-pass'),
          stat(sm.pages_read, 'Pages read'), stat(sm.links_checked, 'Links checked')
        ])
      ]),
      h('div', { 'class': 'hero-score' }, [
        ring(ov.score, 148, 12, { sub: 'of 100', label: 'Overall score ' + (num(ov.score) == null ? 'not available' : ov.score + ' out of 100') }),
        h('div', { 'class': 'grade' }, [ov.grade ? 'Grade ' + ov.grade : 'Overall score', h('br'), badge('measured')])
      ])
    ], 'hero');
    var out = [hero];
    var row = [];
    if (AI.executive_summary || arr(AI.strengths).length) {
      row.push(card([
        h('div', { 'class': 'eyebrow' }, ['Executive summary', badge('ai')]),
        AI.executive_summary ? h('p', { text: AI.executive_summary }) : null,
        arr(AI.strengths).length ? h('h3', { text: "What's working", style: 'margin:16px 0 8px;font-size:14px' }) : null,
        bullets(AI.strengths)
      ]));
    }
    var prof = [];
    if (BIZ.what_it_does) prof.push(['What it does', BIZ.what_it_does]);
    if (arr(BIZ.audience).length) prof.push(['Audience', arr(BIZ.audience).join('; ')]);
    if (arr(BIZ.offer).length) prof.push(['Offer', arr(BIZ.offer).join('; ')]);
    if (arr(BIZ.locations).length) prof.push(['Locations', arr(BIZ.locations).join('; ')]);
    if (BIZ.business_model) prof.push(['Model', BIZ.business_model]);
    if (BIZ.tone) prof.push(['Tone', BIZ.tone]);
    if (prof.length) {
      row.push(card([
        h('div', { 'class': 'eyebrow' }, ['Business profile', badge('ai')]),
        h('dl', { 'class': 'kv' }, prof.map(function (p) { return [h('dt', { text: p[0] }), h('dd', { text: p[1] })]; }))
      ]));
    }
    if (row.length) out.push(h('div', { 'class': 'grid g2', style: 'margin-top:16px' }, row));
    return out;
  }

  function secScores() {
    if (!CATS.length) return null;
    var ps = obj(M.pagespeed);
    return [
      head('Where you stand', 'measured', 'Scores by area', 'Each area is scored from the checks run on your site today. Select an area to see its findings.'),
      h('div', { 'class': 'grid g4' }, CATS.map(function (c) {
        var b = h('button', { type: 'button', 'class': 'cat', 'aria-label': c.label + ': ' + (c.score == null ? 'not measured' : c.score + ' out of 100') + '. Show findings' }, [
          ring(c.score, 58, 6),
          h('span', { style: 'min-width:0' }, [
            h('h3', { text: c.label }),
            c.about ? h('p', { text: c.about }) : null,
            h('span', { 'class': 'meta', style: 'display:block', text: c.total ? c.passed + ' of ' + c.total + ' checks passed' : 'Not measured' })
          ])
        ]);
        b.addEventListener('click', function () { if (findings) findings.focusCategory(c.key); });
        return b;
      })),
      ps.measured === false && ps.reason ? h('p', { 'class': 'callout', style: 'margin-top:14px', text: 'Speed was not measured this time: ' + ps.reason }) : null
    ];
  }

  function secChanges() {
    var cm = X.comparison;
    if (!cm || typeof cm !== 'object') return null;
    var o = obj(cm.overall);
    function delta(d) {
      d = num(d);
      if (d == null) return null;
      return h('span', { 'class': 'delta ' + (d > 0 ? 'up' : d < 0 ? 'down' : 'flat'), text: (d > 0 ? '+' : d < 0 ? '−' : '±') + Math.abs(d) });
    }
    var bars = arr(cm.categories).map(function (c) {
      var b = num(c.before), a = num(c.after);
      return h('div', { 'class': 'bar-row' }, [
        h('span', { text: c.label }),
        h('div', { 'class': 'bar-pair', 'aria-hidden': 'true' }, [
          h('div', { 'class': 'bar before' }, h('i', { style: 'width:' + (b || 0) + '%' })),
          h('div', { 'class': 'bar after' }, h('i', { style: 'width:' + (a || 0) + '%' }))
        ]),
        h('span', { 'class': 'bar-vals' }, [
          h('span', { 'class': 'sr', text: c.label + ': before ' + (b == null ? 'not measured' : b) + ', now ' + (a == null ? 'not measured' : a) + '. ' }),
          h('span', { 'aria-hidden': 'true', text: (b == null ? '–' : b) + ' → ' + (a == null ? '–' : a) + ' ' }),
          delta(c.change)
        ])
      ]);
    });
    function listCard(title, items, dotCls, render, emptyText) {
      return card([
        h('h3', { style: 'margin-bottom:8px' }, [title, ' ', h('span', { 'class': 'muted small', text: '(' + items.length + ')' })]),
        items.length ? h('ul', { 'class': 'ilist' }, items.map(function (it) {
          return h('li', null, [h('span', { 'class': 'dot ' + dotCls }), h('div', null, render(it))]);
        })) : h('p', { 'class': 'muted small', text: emptyText })
      ]);
    }
    var conn = [];
    if (arr(cm.connectors_added).length) conn.push(h('p', { 'class': 'small' }, [h('b', { text: 'Connected since: ' }), arr(cm.connectors_added).join(', ')]));
    if (arr(cm.connectors_removed).length) conn.push(h('p', { 'class': 'small' }, [h('b', { text: 'Disconnected since: ' }), arr(cm.connectors_removed).join(', ')]));
    return [
      head('Progress', 'measured', 'What changed since ' + (cm.previous_at || 'the last analysis'), 'Compared with the previous completed analysis of the same site.'),
      h('div', { 'class': 'grid g2' }, [
        card([
          h('div', { 'class': 'eyebrow' }, ['Overall score']),
          h('div', { 'class': 'big-delta' }, [
            h('span', { 'class': 'muted', text: num(o.before) == null ? '–' : String(o.before) }), h('span', { 'class': 'muted', text: '→' }),
            h('b', { text: num(o.after) == null ? '–' : String(o.after) }), delta(o.change)
          ]),
          h('div', { 'class': 'stats', style: 'grid-template-columns:repeat(3,minmax(0,1fr))' }, [
            h('div', { 'class': 'stat s-pass' }, [h('b', { text: String(arr(cm.fixed).length) }), h('span', { text: 'Fixed' })]),
            h('div', { 'class': 'stat s-fail' }, [h('b', { text: String(arr(cm.new_issues).length) }), h('span', { text: 'New issues' })]),
            h('div', { 'class': 'stat s-warn' }, [h('b', { text: String(arr(cm.still_open).length) }), h('span', { text: 'Still open' })])
          ]),
          conn.length ? h('div', { 'class': 'stack', style: 'margin-top:14px' }, conn) : null
        ]),
        bars.length ? card([
          h('div', { 'class': 'sub-h' }, [h('h4', { text: 'By area' }), h('div', { 'class': 'legend' }, [
            h('span', null, [h('i', { style: 'background:var(--border-strong)' }), 'Before']),
            h('span', null, [h('i', { style: 'background:var(--accent)' }), 'Now'])
          ])]),
          h('div', { 'class': 'bars', style: 'margin-top:10px' }, bars)
        ]) : null
      ]),
      h('div', { 'class': 'grid g3', style: 'margin-top:16px' }, [
        listCard('Fixed', arr(cm.fixed), 'd-pass', function (f) { return [h('b', { text: f.title }), f.after ? h('div', { 'class': 'muted small', text: f.after }) : null]; }, 'Nothing newly fixed.'),
        listCard('New issues', arr(cm.new_issues), 'd-fail', function (f) { return [h('b', { text: f.title }), f.evidence ? h('div', { 'class': 'muted small', text: f.evidence }) : null]; }, 'No new issues.'),
        listCard('Still open', arr(cm.still_open), 'd-warn', function (t) { return [h('span', { text: t })]; }, 'Nothing carried over.')
      ])
    ];
  }

  function secPriorities() {
    var pr = arr(AI.top_priorities), la = arr(AI.local_actions);
    if (!pr.length && !la.length) return null;
    var out = [head('Action plan', 'ai', 'Top priorities', 'Ordered by expected impact. Written from what was measured on your site.')];
    if (pr.length) out.push(h('ol', { 'class': 'prios' }, pr.map(function (p, i) {
      return h('li', { 'class': 'card prio' }, [
        h('span', { 'class': 'prio-n', text: String(i + 1), 'aria-hidden': 'true' }),
        h('div', { style: 'min-width:0' }, [
          h('h3', { text: p.title }),
          h('div', { 'class': 'tags' }, [
            p.impact ? h('span', { 'class': 'tag' + (/high/i.test(p.impact) ? ' hi' : ''), text: cap(p.impact) + ' impact' }) : null,
            p.effort ? h('span', { 'class': 'tag', text: cap(p.effort) + ' effort' }) : null,
            p.timeframe ? h('span', { 'class': 'tag', text: cap(p.timeframe) }) : null
          ]),
          h('div', { 'class': 'prio-cols' }, [
            p.why ? h('div', null, [h('h4', { text: 'Why' }), h('p', { text: p.why })]) : null,
            p.how ? h('div', null, [h('h4', { text: 'How' }), h('p', { text: p.how })]) : null
          ])
        ])
      ]);
    })));
    if (la.length) out.push(gap(card([h('div', { 'class': 'eyebrow' }, ['Local actions', badge('ai')]), bullets(la)])));
    return out;
  }

  var findings = null;
  function secFindings() {
    if (!CHECKS.length) return null;
    var state = { cat: 'all', status: 'all', q: '' };
    var checks = CHECKS.slice().sort(function (a, b) {
      return (RANK[a.status] - RANK[b.status]) || ((PRANK[a.priority] == null ? 3 : PRANK[a.priority]) - (PRANK[b.priority] == null ? 3 : PRANK[b.priority]));
    });
    var items = checks.map(function (c) {
      var body = h('div', { 'class': 'check-b' }, [
        c.why ? h('div', null, [h('h4', { text: 'Why it matters' }), h('p', { text: c.why })]) : null,
        c.fix && c.status !== 'pass' ? h('div', null, [h('h4', { text: 'How to fix' }), h('p', { text: c.fix })]) : null,
        arr(c.pages).length ? h('div', { 'class': 'wide' }, [h('h4', { text: 'Affected pages (' + c.pages.length + ')' }),
          h('ul', { 'class': 'urls' }, c.pages.map(function (u) { return h('li', { text: u }); }))]) : null,
        c.agent && c.status !== 'pass' ? h('div', { 'class': 'wide' }, [h('p', { 'class': 'callout' }, [
          'Can be handled by the ', h('b', { text: agentName[c.agent] || cap(c.agent.replace(/_/g, ' ')) }), ' agent in ' + D.app + '.'])]) : null
      ]);
      var btn = h('button', { type: 'button', 'class': 'check-h' }, [
        h('span', { 'class': 'status-col' }, h('span', { 'class': 'pill p-' + c.status, text: STATUS[c.status] })),
        h('span', { style: 'min-width:0' }, [
          h('span', { 'class': 't', style: 'display:block', text: c.title }),
          c.evidence ? h('span', { 'class': 'ev', style: 'display:block', text: c.evidence }) : null,
          h('span', { 'class': 'cm' }, [
            catLabel[c.category] ? h('span', { 'class': 'tag', text: catLabel[c.category] }) : null,
            c.priority && c.status !== 'pass' ? h('span', { 'class': 'tag' + (c.priority === 'high' ? ' hi' : ''), text: cap(c.priority) + ' priority' }) : null,
            c.effort && c.status !== 'pass' ? h('span', { 'class': 'tag', text: c.effort === 'quick' ? 'Quick fix' : cap(c.effort) + ' effort' }) : null
          ])
        ]),
        icon('chev', 'chev')
      ]);
      var set = expander(btn, body);
      var el = h('div', { 'class': 'check' }, [btn, body]);
      var hay = [c.title, c.evidence, c.why, c.fix, catLabel[c.category], STATUS[c.status]].concat(arr(c.pages)).join(' ').toLowerCase();
      return { c: c, el: el, btn: btn, set: set, hay: hay };
    });

    function chip(label, value, key, dotCls) {
      var ct = h('span', { 'class': 'ct', text: '0' });
      var b = h('button', { type: 'button', 'class': 'chip', 'aria-pressed': 'false' }, [dotCls ? h('span', { 'class': 'dot ' + dotCls }) : null, label, ct]);
      b.addEventListener('click', function () { state[key] = value; apply(); });
      return { b: b, ct: ct, value: value, key: key };
    }
    var catChips = [chip('All areas', 'all', 'cat')].concat(CATS.filter(function (c) {
      return checks.some(function (x) { return x.category === c.key; });
    }).map(function (c) { return chip(c.label, c.key, 'cat'); }));
    var statusChips = [chip('All', 'all', 'status')].concat(['fail', 'warn', 'info', 'pass'].map(function (k) { return chip(STATUS_LONG[k], k, 'status', 'd-' + k); }));
    var input = h('input', { type: 'search', 'class': 'input', placeholder: 'Search checks, evidence, pages…', 'aria-label': 'Search findings' });
    input.addEventListener('input', function () { state.q = input.value.trim().toLowerCase(); apply(); });
    var count = h('span', { 'class': 'result-count', 'aria-live': 'polite' });
    var allBtn = h('button', { type: 'button', 'class': 'btn' }, [icon('expand'), h('span', { 'class': 'lbl', text: 'Expand all' })]);
    allBtn.addEventListener('click', function () {
      var vis = items.filter(function (it) { return !it.el.hidden; });
      var open = vis.some(function (it) { return it.btn.getAttribute('aria-expanded') !== 'true'; });
      vis.forEach(function (it) { it.set(open); });
      syncAll();
    });
    function syncAll() {
      var vis = items.filter(function (it) { return !it.el.hidden; });
      var anyClosed = vis.some(function (it) { return it.btn.getAttribute('aria-expanded') !== 'true'; });
      allBtn.querySelector('.lbl').textContent = anyClosed || !vis.length ? 'Expand all' : 'Collapse all';
    }
    items.forEach(function (it) { it.btn.addEventListener('click', syncAll); });
    var clear = h('button', { type: 'button', 'class': 'btn btn-sm', text: 'Clear filters' });
    clear.addEventListener('click', function () { state.cat = 'all'; state.status = 'all'; state.q = ''; input.value = ''; apply(); });
    var empty = h('div', { 'class': 'empty no-print', hidden: true }, ['No checks match these filters. ', clear]);

    function match(it, skip) {
      var c = it.c;
      if (skip !== 'cat' && state.cat !== 'all' && c.category !== state.cat) return false;
      if (skip !== 'status' && state.status !== 'all' && c.status !== state.status) return false;
      if (state.q && it.hay.indexOf(state.q) < 0) return false;
      return true;
    }
    function apply() {
      var shown = 0;
      items.forEach(function (it) { var ok = match(it); it.el.hidden = !ok; if (ok) shown++; });
      catChips.forEach(function (ch) {
        ch.b.setAttribute('aria-pressed', state.cat === ch.value ? 'true' : 'false');
        ch.ct.textContent = String(items.filter(function (it) { return match(it, 'cat') && (ch.value === 'all' || it.c.category === ch.value); }).length);
      });
      statusChips.forEach(function (ch) {
        ch.b.setAttribute('aria-pressed', state.status === ch.value ? 'true' : 'false');
        ch.ct.textContent = String(items.filter(function (it) { return match(it, 'status') && (ch.value === 'all' || it.c.status === ch.value); }).length);
      });
      count.textContent = 'Showing ' + shown + ' of ' + items.length + ' checks';
      empty.hidden = shown > 0;
      syncAll();
    }
    var sec;
    findings = {
      focusCategory: function (key) {
        state.cat = key; state.status = 'all'; state.q = ''; input.value = ''; apply();
        if (sec) { sec.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
        var pressed = catChips.filter(function (ch) { return ch.value === key; })[0];
        if (pressed) setTimeout(function () { pressed.b.focus({ preventScroll: true }); }, 350);
      },
      bind: function (el) { sec = el; }
    };
    var sm = obj(D.summary);
    var out = [
      head('Findings', 'measured', 'Every check (' + checks.length + ')',
        (sm.pages_read ? sm.pages_read + ' pages read and ' + (sm.links_checked || 0) + ' links checked. ' : '') + 'Filter, search and open any check for why it matters and how to fix it.'),
      h('div', { 'class': 'toolbar' }, [
        h('div', { 'class': 'chips', role: 'group', 'aria-label': 'Filter by area' }, catChips.map(function (c) { return c.b; })),
        h('div', { 'class': 'chips', role: 'group', 'aria-label': 'Filter by status' }, statusChips.map(function (c) { return c.b; })),
        h('div', { 'class': 'tool-row' }, [h('div', { 'class': 'search' }, [icon('search'), input]), allBtn, count])
      ]),
      h('div', { 'class': 'checks' }, items.map(function (it) { return it.el; })),
      empty
    ];
    apply();
    return out;
  }

  function secSpeed() {
    var ps = obj(M.pagespeed);
    if (!ps.measured) return null;
    var sc = obj(ps.scores), lab = obj(ps.lab), field = obj(ps.field);
    var names = [['performance', 'Performance'], ['accessibility', 'Accessibility'], ['best_practices', 'Best practices'], ['seo', 'SEO']];
    var labNames = [['fcp', 'First contentful paint'], ['lcp', 'Largest contentful paint'], ['tbt', 'Total blocking time'], ['cls', 'Cumulative layout shift'], ['speed_index', 'Speed index']];
    var fieldNames = [['lcp', 'LCP (p75)'], ['inp', 'INP (p75)'], ['cls', 'CLS (p75)']];
    var labRows = labNames.filter(function (n) { return obj(lab[n[0]]).display; });
    var fieldRows = fieldNames.filter(function (n) { return num(obj(field[n[0]]).p75) != null; });
    var opps = arr(ps.opportunities);
    return [
      head('Lighthouse', 'measured', 'Speed & experience', 'Google PageSpeed Insights, ' + (ps.strategy || 'mobile') + ' test of the home page.', null, 'Measured by Google'),
      card(h('div', { 'class': 'lh' }, names.map(function (n) {
        return h('div', null, [ring(sc[n[0]], 84, 7, { label: n[1] + ' ' + (num(sc[n[0]]) == null ? 'not measured' : sc[n[0]]) }), h('div', { 'class': 'lbl', text: n[1] })]);
      }))),
      labRows.length ? h('div', { style: 'margin-top:16px' }, [h('div', { 'class': 'sub-h' }, h('h4', { text: 'Lab metrics' })),
        h('div', { 'class': 'metrics' }, labRows.map(function (n) { return h('div', { 'class': 'metric' }, [h('span', { text: n[1] }), h('b', { text: lab[n[0]].display })]); }))]) : null,
      fieldRows.length ? h('div', { style: 'margin-top:16px' }, [h('div', { 'class': 'sub-h' }, h('h4', { text: 'Real visitors (Chrome UX report)' })),
        h('div', { 'class': 'metrics' }, fieldRows.map(function (n) {
          var f = field[n[0]]; var v = n[0] === 'cls' ? (f.p75 / 100).toFixed(2) : n[0] === 'lcp' ? (f.p75 / 1000).toFixed(1) + ' s' : f.p75 + ' ms';
          return h('div', { 'class': 'metric' }, [h('span', { text: n[1] }), h('b', { text: v }), f.rating ? h('span', { text: cap(f.rating.toLowerCase().replace(/_/g, ' ')) }) : null]);
        }))]) : null,
      opps.length ? gap(card([h('div', { 'class': 'sub-h' }, h('h4', { text: 'Biggest opportunities' })),
        h('ul', { 'class': 'opps' }, opps.map(function (o) {
          return h('li', null, [h('span', { text: o.title }), h('b', { 'class': 'num-cell', text: num(o.saving_ms) == null ? '' : '~' + (o.saving_ms / 1000).toFixed(1) + ' s' })]);
        }))])) : null
    ];
  }

  function secPages() {
    var pages = arr(M.pages);
    if (!pages.length) return null;
    return [
      head('Crawl', 'measured', 'Pages read (' + pages.length + ')', 'What we read on each page. Connecting the site audits every page.'),
      h('div', { 'class': 'table-wrap' }, h('table', null, [
        h('thead', null, h('tr', null, [h('th', { text: 'Page' }), h('th', { text: 'Title' }), h('th', { 'class': 'num-cell', text: 'Words' }), h('th', { 'class': 'num-cell', text: 'Response' }), h('th', { 'class': 'num-cell', text: 'Images no alt' })])),
        h('tbody', null, pages.map(function (p) {
          var slow = num(p.response_ms) != null && p.response_ms > 1500;
          return h('tr', null, [
            h('td', { style: 'font-family:var(--mono);font-size:12.5px;overflow-wrap:anywhere;min-width:140px', text: pathOf(p.url) || '/' }),
            h('td', { style: 'min-width:220px' }, [p.title || h('span', { 'class': 'muted', text: 'No title' }), p.noindex ? [' ', h('span', { 'class': 'pill p-warn', text: 'noindex' })] : null]),
            h('td', { 'class': 'num-cell', text: num(p.words) == null ? '–' : p.words.toLocaleString() }),
            h('td', { 'class': 'num-cell', style: slow ? 'color:var(--warn);font-weight:600' : null, text: num(p.response_ms) == null ? '–' : p.response_ms.toLocaleString() + ' ms' }),
            h('td', { 'class': 'num-cell', text: num(p.images_without_alt) == null ? '–' : String(p.images_without_alt) })
          ]);
        }))
      ]))
    ];
  }

  function secMessaging() {
    var m = obj(AI.messaging);
    if (!m.suggested_headline && !m.clarity_verdict && !arr(m.trust_gaps).length) return null;
    var cs = num(m.clarity_score);
    var rewrite = [m.suggested_headline, m.suggested_subheadline, m.suggested_cta ? 'Button: ' + m.suggested_cta : ''].filter(Boolean).join('\n');
    return [
      head('Home page', 'ai', 'Your home page message', 'Can a visitor tell in five seconds what you do, for whom, and what to do next?'),
      (cs != null || m.clarity_verdict) ? card([
        cs != null ? h('div', { 'class': 'progress', style: 'margin-bottom:8px' }, [h('b', { style: 'color:var(--text)', text: 'Clarity ' + cs + '/10' }),
          h('div', { 'class': 'meter', role: 'img', 'aria-label': 'Clarity ' + cs + ' out of 10' }, h('i', { style: 'width:' + Math.max(0, Math.min(10, cs)) * 10 + '%' }))]) : null,
        m.clarity_verdict ? h('p', { 'class': 'muted', text: m.clarity_verdict }) : null
      ]) : null,
      m.suggested_headline ? h('div', { 'class': 'compare', style: 'margin-top:16px' }, [
        card([h('div', { 'class': 'eyebrow' }, ['Today']), h('div', { 'class': 'headline', text: m.current_headline || 'No clear headline found' })]),
        card([
          h('div', { 'class': 'copy-row' }, [h('div', { 'class': 'eyebrow' }, ['Suggested rewrite']), copyBtn(rewrite, 'Copy', 'suggested headline, subheadline and button')]),
          h('div', { 'class': 'headline', text: m.suggested_headline }),
          m.suggested_subheadline ? h('p', { 'class': 'sub', text: m.suggested_subheadline }) : null,
          m.suggested_cta ? h('span', { 'class': 'cta-demo', text: m.suggested_cta }) : null
        ])
      ]) : null,
      arr(m.trust_gaps).length ? gap(card([h('h3', { style: 'margin-bottom:8px', text: 'Trust gaps to close' }), bullets(m.trust_gaps)])) : null
    ];
  }

  function secKeywords() {
    var kws = arr(AI.keywords), comps = arr(AI.competitors);
    if (!kws.length && !comps.length) return null;
    var out = [head('Search', 'ai', 'Searches you should win', 'Searches that match what you sell. Search volumes are not estimated here; check them in Google Keyword Planner or Search Console.')];
    if (kws.length) {
      var sort = { key: null, dir: 1 };
      var filterIn = h('input', { type: 'search', 'class': 'input', placeholder: 'Filter keywords…', 'aria-label': 'Filter keywords' });
      var intents = kws.map(function (k) { return k.intent; }).filter(function (v, i, a) { return v && a.indexOf(v) === i; }).sort();
      var intentSel = h('select', { 'class': 'input', 'aria-label': 'Filter by intent' }, [h('option', { value: '', text: 'All intents' })].concat(intents.map(function (v) { return h('option', { value: v, text: cap(v) }); })));
      var tbody = h('tbody');
      var shownCount = h('span', { 'class': 'result-count', 'aria-live': 'polite' });
      var rows = kws.map(function (k) {
        return { k: k, el: h('tr', null, [
          h('td', { 'class': 'kw' }, h('div', { 'class': 'copy-row' }, [h('span', { text: k.keyword }), copyBtn(k.keyword, '', 'keyword ' + k.keyword)])),
          h('td', null, k.intent ? h('span', { 'class': 'tag', text: cap(k.intent) }) : null),
          h('td', { 'class': 'muted', style: 'min-width:240px', text: k.why }),
          h('td', { style: 'font-size:13px;min-width:140px;overflow-wrap:anywhere', 'data-label': 'Page', text: k.target_page })
        ]) };
      });
      var visible = rows;
      function th(label, key) {
        var cell = h('th', { scope: 'col', 'aria-sort': 'none' });
        var b = h('button', { type: 'button' }, [label, h('span', { 'class': 'arrow', 'aria-hidden': 'true', text: '↕' })]);
        b.addEventListener('click', function () {
          sort.dir = sort.key === key ? -sort.dir : 1; sort.key = key; render();
        });
        cell.appendChild(b); cell._key = key; return cell;
      }
      var heads = [th('Keyword', 'keyword'), th('Intent', 'intent')];
      function render() {
        var q = filterIn.value.trim().toLowerCase(), it = intentSel.value;
        visible = rows.filter(function (r) {
          return (!it || r.k.intent === it) && (!q || (r.k.keyword + ' ' + r.k.why + ' ' + r.k.target_page).toLowerCase().indexOf(q) >= 0);
        });
        if (sort.key) visible = visible.slice().sort(function (a, b) {
          return sort.dir * (str(a.k[sort.key]).localeCompare(str(b.k[sort.key])) || a.k.keyword.localeCompare(b.k.keyword));
        });
        heads.forEach(function (c) {
          var on = c._key === sort.key;
          c.setAttribute('aria-sort', on ? (sort.dir > 0 ? 'ascending' : 'descending') : 'none');
          c.querySelector('.arrow').textContent = on ? (sort.dir > 0 ? '↑' : '↓') : '↕';
        });
        tbody.textContent = '';
        visible.forEach(function (r) { tbody.appendChild(r.el); });
        if (!visible.length) tbody.appendChild(h('tr', null, h('td', { colspan: 4, 'class': 'muted', text: 'No keywords match.' })));
        shownCount.textContent = visible.length + ' of ' + rows.length + ' searches';
      }
      filterIn.addEventListener('input', render); intentSel.addEventListener('change', render);
      out.push(h('div', { 'class': 'tool-row no-print', style: 'margin-bottom:12px' }, [
        h('div', { 'class': 'search' }, [icon('search'), filterIn]), intentSel,
        copyBtn(function () { return visible.map(function (r) { return r.k.keyword; }).join('\n'); }, 'Copy shown', 'the keywords shown'), shownCount
      ]));
      out.push(h('div', { 'class': 'table-wrap' }, h('table', { 'class': 'stack-table' }, [
        h('thead', null, h('tr', null, heads.concat([h('th', { scope: 'col', text: 'Why it fits' }), h('th', { scope: 'col', text: 'Target page' })]))), tbody])));
      render();
    }
    if (comps.length) {
      out.push(h('div', { style: 'margin-top:24px' }, [
        h('div', { 'class': 'eyebrow' }, ['Competitors to research', badge('ai', 'Suggested, not measured')]),
        h('p', { 'class': 'muted small', style: 'margin-bottom:12px', text: 'Likely competitors named by the AI from your site’s text. Nothing about them was measured.' }),
        h('div', { 'class': 'grid g3' }, comps.map(function (c) { return card([h('h3', { text: c.name }), c.why ? h('p', { 'class': 'muted small', style: 'margin-top:6px', text: c.why }) : null]); }))
      ]));
    }
    return out;
  }

  function secTitles() {
    var fixes = arr(AI.page_fixes).filter(function (f) { return f.suggested_title || f.suggested_description; });
    if (!fixes.length) return null;
    return [
      head('Search snippets', 'ai', 'Ready-to-paste titles and descriptions', 'How each page could appear in Google. Aim for titles under about 60 characters and descriptions under about 160.'),
      h('div', { 'class': 'grid g2' }, fixes.map(function (f) {
        var u = f.url || D.website || D.site;
        var dom = domainOf(u) || D.site, path = pathOf(u);
        return h('div', { 'class': 'serp' }, [
          h('div', { 'class': 'site' }, [h('span', { 'class': 'fav', 'aria-hidden': 'true', text: (dom.replace(/^www\./, '').charAt(0) || '·').toUpperCase() }),
            h('div', { style: 'min-width:0' }, [h('div', { 'class': 'dom', text: dom }), h('div', { 'class': 'path', text: 'https://' + dom + (path ? ' › ' + path.replace(/^\//, '').split('/').join(' › ') : '') })])]),
          f.suggested_title ? h('div', { 'class': 'st', text: f.suggested_title }) : null,
          f.suggested_description ? h('div', { 'class': 'sd', text: f.suggested_description }) : null,
          f.note ? h('p', { 'class': 'small muted', style: 'margin-top:10px;font-family:var(--font)', text: f.note }) : null,
          h('div', { 'class': 'fix-foot', style: 'font-family:var(--font)' }, [
            f.suggested_title ? h('span', { 'class': 'pair' }, [copyBtn(f.suggested_title, 'Title', 'title for ' + (path || dom)), counter(f.suggested_title, 60, true)]) : null,
            f.suggested_description ? h('span', { 'class': 'pair' }, [copyBtn(f.suggested_description, 'Description', 'description for ' + (path || dom)), counter(f.suggested_description, 160, true)]) : null
          ])
        ]);
      }))
    ];
  }

  function secContent() {
    var plan = arr(AI.content_plan);
    if (!plan.length) return null;
    return [
      head('Content', 'ai', 'Content plan', 'Articles and pages to write next, each aimed at a search your customers make.'),
      h('div', { 'class': 'grid g3' }, plan.map(function (a) {
        var outline = arr(a.outline);
        var kids = [
          h('div', { 'class': 'tags', style: 'display:flex;gap:6px;flex-wrap:wrap' }, [a.type ? h('span', { 'class': 'tag hi', text: cap(a.type) }) : null,
            a.target_keyword ? h('span', { 'class': 'tag', text: 'Target: ' + a.target_keyword }) : null]),
          h('h3', { text: a.title }),
          a.angle ? h('p', { 'class': 'angle', text: a.angle }) : null
        ];
        if (outline.length) {
          var btn = h('button', { type: 'button', 'class': 'linkish' }, [h('span', { 'class': 'lbl', text: 'Show outline (' + outline.length + ')' }), icon('chev', 'chev')]);
          var ol = h('ol', null, outline.map(function (x) { return h('li', { text: x }); }));
          expander(btn, ol, 'Show outline (' + outline.length + ')', 'Hide outline');
          kids.push(btn, ol);
        }
        return card(kids, 'plan');
      }))
    ];
  }

  function combos(n, most) {
    var out = [];
    if (n < 3) { var all = []; for (var z = 0; z < n; z++) all.push(z); return [all]; }
    for (var i = 0; i < n; i++) for (var j = i + 1; j < n; j++) for (var k = j + 1; k < n; k++) { out.push([i, j, k]); if (out.length >= most) return out; }
    return out;
  }
  function secAds() {
    var camp = obj(AI.campaign), groups = arr(camp.ad_groups), angles = arr(AI.angles), neg = arr(camp.negative_keywords);
    if (!groups.length && !angles.length) return null;
    var out = [head('Paid search', 'ai', camp.campaign_name || 'Google search campaign',
      [camp.goal ? 'Goal: ' + camp.goal + '.' : '', camp.budget_note].filter(Boolean).join(' ') || 'Responsive search ads ready to paste into Google Ads.')];
    var siteDom = domainOf(D.website) || D.site;
    var list = h('div', { 'class': 'stack' });
    groups.forEach(function (g) {
      var hs = arr(g.headlines), ds = arr(g.descriptions), kw = arr(g.keywords);
      var dom = domainOf(g.landing_page) || siteDom, path = pathOf(g.landing_page);
      var cs = combos(hs.length, 60), idx = 0;
      var titleEl = h('div', { 'class': 'st' }), descEl = h('div', { 'class': 'sd' });
      var posEl = h('span', { 'aria-live': 'polite' });
      function show() {
        titleEl.textContent = cs[idx].map(function (i) { return hs[i]; }).join(' | ') || g.name;
        var d = ds.length ? (ds.length > 1 ? [ds[idx % ds.length], ds[(idx + 1) % ds.length]] : [ds[0]]) : [];
        descEl.textContent = d.join(' ');
        posEl.textContent = 'Combination ' + (idx + 1) + ' of ' + cs.length;
      }
      var next = h('button', { type: 'button', 'class': 'btn btn-sm' }, [icon('shuffle'), 'Next combination']);
      next.addEventListener('click', function () { idx = (idx + 1) % cs.length; show(); });
      show();
      var preview = h('div', { 'class': 'serp' }, [
        h('div', { 'class': 'sp', text: 'Sponsored' }),
        h('div', { 'class': 'site' }, [h('span', { 'class': 'fav', 'aria-hidden': 'true', text: (dom.replace(/^www\./, '').charAt(0) || '·').toUpperCase() }),
          h('div', { style: 'min-width:0' }, [h('div', { 'class': 'dom', text: dom }), h('div', { 'class': 'path', text: 'https://' + dom + (path ? ' › ' + path.replace(/^\//, '').split('/').join(' › ') : '') })])]),
        titleEl, descEl
      ]);
      function lines(items, limit, what) {
        return h('ul', { 'class': 'lines' }, items.map(function (t) {
          return h('li', { 'class': chars(t) > limit ? 'over' : '' }, [h('span', { text: t }), counter(t, limit), copyBtn(t, '', what + ': ' + t)]);
        }));
      }
      var right = h('div', { 'class': 'stack' }, [
        hs.length ? h('div', null, [h('div', { 'class': 'sub-h' }, [h('h4', { text: 'Headlines (' + hs.length + ')' }), copyBtn(hs.join('\n'), 'Copy all', 'all headlines')]), lines(hs, 30, 'headline')]) : null,
        ds.length ? h('div', null, [h('div', { 'class': 'sub-h' }, [h('h4', { text: 'Descriptions (' + ds.length + ')' }), copyBtn(ds.join('\n'), 'Copy all', 'all descriptions')]), lines(ds, 90, 'description')]) : null
      ]);
      var left = h('div', null, [
        h('div', { 'class': 'sub-h' }, h('h4', { text: 'Ad preview' })), preview,
        cs.length > 1 ? h('div', { 'class': 'rot' }, [next, posEl]) : null,
        kw.length ? h('div', { style: 'margin-top:18px' }, [h('div', { 'class': 'sub-h' }, [h('h4', { text: 'Keywords (' + kw.length + ')' }), copyBtn(kw.join('\n'), 'Copy all', 'all keywords for ' + g.name)]),
          h('div', { 'class': 'kchips' }, kw.map(function (k) { return h('span', { 'class': 'kchip', text: k }); }))]) : null
      ]);
      list.appendChild(card([
        h('div', { 'class': 'group-h' }, [h('h3', { text: 'Ad group: ' + (g.name || 'Untitled') }), g.landing_page ? h('span', { 'class': 'small muted', style: 'overflow-wrap:anywhere', text: 'Landing page: ' + hostOf(g.landing_page) }) : null]),
        h('div', { 'class': 'group-b' }, [left, right])
      ], 'group'));
    });
    out.push(list);
    if (neg.length) out.push(gap(card([
      h('div', { 'class': 'sub-h' }, [h('h4', { text: 'Negative keywords (' + neg.length + ')' }), copyBtn(neg.join('\n'), 'Copy all', 'all negative keywords')]),
      h('p', { 'class': 'muted small', style: 'margin-bottom:10px', text: 'Searches to exclude so the budget is not spent on the wrong people.' }),
      h('div', { 'class': 'kchips' }, neg.map(function (k) { return h('span', { 'class': 'kchip', text: k }); }))
    ])));
    if (angles.length) out.push(h('div', null, [
      h('h3', { style: 'margin:24px 0 12px;font-size:16px', text: 'Ad creative angles' }),
      h('div', { 'class': 'grid g2' }, angles.map(function (a) {
        return card([h('div', { 'class': 'copy-row' }, [h('h3', { text: a.angle }), a.hook ? copyBtn(a.hook, 'Hook', 'hook for ' + a.angle) : null]),
          a.hook ? h('p', { style: 'margin-top:8px;font-weight:500', text: '“' + a.hook + '”' }) : null,
          a.audience ? h('p', { 'class': 'muted small', style: 'margin-top:6px', text: 'For: ' + a.audience }) : null]);
      }))
    ]));
    return out;
  }

  function secSocial() {
    var posts = arr(AI.social_posts), mails = arr(AI.emails);
    if (!posts.length && !mails.length) return null;
    var out = [head('Social & email', 'ai', 'Posts and emails, ready to send', 'Drafts written in your voice from your site. Review before publishing.')];
    if (posts.length) out.push(h('div', { 'class': 'grid g2' }, posts.map(function (p) {
      var tags = arr(p.hashtags).map(function (t) { return '#' + t.replace(/^#/, '').replace(/\s+/g, ''); });
      var full = p.text + (tags.length ? '\n\n' + tags.join(' ') : '');
      return card([
        h('div', { 'class': 'post-h' }, [h('span', { 'class': 'post-net' }, [h('span', { 'class': 'fav', 'aria-hidden': 'true', text: (p.network || '·').charAt(0).toUpperCase() }), p.network || 'Post']),
          copyBtn(full, 'Copy post', (p.network || '') + ' post')]),
        h('div', { 'class': 'post-text', text: p.text }),
        tags.length ? h('div', { 'class': 'hash', text: tags.join(' ') }) : null,
        p.visual_idea ? h('p', { 'class': 'muted small' }, [h('b', { text: 'Visual: ' }), p.visual_idea]) : null,
        h('span', { 'class': 'count', text: chars(full) + ' characters' })
      ], 'post');
    })));
    if (mails.length) out.push(h('div', null, [
      h('h3', { style: 'margin:24px 0 12px;font-size:16px', text: 'Email campaigns' }),
      h('div', { 'class': 'grid g3' }, mails.map(function (m) {
        var outline = arr(m.body_outline);
        var kids = [
          m.name ? h('span', { 'class': 'tag hi', text: cap(m.name) }) : null,
          h('div', { 'class': 'copy-row', style: 'margin-top:10px' }, [h('div', null, [h('div', { 'class': 'muted small', text: 'Subject' }), h('div', { 'class': 'mail-subj', text: m.subject || '—' })]),
            m.subject ? copyBtn(m.subject, 'Subject', 'email subject') : null]),
          m.preview_text ? h('p', { 'class': 'mail-prev', style: 'margin-top:6px', text: m.preview_text }) : null
        ];
        if (outline.length) {
          var btn = h('button', { type: 'button', 'class': 'linkish' }, [h('span', { 'class': 'lbl', text: 'Show outline (' + outline.length + ')' }), icon('chev', 'chev')]);
          var ol = h('ol', { 'class': 'small', style: 'margin-top:8px' }, outline.map(function (x) { return h('li', { text: x }); }));
          expander(btn, ol, 'Show outline (' + outline.length + ')', 'Hide outline');
          kids.push(btn, ol);
        }
        if (m.cta) kids.push(h('div', { style: 'margin-top:12px' }, h('span', { 'class': 'cta-demo', style: 'display:inline-block;font-size:13px', text: m.cta })));
        return card(kids);
      }))
    ]));
    return out;
  }

  function secFaq() {
    var faq = arr(AI.faq);
    if (!faq.length) return null;
    return [
      head('Answers', 'ai', 'Questions to answer on your site', 'Clear answers help customers and get quoted by Google and AI assistants. Add them with FAQ schema.'),
      h('div', { 'class': 'faq' }, faq.map(function (f) {
        var btn = h('button', { type: 'button', 'class': 'faq-q' }, [h('span', { text: f.q }), icon('chev', 'chev')]);
        var ans = h('div', { 'class': 'faq-a' }, [h('p', { text: f.a || 'No answer suggested.' }), f.a ? copyBtn(f.a, 'Answer', 'answer to ' + f.q) : null]);
        expander(btn, ans);
        return h('div', { 'class': 'faq-item' }, [h('h3', { style: 'font-size:inherit' }, btn), ans]);
      }))
    ];
  }

  function secRoadmap() {
    var phases = arr(AI.roadmap).filter(function (p) { return arr(p.items).length; });
    if (!phases.length) return null;
    var key = 'amr-plan:' + D.site + ':' + (D.generated_at || '');
    var ticks = {};
    try { ticks = JSON.parse(store.get(key) || '{}') || {}; } catch (e) { ticks = {}; }
    if (typeof ticks !== 'object') ticks = {};
    var total = 0; phases.forEach(function (p) { total += p.items.length; });
    var overall = h('span'), meterI = h('i');
    var tabs = [], panels = [], cts = [];
    function refresh() {
      var done = 0;
      phases.forEach(function (p, pi) {
        var d = p.items.filter(function (_, i) { return ticks[p.key + ':' + i]; }).length;
        done += d; cts[pi].textContent = d + '/' + p.items.length;
      });
      overall.textContent = done + ' of ' + total + ' done';
      meterI.style.width = (total ? done / total * 100 : 0) + '%';
    }
    function select(i, focus) {
      tabs.forEach(function (t, j) { t.setAttribute('aria-selected', i === j ? 'true' : 'false'); t.tabIndex = i === j ? 0 : -1; panels[j].hidden = i !== j; });
      if (focus) tabs[i].focus();
    }
    phases.forEach(function (p, pi) {
      var tid = id('tab'), pid = id('tabpanel');
      var ct = h('span', { 'class': 'ct' }); cts.push(ct);
      var t = h('button', { type: 'button', role: 'tab', id: tid, 'aria-controls': pid, 'class': 'tab' }, [p.label, ct]);
      t.addEventListener('click', function () { select(pi); });
      t.addEventListener('keydown', function (e) {
        var n = tabs.length, j = null;
        if (e.key === 'ArrowRight') j = (pi + 1) % n; else if (e.key === 'ArrowLeft') j = (pi - 1 + n) % n;
        else if (e.key === 'Home') j = 0; else if (e.key === 'End') j = n - 1;
        if (j != null) { e.preventDefault(); select(j, true); }
      });
      tabs.push(t);
      panels.push(h('div', { role: 'tabpanel', id: pid, 'aria-labelledby': tid, tabindex: '0' }, [
        h('h3', { 'class': 'print-only', style: 'margin:14px 0 8px', text: p.label }),
        h('ul', { 'class': 'todo' }, p.items.map(function (item, i) {
          var k = p.key + ':' + i;
          var cb = h('input', { type: 'checkbox' });
          cb.checked = !!ticks[k];
          cb.addEventListener('change', function () {
            if (cb.checked) ticks[k] = 1; else delete ticks[k];
            store.set(key, JSON.stringify(ticks)); refresh();
          });
          return h('li', null, h('label', null, [cb, h('span', { text: item })]));
        }))
      ]));
    });
    var reset = h('button', { type: 'button', 'class': 'btn btn-sm no-print', text: 'Clear ticks' });
    reset.addEventListener('click', function () {
      ticks = {}; store.set(key, '{}');
      panels.forEach(function (p) { Array.prototype.forEach.call(p.querySelectorAll('input'), function (c) { c.checked = false; }); });
      refresh();
    });
    select(0); refresh();
    return [
      head('Roadmap', 'ai', '90-day plan', 'Tick items off as you go. Ticks are saved in this browser only.'),
      h('div', { 'class': 'progress' }, [h('div', { 'class': 'meter', 'aria-hidden': 'true' }, meterI), overall, reset]),
      h('div', { role: 'tablist', 'aria-label': '90-day plan phases', 'class': 'tabs' }, tabs),
      panels
    ];
  }

  function secAutomation() {
    var auto = arr(D.automation);
    if (!auto.length) return null;
    return [
      head(D.app, null, 'What ' + D.app + ' can take care of', 'Agents that work on the issues above once your site is connected.'),
      h('div', { 'class': 'grid g3' }, auto.map(function (a) {
        return card([h('h3', { text: a.name }), a.what_it_does ? h('p', { 'class': 'muted small', style: 'margin:6px 0 10px', text: a.what_it_does }) : null,
          arr(a.addresses).length ? h('div', { 'class': 'kchips' }, a.addresses.map(function (x) { return h('span', { 'class': 'tag', text: x }); })) : null]);
      }))
    ];
  }

  function secContext() {
    var cx = X.context;
    if (!cx || typeof cx !== 'object') return null;
    var conns = arr(cx.connectors), agents = arr(cx.agents);
    function healthCls(v) { v = str(v).toLowerCase(); return /ok|good|healthy|connected/.test(v) ? 'h-good' : /error|fail|bad|expired/.test(v) ? 'h-bad' : 'h-mid'; }
    var AST = { running: 'p-pass', paused: 'p-info', error: 'p-fail' };
    var info = [];
    if (cx.llm) info.push(['AI model', cx.llm]);
    if (cx.pagespeed) info.push(['PageSpeed', cx.pagespeed]);
    return [
      head('Workspace', 'measured', 'Connected at the time', 'What this workspace had connected and switched on when the analysis ran.', null, 'Recorded'),
      info.length ? card(h('dl', { 'class': 'kv' }, info.map(function (p) { return [h('dt', { text: p[0] }), h('dd', { text: p[1] })]; }))) : null,
      h('div', { 'class': 'grid g2', style: 'margin-top:16px' }, [
        card([h('h3', { style: 'margin-bottom:10px', text: 'Connectors (' + conns.length + ')' }),
          conns.length ? h('ul', { 'class': 'ilist' }, conns.map(function (c) {
            return h('li', { style: 'justify-content:space-between' }, [
              h('div', null, [h('b', { text: c.name }), c.category ? h('div', { 'class': 'muted small', text: c.category.length <= 3 ? c.category.toUpperCase() : cap(c.category) }) : null,
                c.last_error ? h('div', { 'class': 'small', style: 'color:var(--bad)', text: c.last_error }) : null]),
              c.health ? h('span', { 'class': 'health ' + healthCls(c.health), text: cap(c.health) }) : null
            ]);
          })) : h('p', { 'class': 'muted small', text: 'Nothing was connected.' })]),
        card([h('h3', { style: 'margin-bottom:10px', text: 'Agents (' + agents.length + ')' }),
          agents.length ? h('ul', { 'class': 'ilist' }, agents.map(function (a) {
            return h('li', { style: 'justify-content:space-between' }, [
              h('div', null, [h('b', { text: a.name }), h('div', { 'class': 'muted small', text: (a.configured ? 'Configured' : 'Not configured') + (a.last_run ? ' · last run ' + a.last_run : '') })]),
              a.status ? h('span', { 'class': 'pill ' + (AST[a.status] || 'p-info'), text: cap(a.status) }) : null
            ]);
          })) : h('p', { 'class': 'muted small', text: 'No agents were set up.' })])
      ])
    ];
  }

  // ── Page ─────────────────────────────────────────────────────────────────
  var SECTIONS = [
    ['overview', 'Overview', secOverview], ['scores', 'Scores', secScores], ['changes', 'What changed', secChanges],
    ['priorities', 'Priorities', secPriorities], ['findings', 'Findings', secFindings], ['speed', 'Speed', secSpeed],
    ['messaging', 'Messaging', secMessaging], ['keywords', 'Keywords', secKeywords], ['snippets', 'Titles', secTitles],
    ['content', 'Content plan', secContent], ['ads', 'Search ads', secAds], ['social', 'Social & email', secSocial],
    ['faq', 'FAQ', secFaq], ['roadmap', '90-day plan', secRoadmap], ['pages', 'Pages read', secPages],
    ['automation', 'Automation', secAutomation], ['context', 'Connected', secContext]
  ];

  function setTheme(t, save) {
    root.setAttribute('data-theme', t);
    if (save) store.set('amr-theme', t);
    if (themeBtn) {
      themeBtn.textContent = '';
      add(themeBtn, [icon(t === 'dark' ? 'sun' : 'moon'), h('span', { 'class': 'txt', text: t === 'dark' ? 'Light' : 'Dark' })]);
      themeBtn.setAttribute('aria-label', 'Switch to ' + (t === 'dark' ? 'light' : 'dark') + ' theme');
    }
  }
  var themeBtn = null;

  function build() {
    var stored = store.get('amr-theme');
    var prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    setTheme(stored === 'dark' || stored === 'light' ? stored : (prefersDark ? 'dark' : 'light'));

    var main = h('main', { id: 'main' });
    var nav = h('nav', { 'class': 'nav', 'aria-label': 'Report sections' });
    var built = [];
    SECTIONS.forEach(function (sd) {
      var content;
      try { content = sd[2](); } catch (e) { content = null; if (window.console) console.error('Section ' + sd[0] + ' failed', e); }
      if (!content) return;
      var sec = h('section', { 'class': 'sec', id: sd[0], 'aria-label': sd[1] }, content);
      if (sd[0] === 'findings' && findings) findings.bind(sec);
      main.appendChild(sec);
      var link = h('a', { href: '#' + sd[0], text: sd[1] });
      nav.appendChild(link);
      built.push({ sec: sec, link: link });
    });
    var notes = arr(D.notes);
    main.appendChild(h('footer', { 'class': 'foot' }, [
      notes.length ? h('ul', null, notes.map(function (n) { return h('li', { text: n }); })) : null,
      h('p', { style: 'margin-top:12px' }, [
        h('span', { 'class': 'badge b-measured', text: 'Measured' }), ' read from the site today.  ',
        h('span', { 'class': 'badge b-ai', text: 'AI suggestion' }), AI.model ? ' written by ' + AI.model + ' from the site’s own text; review before use.' : ' written from the site’s own text; review before use.'
      ]),
      h('p', { style: 'margin-top:8px', text: 'Generated by ' + D.app + (D.generated_label ? ' on ' + D.generated_label : '') + '.' })
    ]));

    themeBtn = h('button', { type: 'button', 'class': 'btn btn-icon' });
    themeBtn.addEventListener('click', function () { setTheme(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark', true); });
    setTheme(root.getAttribute('data-theme'));
    var printBtn = h('button', { type: 'button', 'class': 'btn', 'aria-label': 'Print or save as PDF' }, [icon('print'), h('span', { 'class': 'txt', text: 'Print' })]);
    printBtn.addEventListener('click', function () { window.print(); });
    var ov = obj(D.overall);
    var top = h('header', { 'class': 'top' }, [
      h('div', { 'class': 'top-row' }, [
        h('div', { 'class': 'brand' }, [h('span', { 'class': 'logo', 'aria-hidden': 'true', text: (D.app || 'A').charAt(0) }), D.app]),
        h('div', { 'class': 'top-site', text: (str(BIZ.name) && BIZ.name !== D.site ? BIZ.name + ' · ' : '') + D.site }),
        h('div', { 'class': 'top-score' }, [h('span', { 'class': 'lbl', text: 'Overall' }),
          h('span', { 'class': 'chip-score bg-' + tone(num(ov.score)), text: num(ov.score) == null ? '–' : ov.score + (ov.grade ? ' · ' + ov.grade : '') })]),
        h('div', { 'class': 'actions' }, [themeBtn, printBtn])
      ]),
      nav
    ]);
    var skip = h('a', { href: '#main', 'class': 'sr', text: 'Skip to report' });
    skip.addEventListener('focus', function () { skip.classList.remove('sr'); });
    skip.addEventListener('blur', function () { skip.classList.add('sr'); });
    app.textContent = '';
    add(app, [skip, top, main, live, toast]);
    doc.title = 'Website analysis – ' + D.site;

    // Scroll spy
    var current = null, ticking = false;
    function spy() {
      ticking = false;
      var y = (top.offsetHeight || 100) + 24, active = built[0];
      for (var i = 0; i < built.length; i++) { if (built[i].sec.getBoundingClientRect().top - y <= 0) active = built[i]; }
      if (window.innerHeight + window.scrollY >= doc.documentElement.scrollHeight - 4 && built.length) active = built[built.length - 1];
      if (!active || active === current) return;
      if (current) { current.link.classList.remove('on'); current.link.removeAttribute('aria-current'); }
      current = active; active.link.classList.add('on'); active.link.setAttribute('aria-current', 'true');
      var l = active.link, n = nav;
      if (l.offsetLeft < n.scrollLeft || l.offsetLeft + l.offsetWidth > n.scrollLeft + n.clientWidth) {
        n.scrollLeft = Math.max(0, l.offsetLeft - n.clientWidth / 2 + l.offsetWidth / 2);
      }
    }
    window.addEventListener('scroll', function () { if (!ticking) { ticking = true; requestAnimationFrame(spy); } }, { passive: true });
    window.addEventListener('resize', spy);
    // The sections did not exist when the browser looked for #hash; honour it now.
    var target = null;
    try { target = location.hash ? doc.getElementById(decodeURIComponent(location.hash.slice(1))) : null; } catch (e) { target = null; }
    if (target) target.scrollIntoView();
    spy();
  }

  function fallback() {
    // Show the server-rendered summary if the interactive page cannot be built.
    var ns = doc.querySelector('noscript');
    if (!ns || !window.DOMParser) return;
    var parsed = new DOMParser().parseFromString(ns.textContent || '', 'text/html');
    app.textContent = '';
    Array.prototype.forEach.call(parsed.body.childNodes, function (n) { app.appendChild(doc.importNode(n, true)); });
  }

  try { build(); } catch (e) { if (window.console) console.error(e); fallback(); }
})();
