#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
부하테스트 리포트 생성기 (WhaTap 성능추이 CSV -> HTML 리포트)
================================================================

사용법:
    python generate_report.py --csv perf-trending.csv
    python generate_report.py --csv perf.csv --title "수강신청 부하테스트" --out report.html
    python generate_report.py --csv perf.csv --start "2026-07-27 10:00" --end "2026-07-27 17:00"
    python generate_report.py --csv perf.csv --notes 인계.md      # 정성 분석(마크다운)을 리포트에 첨부
    python generate_report.py --csv perf.csv --year 2026 --term 2 --compare 2026-1학기.summary.json
    python generate_report.py --csv perf.csv --year 2026 --term 2 --history-dir history   # 지난 학기 자동 비교

입력 CSV: WhaTap [애플리케이션 > 분석 > 성능 추이] 화면에서 날짜/시간대 지정 후 CSV 내보내기.
헤더 형식: "Timestamp","Realtime User (count)","TPS","Response Time (ms)","CPU (%)","Heap (byte)","Active Tx (count)"

동작:
  1) CSV 파싱 (5분 버킷)
  2) 부하 세션 자동 탐지 (TPS 임계 이상 버킷을 시간 간격으로 묶음)
  3) 세션별/전체 KPI 계산 (최대 TPS/응답시간/동시 Active Tx, 처리 요청 추정)
  4) 동일 디자인의 self-contained HTML 리포트 출력 (테마 대응 + 내장 SVG 차트)
  5) --notes 마크다운이 있으면 정성 분석 섹션으로 첨부
  6) --year/--term 학기 표기, 요약 JSON 저장, --compare/--history-dir 로 지난 학기 대비 비교
