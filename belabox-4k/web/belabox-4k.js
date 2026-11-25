/*
 * belabox-4k add-on for belaUI
 *
 * 1. Friendly connection names: "iPhone hotspot · 82%", "Ethernet",
 *    "EE 5G · 64%"... Tap a name to set your own (remembered on that
 *    phone/browser); leave it empty to go back to the automatic name.
 * 2. "What can my connections carry?": while streaming, tracks what the
 *    connections actually send and suggests a max bitrate (about 80% of the
 *    best they managed) and the best resolution for it.
 *
 * Only reads what belaUI already shows; the only thing it ever changes is
 * the max bitrate, and only when you press "Use".
 */
(function () {
  'use strict';
  const NAMES_KEY = 'belabox4k-names';
  const WINDOW_S = 120;          // measure over the last 2 minutes
  const MIN_MEASURE_S = 20;      // before suggesting anything

  let custom = {};
  try { custom = JSON.parse(localStorage.getItem(NAMES_KEY) || '{}') || {}; } catch (e) { custom = {}; }
  const saveNames = () => { try { localStorage.setItem(NAMES_KEY, JSON.stringify(custom)); } catch (e) {} };

  const wifiByIf = {}, modemByIf = {};
  let lastNetifs = null, samples = [], streamStart = 0;

  // ---------------------------------------------------------------- names
  // signal strength (%) for Wi-Fi and modems, shown under the name
  function signalOf(ifname) {
    const w = wifiByIf[ifname];
    const net = w && (w.available || []).find((a) => a.active);
    if (net && net.signal) return net.signal;
    const m = modemByIf[ifname];
    const st = (m && m.status) || {};
    return st.signal != null && st.signal !== '' ? st.signal : null;
  }

  function autoName(ifname, ip) {
    const w = wifiByIf[ifname];
    if (w) {
      const net = (w.available || []).find((a) => a.active);
      if (net) {
        if (/iphone/i.test(net.ssid) || /^172\.20\.10\./.test(ip)) return 'iPhone hotspot';
        if (/android|galaxy|pixel|phone/i.test(net.ssid)) return 'Phone hotspot';
        return `Wi-Fi: ${net.ssid}`;
      }
      if (w.hotspot) return 'BELABOX hotspot';
    }
    const m = modemByIf[ifname];
    if (m) {
      const st = m.status || {};
      return [st.network, st.network_type].filter(Boolean).join(' ') || 'Modem';
    }
    if (/^172\.20\.10\./.test(ip)) return 'iPhone (USB)';
    if (/^192\.168\.42\./.test(ip)) return 'Android phone (USB)';
    if (/^(wwan|ppp)/.test(ifname)) return 'Modem';
    if (/^(eth|end|enp)/.test(ifname)) return 'Ethernet';
    if (/^(usb|enx)/.test(ifname)) return 'USB connection';
    if (/^wl/.test(ifname)) return 'Wi-Fi';
    return ifname;
  }

  function nameFor(ifname, ip) {
    return custom[ifname] || autoName(ifname, ip);
  }

  function rename(ifname, ip) {
    const cur = custom[ifname] || '';
    const v = window.prompt(`Name for ${ifname} (${autoName(ifname, ip)}).\nLeave empty for the automatic name:`, cur);
    if (v === null) return;
    if (v.trim()) custom[ifname] = v.trim().slice(0, 40); else delete custom[ifname];
    saveNames();
    if (lastNetifs) decorate(lastNetifs);
  }

  // "13800 Kbps" -> "13.8 Mbps": shorter, so the row fits a phone screen
  function compactSpeeds() {
    $('#netifTable td.netif_tp').each(function () {
      const m = $(this).text().match(/^(\d+) Kbps$/);
      if (m && +m[1] >= 1000) $(this).text(`${(+m[1] / 1000).toFixed(1)} Mbps`);
    });
  }

  function decorate(netifs) {
    compactSpeeds();
    $('#netifTable>tbody>tr').each(function () {
      const cell = $(this).find('.netif_name');
      const ifname = cell.text();
      if (!netifs[ifname]) return;              // the Total row, or already decorated
      const ip = netifs[ifname].ip || '';
      cell.empty()
        .append($('<span class="bb4k-name">').text(nameFor(ifname, ip)))
        .append($('<small class="d-block text-muted">').text(
          ifname + (signalOf(ifname) != null ? ` · ${signalOf(ifname)}%` : '')))
        .css('cursor', 'pointer')
        .attr('title', 'Tap to rename')
        .on('click', () => rename(ifname, ip));
    });
  }

  // ------------------------------------------------------------ capacity
  function ensurePanel() {
    let p = $('#bb4k-capacity');
    if (!p.length) {
      p = $('<div id="bb4k-capacity"></div>');
      $('#netifTable').after(p);
    }
    return p;
  }

  const fmt = (kbps) => `${(kbps / 1000).toFixed(1)} Mbps`;

  function currentMax() {
    try { return $('#bitrateSlider').slider('value'); } catch (e) { return null; }
  }

  // how good 4K looks at a given bitrate (H.265, 30 fps; rule of thumb)
  function quality4k(kbps) {
    if (kbps >= 18000) return 'Excellent';
    if (kbps >= 12000) return 'Good';
    if (kbps >= 8000) return 'Fair · softer in motion';
    return 'Low · add connections';
  }

  // guardian status (written by the belabox-guardian service)
  let guardian = null, guardianFetched = 0;
  function pollGuardian() {
    if (Date.now() - guardianFetched < 5000) return;
    guardianFetched = Date.now();
    fetch('/belabox-guardian.json?t=' + Date.now(), { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : null)).then((g) => { guardian = g; }).catch(() => { guardian = null; });
  }
  function guardianRow(row) {
    if (!guardian || !guardian.running) return '';
    const data = guardian.data_gb != null ? row('Data used this stream', `${guardian.data_gb.toFixed(1)} GB`) : '';
    if (!guardian.cap_kbps) {
      return row('Auto-protect', guardian.drops ? `On · ${guardian.drops} fixed` : 'On') + data;
    }
    const mins = guardian.next_raise_in_s != null ? Math.ceil(guardian.next_raise_in_s / 60) : null;
    return row('Auto-protect', `${fmt(guardian.cap_kbps)} cap`) +
      `<div class="bb4k-note">After a drop at ${guardian.last_drop || '?'}` +
      (mins != null ? ` · back up in ${mins} min` : '') + '</div>' + data;
  }

  function updateCapacity(totalKbps) {
    const p = ensurePanel();
    pollGuardian();
    const streaming = (typeof isStreaming !== 'undefined') && isStreaming;
    const now = Date.now();
    const head = '<div class="bb4k-title">What can my connections carry?</div>';
    const row = (k, v) => `<div class="bb4k-row"><span>${k}</span><strong>${v}</strong></div>`;
    if (!streaming) {
      samples = []; streamStart = 0;
      p.html(head + '<div class="bb4k-note">Start the stream to measure.</div>' + guardianRow(row));
      return;
    }
    if (!streamStart) streamStart = now;
    samples.push({ t: now, kbps: totalKbps });
    samples = samples.filter((s) => now - s.t <= WINDOW_S * 1000);
    const secs = Math.round((now - streamStart) / 1000);
    if (secs < MIN_MEASURE_S) {
      p.html(head + `<div class="bb4k-note">Measuring… ${secs} / ${MIN_MEASURE_S} s</div>` + guardianRow(row));
      return;
    }
    const peak = Math.max(...samples.map((s) => s.kbps));
    const maxBr = currentMax();
    const rounded = (v) => Math.max(500, Math.floor(v / 250) * 250);
    let html = head + row('Sending now', fmt(totalKbps)) + row('Best in last 2 min', fmt(peak)) + guardianRow(row);
    if (maxBr && peak >= 0.9 * maxBr) {
      html += row('4K quality', quality4k(maxBr)) +
              `<div class="bb4k-note">Your connections kept up with your max bitrate (${fmt(maxBr)}). ` +
              `They may carry more: raise it in small steps and watch for drops.</div>`;
    } else {
      const suggest = rounded(peak * 0.8);
      html += row('Suggested max bitrate', `${suggest} Kbps`) + row('4K quality', quality4k(suggest)) +
              (maxBr && suggest < maxBr - 250
                ? `<button type="button" class="btn btn-primary btn-block mt-2" id="bb4k-use">Use ${suggest} Kbps</button>` : '');
      p.html(html);
      $('#bb4k-use').on('click', () => {
        try {
          $('#bitrateSlider').slider('value', suggest);
          showBitrate(suggest);
          setBitrate(suggest);
        } catch (e) {}
      });
      return;
    }
    p.html(html);
  }

  // ---------------------------------------------------------------- styles
  $('<style>').text(`
    #netifTable td.netif_tp { width: 1%; white-space: nowrap; text-align: right; }
    #netifTable td.netif_ip { font-size: .85em; opacity: .7; white-space: nowrap; }
    #netifTable td.netif_name .bb4k-name { white-space: nowrap; }
    #netifTable td.netif_name small { white-space: nowrap; font-size: .75em; }
    #bb4k-capacity { margin: 0 0 .5rem; padding: .6rem .75rem; border-top: 1px solid rgba(128,128,128,.35);
                     font-size: .9rem; }
    #bb4k-capacity .bb4k-title { font-weight: 600; margin-bottom: .25rem; }
    #bb4k-capacity .bb4k-row { display: flex; justify-content: space-between; gap: 1rem; padding: .1rem 0; }
    #bb4k-capacity .bb4k-row span { opacity: .75; }
    #bb4k-capacity .bb4k-note { opacity: .75; }
    @media (max-width: 400px) {
      #netifTable td.netif_ip { font-size: .75em; }
    }
  `).appendTo('head');

  // ------------------------------------------------- hook into belaUI
  function wrap(fnName, after) {
    const orig = window[fnName];
    if (typeof orig !== 'function') return;
    window[fnName] = function (msg) {
      const r = orig.apply(this, arguments);
      try { after(msg); } catch (e) { console.warn('belabox-4k:', e); }
      return r;
    };
  }

  wrap('updateWifiState', (msg) => {
    for (const k of Object.keys(wifiByIf)) delete wifiByIf[k];
    for (const id in msg || {}) if (msg[id] && msg[id].ifname) wifiByIf[msg[id].ifname] = msg[id];
    if (lastNetifs) decorate(lastNetifs);
  });
  wrap('updateModemsState', (msg) => {
    for (const id in msg || {}) {
      const m = msg[id];
      if (!m) continue;
      const prev = Object.values(modemByIf).find((x) => x._id === id) || {};
      const merged = Object.assign({}, prev, m, { _id: id });
      if (merged.ifname) modemByIf[merged.ifname] = merged;
    }
    if (lastNetifs) decorate(lastNetifs);
  });
  wrap('updateNetif', (netifs) => {
    lastNetifs = netifs || {};
    decorate(lastNetifs);
    let total = 0;
    for (const i in lastNetifs) total += Math.round((lastNetifs[i].tp * 8) / 1024);   // same as belaUI
    updateCapacity(total);
  });
})();
