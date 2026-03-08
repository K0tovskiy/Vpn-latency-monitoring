import time
import random
import signal
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import C, SPEED_HOST, SPEED_PATH, SPEED_PORT, SPEED_TLS
from .utils import uri_hash, _srv_name
from .db import get_db
from .parsers import fetch_sub, parse_uri, build_multi_config
from .tester import tcp_ping, socks5_speed_test
from .xray import xray_test_batch, XrayManager, run_xray, wait_port
from .stats import gather_server_stats, calc_jitter
from .display import _show, _show_monitor_line, _show_speed_line

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

def cmd_fetch(args):
    conn = get_db()
    new = upd = skip = 0
    for url in args.urls:
        print(f"📥 {url}")
        try:
            uris = fetch_sub(url)
            print(f"   links: {len(uris)}")
            for u in uris:
                s = parse_uri(u)
                if not s: skip += 1; continue
                key = uri_hash(u)
                ex = conn.execute("SELECT id FROM servers WHERE uri_key=?", (key,)).fetchone()
                if ex:
                    conn.execute("UPDATE servers SET sub_url=?,remark=?,raw_uri=? WHERE uri_key=?",
                        (url, s['remark'], s['raw'], key))
                    upd += 1
                else:
                    conn.execute("INSERT INTO servers (sub_url,protocol,transport,host,port,remark,raw_uri,uri_key) VALUES (?,?,?,?,?,?,?,?)",
                        (url, s['protocol'], s.get('transport','tcp'), s['host'], s['port'], s['remark'], s['raw'], key))
                    new += 1
            conn.commit()
        except Exception as e:
            print(f"   ❌ {e}")
    if skip: print(f"⚠ Not recognized: {skip}")
    total = conn.execute('SELECT count(*) FROM servers').fetchone()[0]
    print(f"✅ New: {new}, updated: {upd}, total in DB: {total}")
    conn.close()

def cmd_list(args):
    conn = get_db()
    rows = conn.execute("SELECT * FROM servers ORDER BY sub_url, remark").fetchall()
    if not rows: print("No servers."); return
    print(f"\n{'ID':>4}  {'Proto':7s} {'Tr':9s} {'Remark':32s} {'Host':30s} {'Port':>5s}")
    print('─' * 92)
    cur = None
    for r in rows:
        if r['sub_url'] != cur:
            cur = r['sub_url']; print(f"\n  📦 {cur}")
        print(f"{r['id']:4d}  {r['protocol']:7s} {(r['transport'] or 'tcp'):9s} "
              f"{(r['remark'] or '—'):32s} {r['host']:30s} {r['port']:5d}")
    print(f"\nTotal: {len(rows)}\n")
    conn.close()

def _filter_servers(all_servers, args):
    filters = getattr(args, 'servers', None)
    if not filters:
        return all_servers
        
    patterns = [p.strip() for p in filters.split(',')]
    matched_ids = set()
    final_servers = []
    
    for p in patterns:
        if not p: continue
        low_p = p.lower()
        for s in all_servers:
            if s['id'] in matched_ids: continue
            
            # 1. ID check
            if p.isdigit() and s['id'] == int(p):
                final_servers.append(s); matched_ids.add(s['id'])
            # 2. Sub URL check
            elif p.startswith('http') and low_p in (s['sub_url'] or '').lower():
                final_servers.append(s); matched_ids.add(s['id'])
            # 3. Name/Host check
            elif low_p in (s['remark'] or '').lower() or low_p in (s['host'] or '').lower():
                final_servers.append(s); matched_ids.add(s['id'])
                
    return final_servers

def _get_time_range(args):
    now = datetime.now()
    def _fmt(dt): return dt.strftime('%Y-%m-%d %H:%M:%S')

    timespan = getattr(args, 'timespan', None)
    if timespan:
        import re
        parts = re.split(r'\s+-\s+|\s+to\s+|/|\.\.', timespan)
        if len(parts) >= 2:
            start = parts[0].strip().replace('T', ' ')
            # If user provided short YYYY-MM-DD, try adding HH:MM:SS if needed? 
            # Or just leave it to SQLite to handle.
            end = parts[1].strip().replace('T', ' ')
            return start, end
        elif len(parts) == 1:
            return parts[0].strip().replace('T', ' '), _fmt(now)
        
    hours = getattr(args, 'hours', 24)
    days = getattr(args, 'days', 0)
    if days > 0:
        hours = days * 24
    
    if hours == 0 and getattr(args, 'cmd', '') != 'cleanup':
        hours = 24
        
    start = _fmt(now - timedelta(hours=hours))
    end = _fmt(now)
    return start, end