"""

import argparse
import csv
import datetime as dt
import html
import io
import json
import os
import re
import sys

# ----------------------------------------------------------------------------- CSV
FIELDS = {
    "ts": "Timestamp",
    "users": "Realtime User (count)",
    "tps": "TPS",
    "rt": "Response Time (ms)",
    "cpu": "CPU (%)",
    "heap": "Heap (byte)",
    "atx": "Active Tx (count)",
}


def load_csv(path):
    with io.open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for r in reader:
            def num(key):
                v = (r.get(FIELDS[key]) or "").strip().strip('"')
                try:
                    return float(v)
                except ValueError:
                    return 0.0
            ts_raw = (r.get(FIELDS["ts"]) or "").strip().strip('"')
            try:
                ts = dt.datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            rows.append({
                "ts": ts, "hm": ts.strftime("%H:%M"),
                "users": num("users"), "tps": num("tps"), "rt": num("rt"),
                "cpu": num("cpu"), "heap": num("heap"), "atx": num("atx"),
            })
    rows.sort(key=lambda x: x["ts"])
    return rows


def clip(rows, start, end):
    if start:
        rows = [r for r in rows if r["ts"] >= start]
    if end:
        rows = [r for r in rows if r["ts"] <= end]
    return rows


# ----------------------------------------------------------------------------- 세션 탐지
def detect_sessions(rows, tps_thresh, max_gap_min):
    """TPS>=임계인 버킷을 활성으로 보고, 간격 <= max_gap 이면 같은 세션으로 병합."""
    active = [r for r in rows if r["tps"] >= tps_thresh]
    if not active:
        return []
    sessions, cur = [], [active[0]]
    for r in active[1:]:
        gap = (r["ts"] - cur[-1]["ts"]).total_seconds() / 60.0
        if gap <= max_gap_min:
            cur.append(r)
        else:
            sessions.append(cur)
            cur = [r]
    sessions.append(cur)
    out = []
    for i, s in enumerate(sessions, 1):
        out.append({
            "no": i,
            "start": s[0]["ts"], "end": s[-1]["ts"],
            "start_hm": s[0]["hm"], "end_hm": s[-1]["hm"],
            "max_tps": max(x["tps"] for x in s),
            "max_rt": max(x["rt"] for x in s),
            "max_atx": max(x["atx"] for x in s),
            "max_users": max(x["users"] for x in s),
            "buckets": s,
        })
    return out


def active_window(rows, sessions, pad_min=20):
    """차트/표시용: 첫 세션 시작 전, 마지막 세션 종료 후 pad 만큼만 남김."""
    if not sessions:
        return rows
    lo = sessions[0]["start"] - dt.timedelta(minutes=pad_min)
    hi = sessions[-1]["end"] + dt.timedelta(minutes=pad_min)
    return [r for r in rows if lo <= r["ts"] <= hi]


def compute_kpis(rows, sessions):
    peak_tps = max((r["tps"] for r in rows), default=0)
    peak_rt = max((r["rt"] for r in rows), default=0)
    peak_atx = max((r["atx"] for r in rows), default=0)
    peak_users = max((r["users"] for r in rows), default=0)
    # 처리 요청 추정: TPS(초당) * 300초(5분 버킷) 합
    total_req = sum(r["tps"] * 300.0 for r in rows)
    # 피크 순간(최대 Active Tx) 시각
    peak_atx_row = max(rows, key=lambda r: r["atx"]) if rows else None
    peak_tps_row = max(rows, key=lambda r: r["tps"]) if rows else None
    return {
        "peak_tps": peak_tps, "peak_rt": peak_rt, "peak_atx": peak_atx,
        "peak_users": peak_users, "total_req": total_req,
        "n_sessions": len(sessions),
        "peak_atx_hm": peak_atx_row["hm"] if peak_atx_row else "-",
        "peak_tps_hm": peak_tps_row["hm"] if peak_tps_row else "-",
    }


def analyze(rows, tps_thresh, max_gap_min):
    """세션 탐지 → 표시 구간 → KPI 를 한 번에. (본 분석·비교 대상 CSV 공용)"""
    sessions = detect_sessions(rows, tps_thresh, max_gap_min)
    win = active_window(rows, sessions)
    kpis = compute_kpis(win, sessions)
    return sessions, win, kpis


def date_label_of(rows):
    label = rows[0]["ts"].strftime("%Y-%m-%d")
    if rows[0]["ts"].date() != rows[-1]["ts"].date():
        label += " ~ " + rows[-1]["ts"].strftime("%Y-%m-%d")
    return label


# ----------------------------------------------------------------------------- 학년도/학기
# 학년도 내 순서: 1학기 → 여름 계절 → 2학기 → 겨울 계절
TERMS = {"1": ("1학기", 1), "여름": ("여름 계절학기", 2), "2": ("2학기", 3), "겨울": ("겨울 계절학기", 4)}
TERM_ALIASES = {"1학기": "1", "2학기": "2", "summer": "여름", "winter": "겨울",
                "여름학기": "여름", "겨울학기": "겨울", "s": "여름", "w": "겨울"}


def norm_term(t):
    if t is None:
        return None
    t = str(t).strip()
    t = TERM_ALIASES.get(t.lower(), t)
    if t not in TERMS:
        raise SystemExit("학기 형식 오류: %r (1 | 2 | 여름 | 겨울)" % t)
    return t


def term_label(year, term):
    if year and term:
        return "%s학년도 %s" % (year, TERMS[term][0])
    if year:
        return "%s학년도" % year
    return ""


def term_key(year, term):
    return (int(year), TERMS[term][1])


def is_regular(term):
    return term in ("1", "2")


# ----------------------------------------------------------------------------- 요약(JSON) / 학기 비교
SUMMARY_VERSION = 1


def build_summary(year, term, title, date_label, sessions, kpis, win, params):
    """학기 비교용 집계 요약. 원본 CSV 없이 다음 학기 리포트와 비교할 수 있도록 저장."""
    base = win[0]["ts"] if win else None
    return {
        "version": SUMMARY_VERSION,
        "year": year, "term": term, "label": term_label(year, term),
        "title": title, "date": date_label, "params": params,
        "kpis": kpis,
        "sessions": [{k: s[k] for k in ("no", "start_hm", "end_hm", "max_tps", "max_rt", "max_atx", "max_users")}
                     for s in sessions],
        # 표시 구간 시작 기준 경과분, TPS, 응답시간, Active Tx
        "series": [[int((r["ts"] - base).total_seconds() // 60), round(r["tps"], 1), round(r["rt"], 1),
                    round(r["atx"], 1)] for r in win],
    }


def save_summary(summary, path):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False, indent=1))


def load_summary(path):
    with io.open(path, "r", encoding="utf-8") as f:
        s = json.load(f)
    if "kpis" not in s or "sessions" not in s:
        raise SystemExit("요약 JSON 형식이 아닙니다: %s" % path)
    return s


def find_prev_summary(history_dir, year, term):
    """history 폴더에서 '지난 학기' 요약을 찾는다.
    정규학기(1·2)는 직전 정규학기, 계절학기는 직전 계절학기와 비교."""
    if not os.path.isdir(history_dir):
        return None
    cur = term_key(year, term)
    best = None
    for name in os.listdir(history_dir):
        m = re.match(r"^(\d{4})-(1|2|여름|겨울)\.json$", name)
        if not m:
            continue
        y, t = m.group(1), m.group(2)
        if is_regular(t) != is_regular(term):
            continue
        k = term_key(y, t)
        if k < cur and (best is None or k > best[0]):
            best = (k, os.path.join(history_dir, name))
    return best[1] if best else None


def summary_name(s):
    return s.get("label") or s.get("date") or "비교 대상"


# ----------------------------------------------------------------------------- 숫자 포맷
def fnum(v, unit=""):
    if v >= 1000:
        return "{:,}".format(int(round(v))) + unit
    if v >= 100:
        return "{:.0f}".format(v) + unit
    if v >= 10:
        return "{:.0f}".format(v) + unit
    return "{:.1f}".format(v) + unit


def fint(v):
    return "{:,}".format(int(round(v)))


# ----------------------------------------------------------------------------- 미니 마크다운 -> HTML
def md_to_html(md):
    lines = md.replace("\r\n", "\n").split("\n")
    out, i, n = [], 0, len(lines)

    def inline(t):
        t = html.escape(t)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
        return t

    while i < n:
        ln = lines[i]
        if ln.strip().startswith("```"):
            buf = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre>" + "\n".join(buf) + "</pre>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            lvl = min(len(m.group(1)) + 1, 4)
            out.append("<h{0}>{1}</h{0}>".format(lvl, inline(m.group(2))))
            i += 1
            continue
        if ln.strip().startswith(">"):
            out.append('<div class="callout">' + inline(ln.strip()[1:].strip()) + "</div>")
            i += 1
            continue
        # 표
        if "|" in ln and i + 1 < n and re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", lines[i + 1]):
            header = [c.strip() for c in ln.strip().strip("|").split("|")]
            i += 2
            body = []
            while i < n and "|" in lines[i]:
                body.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            t = ['<div class="scroll"><table><tr>'] + ["<th>" + inline(h) + "</th>" for h in header] + ["</tr>"]
            for r in body:
                t.append("<tr>" + "".join("<td>" + inline(c) + "</td>" for c in r) + "</tr>")
            t.append("</table></div>")
            out.append("".join(t))
            continue
        if re.match(r"^\s*[-*]\s+", ln):
            items = []
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append("<li>" + inline(re.sub(r"^\s*[-*]\s+", "", lines[i])) + "</li>")
                i += 1
            out.append('<ul class="clean">' + "".join(items) + "</ul>")
            continue
        if ln.strip() == "" or ln.strip() == "---":
            i += 1
            continue
        out.append("<p>" + inline(ln) + "</p>")
        i += 1
    return "\n".join(out)


# ----------------------------------------------------------------------------- HTML
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2530;--muted:#5b6675;--line:#e5e9ef;--accent:#2f6df6;--good:#1a9d6e;--warn:#e5893b;--bad:#e0484d;--tps:#7b3ff2;--rt:#12a3a3;--tx:#2f6df6;--grid:#eef1f5;--chipbg:#eef2fb;}
@media(prefers-color-scheme:dark){:root{--bg:#0f141a;--card:#161d26;--ink:#e7edf5;--muted:#9aa7b6;--line:#243040;--grid:#1e2733;--chipbg:#1a2436;}}
:root[data-theme="dark"]{--bg:#0f141a;--card:#161d26;--ink:#e7edf5;--muted:#9aa7b6;--line:#243040;--grid:#1e2733;--chipbg:#1a2436;}
:root[data-theme="light"]{--bg:#f6f7f9;--card:#fff;--ink:#1c2530;--muted:#5b6675;--line:#e5e9ef;--grid:#eef1f5;--chipbg:#eef2fb;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Pretendard","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif;line-height:1.62;-webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding:32px 20px 80px}
header.rep{border-radius:16px;padding:28px;color:#fff;background:linear-gradient(120deg,#2f6df6,#7b3ff2);box-shadow:0 10px 30px rgba(47,109,246,.25)}
header.rep .kicker{font-size:13px;opacity:.9;margin:0 0 8px}
header.rep h1{margin:0;font-size:25px;font-weight:800}
header.rep .sub{margin:10px 0 0;font-size:14px;opacity:.92}
.metarow{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.metarow span{background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.25);padding:5px 11px;border-radius:999px;font-size:12.5px}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin-top:18px}
h2{font-size:18px;margin:0 0 14px;display:flex;align-items:center;gap:9px}
h2 .n{display:inline-flex;width:26px;height:26px;border-radius:8px;background:var(--chipbg);color:var(--accent);font-size:13px;font-weight:800;align-items:center;justify-content:center}
h3{font-size:15px;margin:20px 0 8px}
p{margin:8px 0}.muted{color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
@media(max-width:720px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 15px}
.kpi .v{font-size:21px;font-weight:800}.kpi .l{font-size:12.5px;color:var(--muted);margin-top:3px}.kpi .s{font-size:11.5px;color:var(--muted);margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:6px}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12.5px;background:var(--grid)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr.peak td{background:rgba(224,72,77,.08)}
.tag{display:inline-block;font-size:11.5px;padding:2px 8px;border-radius:6px;font-weight:600}
.tag.p{background:rgba(224,72,77,.14);color:var(--bad)}.tag.n{background:var(--grid);color:var(--muted)}.tag.g{background:rgba(26,157,110,.14);color:var(--good)}.tag.w{background:rgba(229,137,59,.16);color:var(--warn)}
.callout{border-left:4px solid var(--warn);background:rgba(229,137,59,.08);border-radius:0 10px 10px 0;padding:14px 16px;margin:12px 0}
pre{background:var(--grid);border:1px solid var(--line);border-radius:9px;padding:12px 14px;overflow-x:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px;line-height:1.5;margin:8px 0}
code{background:var(--grid);padding:1px 6px;border-radius:5px;font-size:12.5px;font-family:ui-monospace,Consolas,monospace}
ul.clean{margin:8px 0;padding-left:20px}ul.clean li{margin:6px 0}
.chart{width:100%;height:230px;display:block}.chart.sm{height:150px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);margin:2px 0 4px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.foot{color:var(--muted);font-size:12px;margin-top:26px;text-align:center}
.scroll{overflow-x:auto}
"""

