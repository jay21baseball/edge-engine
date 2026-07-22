"""Collapse the duplicate-snapshot backlog and reclaim disk.

One-off cleanup for databases written before change-detection landed. Keeps the
most recent snapshot per market and discards the identical repeats.
"""
import sqlite3

con = sqlite3.connect("data/edge.db")
before = con.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
print(f"snapshot rows before: {before:,}")

con.execute(
    "DELETE FROM market_snapshots WHERE id NOT IN "
    "(SELECT MAX(id) FROM market_snapshots GROUP BY key)"
)
con.commit()

after = con.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
print(f"rows after:           {after:,}  ({before - after:,} removed)")

print("vacuuming (reclaims the file space)...")
con.execute("VACUUM")
con.close()
print("done")