def _test_tcp(conn, servers, workers):
    results = {}
    def job(srv): return srv['id'], tcp_ping(srv['host'], srv['port'])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(job, s): s for s in servers}
        for f in as_completed(futs):
            sid, r = f.result(); results[sid] = r
    for srv in servers:
        lat, err = results.get(srv['id'], (None, '?'))
        _show(srv, lat, err)
        conn.execute("INSERT INTO pings (server_id,method,latency_ms,error) VALUES (?,?,?,?)",
            (srv['id'], 'tcp', lat, err))
    conn.commit(); print()

def _test_xray_fresh(conn, servers, workers):
    print("  ▶ Starting fresh xray...")
    t0 = time.monotonic()
    results = xray_test_batch(servers, workers)
    elapsed = time.monotonic() - t0
    ok = sum(1 for l, e in results.values() if l is not None)
    print(f"  ✓ Done in {elapsed:.1f}s — {ok}/{len(servers)} OK\n")
    for srv in servers:
        lat, err = results.get(srv['id'], (None, '?'))
        _show(srv, lat, err)
        conn.execute("INSERT INTO pings (server_id,method,latency_ms,error) VALUES (?,?,?,?)",
            (srv['id'], 'xray', lat, err))
    conn.commit(); print()

def _do_speed_all_fresh(conn, servers_to_test, workers, speed_kwargs):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {C.CYN}🚀 Speed test ({len(servers_to_test)} servers)...{C.RST}")
    from .config import BASE_PORT
    cfg, pmap = build_multi_config(list(servers_to_test), BASE_PORT)
    if not cfg:
        print(f"         {C.RED}No supported servers{C.RST}")
        return 0
    count = 0
    try:
        with run_xray(cfg):
            p0 = min(pmap.values())
            if not wait_port(p0, timeout=8):
                print(f"         {C.RED}xray failed to start{C.RST}")
                return 0
            t0 = time.monotonic()
            def job(srv):
                port = pmap.get(srv['id'])
                if not port: return srv['id'], (0, 0, 0, 'not_configured')
                return srv['id'], socks5_speed_test(port, **speed_kwargs)
            results = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(job, s): s for s in servers_to_test}
                for f in as_completed(futs):
                    sid, result = f.result()
                    results[sid] = result
            elapsed = time.monotonic() - t0
            ok_count = 0
            for srv in servers_to_test:
                sz, speed, dur, err = results.get(srv['id'], (0, 0, 0, 'not_tested'))
                _show_speed_line(srv, sz, speed, dur, err)
                if conn:
                    conn.execute(
                        "INSERT INTO speed_tests (server_id,size_bytes,duration_s,speed_mbps,error) VALUES (?,?,?,?,?)",
                        (srv['id'], sz, dur, speed, err))
                count += 1
                if not err: ok_count += 1
            if conn: conn.commit()
            print(f"         ⏱ {elapsed:.1f}s  OK: {ok_count}/{len(servers_to_test)}\n")
    except (FileNotFoundError, RuntimeError) as e:
        print(f"         {C.RED}❌ {e}{C.RST}")
    return count