CHART_JS = """
(function(){
 var DATA=%(data)s, LAB=%(labels)s;
 var svg=document.getElementById("chart");if(!svg||!DATA.length)return;
 var W=960,H=240,pl=6,pr=6,pt=14,pb=8,n=DATA.length;
 var mT=Math.max.apply(0,DATA.map(function(d){return d[1]}))||1;
 var mR=Math.max.apply(0,DATA.map(function(d){return d[2]}))||1;
 var mX=Math.max.apply(0,DATA.map(function(d){return d[3]}))||1;
 var x=function(i){return pl+i*(W-pl-pr)/(n-1)};
 var yb=function(v,mx){return (H-pb)-v/mx*(H-pt-pb)};
 var NS="http://www.w3.org/2000/svg";
 function line(k,color,mx,w){var d="";DATA.forEach(function(r,i){d+=(i?"L":"M")+x(i).toFixed(1)+","+yb(r[k],mx).toFixed(1)+" "});
  var p=document.createElementNS(NS,"path");p.setAttribute("d",d);p.setAttribute("fill","none");
  p.setAttribute("stroke",color);p.setAttribute("stroke-width",w);p.setAttribute("stroke-linejoin","round");svg.appendChild(p)}
 for(var g=1;g<4;g++){var yy=pt+g*(H-pt-pb)/4;var ln=document.createElementNS(NS,"line");
  ln.setAttribute("x1",pl);ln.setAttribute("x2",W-pr);ln.setAttribute("y1",yy);ln.setAttribute("y2",yy);
  ln.setAttribute("stroke","currentColor");ln.setAttribute("opacity",".08");svg.appendChild(ln)}
 var c=getComputedStyle(document.documentElement);
 line(3,c.getPropertyValue("--tx").trim()||"#2f6df6",mX,1.6);
 line(1,c.getPropertyValue("--tps").trim()||"#7b3ff2",mT,1.8);
 line(2,c.getPropertyValue("--rt").trim()||"#12a3a3",mR,1.8);
 var ax=document.getElementById("axis");if(ax){LAB.forEach(function(t){var s=document.createElement("span");s.textContent=t;ax.appendChild(s)})}
})();
"""


