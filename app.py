import datetime as dt
import math
import os
import sqlite3
from collections import defaultdict
from contextlib import closing

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

# -------------------------------------------------------------------
# CONFIGURATION & PAGE SETUP
# -------------------------------------------------------------------
st.set_page_config(
    page_title="Multi-Book Parlay Optimizer",
    page_icon="🧠",
    layout="wide",
)

DB_PATH = "enterprise_parlay_bot.db"
ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"
BOOKMAKERS_TO_SCAN = ["draftkings", "fanduel", "betmgm", "caesars", "bet365"]
SPORTS = ["basketball_nba", "americanfootball_nfl", "icehockey_nhl", "baseball_mlb"]
MARKET_LABELS = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}
STATUSES = ["Pending", "Won", "Lost"]

# Payout window (decimal multiplier), stake and leg-count rules per tier.
TIERS = [
    {"name": "PARLAY #1: SAFE", "stake": 20.0, "min_mult": 2.0, "max_mult": 3.0, "min_legs": 1, "max_legs": 4},
    {"name": "PARLAY #2: BALANCED", "stake": 10.0, "min_mult": 4.0, "max_mult": 8.0, "min_legs": 2, "max_legs": 6},
    {"name": "PARLAY #3: LOTTO", "stake": 5.0, "min_mult": 10.0, "max_mult": 25.0, "min_legs": 3, "max_legs": 8},
]


def tier_label(tier):
    return f"{tier['name']} (${tier['stake']:.0f} Stake)"


def get_default_api_key():
    """Read the key from .streamlit/secrets.toml or the ODDS_API_KEY env var."""
    try:
        key = st.secrets.get("ODDS_API_KEY", "")
    except Exception:  # no secrets file present
        key = ""
    return key or os.environ.get("ODDS_API_KEY", "")