def cmd_test(args):
    conn = get_db()
    all_servers = conn.execute("SELECT * FROM servers ORDER BY remark").fetchall()
    servers = _filter_servers(all_servers, args)
    if not servers:
        print("No servers for testing.")
        conn.close()
        return

    tasks = [t.strip().lower() for t in args.tasks.split(',')]
    batch_size = args.batch
    if batch_size != -1:
        # Sub-sample if batch is limited
        servers = random.sample(list(servers), min(batch_size, len(servers)))

    print(f"\n🔍 Testing {len(servers)} servers, tasks: {tasks}\n")

    if 'tcp-ping' in tasks:
        print(f"{C.BLD}=== TCP PING ==={C.RST}")
        _test_tcp(conn, servers, args.workers)
    
    if 'xray-ping' in tasks:
        print(f"{C.BLD}=== XRAY PING ==={C.RST}")
        _test_xray_fresh(conn, servers, args.workers)

    if 'speed' in tasks:
        print(f"{C.BLD}=== SPEED TEST ==={C.RST}")
        speed_kwargs = dict(
            host=getattr(args, 'speed_host', SPEED_HOST) or SPEED_HOST,
            path=getattr(args, 'speed_path', SPEED_PATH) or SPEED_PATH,
            port=getattr(args, 'speed_port', SPEED_PORT) or SPEED_PORT,
            use_tls=getattr(args, 'speed_tls', SPEED_TLS),
        )
        _do_speed_all_fresh(conn, servers, args.workers, speed_kwargs)

    conn.close()

def _sleep_interruptible(secs, alive):
    end = time.monotonic() + secs
    while alive[0] and time.monotonic() < end:
        time.sleep(0.3)

def cmd_monitor(args):
    conn = get_db()
    all_servers = conn.execute("SELECT * FROM servers ORDER BY remark").fetchall()
    servers = _filter_servers(all_servers, args)
    if not servers:
        print("No servers for monitoring.")
        conn.close()
        return

    raw_tasks = args.tasks.split(',')
    schedule = {}
    for t in raw_tasks:
        parts = t.split(':')
        if len(parts) != 2: continue
        name = parts[0].strip().lower()
        val = parts[1].strip()
        mult = 1
        if val.endswith('s'): mult = 1
        elif val.endswith('m'): mult = 60
        elif val.endswith('h'): mult = 3600
        val = int(val.rstrip('smh'))
        schedule[name] = val * mult

    print(f"{C.BLD}📡 Monitoring ({len(servers)} servers){C.RST}")
    print(f"   Tasks: {schedule}")
    print(f"   Ctrl+C — stop\n")

    alive = [True]
    def stop(s,f): alive[0] = False; print(f"\n{C.YEL}⏹ Stopping{C.RST}")
    signal.signal(signal.SIGINT, stop)

    speed_kwargs = dict(
        host=getattr(args, 'speed_host', SPEED_HOST) or SPEED_HOST,
        path=getattr(args, 'speed_path', SPEED_PATH) or SPEED_PATH,
        port=getattr(args, 'speed_port', SPEED_PORT) or SPEED_PORT,
        use_tls=getattr(args, 'speed_tls', SPEED_TLS),
    )

    last_run = {k: 0.0 for k in schedule.keys()}
    round_num = 0
    prev_tcp = {}
    prev_xray = {}
    batch_size = args.batch
    effective_batch = len(servers) if batch_size == -1 else min(batch_size, len(servers))
    workers = args.workers

    while alive[0]:
        now = time.monotonic()
        did_something = False

        if 'tcp-ping' in schedule and (now - last_run['tcp-ping']) >= schedule['tcp-ping']:
            batch = random.sample(list(servers), effective_batch)
            ts = datetime.now().strftime('%H:%M:%S')
            print(f"[{ts}] {C.CYN}TCP Round{C.RST}: {len(batch)} servers")
            
            def ping_job(srv):
                lat, err = tcp_ping(srv['host'], srv['port'])
                return srv['id'], lat, err
            
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(ping_job, s): s for s in batch}
                for f in as_completed(futs):
                    sid, lat, err = f.result()
                    srv = [x for x in batch if x['id'] == sid][0]
                    p = prev_tcp.get(sid)
                    _show_monitor_line(srv, lat, err, p, method='TCP')
                    if lat is not None: prev_tcp[sid] = lat
                    if alive[0]:
                        conn.execute("INSERT INTO pings (server_id,method,latency_ms,error) VALUES (?,?,?,?)", (sid, 'tcp', lat, err))
            if alive[0]: conn.commit()
            last_run['tcp-ping'] = time.monotonic()
            did_something = True

        if 'xray-ping' in schedule and (now - last_run['xray-ping']) >= schedule['xray-ping'] and alive[0]:
            round_num += 1
            batch = random.sample(list(servers), effective_batch)
            ts = datetime.now().strftime('%H:%M:%S')
            names = ', '.join(_srv_name(s, 15) for s in batch[:3])
            if len(batch) > 3: names += f'… +{len(batch)-3}'
            print(f"[{ts}] {C.BLU}XRAY Round {round_num}{C.RST}: {len(batch)} servers ({names})")
            t0 = time.monotonic()
            results = xray_test_batch(batch, workers)
            elapsed = time.monotonic() - t0
            for srv in batch:
                lat, err = results.get(srv['id'], (None, '?'))
                p = prev_xray.get(srv['id'])
                _show_monitor_line(srv, lat, err, p, method='XRAY')
                if lat is not None: prev_xray[srv['id']] = lat
                if alive[0]:
                    conn.execute("INSERT INTO pings (server_id,method,latency_ms,error) VALUES (?,?,?,?)",
                        (srv['id'], 'xray', lat, err))
            if alive[0]: conn.commit()
            ok = sum(1 for l, e in results.values() if l is not None)
            print(f"         ⏱ {elapsed:.1f}s  OK: {ok}/{len(batch)}\n")
            last_run['xray-ping'] = time.monotonic()
            did_something = True

        if 'speed' in schedule and (now - last_run['speed']) >= schedule['speed'] and alive[0]:
            batch = random.sample(list(servers), effective_batch)
            _do_speed_all_fresh(conn, batch, workers, speed_kwargs)
            last_run['speed'] = time.monotonic()
            did_something = True

        if not did_something:
            _sleep_interruptible(1.0, alive)
    conn.close()