def axis_labels(win):
    """활성 구간에서 정시(hh:00) 라벨 몇 개 뽑기."""
    labs = []
    seen = set()
    for r in win:
        if r["ts"].minute == 0 and r["hm"] not in seen:
            labs.append(r["hm"])
            seen.add(r["hm"])
    if len(labs) > 10:
        step = len(labs) // 8 + 1
        labs = labs[::step]
    return labs


def pct_change(cur, prev):
    if not prev:
        return None
    return (cur - prev) / prev * 100.0


def fdelta(cur, prev, fmt):
    d = cur - prev
    sign = "+" if d > 0 else ("−" if d < 0 else "±")
    p = pct_change(cur, prev)
    ptxt = "" if p is None else " (%s%.1f%%)" % ("+" if p > 0 else ("−" if p < 0 else "±"), abs(p))
    return sign + fmt(abs(d)) + ptxt


# (라벨, kpi 키, 포맷, 판정) — 판정: "lower" 는 낮을수록 좋음, None 은 증감만 표시
COMPARE_METRICS = [
    ("최대 TPS", "peak_tps", fint, None),
    ("최대 동시 Active Tx", "peak_atx", fint, "lower"),
    ("최대 평균 응답시간 (ms)", "peak_rt", fnum, "lower"),
    ("피크 동시접속", "peak_users", fint, None),
    ("총 처리요청 (추정)", "total_req", fint, None),
    ("부하 세션 수", "n_sessions", fint, None),
]
JUDGE_PCT = 5.0  # 이 비율 이상 변하면 개선/악화 표시


def judge_tag(cur, prev, rule):
    p = pct_change(cur, prev)
    if rule is None or p is None:
        return '<span class="tag n">참고</span>'
    if abs(p) < JUDGE_PCT:
        return '<span class="tag n">유사</span>'
    better = p < 0 if rule == "lower" else p > 0
    return '<span class="tag g">개선</span>' if better else '<span class="tag p">악화</span>'


