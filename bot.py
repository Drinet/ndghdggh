import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import os
import numpy as np
import json
import sys
import time
from scipy.signal import argrelextrema

# --- CONFIG ---
DISCORD_WEBHOOK = os.getenv('DISCORD_WEBHOOK_URL')
DB_FILE = "trade_history.json"
STARTING_BALANCE = 500.0

# --- LEVERAGE & RISK SETTINGS ---
LEVERAGE = 10                  # High leverage to allow multiple trades
DOLLAR_RISK_PER_TRADE = 7.5    # We lose exactly $7.50 if 2% SL is hit
SL_PERCENT = 0.02              # 2% SL
POSITION_SIZE_USD = DOLLAR_RISK_PER_TRADE / SL_PERCENT  # Total Trade Value ($375)
MARGIN_REQUIRED = POSITION_SIZE_USD / LEVERAGE         # Collateral per trade ($37.50)

# Multi-Exchange Fallback
EXCHANGES = {
    "kraken": ccxt.kraken({'enableRateLimit': True}),
    "binance": ccxt.binance({'enableRateLimit': True}),
    "gateio": ccxt.gateio({'enableRateLimit': True})
}

def log(msg):
    print(f"DEBUG: {msg}", flush=True)

def format_price(price):
    if price is None: return "0.00"
    if price < 0.0001:
        return f"{price:.10f}".rstrip('0').rstrip('.')
    elif price < 1:
        return f"{price:.6f}"
    else:
        return f"{price:.4f}"

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                db = json.load(f)
                db.setdefault("wins", 0)
                db.setdefault("losses", 0)
                db.setdefault("balance", STARTING_BALANCE)
                db.setdefault("active_trades", {})
                # Migration: Ensure older trades don't crash the bot
                for sym, data in db['active_trades'].items():
                    data.setdefault("tp1_hit", False)
                    data.setdefault("tp2_hit", False)
                return db
        except: pass
    return {"wins": 0, "losses": 0, "balance": STARTING_BALANCE, "active_trades": {}}

def save_db(db):
    with open(DB_FILE, 'w') as f:
        json.dump(db, f, indent=4)

def get_top_coins():
    log("Fetching top 120 coins from CoinGecko...")
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {'vs_currency': 'usd', 'order': 'market_cap_desc', 'per_page': 120, 'page': 1}
        data = requests.get(url, params=params).json()
        excluded = ['usdt', 'usdc', 'dai', 'fdusd', 'pyusd', 'usde', 'steth', 'wbtc', 'weth']
        return [c['symbol'].upper() for c in data if c['symbol'].lower() not in excluded]
    except Exception as e:
        log(f"CoinGecko Error: {e}")
        return []

def get_ohlcv_multi_exchange(coin_symbol):
    pair_variants = [f"{coin_symbol}/USD", f"{coin_symbol}/USDT"]
    for ex_name, exchange in EXCHANGES.items():
        for p in pair_variants:
            try:
                bars = exchange.fetch_ohlcv(p, timeframe='15m', limit=150)
                if bars:
                    ticker = exchange.fetch_ticker(p)
                    return bars, ticker['last'], p, ex_name
            except: continue
    return None, None, None, None

def detect_triple_divergence(df, order=4):
    df['RSI'] = ta.rsi(df['close'], length=14)
    df = df.dropna().reset_index(drop=True)
    if len(df) < 50: return None
    low_pivots = argrelextrema(df['close'].values, np.less, order=order)[0]
    if len(low_pivots) >= 3:
        p1, p2, p3 = df['close'].iloc[low_pivots[-3:]].values
        r1, r2, r3 = df['RSI'].iloc[low_pivots[-3:]].values
        if p1 > p2 > p3 and r1 < r2 < r3: return "Long trade"
    high_pivots = argrelextrema(df['close'].values, np.greater, order=order)[0]
    if len(high_pivots) >= 3:
        p1, p2, p3 = df['close'].iloc[high_pivots[-3:]].values
        r1, r2, r3 = df['RSI'].iloc[high_pivots[-3:]].values
        if p1 < p2 < p3 and r1 > r2 > r3: return "Short trade"
    return None