def cmd_stats(args):
    conn = get_db()
    since, until = _get_time_range(args)
    
    all_servers = conn.execute("SELECT * FROM servers ORDER BY remark").fetchall()
    servers = _filter_servers(all_servers, args)
    if not servers:
        print("No servers."); conn.close(); return

    cols = [c.strip() for c in args.cols.split(',')]
    
    # Calculate duration for display
    t_start = datetime.fromisoformat(since)
    t_end = datetime.fromisoformat(until)
    dur_h = (t_end - t_start).total_seconds() / 3600
    
    title = f"📊 Statistics for {dur_h:.1f} h."
    if getattr(args, 'timespan', None):
        title = f"📊 Statistics [{since} - {until}]"
    out_lines = []
    def out(msg):
        print(msg)
        out_lines.append(msg)

    out(f"\n{C.BLD}{title}{C.RST} (sort: {args.sort})\n")
    
    hdr1 = ""
    hdr2 = ""
    hdr3 = ""
    widths = {}

    for c in cols:
        parts = c.split(':')
        domain_str = parts[0] if len(parts) > 1 else ""
        metric_str = parts[-1]
        
        domain_parts = domain_str.split('-')
        proto = domain_parts[0] if len(domain_parts) > 0 else ""
        type_str = domain_parts[1] if len(domain_parts) > 1 else ""
        
        if metric_str == 'mean': metric_str = 'Mean'
        elif metric_str == 'jit': metric_str = 'Jit'
        elif metric_str == 'σ': metric_str = 'σ'
        else: metric_str = metric_str.capitalize()
        
        w = 8
        if c == 'Server':
            w = 38
            proto = ""
            type_str = ""
            metric_str = "Server"
        elif c in ('N', 'OK%'):
            w = 5
            proto = ""
            type_str = ""
            metric_str = c
        elif c.startswith('score') or 'score' in c.lower():
            w = 7
            proto = ""
            type_str = ""
            metric_str = c.split(':')[-1].capitalize() if ':' in c else c.capitalize()
        elif c == 'speed' or 'speed' in c.lower():
            w = 7
            proto = ""
            type_str = ""
            metric_str = "Speed"

        widths[c] = w
        if c == 'Server':
            hdr1 += f"{proto:<{w}} "
            hdr2 += f"{type_str:<{w}} "
            hdr3 += f"{metric_str:<{w}} "
        else:
            hdr1 += f"{proto:>{w}} "
            hdr2 += f"{type_str:>{w}} "
            hdr3 += f"{metric_str:>{w}} "
        
    out(hdr1)
    out(hdr2)
    out(hdr3)
    out('─' * len(hdr1))

    req_pcts = set()
    for c in cols + [args.sort]:
        if c.startswith('p') and c[1:].isdigit():
            req_pcts.add(int(c[1:]))
        elif ':' in c:
            m = c.split(':')[-1]
            if m.startswith('p') and m[1:].isdigit():
                req_pcts.add(int(m[1:]))
                
    req_pcts = list(req_pcts) or [50, 90, 95]

    items = []
    for s in servers:
        st = gather_server_stats(conn, s['id'], since, until=until, pcts=req_pcts)
        if not st: continue
        mapped = {'Server': _srv_name(s, 38), 'raw_id': s['id']}
        # Map variables
        for p in ('tcp', 'xray'):
            mapped[f'{p}-ping:N'] = st[p]['n']
            mapped[f'{p}-ping:OK%'] = 100 - st[p]['loss'] if st[p]['loss'] is not None else None
            for key, val in st[p].items():
                if key.startswith('p') and key[1:].isdigit():
                    mapped[f'{p}-ping:{key}'] = val
            mapped[f'{p}-ping:mean'] = st[p]['mean']
            mapped[f'{p}-ping:score1'] = st[p]['score1']
            mapped[f'{p}-ping:score2'] = st[p]['score2']
            mapped[f'{p}-ping:score3'] = st[p]['score3']
            mapped[f'{p}-ping:σ'] = st[p]['stddev']
            
            mapped[f'{p}-jit:mean'] = st[p]['jit_mean']
            for key, val in st[p].items():
                if key.startswith('jit_p') and key[5:].isdigit():
                    mapped[f'{p}-jit:p{key[5:]}'] = val
        
        mapped['speed'] = st['speed']['mean']
        if mapped.get('xray-ping:score2') is not None:
             mapped['score1'] = mapped['xray-ping:score1']
             mapped['score2'] = mapped['xray-ping:score2']
             mapped['score3'] = mapped['xray-ping:score3']
             mapped['score'] = mapped['score2']
        else:
             mapped['score1'] = mapped.get('tcp-ping:score1')
             mapped['score2'] = mapped.get('tcp-ping:score2')
             mapped['score3'] = mapped.get('tcp-ping:score3')
             mapped['score'] = mapped.get('score2', 0.0)
        
        mapped['N'] = mapped['xray-ping:N'] if mapped['xray-ping:N'] > 0 else mapped['tcp-ping:N']
        mapped['OK%'] = mapped['xray-ping:OK%'] if 'xray-ping:N' in mapped and mapped['xray-ping:N'] > 0 else mapped.get('tcp-ping:OK%')
        
        items.append(mapped)

    def get_val(it, key):
        v = it.get(key)
        if v is None:
            is_desc = key in ('speed', 'score') or 'score' in key or 'OK%' in key or key == 'N'
            return float('-inf') if is_desc else float('inf')
        return v

    is_desc = args.sort in ('speed', 'score') or 'score' in args.sort or 'OK%' in args.sort or args.sort == 'N'
    items.sort(key=lambda x: get_val(x, args.sort), reverse=is_desc)

    import re
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    
    # Simple regex to strip emojis and other weird characters to leave only standard text / ascii / cyrillic
    def strip_emojis(s):
        # We allow standard word characters, whitespace, basic symbols, math symbols, and arrows
        return re.sub(r'[^\w\s\.\-—\|]', '', str(s)).strip()

    def pad(s, width, left_align=False):
        # Apply ANSI escape strip
        clean_s = ansi_escape.sub('', str(s))
        
        # If this is the server string, we strip the emojis out completely
        if left_align:
            clean_s = strip_emojis(clean_s)
            s = strip_emojis(str(s)) # Replace original with stripped version
            
        vis_len = len(clean_s)
        actual_spaces = max(0, width - vis_len)
        if left_align:
            return s + " " * actual_spaces
        return " " * actual_spaces + str(s)

    for it in items:
        line = ""
        for c in cols:
            w = widths[c]
            val = it.get(c)
            
            if c == 'Server':
                line += pad(str(val), w, left_align=True) + " "
            elif val is None:
                line += pad("—", w) + " "
            elif c.endswith('OK%') or c == 'OK%':
                line += pad(f"{val:4.0f}%", w) + " "
            elif c.endswith(':N') or c == 'N':
                line += pad(f"{val:4d}", w) + " "
            elif c.startswith('score') or 'score' in c:
                line += pad(f"{C.score(val)}{val:5.1f}{C.RST}", w) + " "
            elif c == 'speed' or c.endswith('speed:mean'):
                line += pad(f"{C.spd(val)}{val:5.1f}M{C.RST}", w) + " "
            elif c.endswith(':mean'):
                line += pad(f"{C.lat(val)}{val:6.1f}{C.RST}ms", w) + " "
            elif 'jit' in c or c.endswith('σ') or (':' in c and c.split(':')[-1].startswith('p')) or c.startswith('p'):
                line += pad(f"{val:6.1f}ms", w) + " "
            else:
                line += pad(f"{val:6.1f}", w) + " "
        out(line)
    out("\n")
    conn.close()

    import re
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    md_content = ["```text"] + [ansi_escape.sub('', l) for l in out_lines] + ["```"]
    try:
        with open('last_general_stats.md', 'w', encoding='utf-8') as f:
            f.write('\n'.join(md_content))
    except Exception as e:
        print(f"Failed to save stats file: {e}")