# -------------------------------------------------------------------
# 1. PERSISTENT DATABASE LAYER (SQLite)
# -------------------------------------------------------------------
def _connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with closing(_connect()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                tier TEXT,
                stake REAL,
                multiplier REAL,
                win_prob REAL,
                potential_payout REAL,
                selections TEXT,
                status TEXT,
                expected_value REAL
            )
        """)
        # Databases created by the earlier version lack this column.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
        if "expected_value" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN expected_value REAL")


def save_ticket_to_db(tier, ticket):
    sel_str = " | ".join(
        f"[{l['type']}] {l['match']} -> {l['selection']} @ {l['best_book']} ({l['american_odds']:+d})"
        for l in ticket["legs"]
    )
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO tickets (date, tier, stake, multiplier, win_prob, potential_payout,
                                 selections, status, expected_value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                tier,
                ticket["stake"],
                ticket["total_multiplier"],
                round(ticket["win_prob"] * 100, 2),
                ticket["potential_payout"],
                sel_str,
                "Pending",
                round(ticket["expected_value"] * 100, 2),
            ),
        )


def update_ticket_status(ticket_id, new_status):
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (new_status, int(ticket_id)))


def delete_ticket(ticket_id):
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM tickets WHERE id = ?", (int(ticket_id),))


def load_ticket_history():
    with closing(_connect()) as conn:
        return pd.read_sql_query("SELECT * FROM tickets ORDER BY id DESC", conn)


init_db()


# -------------------------------------------------------------------
# 2. ODDS INGESTION, LINE SHOPPING & NO-VIG PROBABILITIES
# -------------------------------------------------------------------
class OddsAPIError(Exception):
    pass


@st.cache_data(ttl=120, show_spinner=False)
def fetch_multi_book_odds(api_key, sport_key):
    """Returns (games, requests_remaining). Cached for 2 minutes to save API quota.
    Errors are raised (and therefore not cached)."""
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": ",".join(MARKET_LABELS),
        "oddsFormat": "american",
        "bookmakers": ",".join(BOOKMAKERS_TO_SCAN),
    }
    resp = requests.get(ODDS_API_URL.format(sport=sport_key), params=params, timeout=15)
    if resp.status_code != 200:
        try:
            msg = resp.json().get("message", resp.text)
        except ValueError:
            msg = resp.text
        raise OddsAPIError(f"The Odds API returned HTTP {resp.status_code}: {msg}")
    return resp.json(), resp.headers.get("x-requests-remaining")


def american_to_decimal(odds):
    return (odds / 100.0) + 1.0 if odds > 0 else (100.0 / abs(odds)) + 1.0


def decimal_to_american(dec):
    return int(round((dec - 1.0) * 100)) if dec >= 2.0 else int(round(-100 / (dec - 1.0)))


def _parse_time(value):
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _selection_name(m_key, outcome):
    if m_key == "h2h":
        return f"{outcome['name']} ML"
    point = outcome.get("point")
    if point is None:
        return None
    if m_key == "spreads":
        return f"{outcome['name']} {point:+g}"
    return f"{outcome['name']} {point:g}"


def build_markets(raw_games, upcoming_only=True):
    """
    For every selection (game + market + side + line), collect each book's price
    and that book's no-vig probability. The no-vig probability comes from the
    *same book's* prices on every side of that market, so the bookmaker's margin
    is removed correctly. The fair probability is the average across books.
    """
    now = dt.datetime.now(dt.timezone.utc)
    agg = {}

    for game in raw_games:
        start = _parse_time(game.get("commence_time"))
        if upcoming_only and start is not None and start <= now:
            continue
        home, away = game.get("home_team"), game.get("away_team")
        match = f"{away} @ {home}"

        for bm in game.get("bookmakers", []):
            book = bm.get("key")
            for market in bm.get("markets", []):
                m_key = market.get("key")
                outcomes = market.get("outcomes", [])
                if m_key not in MARKET_LABELS or len(outcomes) < 2:
                    continue
                try:
                    decimals = [american_to_decimal(o["price"]) for o in outcomes]
                except (KeyError, TypeError, ZeroDivisionError):
                    continue
                overround = sum(1.0 / d for d in decimals)

                for out, dec in zip(outcomes, decimals):
                    sel = _selection_name(m_key, out)
                    if sel is None:
                        continue
                    entry = agg.setdefault(
                        (match, MARKET_LABELS[m_key], sel),
                        {"start": start, "prices": {}, "fair_probs": []},
                    )
                    entry["prices"][book] = int(out["price"])
                    entry["fair_probs"].append((1.0 / dec) / overround)

    legs = []
    for (match, m_type, sel), entry in agg.items():
        best_book, best_price = max(entry["prices"].items(), key=lambda kv: american_to_decimal(kv[1]))
        dec = american_to_decimal(best_price)
        p_fair = sum(entry["fair_probs"]) / len(entry["fair_probs"])
        legs.append({
            "match": match,
            "type": m_type,
            "selection": sel,
            "start": entry["start"],
            "best_book": best_book,
            "american_odds": best_price,
            "decimal_odds": dec,
            "all_prices": entry["prices"],
            "num_books": len(entry["prices"]),
            "p_fair": p_fair,
            "leg_ev": p_fair * dec - 1.0,
        })
    return legs


# -------------------------------------------------------------------
# 3. PARLAY OPTIMIZER (branch and bound, one leg per game)
# -------------------------------------------------------------------
def optimize_parlay(legs, stake, min_mult, max_mult, min_legs, max_legs):
    """
    Finds the parlay with the highest joint fair win probability whose multiplier
    is inside [min_mult, max_mult], using at most one leg per game.

    Exact search, pruned two ways:
      * adding a leg can only raise the multiplier, so stop once it exceeds max_mult;
      * adding a leg can only lower the probability, so stop once a branch can't
        beat the best parlay found so far.
    """
    by_match = defaultdict(list)
    for leg in legs:
        by_match[leg["match"]].append(leg)
    # Try the likeliest legs first so good parlays are found early and prune more.
    games = [sorted(g, key=lambda l: l["p_fair"], reverse=True) for g in by_match.values()]
    games.sort(key=lambda g: g[0]["p_fair"], reverse=True)
    n_games = len(games)

    best = {"prob": 0.0, "legs": None}
    chosen = []

    def search(i, mult, prob):
        if prob <= best["prob"]:
            return
        k = len(chosen)
        if k >= min_legs and min_mult <= mult <= max_mult:
            # Any extension has a lower probability, so this branch is done.
            best["prob"], best["legs"] = prob, list(chosen)
            return
        if k == max_legs or i == n_games or (n_games - i) < (min_legs - k):
            return
        for leg in games[i]:
            new_mult = mult * leg["decimal_odds"]
            if new_mult > max_mult:
                continue
            chosen.append(leg)
            search(i + 1, new_mult, prob * leg["p_fair"])
            chosen.pop()
        search(i + 1, mult, prob)  # take nothing from this game

    search(0, 1.0, 1.0)

    if best["legs"] is None:
        return None
    combo = best["legs"]
    total_mult = math.prod(l["decimal_odds"] for l in combo)
    win_prob = math.prod(l["p_fair"] for l in combo)
    return {
        "stake": stake,
        "legs": combo,
        "total_multiplier": round(total_mult, 2),
        "combined_american": decimal_to_american(total_mult),
        "win_prob": win_prob,
        "expected_value": win_prob * total_mult - 1.0,
        "potential_payout": round(stake * total_mult, 2),
        "potential_profit": round(stake * total_mult - stake, 2),
    }


# -------------------------------------------------------------------
# 4. NOTIFICATIONS
# -------------------------------------------------------------------
def send_telegram(bot_token, chat_id, text):
    """Returns None on success, otherwise an error message."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return str(exc)
    if not body.get("ok"):
        return body.get("description", f"HTTP {resp.status_code}")
    return None


