/**
 * renderers.js — MIME output renderers and ANSI-to-HTML converter.
 * Exposed on window.Renderers.
 */
window.Renderers = (function () {
  'use strict';

  // ── Helpers ────────────────────────────────────────────────────────────

  function escHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function normaliseText(val) {
    return Array.isArray(val) ? val.join('') : String(val ?? '');
  }

  // ── ANSI parser ────────────────────────────────────────────────────────
  //  Converts ANSI escape sequences to <span class="ansi-*"> HTML.

  const FG_NORMAL = ['black','red','green','yellow','blue','magenta','cyan','white'];
  const FG_BRIGHT = ['bright-black','bright-red','bright-green','bright-yellow',
                     'bright-blue','bright-magenta','bright-cyan','bright-white'];
  const BG_NORMAL = ['bg-black','bg-red','bg-green','bg-yellow',
                     'bg-blue','bg-magenta','bg-cyan','bg-white'];

  function ansiToHtml(raw) {
    const ESC = /\x1b\[([0-9;]*)m/g;
    const parts = [];
    let lastIdx = 0;
    let openSpans = 0;
    let match;

    while ((match = ESC.exec(raw)) !== null) {
      // Push literal text before the escape
      if (match.index > lastIdx) {
        parts.push(escHtml(raw.slice(lastIdx, match.index)));
      }
      lastIdx = match.index + match[0].length;

      const codes = match[1] ? match[1].split(';').map(Number) : [0];

      for (const code of codes) {
        if (code === 0) {
          // Reset — close all open spans
          for (let i = 0; i < openSpans; i++) parts.push('</span>');
          openSpans = 0;
        } else if (code === 1) {
          parts.push('<span class="ansi-bold">'); openSpans++;
        } else if (code === 3) {
          parts.push('<span class="ansi-italic">'); openSpans++;
        } else if (code === 4) {
          parts.push('<span class="ansi-underline">'); openSpans++;
        } else if (code >= 30 && code <= 37) {
          parts.push(`<span class="ansi-fg-${FG_NORMAL[code - 30]}">`); openSpans++;
        } else if (code >= 90 && code <= 97) {
          parts.push(`<span class="ansi-fg-${FG_BRIGHT[code - 90]}">`); openSpans++;
        } else if (code >= 40 && code <= 47) {
          parts.push(`<span class="ansi-${BG_NORMAL[code - 40]}">`); openSpans++;
        }
        // Unknown codes: ignore
      }
    }

    // Remaining text after last escape
    if (lastIdx < raw.length) {
      parts.push(escHtml(raw.slice(lastIdx)));
    }
    // Close any still-open spans
    for (let i = 0; i < openSpans; i++) parts.push('</span>');

    return parts.join('');
  }

  // ── MIME renderers ─────────────────────────────────────────────────────

  const MIME_PRIORITY = [
    'text/html',
    'image/svg+xml',
    'image/png',
    'image/jpeg',
    'image/gif',
    'application/json',
    'text/plain',
  ];

  function renderMimeBundle(data) {
    for (const mime of MIME_PRIORITY) {
      if (data[mime] !== undefined && data[mime] !== null) {
        return renderMime(mime, data[mime]);
      }
    }
    return null;
  }

  function renderMime(mime, value) {
    const el = document.createElement('div');
    el.className = 'output-item';

    if (mime === 'text/html') {
      const inner = document.createElement('div');
      inner.className = 'output-html';
      inner.innerHTML = normaliseText(value);
      el.appendChild(inner);

    } else if (mime === 'image/svg+xml') {
      const inner = document.createElement('div');
      inner.className = 'output-image';
      inner.innerHTML = normaliseText(value);
      el.appendChild(inner);

    } else if (mime === 'image/png') {
      const img = document.createElement('img');
      img.className = 'output-image';
      img.src = `data:image/png;base64,${normaliseText(value).replace(/\n/g, '')}`;
      img.alt = 'output image';
      const wrap = document.createElement('div');
      wrap.className = 'output-image';
      wrap.appendChild(img);
      el.appendChild(wrap);

    } else if (mime === 'image/jpeg') {
      const img = document.createElement('img');
      img.src = `data:image/jpeg;base64,${normaliseText(value).replace(/\n/g, '')}`;
      img.alt = 'output image';
      const wrap = document.createElement('div');
      wrap.className = 'output-image';
      wrap.appendChild(img);
      el.appendChild(wrap);

    } else if (mime === 'image/gif') {
      const img = document.createElement('img');
      img.src = `data:image/gif;base64,${normaliseText(value).replace(/\n/g, '')}`;
      img.alt = 'output image';
      const wrap = document.createElement('div');
      wrap.className = 'output-image';
      wrap.appendChild(img);
      el.appendChild(wrap);

    } else if (mime === 'application/json') {
      const pre = document.createElement('pre');
      pre.className = 'output-text';
      pre.textContent = JSON.stringify(value, null, 2);
      el.appendChild(pre);

    } else {
      // text/plain fallback
      const pre = document.createElement('pre');
      pre.className = 'output-text';
      pre.innerHTML = ansiToHtml(normaliseText(value));
      el.appendChild(pre);
    }

    return el;
  }

  // ── Output-type dispatchers ────────────────────────────────────────────

  function renderOutput(output) {
    switch (output.output_type) {
      case 'stream':       return renderStream(output);
      case 'display_data': return renderDisplayData(output);
      case 'execute_result': return renderExecuteResult(output);
      case 'error':        return renderError(output);
      default:             return null;
    }
  }

  function renderStream(output) {
    const text = normaliseText(output.text);
    const pre  = document.createElement('pre');
    pre.className = output.name === 'stderr' ? 'output-stderr' : 'output-stdout';
    pre.innerHTML = ansiToHtml(text);
    const wrap = document.createElement('div');
    wrap.className = 'output-item';
    wrap.appendChild(pre);
    return wrap;
  }

  function renderDisplayData(output) {
    return renderMimeBundle(output.data || {});
  }

  function renderExecuteResult(output) {
    return renderMimeBundle(output.data || {});
  }

  function renderError(output) {
    const traceback = (output.traceback || [])
      .map(line => ansiToHtml(line))
      .join('\n');

    const wrap  = document.createElement('div');
    wrap.className = 'output-error output-item';

    const name  = document.createElement('div');
    name.className = 'error-name';
    name.textContent = `${output.ename}: ${output.evalue}`;

    const tb = document.createElement('pre');
    tb.className = 'error-traceback';
    tb.innerHTML = traceback;

    wrap.appendChild(name);
    wrap.appendChild(tb);
    return wrap;
  }

  // ── Public API ─────────────────────────────────────────────────────────

  return {
    renderOutput,
    renderStream,
    renderDisplayData,
    renderExecuteResult,
    renderError,
    renderMimeBundle,
    ansiToHtml,
    escHtml,
  };
})();