def compare_lines(cur_kpis, prev_kpis):
    """콘솔 출력용 비교 요약."""
    out = []
    for label, key, fmt, _ in COMPARE_METRICS:
        c, p = cur_kpis.get(key, 0), prev_kpis.get(key, 0)
        out.append("   - %s : %s → %s  [%s]" % (label, fmt(p), fmt(c), fdelta(c, p, fmt)))
    return out


def render_compare(cur, prev):
    """지난 학기 대비 섹션 HTML 과 차트 스크립트."""
    esc = html.escape
    cn, pn = esc(summary_name(cur)), esc(summary_name(prev))

    krows = []
    for label, key, fmt, rule in COMPARE_METRICS:
        c, p = cur["kpis"].get(key, 0), prev["kpis"].get(key, 0)
        krows.append("<tr><td>%s</td><td class='num'>%s</td><td class='num'><b>%s</b></td><td class='num'>%s</td><td>%s</td></tr>"
                     % (esc(label), fmt(p), fmt(c), fdelta(c, p, fmt), judge_tag(c, p, rule)))

    srows = []
    cs = {s["no"]: s for s in cur["sessions"]}
    ps = {s["no"]: s for s in prev["sessions"]}
    for no in sorted(set(cs) | set(ps)):
        c, p = cs.get(no), ps.get(no)

        def cell(s, k, fmt):
            return fmt(s[k]) if s else "<span class='muted'>-</span>"
        rt_tag = judge_tag(c["max_rt"], p["max_rt"], "lower") if c and p else ""
        srows.append(
            "<tr><td>%d차</td><td>%s</td><td>%s</td>"
            "<td class='num'>%s</td><td class='num'><b>%s</b></td>"
            "<td class='num'>%s</td><td class='num'><b>%s</b></td>"
            "<td class='num'>%s</td><td class='num'><b>%s</b></td><td>%s</td></tr>"
            % (no,
               "%s–%s" % (p["start_hm"], p["end_hm"]) if p else "-",
               "%s–%s" % (c["start_hm"], c["end_hm"]) if c else "-",
               cell(p, "max_tps", fint), cell(c, "max_tps", fint),
               cell(p, "max_rt", fnum), cell(c, "max_rt", fnum),
               cell(p, "max_atx", fint), cell(c, "max_atx", fint), rt_tag))

    p_par, c_par = prev.get("params") or {}, cur.get("params") or {}
    par_note = ""
    if p_par and c_par and p_par != c_par:
        par_note = ('<div class="callout">세션 탐지 파라미터가 다릅니다 (지난: TPS≥%s·간격 %s분 / 이번: TPS≥%s·간격 %s분). '
                    '회차 구분이 달라질 수 있으니 회차별 비교는 참고용으로 보세요.</div>'
                    % (p_par.get("tps_threshold"), p_par.get("max_gap_min"),
                       c_par.get("tps_threshold"), c_par.get("max_gap_min")))

    html_out = """
<section>
  <h2><span class="n">3</span>지난 학기 대비</h2>
  <p class="muted">비교 대상: <b>%(pn)s</b> (%(pd)s) → <b>%(cn)s</b> (%(cd)s)</p>
  %(parnote)s
  <div class="scroll"><table>
    <tr><th>지표</th><th class="num">%(pn)s</th><th class="num">%(cn)s</th><th class="num">증감</th><th>판정</th></tr>
    %(krows)s
  </table></div>
  <p class="muted" style="font-size:12.5px">※ 판정: 응답시간·동시 Active Tx 는 낮을수록 개선 (±%(jp)s%% 미만은 유사). TPS·처리요청은 투입 부하에 따라 달라지므로 증감만 표시.</p>

  <h3>회차별 비교</h3>
  <div class="scroll"><table>
    <tr><th>회차</th><th>지난 시간대</th><th>이번 시간대</th><th class="num">지난 TPS</th><th class="num">이번 TPS</th>
        <th class="num">지난 응답(ms)</th><th class="num">이번 응답(ms)</th><th class="num">지난 Active Tx</th><th class="num">이번 Active Tx</th><th>응답</th></tr>
    %(srows)s
  </table></div>

  <h3>추이 겹쳐 보기 <span class="muted" style="font-weight:400;font-size:12.5px">(각 학기 첫 부하 20분 전 기준 경과 시간)</span></h3>
  <div class="legend"><span><i style="background:var(--tps)"></i>%(cn)s</span><span><i style="background:var(--muted)"></i>%(pn)s</span></div>
  <p class="muted" style="margin:6px 0 0;font-size:12.5px">TPS</p>
  <svg class="chart sm" id="cmp_tps" viewBox="0 0 960 160" preserveAspectRatio="none"></svg>
  <p class="muted" style="margin:6px 0 0;font-size:12.5px">평균 응답시간 (ms)</p>
  <svg class="chart sm" id="cmp_rt" viewBox="0 0 960 160" preserveAspectRatio="none"></svg>
  <div id="cmp_axis" class="muted" style="font-size:11.5px;display:flex;justify-content:space-between"></div>
</section>
""" % {"pn": pn, "cn": cn, "pd": esc(prev.get("date", "")), "cd": esc(cur.get("date", "")),
       "parnote": par_note, "krows": "".join(krows), "srows": "".join(srows), "jp": "%g" % JUDGE_PCT}

    js = CMP_JS % {"cur": json.dumps(cur["series"]), "prev": json.dumps(prev.get("series") or [])}
    return html_out, js


