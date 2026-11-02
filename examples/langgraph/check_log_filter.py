#!/usr/bin/env python3
"""Check what the demo showed of LangGraph's log against the full log (standard library only).

    python3 check_log_filter.py FULL_LOG [TRANSCRIPT]

FULL_LOG is the server log the demo saved (STEP 1 prints its path). TRANSCRIPT is the demo's terminal output, saved
for example with `bash examples/langgraph/demo.sh | tee demo.log`. The check
  1. sorts every line of the full log: shown by the demo, left out (a structured line the rule does not show), or
     not a structured line (blank, the start-up banner, a traceback, anything else);
  2. prints in full every left-out line at warning level or above, and every unstructured line that is neither
     banner nor traceback, so that nothing is passed over by its count alone;
  3. with a transcript, checks that the demo printed exactly the lines the rule picks from the full log, in order,
     at the times the log gives (on the transcript's clock).
It exits 1 if a warning or error was left out or the transcript differs, otherwise 0.
"""
import collections
import datetime
import difflib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import log_filter  # noqa: E402  the demo's own rule

BANNER = ('INFO:langgraph_api.cli:', 'Welcome to', '- ', 'This in-memory server', 'For production use')
EXCEPTION = re.compile(r'^[A-Za-z_][\w.]*(Error|Exception|Interrupt|Exit|Group)\b')
PRINTED = re.compile(r"^\s+(\d\d):(\d\d):(\d\d)  (LangGraph's log: .*?)(   \(between the steps\))?$")


def main() -> int:
    lines = open(sys.argv[1], encoding='utf-8', errors='replace').read().split('\n')
    kinds, listed, picked, in_traceback, in_entry = collections.Counter(), [], [], False, False
    for number, raw in enumerate(lines, 1):
        text = log_filter.ANSI.sub('', raw).rstrip()
        p = log_filter.parse(raw)
        if p:
            in_traceback, in_entry = False, not p[3]      # no logger on this line: the entry goes on below
            if log_filter.shows(p[1], p[2]):
                picked.append((number, log_filter.shown(raw)))
                kinds[('shown', p[1], p[2][:60])] += 1
            else:
                kinds[('left out', p[1], p[2][:60])] += 1
                if p[1] not in ('debug', 'info'):
                    listed.append((number, f'LEFT OUT {p[1]}', text))
            continue
        s = text.strip()
        if in_entry:
            kind, in_entry = 'rest of an entry above', not re.match(r'^\[[\w.]+\]', s)   # it ends at its [logger]
        elif not s:
            kind = 'blank'
        elif s.startswith(BANNER) or s[0] in '╦║╩':
            kind = 'banner'
        elif s.startswith('Traceback (most recent call last)'):
            kind, in_traceback = 'traceback', True
        elif in_traceback and (text[:1].isspace() or EXCEPTION.match(s)):
            kind = 'traceback'
        else:
            kind = 'other'
            listed.append((number, 'OTHER', text))
        kinds[(kind, '', '')] += 1

    print(f'{sys.argv[1]}: {len(lines)} lines')
    for c in ('shown', 'left out', 'rest of an entry above', 'banner', 'traceback', 'blank', 'other'):
        print(f'  {c:22} {sum(n for k, n in kinds.items() if k[0] == c):6d}')
    print('\nstructured lines by kind (class, level, message):')
    for (c, level, message), n in sorted(kinds.items(), key=lambda x: (x[0][0] != 'shown', -x[1])):
        if level:
            print(f'  {n:5d}  {c:9} {level:8} {message}')
    print('\nleft-out lines at warning level or above, and unstructured lines that are neither banner nor traceback:')
    for number, c, text in listed:
        print(f'  line {number:5d}  {c:16} {text[:200]}')
    if not listed:
        print('  none')
    failed = any(c.startswith('LEFT OUT') for _, c, _ in listed)

    if len(sys.argv) > 2:
        printed = [m for m in (PRINTED.match(r.rstrip('\n')) for r in open(sys.argv[2], encoding='utf-8',
                                                                           errors='replace')) if m]
        print(f"\ntranscript: {len(printed)} lines of LangGraph's log printed; the rule picks {len(picked)} from the full log")
        offsets, same = set(), 0
        texts = difflib.SequenceMatcher(a=[m.group(4) for m in printed], b=[w[1] for _, w in picked], autojunk=False)
        for tag, i1, i2, j1, j2 in texts.get_opcodes():
            if tag == 'equal':
                for m, (_, w) in zip(printed[i1:i2], picked[j1:j2]):
                    utc = datetime.datetime.fromtimestamp(w[0], datetime.timezone.utc)
                    seconds = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                    offsets.add((seconds - (utc.hour * 3600 + utc.minute * 60 + utc.second)) % 86400)
                    same += 1
                continue
            failed = True
            for m in printed[i1:i2]:
                print(f'  PRINTED BUT NOT IN THE FULL LOG: {m.group(4)}')
            for number, w in picked[j1:j2]:
                print(f'  IN THE FULL LOG BUT NOT PRINTED: {w[1]} (full-log line {number})')
        clock = ''
        if len(offsets) == 1 and next(iter(offsets)) % 900 == 0:
            hours = next(iter(offsets)) / 3600
            clock = f'; every time matches the log on a clock at UTC{"+" if hours <= 12 else "-"}{min(hours, 24 - hours):g}'
        elif offsets:
            failed = True
            clock = f'; TIMES DIFFER from the log (offsets {sorted(offsets)} s)'
        print(f'  identical text, in order: {same} of {max(len(printed), len(picked))}{clock}')
    print('\nRESULT:', 'FAILED' if failed else 'passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
