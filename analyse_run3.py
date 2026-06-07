import sqlite3

conn = sqlite3.connect('./data/wf_sim.db')
conn.row_factory = sqlite3.Row

run_id = 'a1717d6f-376c-4216-943e-65068295bcde'

# ---- POSITIONS ----
print('=== POSITIONS ===')
pos = conn.execute('''
    SELECT ticker, entry_date, exit_date, entry_price, exit_price, stop_price, shares,
           risk_amount, gross_pnl, net_pnl, exit_reason, gap_filled, signal_type, conviction
    FROM wf_positions WHERE run_id=? ORDER BY entry_date
''', (run_id,)).fetchall()
print(f'Total positions: {len(pos)}')

for p in pos:
    gap = 'Y' if p['gap_filled'] else 'N'
    ep  = float(p['exit_price'])  if p['exit_price']  is not None else 0.0
    gp  = float(p['gross_pnl'])   if p['gross_pnl']   is not None else 0.0
    np_ = float(p['net_pnl'])     if p['net_pnl']     is not None else 0.0
    ra  = float(p['risk_amount']) if p['risk_amount']  is not None else 0.0
    rr  = np_ / ra if ra else 0.0
    pnl_sign = '+' if np_ >= 0 else ''
    print(
        f"  {p['ticker']:<8} {str(p['entry_date']):<12} -> {str(p['exit_date']):<12}"
        f"  entry {float(p['entry_price']):>8.2f}  exit {ep:>8.2f}  stop {float(p['stop_price']):>8.2f}"
        f"  {int(p['shares']):>5} shr  risk ${ra:>7.2f}"
        f"  net {pnl_sign}${np_:>8.2f}  R={rr:>+.2f}"
        f"  {str(p['exit_reason']):<12}  gap={gap}"
        f"  {str(p['signal_type']):<18} {p['conviction']}"
    )

# ---- ENTERED SIGNALS ONLY ----
print()
print('=== SIGNALS THAT WERE ENTERED ===')
entered = conn.execute('''
    SELECT signal_date, ticker, signal_type, conviction, signal_rank, entry_price, stop_price, action
    FROM wf_signals WHERE run_id=? AND action='entered' ORDER BY signal_date
''', (run_id,)).fetchall()
for s in entered:
    print(f"  {s['signal_date']}  #{s['signal_rank']}  {s['ticker']:<8}  {s['signal_type']:<20}  {s['conviction']:<10}  entry {float(s['entry_price']):.2f}  stop {float(s['stop_price']):.2f}")

# ---- SIGNAL COUNTS ----
print()
print('=== SIGNAL VOLUME BY MONTH ===')
months = conn.execute('''
    SELECT substr(signal_date,1,7) as month,
           COUNT(*) as total,
           SUM(CASE WHEN action='entered' THEN 1 ELSE 0 END) as entered,
           SUM(CASE WHEN action LIKE 'skipped:%' THEN 1 ELSE 0 END) as skipped
    FROM wf_signals WHERE run_id=?
    GROUP BY month ORDER BY month
''', (run_id,)).fetchall()
for m in months:
    print(f"  {m['month']}  total={m['total']:>4}  entered={m['entered']:>3}  skipped={m['skipped']:>3}")

# ---- SKIP REASON BREAKDOWN ----
print()
print('=== SKIP REASONS ===')
skips = conn.execute('''
    SELECT action, COUNT(*) as n FROM wf_signals
    WHERE run_id=? AND action LIKE 'skipped:%'
    GROUP BY action ORDER BY n DESC
''', (run_id,)).fetchall()
for sk in skips:
    print(f"  {sk['action']:<55}  {sk['n']:>4}")

conn.close()
