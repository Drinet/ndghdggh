import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import os
import json
from datetime import datetime

# --- CONFIG ---
DISCORD_WEBHOOK = os.getenv('DISCORD_WEBHOOK_URL')
TIMEFRAMES = {
    "4h": "4h",
    "Daily": "1d",
    "3-Day": "3d",
    "Weekly": "1w"
}
PROXIMITY_THRESHOLD = 0.02  # 2% distance for "Close to" alerts

# Multi-Exchange Fallback
EXCHANGES = {
    "binance": ccxt.binance({'enableRateLimit': True}),
    "kraken": ccxt.kraken({'enableRateLimit': True}),
    "gateio": ccxt.gateio({'enableRateLimit': True})
}

def log(msg):
    print(f"DEBUG: {msg}", flush=True)

def format_price(price):
    if price is None: return "0.00"
    return f"{price:.4f}" if price >= 1 else f"{price:.7f}".rstrip('0')

def get_top_coins():
    log("Fetching top 120 coins...")
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {'vs_currency': 'usd', 'order': 'market_cap_desc', 'per_page': 120, 'page': 1}
        data = requests.get(url, params=params).json()
        excluded = ['usdt', 'usdc', 'dai', 'fdusd', 'pyusd', 'usde', 'steth', 'wbtc', 'weth', 'rlusd', 'usdg', 'usds', 'meth', 'usdd', 'lseth', 'usd1']
        return [c['symbol'].upper() for c in data if c['symbol'].lower() not in excluded]
    except Exception as e:
        log(f"CoinGecko Error: {e}")
        return []

def get_data(exchange, symbol, tf):
    try:
        pair = f"{symbol}/USDT" if "binance" in exchange.id else f"{symbol}/USD"
        bars = exchange.fetch_ohlcv(pair, timeframe=tf, limit=250)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['sma200'] = ta.sma(df['close'], length=200)
        return df.iloc[-1], pair
    except:
        return None, None

def main():
    coins = get_top_coins()
    touch_alerts = []
    proximity_alerts = {tf_name: [] for tf_name in TIMEFRAMES}
    
    # Determine if we should send the "Hourly Proximity Update"
    current_min = datetime.now().minute
    is_hourly_update = current_min < 15 # Runs once per hour if triggered every 15m

    for coin in coins:
        found_data = False
        for ex_name, ex_obj in EXCHANGES.items():
            if found_data: break
            
            for tf_name, tf_code in TIMEFRAMES.items():
                last_row, pair = get_data(ex_obj, coin, tf_code)
                if last_row is None or pd.isna(last_row['sma200']): continue
                
                found_data = True
                curr_price = last_row['close']
                sma = last_row['sma200']
                diff = abs(curr_price - sma) / sma

                # 1. Check for Touches (Price crossed or is exactly at SMA)
                # We check if low <= SMA <= high of the current candle
                if last_row['low'] <= sma <= last_row['high']:
                    touch_alerts.append(f"🔔 **${coin}** touched the **{tf_name}** 200 SMA! \nPrice: `{format_price(curr_price)}`")

                # 2. Check for Proximity (Within 2%)
                elif diff <= PROXIMITY_THRESHOLD:
                    proximity_alerts[tf_name].append(f"${coin} ({format_price(curr_price)})")

    # --- DISCORD POSTING ---
    
    # Send Immediate Touches
    for alert in touch_alerts:
        requests.post(DISCORD_WEBHOOK, json={"content": alert})

    # Send Hourly Proximity Summary
    if is_hourly_update:
        summary_msg = "🕒 **Hourly 200 SMA Proximity Update** (within 2%)\n"
        has_prox = False
        for tf, list_of_coins in proximity_alerts.items():
            if list_of_coins:
                has_prox = True
                summary_msg += f"\n**{tf}**: {', '.join(list_of_coins)}"
        
        if has_prox:
            requests.post(DISCORD_WEBHOOK, json={"content": summary_msg})

if __name__ == "__main__":
    main()
