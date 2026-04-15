/**
 * notebook.js — Core notebook application.
 *
 * Fetches .ipynb, renders cells with CodeMirror editors and markdown,
 * manages session lifecycle, and drives kernel execution.
 */
(function () {
  'use strict';

  const CFG = window.NB_CONFIG;

  // ── DOM helpers ──────────────────────────────────────────────────────────

  function qs(sel, root) { return (root || document).querySelector(sel); }

  function setStep(id, state /* done | active | error */) {
    const el = document.getElementById(id);
    if (el) el.className = `step ${state}`;
  }

  function setLoadingSub(msg) {
    const el = document.getElementById('loading-sub');
    if (el) el.textContent = msg;
  }

  function hideOverlay() {
    const overlay = document.getElementById('loading-overlay');
    if (overlay) {
      overlay.style.opacity = '0';
      overlay.style.transition = 'opacity 300ms ease';
      setTimeout(() => overlay.classList.add('hidden'), 300);
    }
  }

  function setKernelStatus(state, label) {
    const dot = document.getElementById('status-dot');
    const lbl = document.getElementById('status-label');
    if (dot) dot.dataset.state = state;
    if (lbl) lbl.textContent = label;
  }

  // ── Notebook state ────────────────────────────────────────────────────────

  const state = {
    nb:         null,   // raw .ipynb object
    cells:      [],     // { cellData, el, editorInstance, outputEl, promptEl }
    session:    null,   // { session_id, status }
    kernel:     null,   // KernelClient
    running:    false,
    execQueue:  [],     // array of cell indices waiting to run
  };

  // ── Boot ──────────────────────────────────────────────────────────────────

  async function init() {
    const container = document.getElementById('cells-container');
    if (!container) return;

    if (CFG.previewMode) {
      await loadAndRenderPreview(container);
      return;
    }

    try {
      setStep('step-notebook', 'active');
      setLoadingSub('Fetching notebook…');
      const nb = await fetchNotebook();
      state.nb = nb;
      renderAllCells(nb, container);
      setStep('step-notebook', 'done');

      // Resume an existing session if one was in progress (e.g. page reload after timeout)
      const savedSessionId = sessionStorage.getItem(`session:${CFG.notebookId}`);
      let session = null;
      if (savedSessionId) {
        const resp = await fetch(`/api/sessions/${savedSessionId}`);
        if (resp.ok) {
          const s = await resp.json();
          if (s.status !== 'error' && s.status !== 'terminating') {
            session = s;
            setStep('step-pod', 'done');
            setLoadingSub('Resuming existing session…');
          } else {
            sessionStorage.removeItem(`session:${CFG.notebookId}`);
          }
        } else {
          // Session no longer exists (app restarted, pod reaped, etc.) — start fresh
          sessionStorage.removeItem(`session:${CFG.notebookId}`);
        }
      }

      if (!session) {
        setStep('step-pod', 'active');
        setLoadingSub('Creating session pod…');
        session = await createSession();
        sessionStorage.setItem(`session:${CFG.notebookId}`, session.session_id);
        setStep('step-pod', 'done');
      }
      state.session = session;

      setStep('step-kernel', 'active');
      setLoadingSub('Waiting for kernel…');
      // Warn after 15s that a first-launch environment build may be in progress
      const slowTimer = setTimeout(() => {
        setLoadingSub('This may take a few minutes — building the notebook environment for the first time…');
      }, 15000);
      await pollUntilRunning(session.session_id);
      clearTimeout(slowTimer);
      setStep('step-kernel', 'done');

      setStep('step-connect', 'active');
      setLoadingSub('Connecting WebSocket…');
      await connectKernel(session.session_id);
      setStep('step-connect', 'done');

      hideOverlay();
      enableRunButtons();
      setKernelStatus('idle', 'Idle');

    } catch (err) {
      console.error('Session startup failed:', err);
      setLoadingSub(`Error: ${err.message}`);
      setKernelStatus('error', 'Error');
    }
  }

  // ── Preview mode ──────────────────────────────────────────────────────────

  async function loadAndRenderPreview(container) {
    try {
      const nb = await fetchNotebook();
      renderAllCells(nb, container, /* readOnly= */ true);
      setKernelStatus('unknown', 'Preview');
      if (typeof MathJax !== 'undefined') MathJax.typesetPromise([container]);
    } catch (e) {
      container.innerHTML = `<p style="color:#dc2626;padding:1rem">Failed to load notebook: ${e.message}</p>`;
    }
  }

  // ── API calls ─────────────────────────────────────────────────────────────

  async function fetchNotebook() {
    const resp = await fetch(`/api/notebooks/${CFG.notebookId}/ipynb`);
    if (!resp.ok) throw new Error(`Notebook fetch failed: ${resp.status}`);
    return resp.json();
  }

  async function createSession() {
    const resp = await fetch('/api/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notebook_id: CFG.notebookId }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `Session creation failed: ${resp.status}`);
    }
    return resp.json();
  }

  async function pollUntilRunning(sessionId, timeoutMs = 1_800_000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const resp = await fetch(`/api/sessions/${sessionId}`);
      if (resp.status === 404) {
        sessionStorage.removeItem(`session:${CFG.notebookId}`);
        throw new Error('Session no longer exists — please reload to start a new one');
      }
      if (!resp.ok) throw new Error('Session status check failed');
      const s = await resp.json();
      if (s.status === 'running') return s;
      if (s.status === 'error')   throw new Error('Session failed to start');
      await sleep(2000);
    }
    throw new Error('Timeout waiting for kernel');
  }

  async function connectKernel(sessionId) {
    const kernel = new KernelClient();
    kernel.onStatusChange = (status) => {
      const labels = { idle: 'Idle', busy: 'Busy', starting: 'Starting', error: 'Error' };
      setKernelStatus(status, labels[status] ?? status);
      qs('#btn-interrupt').disabled = status !== 'busy';
    };
    kernel.onDisconnect = () => {
      setKernelStatus('error', 'Disconnected');
      qs('#btn-run-all').disabled = true;
      qs('#btn-interrupt').disabled = true;
      sessionStorage.removeItem(`session:${CFG.notebookId}`);
    };
    await kernel.connect(sessionId);
    state.kernel = kernel;
  }

  // ── Cell rendering ────────────────────────────────────────────────────────

  function renderAllCells(nb, container, readOnly = false) {
    container.innerHTML = '';
    state.cells = [];
    const cells = nb.cells || [];
    cells.forEach((cell, idx) => {
      const entry = renderCell(cell, idx, container, readOnly);
      state.cells.push(entry);
    });
    // CodeMirror calculates dimensions at creation time; if the container
    // isn't fully painted yet those measurements are wrong and the editor
    // appears blank until clicked. A single rAF flush fixes it.
    requestAnimationFrame(() => {
      for (const entry of state.cells) {
        entry.editorInstance?.refresh();
      }
    });
  }

  function renderCell(cell, idx, container, readOnly) {
    const wrapper = document.createElement('div');
    wrapper.className = 'cell';
    wrapper.dataset.idx = idx;

    let entry = { cellData: cell, el: wrapper, editorInstance: null, outputEl: null, promptEl: null };

    switch (cell.cell_type) {
      case 'markdown': renderMarkdownCell(cell, wrapper); break;
      case 'code':     entry = Object.assign(entry, renderCodeCell(cell, idx, wrapper, readOnly)); break;
      case 'raw':      renderRawCell(cell, wrapper); break;
    }

    container.appendChild(wrapper);
    return entry;
  }

  function renderMarkdownCell(cell, wrapper) {
    wrapper.classList.add('markdown-cell');
    const source = getSource(cell);
    wrapper.innerHTML = marked.parse(source);
    if (typeof MathJax !== 'undefined') {
      MathJax.typesetPromise([wrapper]).catch(() => {});
    }
  }

  function renderRawCell(cell, wrapper) {
    wrapper.classList.add('raw-cell');
    wrapper.textContent = getSource(cell);
  }

  function renderCodeCell(cell, idx, wrapper, readOnly) {
    wrapper.classList.add('code-cell');

    const inputRow = document.createElement('div');
    inputRow.className = 'cell-input-row';

    // Execution count prompt
    const prompt = document.createElement('div');
    prompt.className = 'cell-prompt';
    const ec = cell.execution_count;
    prompt.textContent = ec != null ? ec : ' ';

    // Editor wrapper
    const editorWrap = document.createElement('div');
    editorWrap.className = 'cell-editor-wrap';

    const source = getSource(cell);

    let editor = null;
    if (!readOnly && typeof CodeMirror !== 'undefined') {
      editor = CodeMirror(editorWrap, {
        value:             source,
        mode:              'python',
        theme:             CFG.codeTheme || 'default',
        lineNumbers:       false,
        matchBrackets:     true,
        autoCloseBrackets: true,
        indentUnit:        4,
        tabSize:           4,
        indentWithTabs:    false,
        lineWrapping:      true,
        extraKeys: {
          'Shift-Enter': () => runCellByIdx(idx),
          'Tab': (cm) => {
            if (cm.somethingSelected()) cm.indentSelection('add');
            else cm.replaceSelection('    ', 'end');
          },
        },
      });
    } else {
      // Preview / fallback: plain <pre>
      const pre = document.createElement('pre');
      pre.className = 'output-text';
      pre.style.cssText = 'background:#1e2433;color:#abb2bf;padding:.5rem .75rem;border-radius:6px;';
      pre.textContent = source;
      editorWrap.appendChild(pre);
    }

    // Run button (top-right of editor)
    let runBtn = null;
    if (!readOnly) {
      runBtn = document.createElement('button');
      runBtn.className = 'cell-run-btn';
      runBtn.disabled  = true;
      runBtn.title     = 'Run cell (Shift+Enter)';
      runBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>`;
      runBtn.addEventListener('click', () => runCellByIdx(idx));
      editorWrap.appendChild(runBtn);
    }

    inputRow.appendChild(prompt);
    inputRow.appendChild(editorWrap);
    wrapper.appendChild(inputRow);

    // Output area
    const outputEl = document.createElement('div');
    outputEl.className = 'cell-output';
    wrapper.appendChild(outputEl);

    // Render existing outputs from .ipynb
    for (const out of (cell.outputs || [])) {
      const el = Renderers.renderOutput(out);
      if (el) outputEl.appendChild(el);
    }

    return { editorInstance: editor, outputEl, promptEl: prompt, runBtn };
  }

  // ── Enable run buttons after kernel is ready ──────────────────────────────

  function enableRunButtons() {
    for (const entry of state.cells) {
      if (entry.runBtn) entry.runBtn.disabled = false;
    }
    const runAll = qs('#btn-run-all');
    if (runAll) {
      runAll.disabled = false;
      runAll.addEventListener('click', runAll_handler);
    }
    const intBtn = qs('#btn-interrupt');
    if (intBtn) {
      intBtn.addEventListener('click', () => {
        fetch(`/api/sessions/${state.session.session_id}/interrupt`, { method: 'POST' });
      });
    }
  }

  // ── Execution ─────────────────────────────────────────────────────────────

  async function runCellByIdx(idx) {
    const entry = state.cells[idx];
    if (!entry || entry.cellData.cell_type !== 'code') return;
    if (!state.kernel?.connected) return;

    const source = entry.editorInstance
      ? entry.editorInstance.getValue()
      : getSource(entry.cellData);

    if (!source.trim()) return;

    // Clear existing outputs
    entry.outputEl.innerHTML = '';
    entry.cellData.outputs = [];
    setPromptState(entry, '*', true);
    entry.el.className = 'cell cell-running';

    await executeCode(source, entry);

    entry.el.className = 'cell';
  }

  async function runAll_handler() {
    for (let i = 0; i < state.cells.length; i++) {
      const entry = state.cells[i];
      if (entry.cellData.cell_type === 'code') {
        await runCellByIdx(i);
      }
    }
  }

  function executeCode(code, entry) {
    return new Promise((resolve) => {
      state.kernel.execute(code, {
        stream: (msg) => {
          const el = Renderers.renderStream(msg.content);
          if (el) appendOrMergeStream(entry.outputEl, el, msg.content.name);
        },
        display_data: (msg) => {
          const el = Renderers.renderDisplayData(msg.content);
          if (el) { entry.outputEl.appendChild(el); triggerMathJax(entry.outputEl); }
        },
        execute_result: (msg) => {
          const el = Renderers.renderExecuteResult(msg.content);
          if (el) { entry.outputEl.appendChild(el); triggerMathJax(entry.outputEl); }
        },
        error: (msg) => {
          const el = Renderers.renderError(msg.content);
          if (el) { entry.outputEl.appendChild(el); entry.el.className = 'cell cell-error'; }
        },
        onDone: (msg) => {
          const ec = msg.content?.execution_count;
          setPromptState(entry, ec ?? ' ', false);
          resolve(msg);
        },
      });
    });
  }

  // Merge consecutive stream outputs of the same name into a single <pre>
  // to avoid flickering and excessive DOM nodes.
  function appendOrMergeStream(outputEl, newEl, streamName) {
    const last = outputEl.lastElementChild;
    const cls  = streamName === 'stderr' ? 'output-stderr' : 'output-stdout';
    if (last) {
      const pre = last.querySelector(`.${cls}`);
      if (pre) {
        pre.innerHTML += newEl.querySelector(`.${cls}`)?.innerHTML ?? '';
        return;
      }
    }
    outputEl.appendChild(newEl);
  }

  function triggerMathJax(el) {
    if (typeof MathJax !== 'undefined') {
      MathJax.typesetPromise([el]).catch(() => {});
    }
  }

  // ── Prompt state helpers ──────────────────────────────────────────────────

  function setPromptState(entry, value, isRunning) {
    if (entry.promptEl) {
      entry.promptEl.textContent = value;
      entry.promptEl.classList.toggle('running', !!isRunning);
    }
  }

  // ── Cell source helper ────────────────────────────────────────────────────

  function getSource(cell) {
    const src = cell.source;
    return Array.isArray(src) ? src.join('') : (src || '');
  }

  // ── Utilities ─────────────────────────────────────────────────────────────

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  // ── Cancel button ─────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', () => {
    const cancelBtn = document.getElementById('btn-cancel');
    if (cancelBtn) {
      cancelBtn.addEventListener('click', async () => {
        if (state.session) {
          await fetch(`/api/sessions/${state.session.session_id}`, { method: 'DELETE' }).catch(() => {});
        }
        sessionStorage.removeItem(`session:${CFG.notebookId}`);
        location.href = '/';
      });
    }

    init();
  });

})();
