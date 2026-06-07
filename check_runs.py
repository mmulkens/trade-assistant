import sqlite3
from datetime import datetime

conn = sqlite3.connect('./data/wf_sim.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(
    'SELECT run_id, sim_start, sim_end, portfolio_start, portfolio_end, '
    'total_trades, started_at, ended_at FROM wf_runs ORDER BY started_at'
).fetchall()

for r in rows:
    duration = 'N/A'
    if r['started_at'] and r['ended_at']:
        s = datetime.fromisoformat(r['started_at'])
        e = datetime.fromisoformat(r['ended_at'])
        duration = str(e - s)
    print("run_id:", r['run_id'])
    print("  sim:", r['sim_start'], "->", r['sim_end'],
          " trades:", r['total_trades'],
          " pf:", r['portfolio_start'], "->", r['portfolio_end'])
    print("  duration:", duration)

# Also check eligible tickers — look at wf_signals for run 2
run2 = '568744f0-62aa-44c1-847a-8fc24921e1f7'
sig_count = conn.execute(
    'SELECT COUNT(*) FROM wf_signals WHERE run_id=?', (run2,)
).fetchone()[0]
pos_count = conn.execute(
    'SELECT COUNT(*) FROM wf_positions WHERE run_id=?', (run2,)
).fetchone()[0]
print("\nRun 2 signals:", sig_count, " positions:", pos_count)
conn.close()
