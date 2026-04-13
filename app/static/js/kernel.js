/**
 * kernel.js — Jupyter kernel WebSocket client.
 *
 * Implements the Jupyter messaging protocol v5.
 * Connects to the FastAPI WebSocket proxy at /ws/kernel/{sessionId}.
 *
 * Exposed as window.KernelClient.
 */
window.KernelClient = (function () {
  'use strict';

  function uuid4() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = (Math.random() * 16) | 0;
      return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  function isoNow() {
    return new Date().toISOString();
  }

  class KernelClient {
    constructor() {
      this._ws            = null;
      this._sessionId     = null;   // our app session ID (used in WS URL)
      this._clientSession = uuid4(); // Jupyter session UUID (per-connection)
      this._pending       = new Map(); // msg_id → {callbacks, timer}
      this.kernelStatus   = 'unknown';
      this.onStatusChange = null;  // (status: string) => void
      this.onDisconnect   = null;  // (event) => void
    }

    // ── Connection ────────────────────────────────────────────────────

    connect(appSessionId) {
      this._sessionId = appSessionId;
      return new Promise((resolve, reject) => {
        const proto  = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url    = `${proto}//${location.host}/ws/kernel/${appSessionId}`;
        this._ws     = new WebSocket(url);

        const onOpen = () => {
          this._ws.removeEventListener('error', onError);
          this._ws.addEventListener('message', e => this._onMessage(JSON.parse(e.data)));
          this._ws.addEventListener('close',   e => this._onClose(e));
          resolve();
        };
        const onError = (e) => {
          this._ws.removeEventListener('open', onOpen);
          reject(new Error('WebSocket connection failed'));
        };

        this._ws.addEventListener('open',  onOpen, { once: true });
        this._ws.addEventListener('error', onError, { once: true });
      });
    }

    disconnect() {
      if (this._ws) {
        this._ws.close(1000, 'client disconnect');
        this._ws = null;
      }
    }

    get connected() {
      return this._ws !== null && this._ws.readyState === WebSocket.OPEN;
    }

    // ── Send messages ─────────────────────────────────────────────────

    execute(code, callbacks = {}, opts = {}) {
      const msgId = uuid4();
      const msg   = this._makeMsg('execute_request', {
        code,
        silent:           opts.silent  ?? false,
        store_history:    opts.history ?? true,
        user_expressions: {},
        allow_stdin:      false,
        stop_on_error:    opts.stopOnError ?? true,
      });

      this._pending.set(msg.header.msg_id, { callbacks, timer: null });
      this._send(msg);
      return msg.header.msg_id;
    }

    kernelInfo() {
      const msg = this._makeMsg('kernel_info_request', {});
      return new Promise((resolve, reject) => {
        this._pending.set(msg.header.msg_id, {
          callbacks: { kernel_info_reply: (m) => resolve(m.content) },
        });
        this._send(msg);
      });
    }

    // ── Message building ──────────────────────────────────────────────

    _makeMsg(msgType, content) {
      return {
        header: {
          msg_id:   uuid4(),
          msg_type: msgType,
          session:  this._clientSession,
          username: 'user',
          version:  '5.3',
          date:     isoNow(),
        },
        parent_header: {},
        metadata:      {},
        content,
        channel:       'shell',
      };
    }

    _send(msg) {
      if (!this.connected) throw new Error('Kernel not connected');
      this._ws.send(JSON.stringify(msg));
    }

    // ── Message dispatch ──────────────────────────────────────────────

    _onMessage(msg) {
      const msgType  = msg.header?.msg_type;
      const parentId = msg.parent_header?.msg_id;

      // ── Global: kernel status ──
      if (msgType === 'status') {
        this.kernelStatus = msg.content?.execution_state ?? 'unknown';
        if (this.onStatusChange) this.onStatusChange(this.kernelStatus);
        return;
      }

      if (!parentId || !this._pending.has(parentId)) return;

      const { callbacks } = this._pending.get(parentId);
      const handler = callbacks[msgType];
      if (handler) handler(msg);

      // execute_reply signals end of execution
      if (msgType === 'execute_reply') {
        if (callbacks.onDone) callbacks.onDone(msg);
        this._pending.delete(parentId);
      }
    }

    _onClose(event) {
      this._ws = null;
      this.kernelStatus = 'error';
      if (this.onStatusChange) this.onStatusChange('error');
      if (this.onDisconnect) this.onDisconnect(event);
    }
  }

  return KernelClient;
})();
