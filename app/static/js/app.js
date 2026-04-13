/**
 * app.js — Gallery search and tag-filter interactions.
 */
(function () {
  'use strict';

  const grid       = document.getElementById('notebook-grid');
  const cards      = grid ? Array.from(grid.querySelectorAll('.notebook-card')) : [];
  const countEl    = document.getElementById('notebook-count');
  const noResults  = document.getElementById('no-results');
  const searchEl   = document.getElementById('search-input');
  const tagFilter  = document.getElementById('tag-filter');
  const clearBtn   = document.getElementById('btn-clear-search');

  let activeTag = '';
  let searchQ   = '';

  // ── Filter logic ───────────────────────────────────────────────────────

  function applyFilters() {
    let visible = 0;
    for (const card of cards) {
      const matchTag    = !activeTag || card.dataset.tags.includes(activeTag.toLowerCase());
      const matchSearch = !searchQ
        || card.dataset.name.includes(searchQ)
        || card.dataset.desc.includes(searchQ)
        || card.dataset.tags.includes(searchQ);

      const show = matchTag && matchSearch;
      card.classList.toggle('hidden', !show);
      if (show) visible++;
    }

    if (countEl) {
      countEl.textContent = `${visible} notebook${visible !== 1 ? 's' : ''}`;
    }
    if (noResults) {
      noResults.style.display = visible === 0 && cards.length > 0 ? '' : 'none';
    }
  }

  // ── Search ─────────────────────────────────────────────────────────────

  if (searchEl) {
    searchEl.addEventListener('input', () => {
      searchQ = searchEl.value.trim().toLowerCase();
      applyFilters();
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      searchQ = '';
      if (searchEl) searchEl.value = '';
      applyFilters();
    });
  }

  // ── Tag pills ──────────────────────────────────────────────────────────

  if (tagFilter) {
    tagFilter.addEventListener('click', (e) => {
      const btn = e.target.closest('.tag-pill');
      if (!btn) return;
      activeTag = btn.dataset.tag;
      tagFilter.querySelectorAll('.tag-pill').forEach(b => {
        b.classList.toggle('active', b === btn);
      });
      applyFilters();
    });
  }

})();
