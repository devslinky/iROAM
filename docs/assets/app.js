// iROAM doc-site behaviour: sidebar toggle, copy buttons, lunr search.

(function () {
  'use strict';

  // ---- sidebar toggle ----
  var toggle = document.querySelector('.nav-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      document.body.classList.toggle('nav-open');
    });
  }
  document.addEventListener('click', function (e) {
    if (!document.body.classList.contains('nav-open')) return;
    if (e.target.closest('.sidebar') || e.target.closest('.nav-toggle')) return;
    document.body.classList.remove('nav-open');
  });

  // ---- copy-to-clipboard on code blocks ----
  document.querySelectorAll('pre').forEach(function (pre) {
    if (pre.classList.contains('mermaid')) return;
    var code = pre.querySelector('code');
    if (!code) return;
    var btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.type = 'button';
    btn.textContent = 'Copy';
    btn.addEventListener('click', function () {
      var text = code.innerText;
      var done = function () {
        btn.textContent = 'Copied';
        btn.classList.add('copied');
        setTimeout(function () { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1400);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { fallback(text); done(); });
      } else {
        fallback(text); done();
      }
    });
    pre.appendChild(btn);
  });

  function fallback(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
  }

  // ---- search ----
  var input = document.getElementById('search-input');
  var results = document.getElementById('search-results');
  if (!input || !results) return;

  var idx = null;
  var docs = null;
  var docsById = {};
  var loaded = false;
  var loading = false;
  var basePath = (function () {
    // search-index.json is referenced from window.SEARCH_INDEX_URL
    var u = window.SEARCH_INDEX_URL || 'search-index.json';
    // Path prefix to apply to href fields ('' for root pages, '../' for modules/ pages)
    if (u.indexOf('../') === 0) return '../';
    return '';
  })();

  function loadIndex() {
    if (loaded || loading) return Promise.resolve();
    loading = true;
    return fetch(window.SEARCH_INDEX_URL).then(function (r) { return r.json(); }).then(function (data) {
      docs = data;
      data.forEach(function (d) { docsById[d.id] = d; });
      idx = lunr(function () {
        this.ref('id');
        this.field('title', { boost: 8 });
        this.field('body');
        this.field('kind');
        this.metadataWhitelist = ['position'];
        var b = this;
        data.forEach(function (d) { b.add(d); });
      });
      loaded = true;
      loading = false;
    }).catch(function (e) {
      loading = false;
      console.error('search index load failed', e);
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function runQuery(q) {
    if (!idx || !q.trim()) { results.hidden = true; results.innerHTML = ''; return; }
    var terms = q.trim().split(/\s+/);
    // Build a fuzzy/expansion query so 'bunch' matches 'bunching'
    var lunrQ = terms.map(function (t) {
      var clean = t.replace(/[^\w*]/g, '');
      if (!clean) return '';
      return clean + ' ' + clean + '* ' + clean + '~1';
    }).join(' ');
    var hits;
    try {
      hits = idx.search(lunrQ);
    } catch (e) {
      try { hits = idx.search(q); } catch (e2) { hits = []; }
    }
    results.innerHTML = '';
    if (!hits.length) {
      results.innerHTML = '<div class="empty">No matches.</div>';
      results.hidden = false;
      return;
    }
    hits.slice(0, 20).forEach(function (h) {
      var d = docsById[h.ref];
      if (!d) return;
      var a = document.createElement('a');
      a.className = 'hit';
      a.href = basePath + d.href;
      a.innerHTML =
        '<span class="hit-title">' + escapeHtml(d.title) + '</span>' +
        '<span class="hit-meta"> · ' + escapeHtml(d.page) + ' · ' + escapeHtml(d.kind) + '</span>';
      results.appendChild(a);
    });
    results.hidden = false;
  }

  var debounceT = null;
  input.addEventListener('focus', loadIndex);
  input.addEventListener('input', function () {
    loadIndex().then(function () {
      clearTimeout(debounceT);
      debounceT = setTimeout(function () { runQuery(input.value); }, 80);
    });
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { input.value = ''; results.hidden = true; input.blur(); }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      var first = results.querySelector('.hit');
      if (first) first.focus();
    }
  });
  document.addEventListener('click', function (e) {
    if (!e.target.closest('.search-box')) results.hidden = true;
  });
  results.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { results.hidden = true; input.focus(); }
  });
})();