def telegram_summary(tickets):
    lines = []
    for tier, ticket in tickets:
        if ticket is None:
            continue
        lines.append(
            f"{tier_label(tier)}\n"
            f"Multiplier: {ticket['total_multiplier']}x ({ticket['combined_american']:+d})\n"
            f"Win prob: {ticket['win_prob']:.1%} | EV: {ticket['expected_value']:+.1%}"
        )
        for leg in ticket["legs"]:
            lines.append(f"  • {leg['match']}: {leg['selection']} @ {leg['best_book']} ({leg['american_odds']:+d})")
        lines.append("")
    return "\n".join(lines).strip()


# -------------------------------------------------------------------
# 5. STREAMLIT FRONTEND & DASHBOARD
# -------------------------------------------------------------------
st.title("🧠 Multi-Book Parlay Optimizer")
st.markdown(
    "Shops every line across sportsbooks, removes each book's margin to get a fair win probability, "
    "and searches for the most likely parlay in each payout window."
)
st.caption(
    "Win probabilities are the books' own consensus with the margin removed, not an independent "
    "prediction. Parlays stack the margin on every leg, so expected value is usually negative; "
    "the EV figure shows how much of that line shopping wins back."
)

if "saved" not in st.session_state:
    st.session_state.saved = set()

with st.sidebar:
    st.header("⚙️ Bot Controls")
    api_key_input = st.text_input(
        "The Odds API Key",
        value=get_default_api_key(),
        type="password",
        help="Can also be set via the ODDS_API_KEY environment variable or .streamlit/secrets.toml.",
    )
    sport_selection = st.selectbox("Sport League", SPORTS)
    upcoming_only = st.checkbox("Skip games that have already started", value=True)
    starting_bankroll = st.number_input("Starting Capital ($)", value=500.0, step=50.0)
    with st.expander("Telegram alerts (optional)"):
        tg_token = st.text_input("Bot token", type="password")
        tg_chat = st.text_input("Chat ID", placeholder="@your_channel or numeric ID")

    run_btn = st.button("Generate Optimized Parlays", type="primary")

if run_btn:
    if not api_key_input:
        st.error("Enter your The Odds API key in the sidebar.")
    else:
        with st.spinner("Fetching odds from every book and searching parlay combinations..."):
            raw_data = None
            try:
                raw_data, remaining = fetch_multi_book_odds(api_key_input, sport_selection)
            except (OddsAPIError, requests.RequestException) as exc:
                st.error(f"Could not fetch odds. {exc}")

            if raw_data is not None:
                legs = build_markets(raw_data, upcoming_only)
                if not legs:
                    st.warning("No odds available: there are no upcoming games for this league right now.")
                else:
                    tickets = [
                        (tier, optimize_parlay(legs, tier["stake"], tier["min_mult"], tier["max_mult"],
                                               tier["min_legs"], tier["max_legs"]))
                        for tier in TIERS
                    ]
                    st.session_state.results = {
                        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "sport": sport_selection,
                        "num_games": len({l["match"] for l in legs}),
                        "num_legs": len(legs),
                        "remaining": remaining,
                        "tickets": tickets,
                    }
                    st.session_state.saved = set()

                    if tg_token and tg_chat:
                        summary = telegram_summary(tickets)
                        if summary:
                            err = send_telegram(tg_token, tg_chat, summary)
                            if err:
                                st.warning(f"Telegram message failed: {err}")
                            else:
                                st.toast("Sent to Telegram")


def _save_ticket(label, ticket):
    save_ticket_to_db(label, ticket)
    st.session_state.saved.add(label)


