# Position Sizing v2.1

Multi-asset position sizer and compounding planner. Single static `index.html`, runs entirely in the browser (no backend, no data stored).

## Features
- Stop loss from your chart price, or an auto stop % from entry
- Reward : risk targets with exact stop loss and exit prices
- Trading fees included in sizing, so loss plus fees stays within your risk %
- Rough isolated liquidation check for crypto perps
- 0.001 coin step for Bybit / Binance; lots for forex, gold and indices
- USD/xxx forex pairs (USDJPY etc.) converted back to USD
- Compounding planner with a reality check on losses
- English / Bahasa Melayu, light / dark

## Deploy to Netlify
1. Netlify → **Add new project → Import an existing project → GitHub** → pick `dewatagiri/position-sizing`.
2. Build command: *(leave empty)*. Publish directory: `.` (already set in `netlify.toml`).
3. Deploy.

Educational calculator only, not financial advice.