CMP_JS = """
(function(){
 var C=%(cur)s, P=%(prev)s; if(!C.length)return;
 var W=960,H=160,pt=10,pb=6;
 var mx=Math.max(C[C.length-1][0], P.length?P[P.length-1][0]:0)||1;
 var NS="http://www.w3.org/2000/svg", cs=getComputedStyle(document.documentElement);
 function draw(id,k){var svg=document.getElementById(id);if(!svg)return;
  var m=Math.max.apply(0,C.concat(P).map(function(d){return d[k]}))||1;
  for(var g=1;g<4;g++){var yy=pt+g*(H-pt-pb)/4,ln=document.createElementNS(NS,"line");
   ln.setAttribute("x1",0);ln.setAttribute("x2",W);ln.setAttribute("y1",yy);ln.setAttribute("y2",yy);
   ln.setAttribute("stroke","currentColor");ln.setAttribute("opacity",".08");svg.appendChild(ln)}
  function path(D,color,w,dash){if(!D.length)return;var d="";D.forEach(function(r,i){
    d+=(i?"L":"M")+(r[0]/mx*W).toFixed(1)+","+((H-pb)-r[k]/m*(H-pt-pb)).toFixed(1)+" "});
   var p=document.createElementNS(NS,"path");p.setAttribute("d",d);p.setAttribute("fill","none");
   p.setAttribute("stroke",color);p.setAttribute("stroke-width",w);p.setAttribute("stroke-linejoin","round");
   if(dash)p.setAttribute("stroke-dasharray",dash);svg.appendChild(p)}
  path(P,cs.getPropertyValue("--muted").trim()||"#888",1.6,"5 4");
  path(C,cs.getPropertyValue("--tps").trim()||"#7b3ff2",2,null)}
 draw("cmp_tps",1);draw("cmp_rt",2);
 var ax=document.getElementById("cmp_axis");if(ax){for(var i=0;i<=6;i++){var s=document.createElement("span");
  var t=Math.round(mx*i/6);s.textContent="+"+Math.floor(t/60)+":"+("0"+t%%60).slice(-2);ax.appendChild(s)}}
})();
"""


