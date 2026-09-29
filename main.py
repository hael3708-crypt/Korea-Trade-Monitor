"""
Korea Trade Monitor - FastAPI 서버
  - 관세청 OpenAPI(공공데이터포털) 프록시 + 캐시 + 검색/엑셀 다운로드
  - API 키는 서버 환경변수(CUSTOMS_API_KEY)에서만 사용하며 브라우저로 전달하지 않습니다.
  - 키가 없으면 '샘플(가상) 데이터' 모드로 동작합니다.
실행:  python main.py        (또는 uvicorn main:app --reload)
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill

import config as cfg

BASE = Path(__file__).parent


# ---------------------------------------------------------------- .env (의존성 없는 로더)
def _load_env() -> None:
    p = BASE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$", line)
        if m and not line.lstrip().startswith("#") and m.group(1) not in os.environ:
            os.environ[m.group(1)] = m.group(2).strip("\"'")


_load_env()
RAW_KEY = os.environ.get("CUSTOMS_API_KEY", "").strip()
SERVICE_KEY = urllib.parse.unquote(RAW_KEY) if "%" in RAW_KEY else RAW_KEY
LIVE = bool(SERVICE_KEY)


class Bad(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def scrub(msg: str) -> str:
    return msg.replace(SERVICE_KEY, "***") if SERVICE_KEY else msg


# ---------------------------------------------------------------- 월 유틸
def ym_list(n: int) -> list[str]:
    d = datetime.now(timezone.utc)
    y, m, out = d.year, d.month, []
    for _ in range(n):
        out.insert(0, f"{y}{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def month_idx(ym: str) -> int:
    return int(ym[:4]) * 12 + int(ym[4:]) - 1


def idx_to_ym(i: int) -> str:
    return f"{i // 12}{i % 12 + 1:02d}"


def norm_ym(v) -> str | None:
    s = re.sub(r"[^0-9]", "", str(v or ""))
    return s if len(s) == 6 else None  # '총계' 행 등은 None


def num(v) -> float:
    try:
        return float(str(v or "0").replace(",", ""))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- XML 파싱 (정규식: XXE/엔티티 폭탄 없음)
_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TAG = re.compile(r"<([A-Za-z0-9_]+)>(.*?)</\1>", re.S)
_ENT = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}
_ENT_RE = re.compile(r"&(?:amp|lt|gt|quot|apos);")


def parse_items(body: str) -> list[dict]:
    m = re.search(r"<resultCode>([^<]*)</resultCode>", body)
    if m and m.group(1).strip() not in ("00", "0", "000"):
        msg = re.search(r"<resultMsg>([^<]*)", body)
        raise RuntimeError("API 오류: " + (msg.group(1) if msg else m.group(1)))
    if "<returnAuthMsg>" in body:
        raise RuntimeError("API 인증 오류(서비스키 확인)")
    items = []
    for im in _ITEM.finditer(body):
        items.append({k: _ENT_RE.sub(lambda x: _ENT[x.group(0)], v.strip()) for k, v in _TAG.findall(im.group(1))})
    return items


# ---------------------------------------------------------------- 관세청 API 호출 (동시성 제한 + 재시도)
_sem = asyncio.Semaphore(cfg.MAX_CONCURRENCY)
_client: httpx.AsyncClient | None = None


async def call_api(base: str, params: dict, timeout: float = cfg.TIMEOUT_S) -> list[dict]:
    async with _sem:
        for attempt in range(3):
            try:
                r = await _client.get(base, params={"serviceKey": SERVICE_KEY, **params}, timeout=timeout)
                r.raise_for_status()
                # 대용량 XML 파싱은 이벤트 루프를 막지 않도록 스레드에서 처리
                return await asyncio.to_thread(parse_items, r.text)
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise RuntimeError(scrub(f"{type(e).__name__}: {e}")) from None
                await asyncio.sleep(0.7 * (attempt + 1))  # 일시적 네트워크 오류 재시도
    return []


# ---------------------------------------------------------------- 시계열 (전체/섹터) - 연도 단위 창(window) 캐시
SERIES_MIN = "201601"       # 조회 가능한 가장 이른 시작월 (전년동월비 계산용 12개월 선행 조회 포함)
SERIES_MAX_MONTHS = 84      # 한 번에 볼 수 있는 최대 기간(개월)
DASH_MONTHS = 36            # 대시보드 기본 적재 개월수 (24개월 표시 + 전년동월비용 12개월)
ENTITIES = {"total": {"id": "total", "name": "국가 전체", "hs": []}, **{s["id"]: s for s in cfg.SECTORS}}

_year_cache: dict[tuple[str, int], tuple[float, dict[str, list[float]]]] = {}
_year_tasks: dict[tuple[str, int], asyncio.Task] = {}


async def _fetch_year(ent_id: str, year: int) -> dict[str, list[float]]:
    """한 엔티티의 한 해(1~12월) 월별 [수출, 수입]. 관세청 API는 조회기간 1년 제한이므로 연도 단위로 호출."""
    a, b = f"{year}01", f"{year}12"
    t = cfg.SEARCH["timeout_s"]
    out: dict[str, list[float]] = {}
    if ent_id == "total":
        for it in await call_api(cfg.TOTAL_BASE, {"strtYymm": a, "endYymm": b}, t):
            ym = norm_ym(it.get("year"))
            if ym:
                out[ym] = [num(it.get("expDlr")), num(it.get("impDlr"))]
    else:
        lists = await asyncio.gather(*[call_api(cfg.API_BASE, {"strtYymm": a, "endYymm": b, "hsSgn": hs}, t) for hs in ENTITIES[ent_id]["hs"]])
        for items in lists:
            for it in items:
                ym = norm_ym(it.get("year"))
                if ym:
                    v = out.setdefault(ym, [0.0, 0.0])
                    v[0] += num(it.get("expDlr"))
                    v[1] += num(it.get("impDlr"))
    return out


async def get_year(ent_id: str, year: int) -> dict[str, list[float]]:
    key = (ent_id, year)
    cur_year = datetime.now(timezone.utc).year
    ttl = cfg.CACHE_TTL_S if year >= cur_year - 1 else 24 * 3600   # 지난 연도는 거의 바뀌지 않음
    hit = _year_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    task = _year_tasks.get(key)
    if task is None:
        async def run():
            try:
                d = await _fetch_year(ent_id, year)
                _year_cache[key] = (time.time(), d)
                return d
            finally:
                _year_tasks.pop(key, None)
        task = _year_tasks[key] = asyncio.create_task(run())
    try:
        return await asyncio.shield(task)
    except Exception:
        if hit:  # 갱신 실패 시 이전 값 사용
            return hit[1]
        raise


def sample_series(ent_id: str, months: list[str]) -> tuple[list[float], list[float]]:
    """가상 데이터(실제 통계 아님): 엔티티·월 조합으로 결정되는 값"""
    import zlib

    base = {"semi": 1150, "power": 300, "auto": 520, "autoparts": 190, "nuclear": 8, "battery": 200, "cosmetic": 95, "food": 90, "medical": 55}

    def one(eid: str, ym: str) -> tuple[float, float]:
        r = lambda salt: zlib.crc32(f"{eid}{ym}{salt}".encode()) / 4294967296  # noqa: E731
        k = month_idx(ym) - month_idx("201601")
        e = base.get(eid, 100) * 1e6 * (1.004 + (zlib.crc32(eid.encode()) % 100) / 10000) ** k * (0.9 + r("e") * 0.2)
        return e, e * (0.35 + r("i") * 0.25)

    if ent_id == "total":
        rows = [[one(s["id"], m) for m in months] for s in cfg.SECTORS]
        return ([sum(r[i][0] for r in rows) * 7 for i in range(len(months))], [sum(r[i][1] for r in rows) * 11 for i in range(len(months))])
    vals = [one(ent_id, m) for m in months]
    return [round(v[0]) for v in vals], [round(v[1]) for v in vals]


async def get_series(ent_id: str, f: int, t: int) -> tuple[list[str], list[float], list[float]]:
    """월 인덱스 f..t 의 수출/수입 시계열"""
    months = [idx_to_ym(i) for i in range(f, t + 1)]
    if not LIVE:
        e, i = sample_series(ent_id, months)
        return months, e, i
    years = range(int(months[0][:4]), int(months[-1][:4]) + 1)
    maps = await asyncio.gather(*[get_year(ent_id, y) for y in years])
    merged: dict[str, list[float]] = {}
    for m in maps:
        merged.update(m)
    return months, [merged.get(m, [0.0, 0.0])[0] for m in months], [merged.get(m, [0.0, 0.0])[1] for m in months]


# ---------------------------------------------------------------- 대시보드 데이터 (/api/data)
async def build_dashboard() -> dict:
    ms = ym_list(DASH_MONTHS)
    f, t = month_idx(ms[0]), month_idx(ms[-1])
    sectors = await asyncio.gather(*[get_series(s["id"], f, t) for s in cfg.SECTORS])
    out_sectors = [{"id": s["id"], "name": s["name"], "hs": s["hs"], "exp": r[1], "imp": r[2]} for s, r in zip(cfg.SECTORS, sectors)]
    total = None  # 국가 전체(수출입총괄) - 실패해도 대시보드는 유지
    try:
        r = await get_series("total", f, t)
        total = {"exp": r[1], "imp": r[2]}
    except Exception as e:  # noqa: BLE001
        print("[total]", scrub(str(e)))
    return {"mode": "live" if LIVE else "sample", "months": ms, "sectors": out_sectors, "total": total}


_cache: dict | None = None
_cache_at = 0.0
_inflight: asyncio.Task | None = None


def _refresh() -> asyncio.Task:
    global _inflight

    async def run():
        global _cache, _cache_at, _inflight
        try:
            d = await build_dashboard()
            d["updatedAt"] = datetime.now(timezone.utc).isoformat()
            _cache, _cache_at = d, time.time()
            return d
        finally:
            _inflight = None

    if _inflight is None:
        _inflight = asyncio.create_task(run())
    return _inflight


async def get_data() -> dict:
    if _cache and time.time() - _cache_at < cfg.CACHE_TTL_S:
        return _cache
    if _cache:  # 만료: 이전 값을 즉시 반환하고 백그라운드 갱신 (stale-while-revalidate)
        t = _refresh()
        t.add_done_callback(lambda f: f.exception())
        return _cache
    return await _refresh()


def parse_series(q) -> tuple[str, int, int, str, str]:
    ent = q.get("id", "")
    if ent not in ENTITIES:
        raise Bad(400, "알 수 없는 대상입니다.")
    f, t = q.get("from", ""), q.get("to", "")
    if not _YM_RE.match(f) or not _YM_RE.match(t):
        raise Bad(400, "기간(연월)을 선택하세요.")
    now = ym_list(1)[0]
    if t > now:
        t = now
    if f < SERIES_MIN:
        raise Bad(400, f"{SERIES_MIN[:4]}.{SERIES_MIN[4:]} 이후만 조회할 수 있습니다.")
    span = month_idx(t) - month_idx(f) + 1
    if span < 1:
        raise Bad(400, "종료월이 시작월보다 빠릅니다.")
    if span > SERIES_MAX_MONTHS:
        raise Bad(400, f"최대 {SERIES_MAX_MONTHS // 12}년({SERIES_MAX_MONTHS}개월)까지 조회할 수 있습니다.")
    return ent, month_idx(f) - 12, month_idx(t), f, t  # 전년동월비 계산용으로 12개월 선행 조회


# ---------------------------------------------------------------- 검색 / 엑셀
_HS_RE = re.compile(r"^(\d{2}|\d{4}|\d{6}|\d{10})$")
_CTY_RE = re.compile(r"^[A-Z]{2}$")
_YM_RE = re.compile(r"^\d{4}(0[1-9]|1[0-2])$")
_CTY_NAME = dict(cfg.COUNTRIES)


def _to_list(v: str | None, rx: re.Pattern, mx: int, label: str) -> list[str]:
    out = list(dict.fromkeys(t for t in re.split(r"[,\s]+", (v or "").upper()) if t))
    if len(out) > mx:
        raise Bad(400, f"{label}는 최대 {mx}개까지 입력할 수 있습니다.")
    for x in out:
        if not rx.match(x):
            raise Bad(400, f"{label} 형식이 올바르지 않습니다: {x[:12]}")
    return out


def parse_search(q) -> dict:
    hs = _to_list(q.get("hs"), _HS_RE, 5, "HS코드(2·4·6·10자리)")
    cty = _to_list(q.get("cty"), _CTY_RE, 5, "국가코드(ISO 2자리)")
    if not hs and not cty:
        raise Bad(400, "HS코드 또는 국가 중 하나 이상을 입력하세요.")
    f, t = q.get("from", ""), q.get("to", "")
    if not _YM_RE.match(f) or not _YM_RE.match(t):
        raise Bad(400, "기간(연월)을 선택하세요.")
    span = month_idx(t) - month_idx(f) + 1
    if span < 1:
        raise Bad(400, "종료월이 시작월보다 빠릅니다.")
    mx = cfg.SEARCH["max_months_with_hs"] if hs else cfg.SEARCH["max_months_no_hs"]
    if span > mx:
        raise Bad(400, f"{'HS코드 지정 시' if hs else '국가만 지정 시'} 최대 {mx}개월까지 조회할 수 있습니다.")
    return {"hs": hs, "cty": cty, "from": f, "to": t, "kw": (q.get("kw") or "").strip()[:40].lower()}


_search_active = 0


async def run_search(q: dict) -> dict:
    global _search_active
    if not LIVE:
        raise Bad(400, "검색은 관세청 API 키가 설정된 LIVE 모드에서만 사용할 수 있습니다.")
    if _search_active >= cfg.SEARCH["max_concurrent"]:
        raise Bad(429, "검색 요청이 많습니다. 잠시 후 다시 시도하세요.")
    _search_active += 1
    try:
        f, t = month_idx(q["from"]), month_idx(q["to"])
        jobs = []
        for hs in q["hs"] or [None]:
            for cty in q["cty"] or [None]:
                for i in range(f, t + 1, 12):
                    p = {"strtYymm": idx_to_ym(i), "endYymm": idx_to_ym(min(i + 11, t))}
                    if hs:
                        p["hsSgn"] = hs
                    if cty:
                        p["cntyCd"] = cty
                    jobs.append(call_api(cfg.API_BASE, p, cfg.SEARCH["timeout_s"]))
        rows: dict[tuple, list] = {}  # ym|hsCd|국가 로 중복 제거
        for items in await asyncio.gather(*jobs):
            for it in items:
                ym = norm_ym(it.get("year"))
                if not ym:
                    continue
                if q["kw"] and q["kw"] not in (it.get("statKor") or "").lower():
                    continue
                rows[(ym, it.get("hsCd"), it.get("statCd"))] = [
                    ym, it.get("hsCd") or "", it.get("statKor") or "", it.get("statCd") or "", it.get("statCdCntnKor1") or "",
                    num(it.get("expDlr")), num(it.get("impDlr")), num(it.get("balPayments")), num(it.get("expWgt")), num(it.get("impWgt"))]
                if len(rows) > cfg.SEARCH["max_rows"]:
                    raise Bad(413, f"결과가 {cfg.SEARCH['max_rows']:,}행을 넘습니다. 기간이나 조건을 좁혀주세요.")
        lst = sorted(rows.values(), key=lambda r: (r[0], -r[5]))
        mm = {idx_to_ym(i): [0.0] * 4 for i in range(f, t + 1)}
        top: dict[str, dict] = {}
        for r in lst:
            m = mm[r[0]]
            m[0] += r[5]; m[1] += r[6]; m[2] += r[8]; m[3] += r[9]
            o = top.setdefault(r[1], {"hs": r[1], "name": r[2], "exp": 0.0, "imp": 0.0})
            o["exp"] += r[5]; o["imp"] += r[6]
        monthly = [{"ym": k, "exp": v[0], "imp": v[1], "bal": v[0] - v[1], "expWgt": v[2], "impWgt": v[3]} for k, v in mm.items()]
        return {"list": lst, "monthly": monthly, "top": sorted(top.values(), key=lambda x: -x["exp"])[:10]}
    finally:
        _search_active -= 1


def cond_text(q: dict) -> str:
    parts = ["HS " + ", ".join(q["hs"]) if q["hs"] else "HS 전체",
             "국가 " + ", ".join(f"{c}({_CTY_NAME[c]})" if c in _CTY_NAME else c for c in q["cty"]) if q["cty"] else "국가 전체",
             f"{q['from'][:4]}.{q['from'][4:]} ~ {q['to'][:4]}.{q['to'][4:]}"]
    if q["kw"]:
        parts.append(f'품목명 "{q["kw"]}"')
    return " / ".join(parts)


_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def to_xlsx(q: dict, r: dict) -> bytes:
    """write_only 모드(메모리 절약). 문자열은 문자열 타입 셀이라 수식으로 실행되지 않음."""
    wb = Workbook(write_only=True)
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="14264D")

    def sheet(title: str, headers: list[str], widths: list[int], rows, numeric_from: int | None):
        ws = wb.create_sheet(title)
        for i, w in enumerate(widths):
            ws.column_dimensions["ABCDEFGHIJ"[i]].width = w
        ws.freeze_panes = "A2"
        hdr = []
        for h in headers:
            c = WriteOnlyCell(ws, value=h)
            c.font, c.fill = head_font, head_fill
            hdr.append(c)
        ws.append(hdr)
        for row in rows:
            out = []
            for j, v in enumerate(row):
                if isinstance(v, str):
                    c = WriteOnlyCell(ws, value=_ILLEGAL.sub("", v))
                    c.data_type = "s"  # 수식 인젝션 차단: 항상 문자열로 저장
                else:
                    c = WriteOnlyCell(ws, value=v)
                    c.number_format = "#,##0"
                out.append(c)
            ws.append(out)

    sheet("월별요약", ["연월", "수출액(USD)", "수입액(USD)", "무역수지(USD)", "수출중량(kg)", "수입중량(kg)"], [10, 18, 18, 18, 16, 16],
          [[f"{m['ym'][:4]}.{m['ym'][4:]}", m["exp"], m["imp"], m["bal"], m["expWgt"], m["impWgt"]] for m in r["monthly"]], 1)
    sheet("상세(품목x국가)", ["연월", "HS코드", "품목명", "국가코드", "국가명", "수출액(USD)", "수입액(USD)", "무역수지(USD)", "수출중량(kg)", "수입중량(kg)"],
          [10, 13, 48, 10, 18, 18, 18, 18, 16, 16], ([f"{x[0][:4]}.{x[0][4:]}", *x[1:]] for x in r["list"]), 5)
    sheet("조회조건", ["항목", "내용"], [16, 80],
          [["조회조건", cond_text(q)], ["출처", "관세청 품목별 국가별 수출입실적(GW) - 공공데이터포털"], ["단위", "금액 USD, 중량 kg"],
           ["생성시각", datetime.now(timezone.utc).isoformat()], ["유의", "관세청 통계는 잠정치이며 이후 수정될 수 있습니다."]], None)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def series_view(months, exp, imp, f_ym: str, t_ym: str):
    """표시 구간(f_ym~t_ym, 값이 있는 마지막 월까지)의 행: [연월, 수출, 수출YoY, 수입, 수입YoY, 수지]"""
    def yoy(arr, i):
        return (arr[i] / arr[i - 12] - 1) if i >= 12 and arr[i - 12] >= 1e6 else None

    end = len(months) - 1
    while end > 12 and not exp[end] > 0:
        end -= 1
    return [[f"{months[i][:4]}.{months[i][4:]}", exp[i], yoy(exp, i), imp[i], yoy(imp, i), exp[i] - imp[i]] for i in range(12, end + 1)]


def series_xlsx(ent_id: str, f: str, t: str, months, exp, imp) -> bytes:
    wb = Workbook(write_only=True)
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="14264D")
    ws = wb.create_sheet("월별")
    for i, w in enumerate([10, 18, 12, 18, 12, 18]):
        ws.column_dimensions["ABCDEF"[i]].width = w
    ws.freeze_panes = "A2"
    hdr = []
    for h in ["연월", "수출액(USD)", "수출 YoY", "수입액(USD)", "수입 YoY", "무역수지(USD)"]:
        c = WriteOnlyCell(ws, value=h)
        c.font, c.fill = head_font, head_fill
        hdr.append(c)
    ws.append(hdr)
    fmts = [None, "#,##0", "0.0%", "#,##0", "0.0%", "#,##0"]
    for row in series_view(months, exp, imp, f, t):
        out = []
        for j, v in enumerate(row):
            c = WriteOnlyCell(ws, value=v)
            if j == 0:
                c.data_type = "s"
            elif fmts[j]:
                c.number_format = fmts[j]
            out.append(c)
        ws.append(out)
    ws2 = wb.create_sheet("조회조건")
    ws2.column_dimensions["A"].width, ws2.column_dimensions["B"].width = 16, 80
    for k, v in [("대상", ENTITIES[ent_id]["name"] + ((" (HS " + ", ".join(ENTITIES[ent_id]["hs"]) + ")") if ENTITIES[ent_id]["hs"] else "")),
                 ("기간", f"{f[:4]}.{f[4:]} ~ {t[:4]}.{t[4:]}"), ("출처", "관세청 공공데이터포털 (품목별 국가별 수출입실적 / 수출입총괄)"),
                 ("단위", "금액 USD, YoY는 전년 동월 대비 (전년 동월 수출·수입액 100만 달러 미만은 공란)"),
                 ("유의", "관세청 통계는 잠정치이며 최신월은 이후 수정될 수 있습니다. 섹터는 대표 HS코드 기준 근사치입니다."),
                 ("생성시각", datetime.now(timezone.utc).isoformat())]:
        ws2.append([k, v])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- 앱
@asynccontextmanager
async def lifespan(_: FastAPI):
    global _client
    _client = httpx.AsyncClient(headers={"Accept": "application/xml"}, limits=httpx.Limits(max_connections=10))
    t = asyncio.create_task(_prewarm())
    try:
        yield
    finally:
        t.cancel()
        await _client.aclose()


async def _prewarm() -> None:
    try:
        await get_data()
        print("초기 데이터 로드 완료")
    except Exception as e:  # noqa: BLE001
        print("초기 로드 실패:", scrub(str(e)))


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)  # API 문서 비공개

SEC = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
}
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
FILES = {p: ((BASE / "public" / f).read_bytes(), ct) for p, (f, ct) in STATIC.items()}  # 화이트리스트, 시작 시 1회 로드

_hits: dict[str, list] = {}


def _rate(ip: str, limit: int, bucket: str) -> bool:
    now, key = time.time(), f"{bucket}:{ip}"
    w = _hits.get(key)
    if not w or now - w[0] > 60:
        if len(_hits) > 5000:  # 메모리 상한
            _hits.clear()
        _hits[key] = w = [now, 0]
    w[1] += 1
    return w[1] > limit


def jerr(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status, headers=SEC)


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.method != "GET":
        return Response(status_code=405, headers=SEC)
    ip = request.client.host if request.client else "?"
    if _rate(ip, 120, "all"):
        return Response(status_code=429, headers=SEC)
    p = request.url.path
    try:
        if p in FILES:
            body, ct = FILES[p]
            return Response(body, media_type=ct, headers={**SEC, "Cache-Control": "no-cache"})
        if p == "/healthz":
            return Response("ok", media_type="text/plain", headers=SEC)
        if p == "/api/data":
            return JSONResponse(await get_data(), headers={**SEC, "Cache-Control": "public, max-age=300"})
        if p == "/api/meta":
            return JSONResponse({"mode": "live" if LIVE else "sample", "countries": cfg.COUNTRIES,
                                 "sectors": [{k: s[k] for k in ("id", "name", "hs")} for s in cfg.SECTORS],
                                 "limits": {**cfg.SEARCH, "seriesMin": SERIES_MIN, "seriesMaxMonths": SERIES_MAX_MONTHS}},
                                headers={**SEC, "Cache-Control": "public, max-age=3600"})
        if p in ("/api/series", "/api/series.xlsx"):
            if _rate(ip, 60, "series"):
                raise Bad(429, "요청이 너무 많습니다. 1분 후 다시 시도하세요.")
            ent, f0, t0, f_ym, t_ym = parse_series(request.query_params)
            months, exp, imp = await get_series(ent, f0, t0)
            if p == "/api/series":
                return JSONResponse({"id": ent, "name": ENTITIES[ent]["name"], "months": months, "exp": exp, "imp": imp},
                                    headers={**SEC, "Cache-Control": "public, max-age=300"})
            data = await asyncio.to_thread(series_xlsx, ent, f_ym, t_ym, months, exp, imp)
            return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={**SEC, "Content-Disposition": f'attachment; filename="trade_{ent}_{f_ym}-{t_ym}.xlsx"', "Cache-Control": "no-store"})
        if p in ("/api/search", "/api/download.xlsx"):
            if _rate(ip, cfg.SEARCH["rate_per_min"], "search"):
                raise Bad(429, "요청이 너무 많습니다. 1분 후 다시 시도하세요.")
            q = parse_search(request.query_params)
            r = await run_search(q)
            if p == "/api/search":
                return JSONResponse({"cond": cond_text(q), "rowCount": len(r["list"]), "monthly": r["monthly"], "top": r["top"],
                                     "preview": r["list"][:200]}, headers={**SEC, "Cache-Control": "no-store"})
            data = await asyncio.to_thread(to_xlsx, q, r)
            return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={**SEC, "Content-Disposition": f'attachment; filename="trade_{q["from"]}-{q["to"]}.xlsx"', "Cache-Control": "no-store"})
        return Response("Not found", status_code=404, headers=SEC)
    except Bad as e:
        return jerr(e.status, e.msg)
    except RuntimeError as e:
        msg = scrub(str(e))
        if msg.startswith("API 오류"):
            return jerr(400, msg)
        print("[error]", msg)
        return jerr(502, "데이터를 불러오지 못했습니다.")
    except Exception as e:  # noqa: BLE001
        print("[error]", scrub(f"{type(e).__name__}: {e}"))
        return jerr(502, "데이터를 불러오지 못했습니다.")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    print(f"http://{host}:{port}  모드: {'LIVE(관세청 API)' if LIVE else 'SAMPLE(가상 데이터)'}")
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*" if os.environ.get("TRUST_PROXY") == "1" else "127.0.0.1")
