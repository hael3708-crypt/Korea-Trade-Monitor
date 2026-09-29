'use strict';
(() => {
  const NS = 'http://www.w3.org/2000/svg';
  const $ = (id) => document.getElementById(id);
  const REFRESH_MS = 30 * 60 * 1000;

  /* ---------- 안전한 DOM 헬퍼 (innerHTML 미사용 → XSS 방지) ---------- */
  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) k === 'class' ? (e.className = v) : e.setAttribute(k, v);
    for (const k of kids) if (k != null) e.append(k);
    return e;
  }
  function s(tag, attrs) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    return e;
  }

  /* ---------- 포맷 / 계산 ---------- */
  const fmtUsd = (v) => {
    const a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toLocaleString('ko-KR', { maximumFractionDigits: 1 }) + '억$';
    if (a >= 1e4) return (v / 1e4).toLocaleString('ko-KR', { maximumFractionDigits: 0 }) + '만$';
    return v.toLocaleString('ko-KR') + '$';
  };
  const fmtPct = (v) => (v == null ? '–' : Math.abs(v) > 999 ? (v > 0 ? '>+999%' : '<-999%') : (v > 0 ? '+' : '') + v.toFixed(1) + '%');
  const ymLabel = (ym) => `${ym.slice(0, 4)}.${ym.slice(4)}`;
  const MIN_BASE = 1e6; // 전년 동월 기준액 100만$ 미만은 증감률 미표시(왜곡 방지)
  const yoy = (arr, i) => (i >= 12 && arr[i - 12] >= MIN_BASE ? (arr[i] / arr[i - 12] - 1) * 100 : null);
  const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down');
  const lastPositive = (arr) => { let i = arr.length - 1; while (i > 0 && !(arr[i] > 0)) i--; return i; };
  const mIdx = (ym) => Number(ym.slice(0, 4)) * 12 + Number(ym.slice(4)) - 1;
  const addMonths = (ym, n) => { const t = mIdx(ym) + n; return `${Math.floor(t / 12)}${String((t % 12) + 1).padStart(2, '0')}`; };
  const curYm = () => { const d = new Date(); return d.getFullYear() + String(d.getMonth() + 1).padStart(2, '0'); };
  const toInput = (ym) => `${ym.slice(0, 4)}-${ym.slice(4)}`;
  const fromInput = (v) => String(v || '').replace('-', '');

  let state = null, meta = null, selected = null, timer = null, ctrl = null;
  let range = null;                 // {from,to} 사용자가 지정한 표시 기간 (null → 기본 24개월)
  let viewSeq = 0, vCtrl = null;    // 상세 패널 비동기 경쟁 방지
  const viewCache = new Map();      // 서버에서 받은 시계열 캐시(최대 12개, 새 데이터 로드 시 초기화)

  /* ---------- 공통 차트 (막대=수출, 선=수입) ---------- */
  function drawChart(box, ms, exp, imp, yoyAt, label) {
    const W = 720, H = 300, L = 52, R = 12, T = 16, B = 30;
    const mx = Math.max(...exp, ...imp) * 1.1 || 1;
    const bw = (W - L - R) / ms.length;
    const y = (v) => T + (H - T - B) * (1 - v / mx);
    const sv = s('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': label });
    for (let g = 0; g <= 4; g++) {
      const v = (mx / 4) * g, yy = y(v);
      sv.append(s('line', { x1: L, x2: W - R, y1: yy, y2: yy, class: 'grid' }));
      const tx = s('text', { x: L - 6, y: yy + 4, class: 'ax', 'text-anchor': 'end' }); tx.textContent = fmtUsd(v); sv.append(tx);
    }
    const step = Math.ceil(ms.length / 8);
    exp.forEach((v, i) => {
      sv.append(s('rect', { x: L + i * bw + bw * 0.18, y: y(v), width: bw * 0.64, height: Math.max(0, H - B - y(v)), class: 'bar', rx: 2 }));
      if (i % step === 0 || i === ms.length - 1) {
        const tx = s('text', { x: L + i * bw + bw / 2, y: H - 10, class: 'ax', 'text-anchor': 'middle' });
        tx.textContent = ms[i].slice(2, 4) + '.' + ms[i].slice(4); sv.append(tx);
      }
    });
    sv.append(s('polyline', { points: imp.map((v, i) => `${L + i * bw + bw / 2},${y(v)}`).join(' '), class: 'line', fill: 'none' }));
    attachTip(box, sv, W, L, R, T, B, H, bw, ms.length, (i) => {
      const yy = yoyAt ? yoyAt(i) : null;
      return [h('b', null, ymLabel(ms[i])), h('div', null, '수출 ' + fmtUsd(exp[i]) + (yy ? ` (YoY ${fmtPct(yy.e)})` : '')),
        h('div', null, '수입 ' + fmtUsd(imp[i]) + (yy ? ` (YoY ${fmtPct(yy.i)})` : '')), h('div', null, '수지 ' + fmtUsd(exp[i] - imp[i]))];
    });
  }

  /* ---------- YoY 차트 (수출/수입 전년동월비, 0% 기준선) ---------- */
  function drawYoy(box, ms, ye, yi) {
    const W = 720, H = 220, L = 52, R = 12, T = 14, B = 28;
    const CAP = 300; // 표시 범위 상한(%) - 극단값이 차트를 망가뜨리지 않게 클램프
    const all = [...ye, ...yi].filter((v) => v != null);
    if (!all.length) { box.replaceChildren(h('p', { class: 'hint' }, '전년 동월 비교 데이터가 없습니다(기준액 100만$ 미만 구간은 제외).')); return; }
    const hi = Math.min(CAP, Math.max(10, ...all)) * 1.1, lo = Math.max(-100, Math.min(-10, ...all)) * 1.1;
    const y = (v) => T + (H - T - B) * (1 - (Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo));
    const bw = (W - L - R) / ms.length;
    const sv = s('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': '수출·수입 전년동월비' });
    for (const v of [lo, 0, hi].map((x) => Math.round(x))) {
      sv.append(s('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), class: v === 0 ? 'zero' : 'grid' }));
      const tx = s('text', { x: L - 6, y: y(v) + 4, class: 'ax', 'text-anchor': 'end' }); tx.textContent = v + '%'; sv.append(tx);
    }
    const step = Math.ceil(ms.length / 8);
    ms.forEach((m, i) => {
      if (i % step === 0 || i === ms.length - 1) {
        const tx = s('text', { x: L + i * bw + bw / 2, y: H - 8, class: 'ax', 'text-anchor': 'middle' });
        tx.textContent = m.slice(2, 4) + '.' + m.slice(4); sv.append(tx);
      }
    });
    const path = (arr, c) => { // null 구간에서 선을 끊음
      let d = '', pen = false;
      arr.forEach((v, i) => { if (v == null) { pen = false; return; } d += `${pen ? 'L' : 'M'}${L + i * bw + bw / 2},${y(v)}`; pen = true; });
      if (d) sv.append(s('path', { d, class: c, fill: 'none' }));
    };
    path(yi, 'line'); path(ye, 'yline');
    attachTip(box, sv, W, L, R, T, B, H, bw, ms.length, (i) => [h('b', null, ymLabel(ms[i])),
      h('div', null, '수출 YoY ' + fmtPct(ye[i])), h('div', null, '수입 YoY ' + fmtPct(yi[i]))]);
  }

  function attachTip(box, sv, W, L, R, T, B, H, bw, n, content) {
    const hover = s('rect', { x: L, y: T, width: W - L - R, height: H - T - B, fill: 'transparent' });
    sv.append(hover);
    const tip = h('div', { class: 'tip' }); tip.hidden = true;
    hover.addEventListener('mousemove', (ev) => {
      const b = sv.getBoundingClientRect();
      const px = ((ev.clientX - b.left) / b.width) * W;
      const i = Math.min(n - 1, Math.max(0, Math.floor((px - L) / bw)));
      tip.replaceChildren(...content(i));
      tip.hidden = false;
      tip.style.left = Math.min(ev.clientX - b.left + 14, b.width - 190) + 'px';
      tip.style.top = ev.clientY - b.top + 'px';
    });
    hover.addEventListener('mouseleave', () => { tip.hidden = true; });
    box.replaceChildren(sv, tip);
  }

  /* ---------- 데이터 로드 ---------- */
  async function load() {
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    $('status').textContent = '불러오는 중… (서버 시작 직후에는 최대 1분 걸릴 수 있습니다)';
    try {
      const r = await fetch('/api/data', { signal: ctrl.signal });
      if (!r.ok) throw new Error(r.status);
      const prev = state && state.updatedAt;
      state = await r.json();
      let last = state.months.length - 1;
      while (last > 0 && !state.sectors.some((x) => x.exp[last] > 0)) last--;
      state.last = last;
      state.tlast = state.total ? lastPositive(state.total.exp) : last;
      if (state.updatedAt !== prev) viewCache.clear();
      if (!selected || !ent(selected)) selected = state.total ? 'total' : state.sectors[0].id;
      render();
    } catch (e) {
      if (e.name !== 'AbortError') $('status').textContent = '데이터를 불러오지 못했습니다. 잠시 후 자동 재시도합니다.';
    }
  }

  /* 선택 가능한 대상: 국가 전체 + 섹터 */
  function ent(id) {
    if (id === 'total') return state.total ? { id, name: '국가 전체', hs: '전체 품목 (수출입총괄)', exp: state.total.exp, imp: state.total.imp, last: state.tlast } : null;
    const x = state.sectors.find((v) => v.id === id);
    return x ? { id, name: x.name, hs: 'HS ' + x.hs.join(', '), exp: x.exp, imp: x.imp, last: state.last } : null;
  }

  /* ---------- 렌더: 요약 / 카드 ---------- */
  function render() {
    const { sectors, months, last, total, tlast } = state;
    $('banner').hidden = state.mode !== 'sample';
    $('status').textContent = `섹터 기준월 ${ymLabel(months[last])}${total ? ' · 전체 기준월 ' + ymLabel(months[tlast]) : ''} · 서버 갱신 ${new Date(state.updatedAt).toLocaleString('ko-KR')}`;

    const sum = (key, i) => sectors.reduce((a, x) => a + x[key][i], 0);
    const E = sum('exp', last), I = sum('imp', last);
    const Ep = last >= 12 ? sum('exp', last - 12) : 0;
    const ey = Ep >= MIN_BASE ? (E / Ep - 1) * 100 : null;
    const kpis = [];
    if (total) {
      const ty = yoy(total.exp, tlast);
      const partial = months[tlast] === curYm();
      kpis.push([`국가 전체 수출 (${ymLabel(months[tlast])}${partial ? ' 잠정' : ''})`, fmtUsd(total.exp[tlast]), fmtPct(ty) + ' 전년동월비', cls(ty)]);
      kpis.push(['국가 전체 수입', fmtUsd(total.imp[tlast]), '무역수지 ' + fmtUsd(total.exp[tlast] - total.imp[tlast]), total.exp[tlast] >= total.imp[tlast] ? 'up' : 'down', true]);
      kpis.push([`추적 섹터 수출 (${ymLabel(months[last])})`, fmtUsd(E), total.exp[last] > 0 ? `전체 대비 ${(E / total.exp[last] * 100).toFixed(1)}% · ${fmtPct(ey)} YoY` : fmtPct(ey) + ' YoY', cls(ey)]);
    } else {
      kpis.push(['추적 섹터 수출', fmtUsd(E), fmtPct(ey) + ' 전년동월비', cls(ey)], ['추적 섹터 수입', fmtUsd(I), null, ''], ['무역수지', fmtUsd(E - I), null, E - I >= 0 ? 'up' : 'down']);
    }
    kpis.push(['최고 증가 섹터', ...topMover(sectors, last)]);
    $('kpis').replaceChildren(...kpis.map(([l, v, d, c, neutral]) =>
      h('div', { class: 'kpi' }, h('div', { class: 'kpi-l' }, l), h('div', { class: 'kpi-v ' + (neutral ? '' : c || '') }, v),
        d ? h('div', { class: 'kpi-d ' + (c || '') }, d) : null)));

    const cards = [];
    if (total) {
      const y = yoy(total.exp, tlast);
      cards.push(h('button', { class: 'card total' + (selected === 'total' ? ' sel' : ''), type: 'button', 'data-id': 'total' },
        h('div', { class: 'card-n' }, '국가 전체'), h('div', { class: 'card-v' }, fmtUsd(total.exp[tlast])),
        h('div', { class: 'card-y ' + cls(y) }, `YoY ${fmtPct(y)}`, h('span', { class: 'mut' }, ' · ' + ymLabel(months[tlast]))),
        spark(total.exp.slice(Math.max(0, tlast - 23), tlast + 1))));
    }
    for (const x of sectors) {
      const y = yoy(x.exp, last);
      const share = total && total.exp[last] > 0 ? ` · 비중 ${(x.exp[last] / total.exp[last] * 100).toFixed(1)}%` : '';
      cards.push(h('button', { class: 'card' + (x.id === selected ? ' sel' : ''), type: 'button', 'data-id': x.id },
        h('div', { class: 'card-n' }, x.name), h('div', { class: 'card-v' }, fmtUsd(x.exp[last])),
        h('div', { class: 'card-y ' + cls(y) }, `YoY ${fmtPct(y)}`, h('span', { class: 'mut' }, share)),
        spark(x.exp.slice(Math.max(0, last - 23), last + 1))));
    }
    $('cards').replaceChildren(...cards);
    detail();
  }

  function topMover(sectors, last) {
    let best = null;
    for (const x of sectors) { const y = yoy(x.exp, last); if (y != null && (!best || y > best.y)) best = { n: x.name, y }; }
    return best ? [best.n, fmtPct(best.y) + ' 전년동월비', 'up'] : ['–', null, ''];
  }

  function spark(vals) {
    const w = 120, hh = 28, mx = Math.max(...vals), mn = Math.min(...vals), r = mx - mn || 1;
    const pts = vals.map((v, i) => `${(i / (vals.length - 1 || 1)) * w},${hh - 3 - ((v - mn) / r) * (hh - 6)}`).join(' ');
    const sv = s('svg', { viewBox: `0 0 ${w} ${hh}`, class: 'spark', 'aria-hidden': 'true' });
    sv.append(s('polyline', { points: pts, fill: 'none', stroke: 'currentColor', 'stroke-width': '1.6', 'stroke-linejoin': 'round' }));
    return sv;
  }

  /* ---------- 상세 패널: 기간 설정 + 전체/섹터 + YoY ---------- */
  function effectiveRange(e) {
    const now = curYm();
    if (range) return { from: range.from, to: range.to > now ? now : range.to };
    const to = state.months[e.last];
    return { from: addMonths(to, -23), to };
  }

  // 표시 구간 [from-12개월 .. to] 데이터 확보: 이미 받은 36개월 이내면 재사용, 아니면 서버 조회
  async function getView(e, from, to, signal) {
    const pre = addMonths(from, -12), ms = state.months;
    const i0 = ms.indexOf(pre);
    if (i0 >= 0 && mIdx(to) <= mIdx(ms[ms.length - 1])) {
      const i1 = Math.max(i0, ms.indexOf(to));
      return { months: ms.slice(i0, i1 + 1), exp: e.exp.slice(i0, i1 + 1), imp: e.imp.slice(i0, i1 + 1) };
    }
    const key = `${e.id}|${pre}|${to}`;
    if (viewCache.has(key)) return viewCache.get(key);
    const r = await fetch(`/api/series?id=${encodeURIComponent(e.id)}&from=${from}&to=${to}`, { signal });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '조회 실패');
    if (viewCache.size >= 12) viewCache.delete(viewCache.keys().next().value);
    viewCache.set(key, d);
    return d;
  }

  async function detail() {
    const e = ent(selected); if (!e) return;
    const seq = ++viewSeq;
    if (vCtrl) vCtrl.abort();
    vCtrl = new AbortController();
    const { from, to } = effectiveRange(e);
    $('d-title').textContent = e.name; $('d-hs').textContent = e.hs;
    $('d-from').value = toInput(from); $('d-to').value = toInput(to);
    $('d-msg').textContent = '조회 중… (처음 보는 긴 기간은 수 초~수십 초 걸릴 수 있습니다)';
    let v;
    try { v = await getView(e, from, to, vCtrl.signal); } catch (err) { if (err.name !== 'AbortError' && seq === viewSeq) $('d-msg').textContent = err.message; return; }
    if (seq !== viewSeq) return;

    // 값이 있는 마지막 월까지만 표시
    let end = v.months.length - 1;
    while (end > 12 && !(v.exp[end] > 0)) end--;
    const M = v.months, X = v.exp, I = v.imp;
    const dm = M.slice(12, end + 1), de = X.slice(12, end + 1), di = I.slice(12, end + 1);
    const ye = de.map((_, k) => yoy(X, 12 + k)), yi = di.map((_, k) => yoy(I, 12 + k));
    $('d-msg').textContent = dm.length ? `${ymLabel(dm[0])} ~ ${ymLabel(dm[dm.length - 1])} (${dm.length}개월)${mIdx(to) > mIdx(dm[dm.length - 1]) ? ` · ${ymLabel(to)}는 아직 데이터가 없습니다` : ''}` : '해당 기간에 데이터가 없습니다.';
    if (!dm.length) { $('chart').replaceChildren(); $('yoy-chart').replaceChildren(); $('rows').replaceChildren(); $('d-sum').replaceChildren(); return; }

    // 기간 합계 & 전년 동기간 대비
    const sumE = de.reduce((a, b) => a + b, 0), sumI = di.reduce((a, b) => a + b, 0);
    const pE = X.slice(0, end - 11).reduce((a, b) => a + b, 0), pI = I.slice(0, end - 11).reduce((a, b) => a + b, 0);
    const pe = pE >= MIN_BASE ? (sumE / pE - 1) * 100 : null, pi = pI >= MIN_BASE ? (sumI / pI - 1) * 100 : null;
    $('d-sum').replaceChildren(
      h('div', { class: 'sm' }, h('span', { class: 'mut' }, '기간 수출 합계'), h('b', null, fmtUsd(sumE)), h('span', { class: cls(pe) }, `전년 동기간 대비 ${fmtPct(pe)}`)),
      h('div', { class: 'sm' }, h('span', { class: 'mut' }, '기간 수입 합계'), h('b', null, fmtUsd(sumI)), h('span', { class: cls(pi) }, `전년 동기간 대비 ${fmtPct(pi)}`)),
      h('div', { class: 'sm' }, h('span', { class: 'mut' }, '기간 무역수지'), h('b', { class: cls(sumE - sumI) }, fmtUsd(sumE - sumI))));

    drawChart($('chart'), dm, de, di, (i) => ({ e: ye[i], i: yi[i] }), `${e.name} 월별 수출입`);
    drawYoy($('yoy-chart'), dm, ye, yi);
    const rows = [];
    for (let i = dm.length - 1; i >= 0; i--) {
      rows.push(h('li', null, h('span', { class: 'm' }, ymLabel(dm[i])),
        h('span', null, `수출 ${fmtUsd(de[i])} `, h('em', { class: cls(ye[i]) }, `(${fmtPct(ye[i])})`)),
        h('span', null, `수입 ${fmtUsd(di[i])} `, h('em', { class: cls(yi[i]) }, `(${fmtPct(yi[i])})`)),
        h('span', { class: cls(de[i] - di[i]) }, `수지 ${fmtUsd(de[i] - di[i])}`)));
    }
    $('rows').replaceChildren(...rows);
  }

  function applyRange() {
    const from = fromInput($('d-from').value), to = fromInput($('d-to').value), now = curYm();
    const min = meta ? meta.limits.seriesMin : '201601', maxM = meta ? meta.limits.seriesMaxMonths : 84;
    if (!/^\d{6}$/.test(from) || !/^\d{6}$/.test(to)) { $('d-msg').textContent = '기간(연월)을 선택하세요.'; return; }
    if (from < min) { $('d-msg').textContent = `${ymLabel(min)} 이후만 조회할 수 있습니다.`; return; }
    if (mIdx(to) < mIdx(from)) { $('d-msg').textContent = '종료월이 시작월보다 빠릅니다.'; return; }
    if (mIdx(to) - mIdx(from) + 1 > maxM) { $('d-msg').textContent = `최대 ${maxM / 12}년까지 조회할 수 있습니다.`; return; }
    range = { from, to: to > now ? now : to };
    detail();
  }
  function preset(n) {
    const e = ent(selected); if (!e) return;
    const to = state.months[e.last];
    range = n === 0 ? null : { from: addMonths(to, -(n - 1)), to };
    detail();
  }
  document.querySelectorAll('[data-preset]').forEach((b) => b.addEventListener('click', () => preset(Number(b.dataset.preset))));
  $('d-apply').addEventListener('click', applyRange);
  $('d-form').addEventListener('submit', (e) => { e.preventDefault(); applyRange(); });

  async function downloadBlob(url, name, msgEl, busyBtns) {
    busyBtns.forEach((b) => { b.disabled = true; });
    msgEl.textContent = '엑셀 생성 중…';
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error((await r.json()).error || '다운로드 실패');
      const blobUrl = URL.createObjectURL(await r.blob());
      const a = h('a', { href: blobUrl, download: name });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 10_000); // 메모리 해제
      msgEl.textContent = '엑셀 다운로드를 시작했습니다.';
    } catch (e) { msgEl.textContent = e.message; } finally { busyBtns.forEach((b) => { b.disabled = false; }); }
  }
  $('d-xlsx').addEventListener('click', () => {
    const e = ent(selected); if (!e) return;
    const { from, to } = effectiveRange(e);
    downloadBlob(`/api/series.xlsx?id=${encodeURIComponent(e.id)}&from=${from}&to=${to}`, `trade_${e.id}_${from}-${to}.xlsx`, $('d-msg'), [$('d-xlsx'), $('d-apply')]);
  });

  /* ---------- 검색 · 엑셀 다운로드 ---------- */
  async function initSearch() {
    try { meta = await (await fetch('/api/meta')).json(); } catch { $('q-msg').textContent = '검색 설정을 불러오지 못했습니다.'; return; }
    $('q-cty-list').replaceChildren(...meta.countries.map(([c, n]) => h('option', { value: n }, c)));
    $('q-preset').append(...meta.sectors.map((x) => h('option', { value: x.hs.join(', ') }, `${x.name} (${x.hs.join(', ')})`)));
    if (meta.mode !== 'live') $('q-msg').textContent = '검색은 관세청 API 키가 설정된 LIVE 모드에서만 동작합니다.';
    const cur = curYm();
    $('q-to').value = toInput(addMonths(cur, -1)); $('q-from').value = toInput(addMonths(cur, -12));
    $('d-from').min = $('q-from').min = toInput(meta.limits.seriesMin); $('d-to').max = $('q-to').max = toInput(cur);
  }
  $('q-preset').addEventListener('change', (e) => { if (e.target.value) $('q-hs').value = e.target.value; });

  function ctyCodes() {
    const out = [];
    for (const tok of $('q-cty').value.split(/[,\s]+/).filter(Boolean)) {
      if (/^[A-Za-z]{2}$/.test(tok)) { out.push(tok.toUpperCase()); continue; }
      const hit = meta.countries.find(([, n]) => n === tok);
      if (!hit) throw new Error(`국가를 찾을 수 없습니다: ${tok} (목록에서 선택하거나 ISO 2자리 코드를 입력하세요)`);
      out.push(hit[0]);
    }
    return out;
  }
  function queryString() {
    const p = new URLSearchParams();
    const hs = $('q-hs').value.trim(); if (hs) p.set('hs', hs);
    const c = ctyCodes(); if (c.length) p.set('cty', c.join(','));
    p.set('from', fromInput($('q-from').value)); p.set('to', fromInput($('q-to').value));
    const kw = $('q-kw').value.trim(); if (kw) p.set('kw', kw);
    return p.toString();
  }

  let sCtrl = null;
  function busy(on) { $('q-go').disabled = on; $('q-dl').disabled = on; }
  async function search() {
    if (sCtrl) sCtrl.abort(); sCtrl = new AbortController();
    let qs; try { qs = queryString(); } catch (e) { $('q-msg').textContent = e.message; return; }
    $('q-msg').textContent = '조회 중… (기간이 길면 수십 초 걸릴 수 있습니다)'; busy(true);
    try {
      const r = await fetch('/api/search?' + qs, { signal: sCtrl.signal });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '조회 실패');
      $('q-msg').textContent = `${d.cond} — 상세 ${d.rowCount.toLocaleString('ko-KR')}행`;
      const m = d.monthly;
      drawChart($('q-chart'), m.map((x) => x.ym), m.map((x) => x.exp), m.map((x) => x.imp), null, '검색 결과 월별 수출입');
      $('q-monthly').replaceChildren(...m.slice().reverse().map((x) => h('li', null, h('span', { class: 'm' }, ymLabel(x.ym)),
        h('span', null, `수출 ${fmtUsd(x.exp)}`), h('span', null, `수입 ${fmtUsd(x.imp)}`), h('span', { class: cls(x.bal) }, `수지 ${fmtUsd(x.bal)}`))));
      $('q-top').replaceChildren(...d.top.map((x) => h('li', null, h('span', { class: 'm' }, x.hs || '-'), h('span', { class: 'grow' }, x.name),
        h('span', null, `수출 ${fmtUsd(x.exp)}`))));
      $('q-res').hidden = false;
    } catch (e) { if (e.name !== 'AbortError') { $('q-msg').textContent = e.message; $('q-res').hidden = true; } }
    finally { busy(false); }
  }
  $('q-form').addEventListener('submit', (e) => { e.preventDefault(); search(); });
  $('q-go').addEventListener('click', search);
  $('q-dl').addEventListener('click', () => {
    let qs; try { qs = queryString(); } catch (e) { $('q-msg').textContent = e.message; return; }
    downloadBlob('/api/download.xlsx?' + qs, `trade_${qs.match(/from=(\d+)/)[1]}-${qs.match(/to=(\d+)/)[1]}.xlsx`, $('q-msg'), [$('q-go'), $('q-dl')]);
  });

  /* ---------- 이벤트 ---------- */
  $('cards').addEventListener('click', (e) => {
    const b = e.target.closest('.card');
    if (!b) return;
    selected = b.dataset.id;
    document.querySelectorAll('#cards .card').forEach((c) => c.classList.toggle('sel', c === b));
    detail();
  });
  function schedule() { clearInterval(timer); timer = setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS); }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && state && Date.now() - new Date(state.updatedAt) > REFRESH_MS) load(); });
  $('refresh').addEventListener('click', load);
  schedule(); load(); initSearch();
})();