def render(rows, sessions, kpis, win, title, date_label, notes_html, label="", cmp_html="", cmp_js=""):
    esc = html.escape
    peak = max(sessions, key=lambda s: s["max_atx"]) if sessions else None

    # KPI 카드
    kpi_cards = [
        ("var(--tps)", fint(kpis["peak_tps"]), "최대 TPS", "@" + kpis["peak_tps_hm"]),
        ("var(--tx)", fint(kpis["peak_atx"]), "최대 동시 Active Tx", "@" + kpis["peak_atx_hm"]),
        ("var(--rt)", fnum(kpis["peak_rt"]) + " ms", "최대 평균 응답시간", "부하 피크 시"),
        ("", fint(kpis["n_sessions"]) + "회", "부하 세션(회차)", "총 처리요청 ≈ " + fint(kpis["total_req"])),
    ]
    kpi_html = "".join(
        '<div class="kpi"><div class="v" style="color:%s">%s</div><div class="l">%s</div><div class="s">%s</div></div>'
        % (c or "inherit", esc(v), esc(l), esc(s)) for c, v, l, s in kpi_cards
    )

    # 세션 표
    srows = []
    for s in sessions:
        cls = ' class="peak"' if peak and s["no"] == peak["no"] else ""
        tag = '<span class="tag p">최대</span>' if peak and s["no"] == peak["no"] else '<span class="tag g">-</span>'
        srows.append(
            "<tr%s><td>%d차</td><td>%s–%s</td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td><td>%s</td></tr>"
            % (cls, s["no"], s["start_hm"], s["end_hm"], fint(s["max_tps"]),
               fnum(s["max_rt"]), fint(s["max_atx"]), fint(s["max_users"]), tag)
        )
    sess_table = "".join(srows) or "<tr><td colspan='7' class='muted'>탐지된 부하 세션이 없습니다.</td></tr>"

    # 차트 데이터 (활성 구간)
    data = [[r["hm"], round(r["tps"], 1), round(r["rt"], 1), round(r["atx"], 1)] for r in win]
    chart_js = CHART_JS % {"data": json.dumps(data, ensure_ascii=False),
                           "labels": json.dumps(axis_labels(win), ensure_ascii=False)}

    notes_section = ""
    if notes_html:
        notes_section = '<section><h2><span class="n">＋</span>정성 분석 / 작업 기록</h2>%s</section>' % notes_html

    win_label = ""
    if win:
        win_label = "%s ~ %s" % (win[0]["ts"].strftime("%Y-%m-%d %H:%M"), win[-1]["ts"].strftime("%H:%M"))

    return """<meta charset="utf-8"><title>%(title)s</title><style>%(css)s</style>
<div class="wrap">
<header class="rep">
  <p class="kicker">%(label)s부하테스트 결과 리뷰 · WhaTap 성능추이 기반 · 자동 생성</p>
  <h1>%(title)s</h1>
  <p class="sub">측정일 <b>%(date)s</b> · 분석 구간 %(win)s</p>
  <div class="metarow"><span>데이터 출처: WhaTap 성능 추이 (5분 단위)</span><span>지표: TPS · 응답시간 · 동시 Active Tx</span></div>
</header>

<section>
  <h2><span class="n">1</span>핵심 요약</h2>
  <div class="kpis">%(kpis)s</div>
  <p style="margin-top:16px">분석 구간에서 총 <b>%(nsess)d회</b>의 부하 세션이 탐지됐습니다.
  피크는 <b>%(peakhm)s</b>경으로 최대 <b>%(ptps)s TPS</b>, 동시 처리 <b>%(patx)s</b>건, 평균 응답 <b>%(prt)s ms</b>에 도달했습니다.
  동시 Active Tx가 동시접속 사용자 수보다 크게 높으면 요청 적체(큐잉)를 뜻합니다.</p>
</section>

<section>
  <h2><span class="n">2</span>회차별 부하 분석</h2>
  <div class="scroll"><table>
    <tr><th>세션</th><th>시간대</th><th class="num">최대 TPS</th><th class="num">최대 평균응답(ms)</th><th class="num">최대 동시 Active Tx</th><th class="num">피크 동시접속</th><th>부하</th></tr>
    %(sess)s
  </table></div>
  <p class="muted" style="font-size:12.5px">※ 값은 5분 버킷 내 최댓값. 세션 = TPS 임계 이상 버킷을 시간 간격으로 묶어 자동 탐지.</p>

  <h3>시간대별 추이</h3>
  <div class="legend"><span><i style="background:var(--tps)"></i>TPS</span><span><i style="background:var(--rt)"></i>평균 응답시간 (ms)</span><span><i style="background:var(--tx)"></i>동시 Active Tx</span></div>
  <svg class="chart" id="chart" viewBox="0 0 960 240" preserveAspectRatio="none"></svg>
  <div id="axis" class="muted" style="font-size:11.5px;display:flex;justify-content:space-between"></div>
</section>

%(cmp)s
%(notes)s

<p class="foot">WhaTap 성능 추이 데이터 기반 · generate_report.py 자동 생성</p>
</div>
<script>%(chart)s%(cmpjs)s</script>
""" % {
        "title": esc(title), "css": CSS, "date": esc(date_label), "win": esc(win_label),
        "kpis": kpi_html, "nsess": kpis["n_sessions"], "peakhm": esc(kpis["peak_atx_hm"]),
        "ptps": fint(kpis["peak_tps"]), "patx": fint(kpis["peak_atx"]), "prt": fnum(kpis["peak_rt"]),
        "sess": sess_table, "notes": notes_section, "chart": chart_js,
        "label": esc(label) + " · " if label else "", "cmp": cmp_html, "cmpjs": cmp_js,
    }


# ----------------------------------------------------------------------------- main
def parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise SystemExit("날짜 형식 오류: %r (예: 2026-07-27 10:00)" % s)


def load_rows(path, start, end):
    if not os.path.exists(path):
        raise SystemExit("CSV 없음: %s" % path)
    rows = load_csv(path)
    if not rows:
        raise SystemExit("CSV에서 데이터 행을 못 읽었습니다. 헤더 형식을 확인하세요: %s" % path)
    rows = clip(rows, parse_dt(start), parse_dt(end))
    if not rows:
        raise SystemExit("지정한 시간대에 데이터가 없습니다: %s" % path)
    return rows


def load_compare(args, params):
    """--compare 가 JSON 이면 저장된 요약을, CSV 면 같은 파라미터로 즉석 분석."""
    path = args.compare
    if not os.path.exists(path):
        raise SystemExit("비교 대상 없음: %s" % path)
    if path.lower().endswith(".json"):
        return load_summary(path)
    rows = load_rows(path, args.compare_start, args.compare_end)
    sessions, win, kpis = analyze(rows, args.tps_threshold, args.max_gap_min)
    return build_summary(args.compare_year, norm_term(args.compare_term), "", date_label_of(rows),
                         sessions, kpis, win, params)


