import sqlite3

conn = sqlite3.connect('./data/wf_sim.db')
conn.row_factory = sqlite3.Row
run_id = 'c6f956b4-3efa-439a-b01c-3c8f9217497d'

print('=== MONTHLY P&L ===')
rows = conn.execute("""
    SELECT substr(exit_date,1,7) as month,
           COUNT(*) as trades,
           SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
           ROUND(SUM(net_pnl),2) as net
    FROM wf_positions
    WHERE run_id=? AND exit_reason != 'sim_end'
    GROUP BY month ORDER BY month
""", (run_id,)).fetchall()
for r in rows:
    n = float(r['net'])
    bar = '+' * int(max(0, n) / 200) if n >= 0 else '-' * int(abs(n) / 200)
    print(f"  {r['month']}  trades={r['trades']:>2}  wins={r['wins']:>2}  net={n:>+10.2f}  {bar}")

print()
print('=== SKIP REASONS ===')
skips = conn.execute("""
    SELECT action, COUNT(*) as n FROM wf_signals
    WHERE run_id=? AND action LIKE 'skipped:%'
    GROUP BY action ORDER BY n DESC LIMIT 10
""", (run_id,)).fetchall()
for s in skips:
    print(f"  {s['action']:<55}  {s['n']}")

total_sigs = conn.execute("SELECT COUNT(*) FROM wf_signals WHERE run_id=?", (run_id,)).fetchone()[0]
entered    = conn.execute("SELECT COUNT(*) FROM wf_signals WHERE run_id=? AND action='entered'", (run_id,)).fetchone()[0]
print()
print(f"Total signals: {total_sigs}  entered: {entered}  hit rate: {entered/total_sigs*100:.1f}%")

print()
print('=== WORST 10 TRADES ===')
worst = conn.execute("""
    SELECT ticker, entry_date, exit_date, entry_price, exit_price, net_pnl, exit_reason, signal_type
    FROM wf_positions WHERE run_id=? ORDER BY net_pnl ASC LIMIT 10
""", (run_id,)).fetchall()
for p in worst:
    print(f"  {p['ticker']:<8}  {p['entry_date']} -> {p['exit_date']}  {float(p['entry_price']):.2f} -> {float(p['exit_price'] or 0):.2f}  net {float(p['net_pnl'] or 0):>+9.2f}  {p['exit_reason']:<12}  {p['signal_type']}")

print()
print('=== BEST 10 TRADES ===')
best = conn.execute("""
    SELECT ticker, entry_date, exit_date, entry_price, exit_price, net_pnl, exit_reason, signal_type
    FROM wf_positions WHERE run_id=? ORDER BY net_pnl DESC LIMIT 10
""", (run_id,)).fetchall()
for p in best:
    print(f"  {p['ticker']:<8}  {p['entry_date']} -> {p['exit_date']}  {float(p['entry_price']):.2f} -> {float(p['exit_price'] or 0):.2f}  net {float(p['net_pnl'] or 0):>+9.2f}  {p['exit_reason']:<12}  {p['signal_type']}")

conn.close()
