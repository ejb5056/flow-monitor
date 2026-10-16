// Shared helpers for the strip and panel windows.
const FM = {
  BULL: '#7CC0FF', BEAR: '#E07B20',

  connect(onMsg, onOpen, onState) {
    let ws, retry = 1000;
    const open = () => {
      ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
      ws.onopen = () => { retry = 1000; onState && onState(true); onOpen && onOpen(ws); };
      ws.onmessage = (e) => { try { onMsg(JSON.parse(e.data)); } catch (err) { console.error(err); } };
      ws.onclose = (e) => {
        onState && onState(false);
        if (e.code === 4401) { location.href = '/login'; return; }
        setTimeout(open, retry); retry = Math.min(retry * 2, 15000);
      };
    };
    open();
    return { send: (m) => ws && ws.readyState === 1 && ws.send(JSON.stringify(m)) };
  },

  px(v) { if (v == null || !isFinite(v)) return '—'; return v >= 1000 ? v.toLocaleString('en-US', { maximumFractionDigits: 2 }) : v.toFixed(2); },
  strike(v) { return v == null ? '—' : (Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/0$/, '')); },
  money(v) {
    if (v == null || !isFinite(v)) return '—';
    const a = Math.abs(v), s = v < 0 ? '−' : '';
    if (a >= 1e9) return s + '$' + (a / 1e9).toFixed(1) + 'B';
    if (a >= 1e6) return s + '$' + (a / 1e6).toFixed(1) + 'M';
    if (a >= 1e3) return s + '$' + Math.round(a / 1e3) + 'K';
    return s + '$' + Math.round(a);
  },
  signed(v, dp = 0) { return v == null ? '—' : (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toFixed(dp); },
  pct(a, b) { return (a == null || !b) ? '' : FM.signed((a - b) / b * 100, 1) + '%'; },
  cls(v) { return v == null || v === 0 ? 'neu' : v > 0 ? 'bull' : 'bear'; },
  time(ts) { return new Date(ts * 1000).toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour12: false }); },
  clock(el) { const t = () => el.textContent = FM.time(Date.now() / 1000); t(); setInterval(t, 1000); },
  esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); },
  barStyle(s) { // s in -1..1
    const w = Math.min(50, Math.abs(s) * 50);
    return (s >= 0 ? 'left:50%' : 'left:' + (50 - w) + '%') + ';width:' + w + '%;background:' + (s >= 0 ? FM.BULL : FM.BEAR);
  },
  badge(el, status, wsUp) {
    if (!wsUp) { el.textContent = 'RECONNECTING'; el.className = 'badge down'; return; }
    if (!status) return;
    if (status.feed === 'sim') { el.textContent = 'SIMULATED'; el.className = 'badge'; el.title = 'Invented data for testing'; return; }
    if (status.feed === 'probe') { el.textContent = 'PROBE'; el.className = 'badge'; el.title = status.msg || ''; return; }
    el.textContent = status.connected ? 'LIVE' : 'FEED DOWN';
    el.className = 'badge ' + (status.connected ? 'live' : 'down');
    el.title = status.msg || '';
  },
  store: {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  },
};
