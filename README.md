# TravisShop – Multi-Book Parlay Optimizer

A Streamlit app that pulls live odds from several sportsbooks via [The Odds API](https://the-odds-api.com/),
finds the best price for every bet, and builds three parlays (Safe / Balanced / Lotto) with the highest
fair win probability inside each payout window. Tickets you place can be logged and tracked against your bankroll.

## Setup

```bash
pip install -r requirements.txt
export ODDS_API_KEY=your_key_here   # or paste it into the sidebar
streamlit run app.py
```

You can also put the key in `.streamlit/secrets.toml`:

```toml
ODDS_API_KEY = "your_key_here"
```

## How the numbers are calculated

- **Fair win probability**: for each sportsbook, the prices on every side of a market are converted to implied
  probabilities and scaled so they sum to 100% (removing that book's margin). The fair probability is the average
  across all books offering that line.
- **Best price**: the highest payout available for that exact bet among the books scanned.
- **Expected value (EV)**: `fair probability × decimal payout − 1`. Positive only when line shopping beats the
  consensus price by more than the margin stacked across the parlay's legs.
- Parlays use at most one leg per game, so legs are treated as independent.

These probabilities reflect what the market believes, not an independent prediction model.

## Data

Logged tickets are stored in `enterprise_parlay_bot.db` (SQLite) in the working directory.
Telegram alerts are optional: enter a bot token and chat ID in the sidebar.