def cmd_graph(args):
    if not HAS_MATPLOTLIB: print("❌ pip install matplotlib"); return
    if not HAS_NUMPY: print("❌ pip install numpy"); return

    import re
    import warnings
    warnings.filterwarnings("ignore", message=".*Glyph.*missing from font.*")

    conn = get_db()
    
    all_servers = conn.execute("SELECT * FROM servers").fetchall()
    matched = []
    if getattr(args, 'servers', None):
        matched = _filter_servers(all_servers, args)
    elif getattr(args, 'name', None):
        from argparse import Namespace
        matched = _filter_servers(all_servers, Namespace(servers=args.name))
    
    if not matched:
        print(f"❌ Server not found.")
        return
    srv = matched[0]

    srv_name = srv['remark'] or srv['host']
    safe_title = re.sub(r'[^\w\s\-\.\(\)\[\]]', '', srv_name).strip()
    since, until = _get_time_range(args)

    def get_data(method):
        rows = conn.execute("SELECT latency_ms FROM pings WHERE server_id=? AND ts>=? AND ts<=? AND method=? AND latency_ms IS NOT NULL ORDER BY latency_ms ASC", (srv['id'], since, until, method)).fetchall()
        chrono_rows = conn.execute("SELECT latency_ms FROM pings WHERE server_id=? AND ts>=? AND ts<=? AND method=? AND latency_ms IS NOT NULL ORDER BY ts ASC", (srv['id'], since, until, method)).fetchall()
        
        lats_sorted = np.array([r[0] for r in rows])
        chrono_lats = [r[0] for r in chrono_rows]
        jit = calc_jitter(chrono_lats)
        chrono_jitters = [0] + [abs(chrono_lats[i]-chrono_lats[i-1]) for i in range(1, len(chrono_lats))]
        
        return {
            'lats_sorted': lats_sorted, 
            'chrono_lats': chrono_lats, 
            'jitters_sorted': np.sort(chrono_jitters), 
            'chrono_jitters': chrono_jitters,
            'jitter_mean': jit
        }

    data = {
        'tcp': get_data('tcp'),
        'xray': get_data('xray')
    }

    speed_rows = conn.execute("SELECT ts, speed_mbps FROM speed_tests WHERE server_id=? AND ts>=? AND ts<=? AND speed_mbps>0 ORDER BY ts ASC", (srv['id'], since, until)).fetchall()
    speed_list = [r['speed_mbps'] for r in speed_rows]
    speeds_sorted = np.sort(speed_list) if len(speed_list) > 0 else np.array([])
    has_speed = len(speed_list) > 0
    avg_spd = f"\nAvg Speed: {np.mean(speed_list):.1f} Mbps" if has_speed else ""

    raw_plots = args.plots.split(',')
    plots_to_draw = []
    
    for p in raw_plots:
        parts = p.split(':')
        if len(parts) != 2: continue
        metric, ptype = parts[0].strip().lower(), parts[1].strip().lower()
        if metric == 'speed' and not has_speed: continue
        plots_to_draw.append((metric, ptype))

    if not plots_to_draw:
        print("❌ Nothing to plot (or no data for selected charts).")
        return

    nplots = len(plots_to_draw)
    ratios = [3] * nplots

    plt.style.use('dark_background')
    fig, axes = plt.subplots(nplots, 1, figsize=(12, 3 + nplots * 3), gridspec_kw={'height_ratios': ratios})
    if nplots == 1: axes = [axes]

    from matplotlib.ticker import ScalarFormatter

    # Assuming info is for xray by primary focus, or tcp if xray is empty
    pnum = len(data['xray']['chrono_lats'])
    total = conn.execute("SELECT count(*) FROM pings WHERE server_id=? AND ts>=? AND ts<=? AND method='xray'", (srv['id'], since, until)).fetchone()[0]
    
    if total == 0:
        pnum = len(data['tcp']['chrono_lats'])
        total = conn.execute("SELECT count(*) FROM pings WHERE server_id=? AND ts>=? AND ts<=? AND method='tcp'", (srv['id'], since, until)).fetchone()[0]
        act = data['tcp']
        metr_name = "TCP"
    else:
        act = data['xray']
        metr_name = "Xray"

    loss = (1 - pnum/total)*100 if total > 0 else 0
    min_lat = act['lats_sorted'][0] if len(act['lats_sorted']) > 0 else 0
    max_lat = act['lats_sorted'][-1] if len(act['lats_sorted']) > 0 else 0

    info = (f"Samples ({metr_name}): {total}\nLoss: {loss:.1f}%\n"
            f"Min Lat: {min_lat:.1f}ms\nMax Lat: {max_lat:.1f}ms\n"
            f"Jitter: {act['jitter_mean']:.1f}ms{avg_spd}")

    def draw_percentile_plot(ax, sorted_data, metric_name, log_scale=False, color='#00ffcc'):
        if len(sorted_data) == 0: return
        quants = np.linspace(0, 100, len(sorted_data))
        ax.plot(quants, sorted_data, color=color, linewidth=2.5)
        ax.fill_between(quants, sorted_data, color=color, alpha=0.08)
        if log_scale:
            ax.set_yscale('log')
            ax.yaxis.set_major_formatter(ScalarFormatter())
            ax.set_ylabel(f"{metric_name} (log)")
            if getattr(args, 'fixed_scale', False):
                if 'speed' in metric_name.lower(): ax.set_ylim(1, 200)
                elif 'jitter' in metric_name.lower(): ax.set_ylim(1, 1000)
                else: ax.set_ylim(10, 5000)
        else:
            ax.set_ylabel(metric_name)
        p_marks = [50, 90, 95, 99]
        c_marks = ['#3498db', '#f1c40f', '#e67e22', '#e74c3c']
        for p, clr in zip(p_marks, c_marks):
            val = np.percentile(sorted_data, p)
            ax.axhline(val, color=clr, ls='--', alpha=0.3)
            ax.scatter(p, val, color=clr, s=50, zorder=5)
            ax.annotate(f"P{p}: {val:.1f}", (p, val), textcoords="offset points", xytext=(10,5), color=clr, fontweight='bold')
        ax.set_xlabel("Percentile %")
        ax.grid(True, which='both', ls='-', alpha=0.15)
        ax.set_xlim(0, 100)

    def draw_dynamic_plot(ax, data_list, metric_name, color='#ff6b6b'):
        if len(data_list) == 0: return
        ax.bar(range(len(data_list)), data_list, color=color, alpha=0.6, width=1.0)
        avg = np.mean(data_list)
        ax.axhline(avg, color=color, ls='--', alpha=0.8, label=f'Mean: {avg:.1f}')
        ax.set_ylabel(metric_name)
        ax.set_xlabel("Measurement #")
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.15)

    for i, (metric, ptype) in enumerate(plots_to_draw):
        ax = axes[i]
        
        if metric == 'xray-ping':
            data_arr = data['xray']['lats_sorted'] if 'percentile' in ptype else data['xray']['chrono_lats']
            c = '#00ffcc'
        elif metric == 'tcp-ping':
            data_arr = data['tcp']['lats_sorted'] if 'percentile' in ptype else data['tcp']['chrono_lats']
            c = '#1abc9c'
        elif metric == 'xray-jit':
            data_arr = data['xray']['jitters_sorted'] if 'percentile' in ptype else data['xray']['chrono_jitters']
            c = '#ff6b6b'
        elif metric == 'tcp-jit':
            data_arr = data['tcp']['jitters_sorted'] if 'percentile' in ptype else data['tcp']['chrono_jitters']
            c = '#e74c3c'
        elif metric == 'speed':
            data_arr = speeds_sorted if 'percentile' in ptype else speed_list
            c = '#3498db'
        else:
            continue

        title_m = metric.replace('-ping', ' Latency').replace('-jitter', ' Jitter').capitalize()

        if ptype == 'percentile-log':
            draw_percentile_plot(ax, data_arr, title_m, log_scale=True, color=c)
        elif ptype == 'percentile':
            draw_percentile_plot(ax, data_arr, title_m, log_scale=False, color=c)
        elif ptype == 'dynamic':
            draw_dynamic_plot(ax, data_arr, title_m, color=c)
        
        if i == 0:
            ax.set_title(f"Profile: {safe_title}", fontsize=14, pad=15)
            ax.text(0.02, 0.95, info, transform=ax.transAxes, va='top', fontsize=9, family='monospace', bbox=dict(boxstyle='round,pad=0.5', fc='#222', alpha=0.8, ec='#444'))

    plt.tight_layout()
    import re
    safe_name = re.sub(r'[\s\-]+', '_', safe_title).strip('_')
    fn = args.output or f"last_stats_{safe_name}.png"
    plt.savefig(fn, dpi=150)
    plt.close()
    print(f"✅ Chart saved: {fn}")

