"""Research data cache — separate from the trading pipeline in src/.

Nothing in src/ reads anything under this package, and nothing here writes to
output/nse.db. See data/fetch.py for why the separation is deliberate.
"""