results = st.session_state.get("results")
if results:
    remaining = results["remaining"]
    st.caption(
        f"{results['sport']} · generated {results['generated_at']} · {results['num_games']} games · "
        f"{results['num_legs']} selections"
        + (f" · API requests remaining: {remaining}" if remaining is not None else "")
    )
    columns = st.columns(len(results["tickets"]))
    for i, ((tier, ticket), column) in enumerate(zip(results["tickets"], columns)):
        label = tier_label(tier)
        with column:
            st.subheader(label)
            if ticket is None:
                st.warning("No combination found in this payout window.")
                continue
            st.metric("Stake Amount", f"${ticket['stake']:.2f}")
            st.metric("Multiplier", f"{ticket['total_multiplier']}x ({ticket['combined_american']:+d})")
            st.metric("Win Probability (no-vig)", f"{ticket['win_prob']:.2%}")
            st.metric("Expected Value", f"{ticket['expected_value']:+.1%}")
            st.metric("Potential Profit", f"${ticket['potential_profit']:.2f}")

            st.markdown("**Selections & Best Sportsbook:**")
            for idx, leg in enumerate(ticket["legs"], 1):
                start = leg["start"].astimezone().strftime("%a %H:%M") if leg["start"] else "TBD"
                others = " · ".join(f"{b}: {p:+d}" for b, p in sorted(leg["all_prices"].items()))
                st.markdown(f"**{idx}. [{leg['type']}] {leg['match']}**")
                st.markdown(
                    f"&nbsp;&nbsp;&nbsp;&nbsp;👉 **{leg['selection']}** @ "
                    f"**{leg['best_book'].upper()}** ({leg['american_odds']:+d})"
                )
                st.caption(
                    f"Fair prob {leg['p_fair']:.1%} · Leg EV {leg['leg_ev']:+.1%} · "
                    f"{leg['num_books']} books · {start}\n\n{others}"
                )

            saved = label in st.session_state.saved
            st.button(
                "Saved ✓" if saved else "Save to bet log",
                key=f"save_{i}",
                disabled=saved,
                on_click=_save_ticket,
                args=(label, ticket),
            )

st.divider()

# -------------------------------------------------------------------
# 6. BANKROLL & ANALYTICS VISUALIZATION
# -------------------------------------------------------------------
st.header("📊 Bankroll Tracking & Performance Analytics")
history_df = load_ticket_history()

if history_df.empty:
    st.info("No tickets logged yet. Generate parlays, then click 'Save to bet log' on the ones you place.")
else:
    chron_df = history_df.sort_values(by="id")
    profit = chron_df.apply(
        lambda r: r["potential_payout"] - r["stake"] if r["status"] == "Won"
        else (-r["stake"] if r["status"] == "Lost" else 0.0),
        axis=1,
    )
    br_series = [starting_bankroll] + list(starting_bankroll + profit.cumsum())
    wins = int((chron_df["status"] == "Won").sum())
    losses = int((chron_df["status"] == "Lost").sum())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Starting Bankroll", f"${starting_bankroll:,.2f}")
    m2.metric("Tickets Logged", f"{len(history_df)}")
    m3.metric("Record (W-L)", f"{wins}-{losses}")
    m4.metric("Current Bankroll", f"${br_series[-1]:,.2f}", delta=f"{br_series[-1] - starting_bankroll:+,.2f}")

    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        st.subheader("📈 Bankroll Over Time")
        fig_br = px.line(
            x=list(range(len(br_series))),
            y=br_series,
            labels={"x": "Ticket #", "y": "Bankroll ($)"},
            markers=True,
        )
        fig_br.update_layout(margin=dict(l=20, r=20, t=30, b=20))
        st.plotly_chart(fig_br)

    with chart_col2:
        st.subheader("📊 Ticket Outcomes by Tier")
        fig_tier = px.histogram(
            history_df, x="tier", color="status", barmode="group",
            category_orders={"status": STATUSES},
            labels={"tier": "Tier", "count": "Tickets"},
        )
        fig_tier.update_layout(margin=dict(l=20, r=20, t=30, b=20))
        st.plotly_chart(fig_tier)

    def _set_status(ticket_id, widget_key):
        update_ticket_status(ticket_id, st.session_state[widget_key])

    st.subheader("📜 Ticket Log & Status Updates")
    for _, row in history_df.iterrows():
        tid = int(row["id"])
        with st.expander(f"Ticket #{tid} | {row['tier']} | Stake: ${row['stake']:.2f} | Status: {row['status']}"):
            st.write(f"**Date:** {row['date']}")
            st.write(f"**Selections:** {row['selections']}")
            st.write(f"**Payout:** ${row['potential_payout']:.2f} ({row['multiplier']}x)")
            win_prob = f"{row['win_prob']:.2f}%" if pd.notna(row["win_prob"]) else "n/a"
            ev = f"{row['expected_value']:+.1f}%" if pd.notna(row.get("expected_value")) else "n/a"
            st.write(f"**Win prob at save:** {win_prob} · **EV:** {ev}")

            widget_key = f"status_{tid}"
            st.selectbox(
                "Update Outcome",
                STATUSES,
                index=STATUSES.index(row["status"]) if row["status"] in STATUSES else 0,
                key=widget_key,
                on_change=_set_status,
                args=(tid, widget_key),
            )
            st.button("Delete ticket", key=f"delete_{tid}", on_click=delete_ticket, args=(tid,))
