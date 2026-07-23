import sys
sys.path.insert(0, "src")
from edge_engine.ingest.polymarket import PolymarketClient
from edge_engine.whales.tracker import Whale, format_trade

poly = PolymarketClient()
tony = Whale(name="Tony", address="0x204f72f35326db932158cba6adff0b9a1da95e14",
             username="swisstony")
trades = poly.recent_trades(tony.address, limit=4)
value = poly.portfolio_value(tony.address)
print("=== how a whale alert will actually read ===\n")
for t in trades[:3]:
    print(format_trade(tony, t, value))
    print("\n" + "-" * 40 + "\n")