def main():
    ap = argparse.ArgumentParser(description="WhaTap 성능추이 CSV -> 부하테스트 HTML 리포트")
    ap.add_argument("--csv", required=True, help="WhaTap 성능추이 CSV 경로")
    ap.add_argument("--start", help="분석 시작 (예: '2026-07-27 10:00')")
    ap.add_argument("--end", help="분석 종료 (예: '2026-07-27 17:00')")
    ap.add_argument("--year", help="학년도 (예: 2026)")
    ap.add_argument("--term", help="학기: 1 | 2 | 여름 | 겨울")
    ap.add_argument("--title", default="부하테스트 결과 리포트", help="리포트 제목")
    ap.add_argument("--out", help="출력 HTML 경로 (기본: <csv이름>_report.html)")
    ap.add_argument("--notes", help="정성 분석 마크다운(.md) 첨부 (선택)")
    ap.add_argument("--tps-threshold", type=float, default=50.0, help="세션 탐지 TPS 임계 (기본 50)")
    ap.add_argument("--max-gap-min", type=float, default=30.0, help="세션 병합 최대 간격(분) (기본 30)")
    g = ap.add_argument_group("지난 학기 비교")
    g.add_argument("--compare", help="비교 대상: 지난 학기 요약(.summary.json) 또는 성능추이 CSV")
    g.add_argument("--compare-year", help="비교 대상 CSV 의 학년도")
    g.add_argument("--compare-term", help="비교 대상 CSV 의 학기")
    g.add_argument("--compare-start", help="비교 대상 CSV 분석 시작")
    g.add_argument("--compare-end", help="비교 대상 CSV 분석 종료")
    g.add_argument("--history-dir", help="학기별 요약 보관 폴더. 이번 결과를 <학년도>-<학기>.json 으로 저장하고, "
                                         "--compare 가 없으면 여기서 지난 학기를 찾아 자동 비교")
    g.add_argument("--no-summary", action="store_true", help="요약 JSON(<out>.summary.json) 저장 안 함")
    args = ap.parse_args()

    term = norm_term(args.term)
    if args.year and not re.match(r"^\d{4}$", str(args.year)):
        raise SystemExit("학년도 형식 오류: %r (예: 2026)" % args.year)
    if args.history_dir and not (args.year and term):
        raise SystemExit("--history-dir 를 쓰려면 --year 와 --term 이 필요합니다.")
    label = term_label(args.year, term)
    params = {"tps_threshold": args.tps_threshold, "max_gap_min": args.max_gap_min}

    rows = load_rows(args.csv, args.start, args.end)
    sessions, win, kpis = analyze(rows, args.tps_threshold, args.max_gap_min)
    date_label = date_label_of(rows)
    summary = build_summary(args.year, term, args.title, date_label, sessions, kpis, win, params)

    prev = None
    if args.compare:
        prev = load_compare(args, params)
    elif args.history_dir:
        prev_path = find_prev_summary(args.history_dir, args.year, term)
        if prev_path:
            prev = load_summary(prev_path)
        else:
            print("참고: %s 에 지난 학기 요약이 없어 비교 없이 생성합니다." % args.history_dir)
    cmp_html, cmp_js = render_compare(summary, prev) if prev else ("", "")

    notes_html = ""
    if args.notes:
        if os.path.exists(args.notes):
            with io.open(args.notes, "r", encoding="utf-8") as f:
                notes_html = md_to_html(f.read())
        else:
            print("경고: notes 파일 없음: %s" % args.notes, file=sys.stderr)

    out = args.out or (os.path.splitext(args.csv)[0] + "_report.html")
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(render(rows, sessions, kpis, win, args.title, date_label, notes_html, label, cmp_html, cmp_js))

    saved = []
    if not args.no_summary:
        saved.append(os.path.splitext(out)[0] + ".summary.json")
    if args.history_dir:
        saved.append(os.path.join(args.history_dir, "%s-%s.json" % (args.year, term)))
    for path in saved:
        save_summary(summary, path)

    print("리포트 생성 완료: %s" % out)
    if label:
        print("  학기      : %s" % label)
    print("  분석 구간 : %s ~ %s" % (rows[0]["ts"], rows[-1]["ts"]))
    print("  세션 수   : %d" % len(sessions))
    for s in sessions:
        print("   - %d차 %s~%s  TPS<=%s  RT<=%sms  ActiveTx<=%s"
              % (s["no"], s["start_hm"], s["end_hm"], fint(s["max_tps"]), fnum(s["max_rt"]), fint(s["max_atx"])))
    print("  피크 TPS  : %s @ %s" % (fint(kpis["peak_tps"]), kpis["peak_tps_hm"]))
    print("  피크 ATx  : %s @ %s" % (fint(kpis["peak_atx"]), kpis["peak_atx_hm"]))
    if prev:
        print("  지난 학기 대비 (%s → %s)" % (summary_name(prev), summary_name(summary)))
        for ln in compare_lines(kpis, prev["kpis"]):
            print(ln)
    for path in saved:
        print("  요약 저장 : %s" % path)


if __name__ == "__main__":
    main()
