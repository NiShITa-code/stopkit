"""The reports (FORMAT.md section 3), generated from run records (stopkit-record/2) and their rows only.

  write(folders, out)    REPORT.md and REPORT.html (the same content), one timeline per run (timelines/*.svg),
                         rows.jsonl (every row of every run) and accuracy.json
  run_details(rec)       one run's own report (report.md in its run folder)

Every number is counted from rows or records; nothing is typed in.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from collections import Counter

from .. import __version__, spec
from ..score import accuracy
from . import results

KINDS = ('P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7')
# The architectures of the kit's bindings, as the reports name them.
ARCHITECTURES = ('A1 Linux host (scripted agent)', 'A2 MCP tool servers', 'A3 n8n queue mode', 'A4 LangGraph (dev server)',
                 'A5 Temporal', 'A6 Claude Code (real model)', 'A6b Codex CLI (real model)',
                 'A7 OpenClaw (stand-in model)', 'A8 Held-out (two systems, mentor picks)')

# ---- blocks: one content model, rendered as Markdown and as HTML ------------------------------------------


def _html_inline(text: str) -> str:
    out, pos = [], 0
    for m in re.finditer(r'\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`', text):
        out.append(html.escape(text[pos:m.start()]))
        if m.group(1) is not None:
            out.append(f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>')
        else:
            out.append(f'<code>{html.escape(m.group(3))}</code>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return ''.join(out)


def to_markdown(blocks: list) -> str:
    lines = []
    for b in blocks:
        kind = b[0]
        if kind in ('h1', 'h2', 'h3'):
            lines += ['#' * int(kind[1]) + ' ' + b[1], '']
        elif kind == 'p':
            lines += [b[1], '']
        elif kind == 'list':
            lines += [f'- {x}' for x in b[1]] + ['']
        elif kind == 'table':
            head, rows = b[1], b[2]
            lines.append('| ' + ' | '.join(head) + ' |')
            lines.append('|' + '---|' * len(head))
            for r in rows:
                lines.append('| ' + ' | '.join(str(c).replace('|', '\\|').replace('\n', ' ') for c in r) + ' |')
            lines.append('')
        elif kind == 'svg':
            lines += [f'![{b[3]}]({b[1]})', '', f'*{b[3]}*', '']
    return '\n'.join(lines).rstrip() + '\n'


CSS = """
:root{--surface-1:#fcfcfb;--page:#f9f9f7;--text-primary:#0b0b0b;--text-secondary:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--border:rgba(11,11,11,.10)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){--surface-1:#1a1a19;--page:#0d0d0d;
--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--series-1:#3987e5;--series-2:#d95926;
--series-3:#199e70;--border:rgba(255,255,255,.10)}}
:root[data-theme="dark"]{--surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#f0efec;--text-secondary:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--border:rgba(255,255,255,.10)}
body{background:var(--page);color:var(--text-primary);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
margin:0;padding:24px 16px}main{max-width:1100px;margin:0 auto}
h1{font-size:24px;margin:0 0 12px}h2{font-size:19px;margin:32px 0 8px}h3{font-size:16px;margin:20px 0 6px}
p,li{color:var(--text-primary)}code{font-size:13px}
.tw{overflow-x:auto;margin:8px 0 16px}table{border-collapse:collapse;background:var(--surface-1);font-size:13px}
th,td{border:1px solid var(--border);padding:4px 8px;text-align:left;vertical-align:top;font-variant-numeric:tabular-nums}
th{color:var(--text-secondary);font-weight:600}figure{margin:12px 0 24px;overflow-x:auto}
figcaption{color:var(--text-secondary);font-size:13px}a{color:var(--series-1)}
svg .bg{fill:var(--surface-1)}svg .ink{fill:var(--text-primary)}svg .ink2{fill:var(--text-secondary)}
svg .mut{fill:var(--muted)}svg .grid{stroke:var(--grid)}svg .axis{stroke:var(--axis)}
svg .band{fill:var(--grid);opacity:.55}svg .alive{fill:var(--series-1)}svg .fire{fill:var(--series-1);stroke:var(--surface-1);stroke-width:2}
svg .eff{stroke:var(--series-2);stroke-width:2}svg .kitp{fill:var(--series-3);stroke:var(--surface-1);stroke-width:2}
svg .kita{fill:var(--surface-1);stroke:var(--series-3);stroke-width:2}svg .kitu{fill:var(--surface-1);stroke:var(--muted);stroke-width:2}
svg .vline{stroke:var(--text-primary);stroke-width:1}svg .vline2{stroke:var(--text-secondary);stroke-width:1}
"""


def to_html(blocks: list, title: str) -> str:
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           f'<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>']
    for b in blocks:
        kind = b[0]
        if kind in ('h1', 'h2', 'h3'):
            out.append(f'<{kind}>{_html_inline(b[1])}</{kind}>')
        elif kind == 'p':
            out.append(f'<p>{_html_inline(b[1])}</p>')
        elif kind == 'list':
            out.append('<ul>' + ''.join(f'<li>{_html_inline(x)}</li>' for x in b[1]) + '</ul>')
        elif kind == 'table':
            head, rows = b[1], b[2]
            out.append('<div class="tw"><table><thead><tr>' + ''.join(f'<th>{_html_inline(h)}</th>' for h in head)
                       + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{_html_inline(str(c))}</td>' for c in r)
                                                          + '</tr>' for r in rows) + '</tbody></table></div>')
        elif kind == 'svg':
            out.append(f'<figure>{b[2]}<figcaption>{_html_inline(b[3])}</figcaption></figure>')
    out.append('</main></body></html>')
    return '\n'.join(out) + '\n'


# ---- one run ------------------------------------------------------------------------------------------------


def _scope_text(rec: dict) -> str:
    s = rec['stop_spec']
    st = ('every structure (the whole agent)' if '*' in s['structures']
          else ', '.join(s['structures']) or 'nothing (a control)')
    src = s['scope_source'] + (f" ([source]({s['scope_doc']}))" if s.get('scope_doc') else '')
    return f'{st}; taken from its {src}'


def _result_text(sr: dict) -> str:
    q = f" ({'; '.join(sr['qualifiers'])})" if sr.get('qualifiers') else ''
    return sr['in_scope'] + q


def run_details(rec: dict) -> str:
    return to_markdown(run_blocks(rec))


def run_blocks(rec: dict, link_prefix: str = '') -> list:
    s, t, claim = rec['stop_spec'], rec.get('timing') or {}, rec.get('claim') or {}
    sr = results.scope_result(results.rows(rec)) if rec.get('items') else {}
    tr = rec.get('truth') or {}
    plat = rec['system']['platform']
    b: list = [('h1', f"Run {rec['run_id']}: {rec['system']['architecture']}, stop `{s['id']}`"),
               ('p', f"{s['mechanism'].capitalize()}. Intended scope: {_scope_text(rec)}. Report kind: {s['report_kind']}."),
               ('list', [
                   f"stopkit {rec['kit_version']}, specification {rec['spec_version']}, commit "
                   f"`{rec.get('git_commit') or 'not recorded'}`"
                   + (' (with uncommitted changes)' if rec.get('git_dirty') not in (None, '0') else ''),
                   f"{plat.get('os')}; Linux {plat.get('kernel')}; systemd {plat.get('systemd')}; WSL: "
                   f"{'yes' if plat.get('wsl') else 'no'}; Python {plat.get('python')}",
                   f"Scenario `{rec['system']['scenario']}`, binding `{rec['system']['binding']}`; checks at "
                   f"{', '.join(f'{x:g} s' for x in rec['system']['configuration']['checks_s'])} after the cut-off",
                   f"The stop returned {t.get('stop_returned_rel_s', 0) or 0:.3f} s after the cut-off; the restart "
                   f"action began at {t.get('restart_action_rel_s') or 0:.1f} s; grace {t.get('grace_s')} s",
                   f"What the stop reported, verbatim: \"{claim.get('message', '')}\" (said stopped: "
                   f"{claim.get('said_stopped')}; report kind: {claim.get('report')})",
                   f"Record: [{link_prefix}record.json]({link_prefix}record.json); raw logs "
                   f"[truth.jsonl]({link_prefix}truth.jsonl) and [effects.jsonl]({link_prefix}effects.jsonl)"]),
               ('h2', 'Result'),
               ('list', [f"In-scope result: **{_result_text(sr)}**" if sr else 'In-scope result: not scored',
                         'Went on in scope: ' + ('; '.join(sr.get('continued') or []) or 'nothing'),
                         'Out-of-scope remainder: ' + ('; '.join(sr.get('remainder') or []) or 'nothing'),
                         'Whole-agent result: ' + (sr.get('whole_agent') or 'not computed (the stop is not whole-agent)'),
                         f"Report truthfulness: {rec['truthfulness']['label']}: {rec['truthfulness']['why']}",
                         f"Ground truth: {tr.get('status')}" + (f" ({tr.get('why')})" if tr.get('why') else '')])]
    cps = [c['name'] for c in rec['checkpoints']]
    rows = []
    for it in rec.get('items', ()):
        tv = ((tr.get('items') or {}).get(it['id']) or {}).get(it['kind']) or {}
        acted = it.get('acted_after') or {}
        acted_txt = (f"counted on {acted['counted_on']}" if 'counted_on' in acted else
                     'not observed' if acted.get('count') is None else
                     f"{acted['count']}" + (f" (last {acted['last_s']} s)" if acted.get('last_s') is not None else ''))
        rows.append([it['id'], it['kind'], it['structure'], 'yes' if it['in_scope'] else 'no',
                     ' · '.join(it['readings'][c][0] for c in cps),
                     results.verdict_word(it['kind'], it['verdict'], it['restart_confirmed']),
                     acted_txt,
                     ' · '.join((tv.get('readings') or {}).get(c, '?')[0] for c in cps),
                     (tv.get('verdict') or '—') if tv.get('definite') else 'indefinite',
                     tv.get('effects_after', '—')])
    b += [('h2', 'Paths'),
          ('p', f"Readings at {', '.join(cps)}: p present, a absent, u unknown; truth readings: p, a, i indefinite. "
                "Acted after: effects the sink received, stamped after the cut-off plus grace."),
          ('table', ['Item', 'Kind', 'Structure', 'In scope', 'Kit readings', 'Kit verdict', 'Acted after',
                     'Truth readings', 'Truth verdict', 'Truth effects after'], rows)]
    cov = rec.get('coverage') or {}
    srows = [[s, ', '.join(c for c, v in d['proven'].items() if v is True) or 'none',
              '; '.join(f'{c}: {v}' for c, v in d['proven'].items() if v is not True) or '—'] for s, d in
             sorted((cov.get('surfaces') or {}).items())]
    b += [('h2', 'Coverage'), ('table', ['Surface', 'Proven at', 'Not proven'], srows),
          ('p', 'Kinds of work not tested in this run: ' + (', '.join(
              f"{k} ({v['status']})" for k, v in (cov.get('kinds') or {}).items() if v['status'] != 'tested') or 'none') + '.')]
    probs = list(rec.get('errors') or []) + [f'clean-up left: {x}' for x in (rec.get('cleanup') or {}).get('remaining', [])]
    b += [('h2', 'Problems'), ('list', probs or ['none'])]
    return b


# ---- timelines ---------------------------------------------------------------------------------------------


def _nice_step(span: float) -> float:
    for s in (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120):
        if span / s <= 10:
            return s
    return 300


def timeline_svg(rec: dict, lo: float | None = None, hi: float | None = None) -> str:
    """One run on one time axis, in seconds from the cut-off: per item, the process lives and trigger fires the
    truth recorded (blue), the last effect the sink received after the stop (orange), and the kit's reading at
    each checkpoint (aqua: filled present, hollow absent, gray diamond unknown)."""
    t = rec['timing']
    cut = t['cut_off_mono']
    tr = (rec.get('truth') or {}).get('items') or {}
    items = []
    for it in rec['items']:
        if it['id'] not in [x[0] for x in items]:
            items.append((it['id'], [i for i in rec['items'] if i['id'] == it['id']]))
    lo = ((t['plant_mono'] - cut) - 2) if lo is None else lo
    hi = ((t['end_mono'] - cut) + 2) if hi is None else hi
    W, left, right, top, lane = 1000, 210, 16, 64, 30
    bottom = top + lane * len(items)
    H = bottom + 66
    x = (lambda s: left + (min(max(s, lo), hi) - lo) / (hi - lo) * (W - left - right))
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
         f'font-family="system-ui,-apple-system,Segoe UI,sans-serif" font-size="12">',
         f'<title>Timeline of run {html.escape(rec["run_id"])}, stop {html.escape(rec["stop_spec"]["id"])}</title>',
         f'<rect class="bg" x="0" y="0" width="{W}" height="{H}"/>']
    marks: list[list[tuple[float, float]]] = [[], [], []]     # label rows: two for events, one for checks

    def place(row_ids, x0: float, text: str):
        """The first label row where text fits beside what is there, or None (the label is then left to the
        mark's own tooltip)."""
        w = 6.5 * len(text)
        for i in row_ids:
            if all(x0 > e + 6 or x0 + w < s - 6 for s, e in marks[i]):
                marks[i].append((x0, x0 + w))
                return i
        return None

    step = _nice_step(hi - lo)
    tick = step * int(lo / step)
    while tick <= hi:
        if tick >= lo:
            o.append(f'<line class="grid" x1="{x(tick):.1f}" x2="{x(tick):.1f}" y1="{top - 6}" y2="{bottom}"/>')
            o.append(f'<text class="mut" x="{x(tick):.1f}" y="{bottom + 16}" text-anchor="middle">{tick:g}</text>')
        tick = round(tick + step, 6)
    o.append(f'<text class="mut" x="{W - right}" y="{bottom + 32}" text-anchor="end">seconds from the cut-off</text>')
    for label, at, cls in (('cut-off', 0.0, 'vline'), ('returned', t.get('stop_returned_rel_s'), 'vline2'),
                           ('restart', t.get('restart_action_rel_s'), 'vline2')):
        if at is None or not lo <= at <= hi:
            continue
        row = place((0, 1), x(at) + 3, label)
        o.append(f'<line class="{cls}" x1="{x(at):.1f}" x2="{x(at):.1f}" y1="{(row or 0) * 16 + 4}" y2="{bottom}">'
                 f'<title>{label}: {at:.3f} s</title></line>')
        if row is not None:
            o.append(f'<text class="ink" x="{x(at) + 3:.1f}" y="{row * 16 + 14}">{label}</text>')
    for c in rec['checkpoints']:
        ws = list((c.get('surface_windows_mono') or {}).values())
        if not ws:
            continue
        a, b2 = min(w[0] for w in ws) - cut, max(w[1] for w in ws) - cut
        if b2 < lo or a > hi:
            continue
        o.append(f'<rect class="band" x="{x(a):.1f}" y="{top - 6}" width="{max(2.0, x(b2) - x(a)):.1f}" '
                 f'height="{bottom - top + 6}"><title>{html.escape(c["name"])} check: {a:.2f} to {b2:.2f} s</title></rect>')
        if place((2,), x(a), c['name']) is not None:
            o.append(f'<text class="ink2" x="{x(a):.1f}" y="{top - 12}">{html.escape(c["name"])}</text>')
    for n, (iid, paths) in enumerate(items):
        y = top + n * lane + lane / 2
        kinds = ', '.join(p['kind'] for p in paths)
        o.append(f'<text class="ink" x="8" y="{y + 4:.1f}">{html.escape(iid)} ({kinds})</text>')
        o.append(f'<line class="grid" x1="{left}" x2="{W - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        for p in paths:
            tv = (tr.get(iid) or {}).get(p['kind']) or {}
            for since, ended, watched in tv.get('instances_s', []):
                end = hi if ended is None else ended
                if end < lo or since > hi:
                    continue
                o.append(f'<rect class="alive" x="{x(since):.1f}" y="{y - 4:.1f}" width="{max(2.0, x(end) - x(since)):.1f}" '
                         f'height="8" rx="4"><title>{html.escape(iid)}: process alive from {since:.2f} s to '
                         f'{"the end" if ended is None else f"{ended:.3f} s"}</title></rect>')
            for f in tv.get('fires_s', []):
                if lo <= f <= hi:
                    o.append(f'<circle class="fire" cx="{x(f):.1f}" cy="{y:.1f}" r="5"><title>{html.escape(iid)}: fired at '
                             f'{f:.2f} s</title></circle>')
            acted = p.get('acted_after') or {}
            if acted.get('last_s') is not None and lo <= acted['last_s'] <= hi:
                xa = x(acted['last_s'])
                o.append(f'<line class="eff" x1="{xa:.1f}" x2="{xa:.1f}" y1="{y - 9:.1f}" y2="{y + 9:.1f}"><title>'
                         f'{html.escape(iid)}: last effect the sink received, {acted["last_s"]} s ({acted["count"]} after '
                         f'the cut-off plus grace)</title></line>')
            for c in rec['checkpoints']:
                ws = [c['surface_windows_mono'][s] for s in p['surfaces'] if s in (c.get('surface_windows_mono') or {})]
                if not ws:
                    continue
                mid = (min(w[0] for w in ws) + max(w[1] for w in ws)) / 2 - cut
                if not lo <= mid <= hi:
                    continue
                r = p['readings'][c['name']]
                tip = f'<title>{html.escape(iid)}/{p["kind"]} at {html.escape(c["name"])}: kit read {r}</title>'
                cy = y + (7 if p['kind'] == 'P4' and len(paths) > 1 else 0)
                if r == 'present':
                    o.append(f'<circle class="kitp" cx="{x(mid):.1f}" cy="{cy:.1f}" r="5">{tip}</circle>')
                elif r == 'absent':
                    o.append(f'<circle class="kita" cx="{x(mid):.1f}" cy="{cy:.1f}" r="4">{tip}</circle>')
                else:
                    xm = x(mid)
                    o.append(f'<path class="kitu" d="M{xm:.1f} {cy - 5:.1f}L{xm + 5:.1f} {cy:.1f}L{xm:.1f} {cy + 5:.1f}'
                             f'L{xm - 5:.1f} {cy:.1f}Z">{tip}</path>')
    ly = bottom + 42
    legend = [('alive', 'rect', 'process alive (truth)'), ('fire', 'circle', 'trigger fired (truth)'),
              ('eff', 'line', 'last effect after the stop (sink)'), ('kitp', 'circle', 'kit: present'),
              ('kita', 'circle', 'kit: absent'), ('kitu', 'path', 'kit: unknown')]
    lx = 8
    for cls, shape, text in legend:
        if shape == 'rect':
            o.append(f'<rect class="{cls}" x="{lx}" y="{ly + 4}" width="16" height="8" rx="4"/>')
        elif shape == 'circle':
            o.append(f'<circle class="{cls}" cx="{lx + 8}" cy="{ly + 8}" r="5"/>')
        elif shape == 'line':
            o.append(f'<line class="{cls}" x1="{lx + 8}" x2="{lx + 8}" y1="{ly}" y2="{ly + 16}"/>')
        else:
            o.append(f'<path class="{cls}" d="M{lx + 8} {ly + 3}L{lx + 13} {ly + 8}L{lx + 8} {ly + 13}L{lx + 3} {ly + 8}Z"/>')
        o.append(f'<text class="ink2" x="{lx + 22}" y="{ly + 12}">{text}</text>')
        lx += 22 + int(6.5 * len(text)) + 18
    o.append('</svg>')
    return '\n'.join(o)


def _standalone(svg: str) -> str:
    """An SVG file that carries its own colors (light, and dark when the viewer prefers it)."""
    style = re.sub(r':root:where\(:not\(\[data-theme="light"\]\)\)', ':root', CSS.split('body{')[0])
    style = style.replace(':root[data-theme="dark"]', ':root.dark')
    return svg.replace('<rect class="bg"', f'<style>{style}{CSS[CSS.index("svg .bg"):]}</style><rect class="bg"', 1)


# ---- the report over many runs --------------------------------------------------------------------------------


def collect(folders: list[str]) -> list[dict]:
    """Every stopkit-record/2 under the given batch or run folders, in the order the runs started."""
    recs = []
    for d in folders:
        for root, _dirs, files in os.walk(d):
            if 'record.json' in files:
                with open(os.path.join(root, 'record.json'), encoding='utf-8') as f:
                    rec = json.load(f)
                if rec.get('format') == 'stopkit-record/2':
                    spec.register_from_record(rec)
                    rec['_dir'] = root
                    recs.append(rec)
    return sorted(recs, key=lambda r: (r.get('started') or 0))


def _cell(rows: list[dict]) -> str:
    if not rows:
        return '—'
    parts = []
    for side, label in ((True, 'in scope'), (False, 'out of scope')):
        words = Counter(r['verdict_word'] for r in rows if bool(r['in_scope']) == side)
        if words:
            acted = sum(1 for r in rows if bool(r['in_scope']) == side and (r['acted_after'] or 0) > 0)
            parts.append(f'{label}: ' + ', '.join(f'{n} {w}' for w, n in sorted(words.items()))
                         + (f'; {acted} acted after the stop' if acted else ''))
    return ' / '.join(parts)


def report_blocks(recs: list[dict], rows: list[dict], out: str, svgs: dict) -> list:
    when = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    runs = len(recs)
    commits = sorted({r.get('git_commit') or 'not recorded' for r in recs})
    plats = sorted({f"{r['system']['platform'].get('os')}, Linux {r['system']['platform'].get('kernel')}"
                    + (' (WSL)' if r['system']['platform'].get('wsl') else '') for r in recs})
    b: list = [('h1', 'Shutdown verification report'),
               ('p', f"Generated {when} by stopkit {__version__} (specification {spec.version()}) from {runs} "
                     f"run{'s' if runs != 1 else ''} and their {len(rows)} rows ([rows.jsonl](rows.jsonl)). "
                     f"Commits: {', '.join(f'`{c[:10]}`' for c in commits)}. Platforms: {'; '.join(plats)}."),
               ('p', 'Status of what is shown: every binding here is implemented (tier 1), not validated. '
                     'Results are single runs on one machine; no rate, accuracy bound or generality is claimed from them. '
                     'Each stop is judged against its own intended scope; work outside that scope is listed as remainder, '
                     'not as a failure.')]
    scope = {rid: results.scope_result([r for r in rows if r['run_id'] == rid]) for rid in {r['run_id'] for r in rows}}
    # 1. reach matrix
    b.append(('h2', '1. Shutdown reach matrix'))
    b.append(('p', 'Per architecture: each stop (rows) against each kind of work (columns). A cell counts the planted '
                   'paths by their verdict, split by whether the stop\'s intended scope covers them, with the runs '
                   'behind it. A dash means no planted path of that kind; the coverage section says why.'))
    for arch in sorted({r['architecture'] for r in rows}):
        ar = [r for r in rows if r['architecture'] == arch]
        stops_run = [s for s in spec.stops() if any(r['stop_id'] == s for r in ar)]
        table = []
        for sid in stops_run:
            sr = [r for r in ar if r['stop_id'] == sid]
            nruns = len({r['run_id'] for r in sr})
            scope_res = Counter(scope[rec['run_id']]['in_scope'] for rec in recs
                                if rec['stop_spec']['id'] == sid and rec['run_id'] in scope)
            table.append([f'`{sid}` ({nruns} run{"s" if nruns != 1 else ""})',
                          ', '.join(f'{n} {w}' for w, n in sorted(scope_res.items()))]
                         + [_cell([r for r in sr if r['kind'] == k]) for k in KINDS])
        b += [('h3', arch), ('table', ['Stop (runs)', 'In-scope result'] + list(KINDS), table)]
    # 2. run details
    b.append(('h2', '2. Run details'))
    dt = []
    for rec in recs:
        rel = os.path.relpath(rec['_dir'], out).replace(os.sep, '/')
        sr = scope.get(rec['run_id']) or {}
        claim = rec.get('claim') or {}
        msg = (claim.get('message') or '')[:90]
        dt.append([f"[{rec['run_id']}]({rel}/report.md)", f"`{rec['stop_spec']['id']}`", f'"{msg}"',
                   rec['truthfulness']['label'], _result_text(sr) if sr else '—',
                   str(len(sr.get('remainder') or [])), f"{(rec['timing'].get('stop_returned_rel_s') or 0):.3f}",
                   (rec.get('truth') or {}).get('status', '—'), f"[record]({rel}/record.json)",
                   f"[timeline](timelines/{rec['run_id']}.svg)" if rec['run_id'] in svgs else '—'])
    b.append(('table', ['Run', 'Stop', 'What the stop reported', 'Report truthfulness', 'In-scope result',
                        'Remainder paths', 'Stop took (s)', 'Truth', 'Record', 'Timeline'], dt))
    # 3. accuracy
    rp = [p for rec in recs for p in accuracy.reading_pairs(rec)]
    vp = [p for rec in recs for p in accuracy.verdict_pairs(rec)]
    ep = [p for rec in recs for p in accuracy.effect_pairs(rec)]
    cats = spec.verdict_rules()['accuracy']['categories']
    b.append(('h2', '3. Accuracy against ground truth'))
    b.append(('p', 'The kit\'s readings and verdicts against the sealed recorder\'s, at the same moments '
                   '(spec/verdicts.json). Indefinite truth is counted, never scored. These are counts from the runs '
                   'above, not rates.'))
    order = ['agree', 'false absent', 'false present', 'false ended', 'false active', 'kit unknown', 'indefinite',
             'invalid']
    tr_all, tr_kind = accuracy.tally(rp), accuracy.tally(rp, 'kind')
    b.append(('h3', 'Readings (each path at each checkpoint)'))
    b.append(('table', ['Kind'] + [c for c in order if c in set(tr_all) | {'agree', 'false absent', 'false present'}],
              [[k] + [str(tr_kind[k].get(c, 0)) for c in order if c in set(tr_all) | {'agree', 'false absent', 'false present'}]
               for k in tr_kind] + [['all'] + [str(tr_all.get(c, 0)) for c in order
                                              if c in set(tr_all) | {'agree', 'false absent', 'false present'}]]))
    tv_all, tv_kind = accuracy.tally(vp), accuracy.tally(vp, 'kind')
    vcols = [c for c in order if c in set(tv_all) | {'agree', 'false ended', 'false active'}]
    b.append(('h3', 'Verdicts (each path, where the truth verdict is definite)'))
    b.append(('table', ['Kind'] + vcols, [[k] + [str(tv_kind[k].get(c, 0)) for c in vcols] for k in tv_kind]
              + [['all'] + [str(tv_all.get(c, 0)) for c in vcols]]))
    b.append(('h3', 'Effects after the stop (sink against truth)'))
    b.append(('table', ['Category', 'Paths'], [[c, str(n)] for c, n in sorted(accuracy.tally(ep).items())]))
    disagreements = [p for p in rp + vp if p['category'] not in ('agree', 'indefinite', 'kit unknown', 'invalid')]
    b.append(('list', [f"{p['run']} {p['item']}/{p['kind']}" + (f" at {p['checkpoint']}" if 'checkpoint' in p else '')
                       + f": kit {p['kit']}, truth {p['truth']} ({p['category']})" for p in disagreements]
              or ['No disagreement between the kit and the truth where the truth is definite.']))
    b.append(('p', 'Categories: ' + '; '.join(f'{k}: {v}' for k, v in cats.items() if k in set(tr_all) | set(tv_all)) + '.'))
    # 4. timelines
    b.append(('h2', '4. Timelines'))
    for rec in recs:
        if rec['run_id'] in svgs:
            b.append(('svg', f"timelines/{rec['run_id']}.svg", svgs[rec['run_id']],
                      f"Run {rec['run_id']}, stop `{rec['stop_spec']['id']}`: the whole run, in seconds from the cut-off."))
            if rec['run_id'] + '-zoom' in svgs:
                b.append(('svg', f"timelines/{rec['run_id']}-zoom.svg", svgs[rec['run_id'] + '-zoom'],
                          f"Run {rec['run_id']}: the first seconds after the cut-off."))
    # 5. coverage and limitations
    b += coverage_blocks(recs, rows)
    return b


def coverage_blocks(recs: list[dict], rows: list[dict]) -> list:
    b: list = [('h2', '5. Coverage and limitations')]
    kinds = spec.kinds()['kinds']
    status: dict = {}
    for rec in recs:
        for k, v in (rec.get('coverage') or {}).get('kinds', {}).items():
            status.setdefault(k, Counter())[v['status']] += 1
    b.append(('table', ['Kind of work', 'Family', 'In these runs'],
              [[f"{k} {kinds[k]['name']}", kinds[k]['family'],
                ', '.join(f'{s} ({n} runs)' for s, n in status.get(k, Counter()).items()) or 'no run'] for k in KINDS]))
    unproven: Counter = Counter()
    listed: Counter = Counter()
    for rec in recs:
        for s, d in (rec.get('coverage') or {}).get('surfaces', {}).items():
            for v in d['proven'].values():
                listed[s] += 1
                unproven[s] += v is not True
    surf = spec.surfaces()
    b.append(('table', ['Surface', 'Listings proven', 'What it cannot see'],
              [[s, f'{listed[s] - unproven[s]} of {listed[s]}', '; '.join(surf[s].blind_spots)] for s in sorted(listed)]))
    run_stops = {r['stop_id'] for r in rows}
    reg = spec.stops()
    ran = sorted({r['architecture'] for r in rows})
    ran_ids = {a.split(' ', 1)[0] for a in ran}          # a binding may name its architecture more exactly
    bindings = sorted({(rec.get('system') or {}).get('binding') for rec in recs} - {None})
    b.append(('list', [
        'Architectures run here: ' + (', '.join(ran) or 'none') + '. Not run in this report: '
        + ', '.join(a for a in ARCHITECTURES if a.split(' ', 1)[0] not in ran_ids) + '.',
        'Stops in the register not run here: ' + ', '.join(f'`{s}`' + ('' if v.implemented else ' (not implemented)')
                                                         for s, v in reg.items() if s not in run_stops) + '.',
        'Every path is work the kit planted through the system\'s own paths: harmless, labeled and reporting to '
        'the truth. Bindings in these runs: ' + (', '.join(f'`{x}`' for x in bindings) or 'none')
        + ' (README.md says what each runs).',
        *_not_planted(recs),
        'One machine. Runs in WSL are for development; scored results are made on a fresh VM.',
        'Indefinite truth: ' + _indefinite_share(recs) + '.']))
    return b


def _not_planted(recs: list[dict]) -> list[str]:
    """For agents from config files: the catalogue items their files could not plant, and why."""
    out = {}
    for rec in recs:
        system = rec.get('system') or {}
        for item, why in ((system.get('configuration') or {}).get('not_planted') or {}).items():
            out.setdefault((system.get('binding'), item), why)
    return [f'Not planted for `{binding}`: ' + '; '.join(f'{item} ({why})' for (b, item), why in sorted(out.items())
                                                         if b == binding) + '.'
            for binding in sorted({b for b, _ in out})]


ORACLE_NAMES = {'pidfd': 'processes (pidfd)', 'fire': 'triggers (fire)', 'beats': 'work inside a process (beats)',
                'holder-log': 'work at a holder (holder-log)'}


def _indefinite_share(recs: list[dict]) -> str:
    """How many truth readings were indefinite, by the oracle that read them."""
    total, by = 0, Counter()
    for rec in recs:
        oracle = {(p['item'], p['kind']): (p.get('truth') or {}).get('oracle') for p in rec.get('paths', ())}
        for p in accuracy.reading_pairs(rec):
            total += 1
            if p['category'] == 'indefinite':
                by[oracle.get((p['item'], p['kind'])) or 'paths whose record names no oracle'] += 1
    if not total:
        return 'no truth readings'
    n = sum(by.values())
    parts = '; '.join(f'{c} of {ORACLE_NAMES.get(o, o)}' for o, c in by.most_common())
    return (f'{n} of {total} truth readings ({100 * n / total:.0f}%)' + (f': {parts}' if parts else '')
            + '. The truth leaves a reading indefinite when its log cannot settle it: a trigger\'s absence before a '
            'full firing window has passed, work that ended without reporting its end, or a start or an end inside '
            'the window')


def write(folders: list[str], out: str) -> dict:
    """Generate the report for every record under `folders` into `out`. Returns what was written."""
    recs = collect(folders)
    if not recs:
        raise ValueError(f'no stopkit-record/2 runs under {", ".join(folders)}')
    os.makedirs(os.path.join(out, 'timelines'), exist_ok=True)
    rows = []
    for rec in recs:
        rows += results.rows(rec, results.relpath(os.path.join(rec['_dir'], 'record.json'), out))
    svgs = {}
    for rec in recs:
        if rec.get('items') and (rec.get('timing') or {}).get('cut_off_mono') is not None:
            svgs[rec['run_id']] = timeline_svg(rec)
            hi = max(3.0, (rec['timing'].get('stop_returned_rel_s') or 0) + 1.5)
            svgs[rec['run_id'] + '-zoom'] = timeline_svg(rec, -0.5, hi)
    for name, svg in svgs.items():
        with open(os.path.join(out, 'timelines', f'{name}.svg'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(_standalone(svg) + '\n')
    with open(os.path.join(out, 'rows.jsonl'), 'w', encoding='utf-8', newline='\n') as f:
        for r in rows:
            f.write(json.dumps({'rows_format': results.ROWS_FORMAT, **r}, sort_keys=True) + '\n')
    acc = {'readings': [p for rec in recs for p in accuracy.reading_pairs(rec)],
           'verdicts': [p for rec in recs for p in accuracy.verdict_pairs(rec)],
           'effects': [p for rec in recs for p in accuracy.effect_pairs(rec)]}
    with open(os.path.join(out, 'accuracy.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(acc, f, indent=1, sort_keys=True)
        f.write('\n')
    blocks = report_blocks(recs, rows, out, svgs)
    with open(os.path.join(out, 'REPORT.md'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(to_markdown(blocks))
    with open(os.path.join(out, 'REPORT.html'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(to_html(blocks, 'Shutdown verification report'))
    return {'runs': len(recs), 'rows': len(rows), 'timelines': len(svgs), 'out': out}
