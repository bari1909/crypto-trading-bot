import os

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="AI Crypto Trader", layout="wide")
API = os.getenv("API_URL", "http://api:8000")
TOKEN = os.getenv("API_TOKEN", "")


def get(path, **params):
    return requests.get(API + path, params=params, timeout=5).json()


st.title(f"AI Crypto Trader · {os.getenv('MARKET', 'spot').upper()}")
try:
    status, health = get("/status"), get("/health")
except Exception as exc:  # noqa: BLE001
    st.error(f"API unavailable: {exc}")
    st.stop()

c = st.columns(5)
c[0].metric("State", status["state"])
c[1].metric("Mode", status["mode"].upper())
c[2].metric("Realized PnL", f"{status['realized_pnl']:+.4f}")
c[3].metric("Closed trades", f"{status['closed_trades']} ({status['wins']} wins)")
c[4].metric("Worker", "alive" if health["ok"] else "NO HEARTBEAT")
if status["reason"]:
    st.caption(f"Reason: {status['reason']}")

st.subheader("Strategy selector")
st.caption("The bot only trades pairs marked eligible: recent backtest after fees passed the min-trades, "
           "profit-factor, expectancy and drawdown rules. Refreshed every few hours.")
scores = pd.DataFrame(get("/strategies"))
if scores.empty:
    st.info("Selector has not run yet.")
else:
    st.dataframe(scores, use_container_width=True, hide_index=True)

st.subheader("Open positions")
st.dataframe(pd.DataFrame(get("/positions", status="OPEN")), use_container_width=True, hide_index=True)

closed = pd.DataFrame(get("/positions", status="CLOSED", limit=1000))
st.subheader("Equity curve (realized)")
if not closed.empty:
    closed = closed.sort_values("closed_at")
    closed["cum_pnl"] = closed.pnl.cumsum()
    st.line_chart(closed.set_index("closed_at")["cum_pnl"])
    st.subheader("Closed trades")
    st.dataframe(closed.iloc[::-1], use_container_width=True, hide_index=True)
else:
    st.info("No closed trades yet.")

st.subheader("Events")
st.dataframe(pd.DataFrame(get("/events", limit=200)), use_container_width=True, hide_index=True)

st.subheader("Controls")
if not TOKEN:
    st.warning("Set API_TOKEN in .env to enable controls.")
else:
    cols = st.columns(4)
    for col, action in zip(cols, ["pause", "resume", "stop", "reset"]):
        if col.button(action.upper() if action != "stop" else "EMERGENCY STOP"):
            r = requests.post(f"{API}/control/{action}", headers={"X-API-Token": TOKEN}, timeout=5)
            (st.success if r.ok else st.error)(r.json())