def update_trades(db):
    active = db['active_trades']
    if not active: return False
    
    status_changed = False
    log(f"Updating {len(active)} active trades...")
    
    for sym in list(active.keys()):
        try:
            t = active[sym]
            ex_name = t.get('exchange', 'kraken')
            exchange = EXCHANGES.get(ex_name, EXCHANGES['kraken'])
            ticker = exchange.fetch_ticker(sym)
            curr = ticker['last']
            is_long = (t['side'] == "Long trade")
            
            # TP3 (Final 25% @ 5%)
            if (is_long and curr >= t['tp3']) or (not is_long and curr <= t['tp3']):
                profit = (POSITION_SIZE_USD * 0.25) * 0.05
                db['balance'] += profit
                requests.post(DISCORD_WEBHOOK, json={"content": f"🚀 **{sym} TP3 HIT!** Profit: +${profit:.2f}"})
                del active[sym]
                status_changed = True
                continue
            
            # TP2 (50% @ 3%)
            if not t.get('tp2_hit', False):
                if (is_long and curr >= t['tp2']) or (not is_long and curr <= t['tp2']):
                    profit = (POSITION_SIZE_USD * 0.50) * 0.03
                    db['balance'] += profit
                    t['tp2_hit'] = True
                    requests.post(DISCORD_WEBHOOK, json={"content": f"🎯 **{sym} TP2 HIT!** Profit: +${profit:.2f}"})

            # TP1 (25% @ 1.5%) + Move SL to Entry
            if not t.get('tp1_hit', False):
                if (is_long and curr >= t['tp1']) or (not is_long and curr <= t['tp1']):
                    profit = (POSITION_SIZE_USD * 0.25) * 0.015
                    db['balance'] += profit
                    t['tp1_hit'] = True
                    t['sl'] = t['entry'] 
                    db['wins'] += 1
                    requests.post(DISCORD_WEBHOOK, json={"content": f"✅ **{sym} TP1 HIT!** Profit: +${profit:.2f}. SL moved to entry."})

            # SL HIT
            if (is_long and curr <= t['sl']) or (not is_long and curr >= t['sl']):
                if t.get('tp1_hit', False):
                    requests.post(DISCORD_WEBHOOK, json={"content": f"⚠️ **{sym} Closed at Entry** (Risk-Free)."})
                else:
                    db['balance'] -= DOLLAR_RISK_PER_TRADE
                    db['losses'] += 1
                    requests.post(DISCORD_WEBHOOK, json={"content": f"💀 **{sym} SL Hit**. Loss: -${DOLLAR_RISK_PER_TRADE:.2f}"})
                del active[sym]
                status_changed = True
                
        except Exception as e: log(f"Update error for {sym}: {e}")
    
    return status_changed

def main():
    db = load_db()
    status_changed = update_trades(db)
    
    # Buy Power Logic
    used_margin = len(db['active_trades']) * MARGIN_REQUIRED
    available_margin = db['balance'] - used_margin
    log(f"Balance: ${db['balance']:.2f} | Available Margin: ${available_margin:.2f}")

    new_trade_opened = False
    if available_margin >= MARGIN_REQUIRED:
        coins = get_top_coins()
        for i, coin in enumerate(coins, 1):
            if any(coin in key for key in db['active_trades'].keys()): continue

            bars, last_price, pair_name, ex_name = get_ohlcv_multi_exchange(coin)
            if not bars: continue

            df = pd.DataFrame(bars, columns=['date', 'open', 'high', 'low', 'close', 'vol'])
            signal = detect_triple_divergence(df)
            
            if signal:
                entry = last_price
                mult = 1 if signal == "Long trade" else -1
                t_data = {
                    "side": signal, "entry": entry, "exchange": ex_name,
                    "sl": entry * (1 - (0.02 * mult)),
                    "tp1": entry * (1 + (0.015 * mult)),
                    "tp2": entry * (1 + (0.03 * mult)),
                    "tp3": entry * (1 + (0.05 * mult)),
                    "tp1_hit": False, "tp2_hit": False
                }
                db['active_trades'][pair_name] = t_data
                new_trade_opened = True
                
                total = db['wins'] + db['losses']
                wr = (db['wins'] / total * 100) if total > 0 else 0
                pnl = db['balance'] - STARTING_BALANCE
                
                msg = (f"✨ **{signal.upper()}**\n🪙 **${coin}** ({ex_name})\n"
                       f"💵 Entry: {format_price(entry)}\n🛑 SL: {format_price(t_data['sl'])}\n"
                       f"🎯 TP1: {format_price(t_data['tp1'])} | TP2: {format_price(t_data['tp2'])} | TP3: {format_price(t_data['tp3'])}\n\n"
                       f"💰 **Balance: ${db['balance']:.2f}** ({'+' if pnl >=0 else ''}{pnl:.2f})\n"
                       f"📊 **Winrate: {wr:.1f}%** ({db['wins']}W | {db['losses']}L)")
                requests.post(DISCORD_WEBHOOK, json={"content": msg})

    if (status_changed or new_trade_opened) and db['active_trades']:
        trade_list = "\n".join([f"{s.split('/')[0]}: {v['side']}" for s, v in db['active_trades'].items()])
        summary = f"📑 **Updated Active Trades**:\n||{trade_list}||"
        requests.post(DISCORD_WEBHOOK, json={"content": summary})
    
    save_db(db)

if __name__ == "__main__":
    main()