def cmd_cleanup(args):
    conn = get_db()
    since, until = _get_time_range(args)
    if getattr(args, 'timespan', None):
        # Precise range deletion
        p = conn.execute("DELETE FROM pings WHERE ts >= ? AND ts <= ?", (since, until)).rowcount
        s = conn.execute("DELETE FROM speed_tests WHERE ts >= ? AND ts <= ?", (since, until)).rowcount
        print(f"🧹 Deleted: {p} pings, {s} speed tests in range [{since} - {until}]")
    else:
        # Older than X days/hours (using since as cutoff)
        p = conn.execute("DELETE FROM pings WHERE ts < ?", (since,)).rowcount
        s = conn.execute("DELETE FROM speed_tests WHERE ts < ?", (since,)).rowcount
        print(f"🧹 Deleted: {p} pings, {s} speed tests (older than {since})")
    conn.execute("VACUUM"); conn.commit()
    print(f"   Remaining: {conn.execute('SELECT count(*) FROM pings').fetchone()[0]} pings")
    conn.close()

def cmd_export(args):
    conn = get_db()
    since, until = _get_time_range(args)
    
    all_servers = conn.execute("SELECT * FROM servers").fetchall()
    servers = _filter_servers(all_servers, args)
    if not servers:
        print("❌ No servers for export."); conn.close(); return
        
    srv_ids = [s['id'] for s in servers]
    placeholders = ','.join(['?'] * len(srv_ids))
    
    rows = conn.execute(f"""
        SELECT s.remark,s.protocol,s.transport,s.host,s.port,
               p.ts,p.method,p.latency_ms,p.error
        FROM pings p JOIN servers s ON s.id=p.server_id
        WHERE p.ts>=? AND p.ts<=? AND s.id IN ({placeholders}) ORDER BY p.ts
    """, [since, until] + srv_ids).fetchall()
    print("remark,protocol,transport,host,port,timestamp,method,latency_ms,error")
    for r in rows:
        rem = (r['remark'] or '').replace(',',';')
        err = (r['error'] or '').replace(',',';')
        print(f"{rem},{r['protocol']},{r['transport'] or ''},{r['host']},{r['port']},{r['ts']},{r['method']},{r['latency_ms'] or ''},{err}")
    conn.close()
