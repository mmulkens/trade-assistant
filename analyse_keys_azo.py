"""
Trace KEYS and AZO lifecycles: show how stop advancement freed RL capacity
and allowed new entries.

For each winner:
  - Entry / trail activation / exit
  - What other positions were open at each milestone
  - What trades were entered while the winner was open
  - Signal queue on days immediately after a stop advance (skipped vs entered)
"""
import sqlite3
from datetime import date, timedelta

conn = sqlite3.connect('./data/wf_sim.db')
conn.row_factory = sqlite3.Row
run_id = 'c6f956b4-3efa-439a-b01c-3c8f9217497d'


def positions_open_on(day: str) -> list[dict]:
    rows = conn.execute("""
        SELECT ticker, entry_date, exit_date, entry_price, stop_price, shares,
               risk_amount, exit_reason, net_pnl, conviction
        FROM wf_positions
        WHERE run_id=?
          AND entry_date <= ?
          AND (exit_date IS NULL OR exit_date > ?)
        ORDER BY entry_date
    """, (run_id, day, day)).fetchall()
    return [dict(r) for r in rows]


def signals_on(day: str) -> list[dict]:
    rows = conn.execute("""
        SELECT ticker, signal_rank, conviction, entry_price, stop_price, action
        FROM wf_signals
        WHERE run_id=? AND signal_date=?
        ORDER BY signal_rank
    """, (run_id, day)).fetchall()
    return [dict(r) for r in rows]


def print_open_book(day: str, label: str = "") -> None:
    pos = positions_open_on(day)
    print(f"  Open positions {label}({day}): {len(pos)}")
    total_risk = sum(float(p['risk_amount']) for p in pos)
    for p in pos:
        pnl_str = f"  net {float(p['net_pnl']):>+.0f}" if p['net_pnl'] is not None else ""
        print(f"    {p['ticker']:<8} entered {p['entry_date']}  risk ${float(p['risk_amount']):.0f}{pnl_str}")
    print(f"    Total open risk: ${total_risk:.0f}  ({total_risk/1000:.2f}% of ~$100k base)")


def date_range(start: str, end: str):
    d = date.fromisoformat(start)
    e = date.fromisoformat(end)
    while d <= e:
        yield d.isoformat()
        d += timedelta(days=1)


# -----------------------------------------------------------------------
# Fetch KEYS and AZO rows
# -----------------------------------------------------------------------
for ticker, label in [('KEYS', 'KEYS (Jan–Mar 2026)'), ('AZO', 'AZO (Jul–Sep 2025)')]:
    row = conn.execute("""
        SELECT ticker, entry_date, exit_date, entry_price, exit_price,
               stop_price, shares, risk_amount, net_pnl, exit_reason
        FROM wf_positions WHERE run_id=? AND ticker=?
        ORDER BY entry_date DESC LIMIT 1
    """, (run_id, ticker)).fetchone()
    p = dict(row)

    print()
    print('=' * 72)
    print(f"  {label}")
    print('=' * 72)
    print(f"  Entry:  {p['entry_date']}  @ ${float(p['entry_price']):.2f}")
    print(f"  Exit:   {p['exit_date']}   @ ${float(p['exit_price']):.2f}  ({p['exit_reason']})")
    print(f"  Shares: {p['shares']}   Risk: ${float(p['risk_amount']):.0f}   Net P&L: ${float(p['net_pnl']):+,.2f}")

    print()
    print_open_book(p['entry_date'], "at ENTRY ")

    # Find all other trades entered while this winner was open
    others = conn.execute("""
        SELECT ticker, entry_date, exit_date, entry_price, exit_price, stop_price,
               shares, risk_amount, net_pnl, exit_reason, signal_type, conviction
        FROM wf_positions
        WHERE run_id=? AND ticker != ?
          AND entry_date > ?
          AND entry_date < ?
        ORDER BY entry_date
    """, (run_id, ticker, p['entry_date'], p['exit_date'])).fetchall()

    print()
    print(f"  Trades ENTERED while {ticker} was open ({p['entry_date']} to {p['exit_date']}):")
    if not others:
        print("    (none)")
    for o in others:
        np_ = float(o['net_pnl']) if o['net_pnl'] is not None else 0.0
        ep  = float(o['exit_price']) if o['exit_price'] is not None else 0.0
        print(f"    {o['ticker']:<8} entered {o['entry_date']}  "
              f"exit {o['exit_date']}  "
              f"risk ${float(o['risk_amount']):.0f}  "
              f"net {np_:>+8.0f}  {o['exit_reason']:<12}  {o['conviction']}")

    # Show signals on entry day: how many were skipped due to capacity?
    print()
    print(f"  Signal queue on entry day ({p['entry_date']}):")
    sigs = signals_on(p['entry_date'])
    for s in sigs:
        print(f"    #{s['signal_rank']}  {s['ticker']:<8}  {s['conviction']:<10}  {s['action']}")

    # Show state at exit day
    print()
    print_open_book(p['exit_date'], "at EXIT  ")

    # After exit: next 5 trading days' entries
    print()
    print(f"  Signals entered in the 10 days AFTER {ticker} exit ({p['exit_date']}):")
    after = conn.execute("""
        SELECT signal_date, ticker, signal_rank, conviction, entry_price, action
        FROM wf_signals
        WHERE run_id=? AND signal_date > ? AND signal_date <= date(?, '+14 days')
          AND action = 'entered'
        ORDER BY signal_date, signal_rank
    """, (run_id, p['exit_date'], p['exit_date'])).fetchall()
    if not after:
        print("    (none entered in next 14 days)")
    for s in after:
        print(f"    {s['signal_date']}  #{s['signal_rank']}  {s['ticker']:<8}  {s['conviction']}")

print()

# -----------------------------------------------------------------------
# Stop advance timeline for KEYS specifically (longer hold, more milestones)
# -----------------------------------------------------------------------
print('=' * 72)
print("  KEYS stop-advance timeline (wf_positions stop_price is final stop;")
print("  cross-reference with equity curve to infer when trail activated)")
print('=' * 72)

# Check what entries happened during KEYS hold month by month
keys_entry = '2026-01-13'
keys_exit  = '2026-03-06'

print()
print("  Monthly entries during KEYS hold:")
for month in ['2026-01', '2026-02', '2026-03']:
    month_entries = conn.execute("""
        SELECT ticker, entry_date, exit_date, risk_amount, net_pnl, exit_reason, conviction
        FROM wf_positions
        WHERE run_id=? AND ticker != 'KEYS'
          AND entry_date >= ? AND entry_date <= ?
          AND entry_date > ? AND entry_date < ?
        ORDER BY entry_date
    """, (run_id, month + '-01', month + '-31', keys_entry, keys_exit)).fetchall()
    for e in month_entries:
        np_ = float(e['net_pnl']) if e['net_pnl'] is not None else 0.0
        print(f"    {e['ticker']:<8} {e['entry_date']}  risk ${float(e['risk_amount']):.0f}  net {np_:>+8.0f}  {e['exit_reason']}")

conn.close()
