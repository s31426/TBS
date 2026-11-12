#!/usr/bin/env python3
"""Agent-side LLD for Linux Optimizer Monitoring.

Reads /etc/zabbix/monitoring.json (JSON or JSONC with // and /* */ comments).
"""
import json
import re
import sys
from pathlib import Path

CONFIG = Path('/etc/zabbix/monitoring.json')
MODES = {
    'monitoring',
    'instances',
    'groups',
    'ports',
    'received',
    'received_group',
}

# Fallbacks when a field is present but problem_after is omitted.
DEFAULT_SENT_AFTER = '1h'
DEFAULT_LOG_AFTER = '1h'          # received + device_query (poll_error)
DEFAULT_PING_AFTER = '15m'
DEFAULT_PORT_AFTER = '15m'


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments outside of strings (JSONC → JSON)."""
    out = []
    i = 0
    n = len(text)
    in_str = False
    esc = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == '/' and i + 1 < n:
            nxt = text[i + 1]
            if nxt == '/':
                i += 2
                while i < n and text[i] not in '\r\n':
                    i += 1
                continue
            if nxt == '*':
                i += 2
                while i + 1 < n and not (text[i] == '*' and text[i + 1] == '/'):
                    i += 1
                i = min(i + 2, n)
                continue
        out.append(ch)
        i += 1
    return ''.join(out)


def esc_regex(value: str) -> str:
    return re.sub(r'([\\.^$|()\[\]{}*+?])', r'\\\1', str(value))


def truthy(v, default=True):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).lower() not in ('0', 'false', 'no', 'off')


def section_enabled(section, default_when_present=True):
    """Missing section (None) => disabled. Present => honour enabled (default True)."""
    if section is None:
        return False
    return truthy(section.get('enabled'), default_when_present)


def log_sections(group: dict):
    """Return (received_cfg_or_None, device_query_cfg_or_None).

    Preferred key: log.device_query (Odpytanie fizycznego urządzenia...).
    Legacy alias: log.poll_error
    """
    log_cfg = group.get('log')
    if not isinstance(log_cfg, dict):
        return None, None
    received = log_cfg.get('received')
    query = log_cfg.get('device_query')
    if query is None:
        query = log_cfg.get('poll_error')
    return received, query


def optimizer_rows(o):
    oid = str(o.get('id', ''))
    oname = str(o.get('name') or oid)
    log = str(o.get('log', ''))
    sent = (o.get('log_monitoring') or {}).get('sent') or {}
    sent_enabled = truthy(sent.get('enabled'), True)
    sent_after = str(sent.get('problem_after') or DEFAULT_SENT_AFTER)
    if not oid or not log:
        return []

    rows = [{
        '{#TYPE}': 'optimizer',
        '{#OPTIMIZER}': oid,
        '{#OPTIMIZER_NAME}': oname,
        '{#LOG_FILE}': log,
        '{#SENT_ENABLED}': 1 if sent_enabled else 0,
        '{#SENT_AFTER}': sent_after,
    }]

    for g in o.get('groups', []):
        gid = str(g.get('id', ''))
        gname = str(g.get('name') or gid)
        gtype = str(g.get('type', ''))
        address = str(g.get('address', ''))
        series = [str(s) for s in (g.get('series') or []) if str(s)]
        ping = g.get('ping') or {}
        rec, query = log_sections(g)
        poll_enabled = section_enabled(query, True)
        rec_enabled = section_enabled(rec, True)

        # Physical group row: ping + device_query (Odpytanie...) item/trigger.
        if gtype == 'device' and address:
            rows.append({
                '{#TYPE}': 'group',
                '{#OPTIMIZER}': oid,
                '{#OPTIMIZER_NAME}': oname,
                '{#GROUP}': gid,
                '{#GROUP_NAME}': gname,
                '{#LOG_FILE}': log,
                '{#IP}': address,
                '{#PING_ENABLED}': 1 if truthy(ping.get('enabled'), True) else 0,
                '{#PING_AFTER}': str(ping.get('problem_after') or DEFAULT_PING_AFTER),
                '{#POLL_ENABLED}': 1 if poll_enabled else 0,
                '{#POLL_AFTER}': str((query or {}).get('problem_after') or DEFAULT_LOG_AFTER),
                '{#POLL_SERIES_REGEX}': '|'.join(esc_regex(s) for s in series) or '(?!)',
                '{#SENT_ENABLED}': 1 if sent_enabled else 0,
                '{#SENT_AFTER}': sent_after,
            })

        if address:
            for p in g.get('ports') or []:
                if not truthy(p.get('enabled'), True):
                    continue
                port = p.get('port')
                if port in (None, ''):
                    continue
                pid = str(p.get('id') or port)
                rows.append({
                    '{#TYPE}': 'port',
                    '{#OPTIMIZER}': oid,
                    '{#OPTIMIZER_NAME}': oname,
                    '{#GROUP}': gid,
                    '{#GROUP_NAME}': gname,
                    '{#IP}': address,
                    '{#PORT_ID}': pid,
                    '{#PORT_NAME}': str(p.get('name') or pid),
                    '{#PORT}': str(port),
                    '{#PORT_AFTER}': str(p.get('problem_after') or DEFAULT_PORT_AFTER),
                })

        if rec_enabled and series:
            rec_after = str((rec or {}).get('problem_after') or DEFAULT_LOG_AFTER)
            rec_mode = str((rec or {}).get('mode') or 'each').lower()
            if rec_mode in ('group', 'all', 'combined'):
                rows.append({
                    '{#TYPE}': 'received_group',
                    '{#OPTIMIZER}': oid,
                    '{#OPTIMIZER_NAME}': oname,
                    '{#GROUP}': gid,
                    '{#GROUP_NAME}': gname,
                    '{#LOG_FILE}': log,
                    '{#RECEIVED_SERIES_REGEX}': '|'.join(esc_regex(s) for s in series),
                    '{#RECEIVED_AFTER}': rec_after,
                    '{#SENT_ENABLED}': 1 if sent_enabled else 0,
                    '{#SENT_AFTER}': sent_after,
                })
            else:
                # mode=each (default): jeden item/trigger na serię
                for s in series:
                    rows.append({
                        '{#TYPE}': 'series',
                        '{#OPTIMIZER}': oid,
                        '{#OPTIMIZER_NAME}': oname,
                        '{#GROUP}': gid,
                        '{#GROUP_NAME}': gname,
                        '{#LOG_FILE}': log,
                        '{#SERIES}': s,
                        '{#SERIES_REGEX}': esc_regex(s),
                        '{#RECEIVED_AFTER}': rec_after,
                        '{#SENT_ENABLED}': 1 if sent_enabled else 0,
                        '{#SENT_AFTER}': sent_after,
                    })
    return rows


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in MODES:
        print(
            'usage: optimizer_lld.py {monitoring|instances|groups|ports|received|received_group}',
            file=sys.stderr,
        )
        return 2
    mode = sys.argv[1]
    try:
        raw = CONFIG.read_text(encoding='utf-8')
        cfg = json.loads(strip_jsonc(raw))
    except Exception as exc:
        print(f'cannot read {CONFIG}: {exc}', file=sys.stderr)
        return 1
    allrows = []
    for o in cfg.get('optimizers', []):
        rows = optimizer_rows(o)
        if mode == 'monitoring':
            allrows.extend(rows)
        else:
            typemap = {
                'instances': 'optimizer',
                'groups': 'group',
                'ports': 'port',
                'received': 'series',
                'received_group': 'received_group',
            }
            wanted = typemap[mode]
            allrows.extend(r for r in rows if r.get('{#TYPE}') == wanted)
    print(json.dumps(allrows, ensure_ascii=False, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
