import argparse
from .config import SPEED_HOST, SPEED_PATH, SPEED_PORT, WORKERS
from .commands import (
    cmd_fetch, cmd_list, cmd_test, cmd_monitor,
    cmd_stats, cmd_graph, cmd_cleanup, cmd_export
)

def main():
    p = argparse.ArgumentParser(
        description='VPN Latency Monitor v4.2',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s fetch https://example.com/sub
  %(prog)s test --tasks "xray-ping,tcp-ping,speed"
  %(prog)s monitor --tasks "xray-ping:60s,tcp-ping:120s,speed:30m"
  %(prog)s stats --hours 12 --sort score
  %(prog)s graph "Italy" --plots "xray-ping:percentile-log,speed:dynamic"
        """)
    sub = p.add_subparsers(dest='cmd')

    f = sub.add_parser('fetch', help='Download subscriptions')
    f.add_argument('urls', nargs='+')

    sub.add_parser('list', help='Show servers')

    t = sub.add_parser('test', help='One-shot test')
    t.add_argument('--tasks', default='xray-ping,tcp-ping,speed', help='Tasks to run (default: %(default)s)')
    t.add_argument('--servers', help='Filter by IDs, names or Sub URLs (can mix, comma-separated)')
    t.add_argument('--batch', type=int, default=-1, help='Number of servers per batch (default: %(default)s, -1=all)')
    t.add_argument('--workers', type=int, default=WORKERS, help='Default: %(default)s')

    m = sub.add_parser('monitor', help='Continuous monitoring')
    m.add_argument('--tasks', default='xray-ping:60s,tcp-ping:120s,speed:30m', help='Tasks with intervals (default: %(default)s)')
    m.add_argument('--batch', type=int, default=-1, help='Number of servers per batch (default: %(default)s, -1=all)')
    m.add_argument('--workers', type=int, default=WORKERS, help='Default: %(default)s')
    m.add_argument('--speed-host', default=None, help=f'Speed host (default: {SPEED_HOST})')
    m.add_argument('--speed-path', default=None, help=f'Speed path (default: {SPEED_PATH})')
    m.add_argument('--speed-port', type=int, default=None, help=f'Speed port (default: {SPEED_PORT})')
    m.add_argument('--speed-tls', action='store_true', default=True, help='Use HTTPS for speed test (default: %(default)s)')
    m.add_argument('--no-speed-tls', dest='speed_tls', action='store_false', help='Use HTTP instead of HTTPS for speed test')
    m.add_argument('--servers', help='Filter by IDs, names or Sub URLs (can mix, comma-separated)')

    s = sub.add_parser('stats', help='Statistics')
    s.add_argument('--hours', type=float, default=24, help='Default: %(default)s')
    s.add_argument('--days', type=float, default=0, help='Takes precedence over --hours (default: %(default)s)')
    s.add_argument('--timespan', help='Time range "start-end" in ISO format (takes precedence)')
    s.add_argument('--servers', help='Filter by IDs, names or Sub URLs (can mix, comma-separated)')
    s.add_argument('--sort', default='score', help='Column to sort by (default: %(default)s)')
    s.add_argument('--cols', default='Server,N,OK%,xray-ping:mean,xray-ping:p50,xray-ping:p90,xray-ping:p95,xray-jit:mean,xray-ping:σ,speed,score', 
                   help='Comma separated columns (default: %(default)s)')

    g = sub.add_parser('graph', help='Generate PNG chart')
    g.add_argument('name', nargs='?', help='Partial server name')
    g.add_argument('--servers', help='Filter by ID, name or Sub URL (takes first match)')
    g.add_argument('--hours', type=float, default=24, help='Default: %(default)s')
    g.add_argument('--days', type=float, default=0, help='Takes precedence over --hours (default: %(default)s)')
    g.add_argument('--timespan', help='Time range "start-end" in ISO format (takes precedence)')
    g.add_argument('--output', help='Output filename')
    g.add_argument('--plots', default="xray-ping:percentile-log,speed:dynamic,xray-jit:percentile-log,speed:percentile", 
                   help='Comma separated plots (default: %(default)s)')
    g.add_argument('--fixed-scale', action='store_true', help='Use fixed Y-axis scale (log only)')

    cl = sub.add_parser('cleanup', help='Purge old data')
    cl.add_argument('--hours', type=float, default=0, help='Default: %(default)s')
    cl.add_argument('--days', type=int, default=30, help='Default: %(default)s')
    cl.add_argument('--timespan', help='Exact range to delete "start-end" in ISO format (takes precedence)')

    e = sub.add_parser('export', help='CSV export')
    e.add_argument('--hours', type=float, default=24, help='Default: %(default)s')
    e.add_argument('--days', type=float, default=0, help='Takes precedence over --hours (default: %(default)s)')
    e.add_argument('--timespan', help='Time range "start-end" in ISO format (takes precedence)')
    e.add_argument('--servers', help='Filter by IDs, names or Sub URLs (can mix, comma-separated)')

    args = p.parse_args()
    if not args.cmd: p.print_help(); return

    cmds = {
        'fetch': cmd_fetch, 'list': cmd_list, 'test': cmd_test,
        'monitor': cmd_monitor, 'stats': cmd_stats, 'graph': cmd_graph,
        'cleanup': cmd_cleanup, 'export': cmd_export,
    }
    cmds[args.cmd](args)

if __name__ == '__main__':
    main()
