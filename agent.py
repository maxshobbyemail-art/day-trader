from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from datetime import datetime, timedelta, timezone
import pandas as pd
import time
import requests
import json
import os

from config import API_KEY, SECRET_KEY, ANTHROPIC_KEY, EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO









client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

SYMBOLS = ["TSLA", "NVDA", "AMD", "MSFT"]
MAX_INVESTMENT = 500
STOP_LOSS_PCT = 0.025
LOG_FILE = "daily_log.txt"
STATUS_FILE = "status.json"
last_order_time = {}
def log(msg):
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    line = f"{timestamp} | {msg}"
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def write_status(boxes, active_position, buy_price, capital):
    status = {
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
        'capital': round(capital, 2),
        'active_position': active_position,
        'buy_price': buy_price,
        'boxes': {s: {k: round(v, 2) for k, v in b.items()} for s, b in boxes.items()}
    }
    with open(STATUS_FILE, 'w') as f:
        json.dump(status, f, indent=2)

def can_place_order(symbol):
    now = datetime.now(timezone.utc)
    if symbol in last_order_time:
        elapsed = (now - last_order_time[symbol]).seconds
        if elapsed < 300:
            return False
    return True

def get_real_position():
    try:
        positions = trading_client.get_all_positions()
        for p in positions:
            if p.symbol in SYMBOLS:
                side = 'long' if float(p.qty) > 0 else 'short'
                return {
                    'symbol': p.symbol,
                    'side': side,
                    'shares': abs(float(p.qty)),
                    'entry_price': float(p.avg_entry_price)
                }
        return None
    except Exception as e:
        print(f"Error getting positions: {e}")
        return None

def get_position(symbol):
    try:
        position = trading_client.get_open_position(symbol)
        return float(position.qty)
    except:
        return 0

def get_current_price(symbol, side=None):
    try:
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quote = client.get_stock_latest_quote(request)
        bid = quote[symbol].bid_price
        ask = quote[symbol].ask_price
        if side == 'buy':
            return ask
        elif side == 'sell':
            return bid
        else:
            return (ask + bid) / 2
    except:
        return None

def is_market_hours(ts):
    if ts.hour < 13:
        return False
    if ts.hour == 13 and ts.minute < 30:
        return False
    if ts.hour >= 19 and ts.minute >= 30:
        return False
    if ts.hour >= 20:
        return False
    return True

def get_box_levels(symbol):
    now = datetime.now(timezone.utc)
    prev_day = now.date() - timedelta(days=1)
    while prev_day.weekday() >= 5:
        prev_day -= timedelta(days=1)
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=datetime(prev_day.year, prev_day.month, prev_day.day, 13, 30, 0),
        end=datetime(prev_day.year, prev_day.month, prev_day.day, 20, 0, 0),
        limit=1000
    )
    bars = client.get_stock_bars(request)
    df = bars.df.copy()
    box_high = df['high'].max()
    box_low = df['low'].min()
    box_mid = (box_high + box_low) / 2
    return box_high, box_low, box_mid

def get_hourly_signal(symbol, box_high, box_low, proximity=0.003):
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Hour,
        start=datetime.now(timezone.utc) - timedelta(hours=6),
        limit=6
    )
    bars = client.get_stock_bars(request)
    try:
        df = bars.df.reset_index(level='symbol', drop=True)
    except:
        df = bars.df
    if len(df) < 2:
        return 0, None, None
    prev_bar = df.iloc[-2]
    curr_bar = df.iloc[-1]
    near_bottom = prev_bar['close'] <= box_low * (1 + proximity)
    prev_green = prev_bar['close'] > prev_bar['open']
    curr_breaks_high = curr_bar['close'] > prev_bar['high']
    if near_bottom and prev_green and curr_breaks_high:
        return 1, curr_bar['close'], box_low * 0.998
    near_top = prev_bar['close'] >= box_high * (1 - proximity)
    prev_red = prev_bar['close'] < prev_bar['open']
    curr_breaks_low = curr_bar['close'] < prev_bar['low']
    if near_top and prev_red and curr_breaks_low:
        return -1, curr_bar['close'], box_high * 1.002

    # Breakout logic - price already outside the box
    box_range = box_high - box_low
    above_box = curr_bar['close'] > box_high * (1 + proximity)
    below_box = curr_bar['close'] < box_low * (1 - proximity)

    if above_box:
        # Price broke above box - look for long breakout confirmation
        curr_bullish = curr_bar['close'] > curr_bar['open']
        curr_momentum = curr_bar['close'] > prev_bar['close']
        if curr_bullish and curr_momentum:
            target = box_high + box_range
            stop = box_high * 0.998  # stop just back inside box
            return 2, curr_bar['close'], stop  # signal 2 = breakout long

    if below_box:
        # Price broke below box - look for short breakout confirmation
        curr_bearish = curr_bar['close'] < curr_bar['open']
        curr_momentum = curr_bar['close'] < prev_bar['close']
        if curr_bearish and curr_momentum:
            target = box_low - box_range
            stop = box_low * 1.002  # stop just back inside box
            return -2, curr_bar['close'], stop  # signal -2 = breakout short

    return 0, curr_bar['close'], None

def get_shares(invest_amount, price, side):
    if side == 'short':
        return max(1, int(invest_amount / price))
    else:
        return round(invest_amount / price, 4)
def check_earnings(symbols):
    from datetime import timezone
    earnings_soon = {}
    today = datetime.now(timezone.utc).date()
    check_until = today + timedelta(days=2)
    for symbol in symbols:
        try:
            url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=calendarEvents"
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers)
            data = response.json()
            earnings_dates = data['quoteSummary']['result'][0]['calendarEvents']['earnings']['earningsDate']
            for date_obj in earnings_dates:
                earnings_date = datetime.fromtimestamp(date_obj['raw'], tz=timezone.utc).date()
                if today <= earnings_date <= check_until:
                    earnings_soon[symbol] = str(earnings_date)
                    break
        except:
            pass
    return earnings_soon

def get_market_regime():
    try:
        request = StockBarsRequest(
            symbol_or_symbols=["SPY", "QQQ"],
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=5),
            limit=5
        )
        bars = client.get_stock_bars(request)
        regimes = []
        for symbol in ["SPY", "QQQ"]:
            try:
                df = bars.df.xs(symbol, level="symbol")
                if len(df) >= 2:
                    change = (df.iloc[-1]["close"] - df.iloc[-2]["close"]) / df.iloc[-2]["close"]
                    if change > 0.005:
                        regimes.append("bull")
                    elif change < -0.005:
                        regimes.append("bear")
                    else:
                        regimes.append("neutral")
            except:
                regimes.append("neutral")
        if regimes.count("bull") == 2:
            return "bull"
        elif regimes.count("bear") == 2:
            return "bear"
        else:
            return "neutral"
    except:
        return "neutral"

def get_sentiment(symbol):
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(response.text)
        headlines = [item.find("title").text for item in root.findall(".//item")[:8] if item.find("title") is not None]
        if not headlines:
            return 0, "No headlines found"
        headlines_text = "\n".join(headlines)
        api_response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 100,
                "messages": [{
                    "role": "user",
                    "content": f"Score the sentiment for {symbol} stock based on these headlines from -10 (very bearish) to +10 (very bullish). Reply with only a number and one sentence explanation.\n\nHeadlines:\n{headlines_text}"

                }]
            }
        )
        result = api_response.json()
        text = result["content"][0]["text"]
        import re
        match = re.search(r'-?\d+', text)
        score = int(match.group()) if match else 0
        score = max(-10, min(10, score))
        return score, text
    except Exception as e:
        return 0, "Sentiment check failed"

def get_fill_price(symbol, side, timeout=5):
    import time as t
    for _ in range(timeout):
        t.sleep(1)
        try:
            orders = trading_client.get_orders()
            for order in orders:
                if order.symbol == symbol and order.status.value == 'filled':
                    if (side == 'long' and order.side.value == 'buy') or (side == 'short' and order.side.value == 'sell'):
                        return float(order.filled_avg_price)
        except:
            pass
    return None

def send_daily_summary(date, trades, pnl_today, running_total, sentiment_scores, market_regime, boxes, errors):
    try:
        pnl_sign = "+" if pnl_today >= 0 else ""
        run_sign = "+" if running_total >= 0 else ""
        title = f"Trading Agent {date} | {pnl_sign}${pnl_today:.2f} today"
        body = f"Regime: {market_regime.upper()} | Errors: {errors}\n\n"
        body += "SENTIMENT\n"
        for symbol, score in sentiment_scores.items():
            body += f"{symbol}: {'+' if score >= 0 else ''}{score}  "
        body += "\n\nTRADES\n"
        if trades:
            for t in trades:
                parts = t.split("|")
                if len(parts) >= 2:
                    body += parts[1].strip() + "\n"
        else:
            body += "No trades today\n"
        body += f"\nP&L: {pnl_sign}${pnl_today:.2f} | Running: {run_sign}${running_total:.2f}\n\n"
        body += "TOMORROW BOXES\n"
        for symbol, box in boxes.items():
            body += f"{symbol}: ${box['low']:.2f}-${box['high']:.2f}\n"
        requests.post(
            "https://ntfy.sh/maxtrader0317",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "default"}
        )
        log("✓ Daily summary notification sent")
    except Exception as e:
        log(f"Notification failed: {e}")

def run_agent():
    symbols = ["TSLA", "NVDA", "AMD", "MSFT"]
    max_investment = 500
    active_position = None
    buy_price = None
    best_price = None
    trailing_stop_active = False
    cooldown = {s: False for s in symbols}
    entry_count = {s: 0 for s in symbols}
    earnings_skip = {}
    market_regime = 'neutral'
    sentiment_scores = {s: 0 for s in symbols}
    last_sentiment_check = None
    summary_sent_today = False
    boxes = {}

    def load_boxes():
        for symbol in symbols:
            try:
                high, low, mid = get_box_levels(symbol)
                boxes[symbol] = {'high': high, 'low': low, 'mid': mid}
                print(f"  {symbol}: ${low:.2f} - ${high:.2f}")
            except Exception as e:
                print(f"  {symbol}: Failed - {e}")

    print("Loading box levels...")
    load_boxes()
    print(f"Agent started | Max investment: ${max_investment}")
    print(f"Watching: {', '.join(symbols)}")

    current_day = None

    while True:
        try:
            now = datetime.now(timezone.utc)
            day = now.date()

            if day != current_day:
                current_day = day
                cooldown = {s: False for s in symbols}
                entry_count = {s: 0 for s in symbols}
                summary_sent_today = False
                earnings_skip = check_earnings(symbols)
                if earnings_skip:
                    for s, d in earnings_skip.items():
                        log(f"⚠ Skipping {s} - earnings on {d}")
                else:
                    log("Earnings check - no upcoming earnings")
                market_regime = get_market_regime()
                log(f"Market regime: {market_regime.upper()}")
                print(f"\n{'='*60}")
                print(f"New day: {day}")
                load_boxes()
                print(f"{'='*60}")


            # Hourly sentiment check
            if active_position is None and (last_sentiment_check is None or (now - last_sentiment_check).seconds >= 3600):
                for symbol in symbols:
                    if symbol not in earnings_skip:
                        score, reason = get_sentiment(symbol)
                        sentiment_scores[symbol] = score
                        log(f"Sentiment {symbol}: {score:+d} | {reason}")
                last_sentiment_check = now

            if now.hour == 19 and now.minute >= 30:
                real_pos = get_real_position()
                if real_pos is not None:
                    symbol = real_pos['symbol']
                    side = real_pos['side']
                    shares = real_pos['shares']
                    try:
                        close_side = OrderSide.SELL if side == 'long' else OrderSide.BUY
                        trading_client.submit_order(MarketOrderRequest(
                            symbol=symbol,
                            qty=abs(int(shares)) if side == 'short' else shares,
                            side=close_side,
                            time_in_force=TimeInForce.DAY
                        ))
                        fill_side = 'short' if side == 'long' else 'long'
                        eod_fill = get_fill_price(symbol, fill_side) or get_current_price(symbol)
                        if eod_fill and buy_price:
                            eod_pnl = (eod_fill - buy_price) * shares if side == 'long' else (buy_price - eod_fill) * shares
                            log(f">>> EOD CLOSE {side.upper()} {symbol} at ${eod_fill:.2f} | P&L: ${eod_pnl:.2f}")
                        else:
                            log(f">>> EOD CLOSE {side.upper()} {symbol}")
                    except Exception as e:
                        log(f"EOD close failed: {e}")
                    active_position = None
                    buy_price = None
                else:
                    log("EOD check - no open positions")

                # Send daily summary notification (once per day)
                if not summary_sent_today:
                    try:
                        date_str = now.strftime('%Y-%m-%d')
                        with open(LOG_FILE, 'r') as f:
                            log_lines = [l.strip() for l in f if date_str in l]
                        trade_lines = [l for l in log_lines if any(x in l for x in ['>>> BUY','>>> SHORT','>>> BREAKOUT','>>> STOP','>>> TARGET','>>> TRAILING','>>> EOD CLOSE'])]
                        pnl_lines = [l for l in trade_lines if 'P&L' in l or 'Profit' in l]
                        total = 0
                        for l in pnl_lines:
                            try:
                                val = float(l.split('$')[-1].replace(')',''))
                                total += val
                            except:
                                pass
                        error_lines = [l for l in log_lines if 'Error' in l]
                        running = total
                        send_daily_summary(date_str, trade_lines, total, running, sentiment_scores, market_regime, boxes, len(error_lines))
                        summary_sent_today = True
                    except Exception as e:
                        log(f"Summary notification failed: {e}")

                time.sleep(60)
            if not is_market_hours(now):
                log("Market closed. Waiting...")
                time.sleep(60)
                continue
                continue

            if active_position is not None:
                symbol = active_position['symbol']
                side = active_position['side']
                box = boxes[symbol]
                try:
                    exit_side = 'sell' if side == 'long' else 'buy'
                    price = get_current_price(symbol, side=exit_side)
                    if price is None:
                        print(f"Could not get price for {symbol} - skipping cycle")
                        time.sleep(30)
                        continue
                    shares = get_position(symbol)
                    if buy_price is not None and shares != 0:
                        if side == 'long':
                            drop = (buy_price - price) / buy_price
                            if drop >= 0.025:
                                trading_client.submit_order(MarketOrderRequest(
                                    symbol=symbol,
                                    qty=shares,
                                    side=OrderSide.SELL,
                                    time_in_force=TimeInForce.DAY
                                ))
                                loss = (price - buy_price) * shares
                                log(f">>> STOP-LOSS LONG {symbol} at ${price:.2f} | P&L: ${loss:.2f}")
                                cooldown[symbol] = True
                                active_position = None
                                buy_price = None
                                time.sleep(30)
                                continue
                        elif side == 'short':
                            rise = (price - buy_price) / buy_price
                            if rise >= 0.025:
                                trading_client.submit_order(MarketOrderRequest(
                                    symbol=symbol,
                                    qty=abs(int(shares)),
                                    side=OrderSide.BUY,
                                    time_in_force=TimeInForce.DAY
                                ))
                                loss = (buy_price - price) * abs(shares)
                                log(f">>> STOP-LOSS SHORT {symbol} at ${price:.2f} | P&L: ${loss:.2f}")
                                cooldown[symbol] = True
                                active_position = None
                                buy_price = None
                                time.sleep(30)
                                continue
                        # Trailing stop logic
                        if side == 'long':
                            best_price = max(best_price, price) if best_price else price
                            profit_pct = (best_price - buy_price) / buy_price
                            if profit_pct >= 0.015:
                                trailing_stop_active = True
                            if trailing_stop_active:
                                trail_stop = best_price * (1 - 0.008)
                                if price <= trail_stop:
                                    trading_client.submit_order(MarketOrderRequest(
                                        symbol=symbol,
                                        qty=shares,
                                        side=OrderSide.SELL,
                                        time_in_force=TimeInForce.DAY
                                    ))
                                    profit = (price - buy_price) * shares
                                    log(f">>> TRAILING STOP LONG {symbol} at ${price:.2f} | Profit: ${profit:.2f}")
                                    active_position = None
                                    buy_price = None
                                    best_price = None
                                    trailing_stop_active = False
                                    time.sleep(30)
                                    continue
                        elif side == 'short':
                            best_price = min(best_price, price) if best_price else price
                            profit_pct = (buy_price - best_price) / buy_price
                            if profit_pct >= 0.015:
                                trailing_stop_active = True
                            if trailing_stop_active:
                                trail_stop = best_price * (1 + 0.008)
                                if price >= trail_stop:
                                    trading_client.submit_order(MarketOrderRequest(
                                        symbol=symbol,
                                        qty=abs(int(shares)),
                                        side=OrderSide.BUY,
                                        time_in_force=TimeInForce.DAY
                                    ))
                                    profit = (buy_price - price) * abs(shares)
                                    log(f">>> TRAILING STOP SHORT {symbol} at ${price:.2f} | Profit: ${profit:.2f}")
                                    active_position = None
                                    buy_price = None
                                    best_price = None
                                    trailing_stop_active = False
                                    time.sleep(30)
                                    continue

                        box_range = box['high'] - box['low']
                        breakout_long_target = box['high'] + box_range
                        breakout_short_target = box['low'] - box_range
                        is_breakout = active_position.get('breakout', False)

                        long_target = breakout_long_target if is_breakout else box['high'] * 0.997
                        short_target = breakout_short_target if is_breakout else box['low'] * 1.003

                        if side == 'long' and price >= long_target:
                            trading_client.submit_order(MarketOrderRequest(
                                symbol=symbol,
                                qty=shares,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY
                            ))
                            profit = (price - buy_price) * shares
                            trade_type = "BREAKOUT TARGET" if is_breakout else "TARGET"
                            log(f">>> {trade_type} LONG {symbol} at ${price:.2f} | Profit: ${profit:.2f}")
                            active_position = None
                            buy_price = None
                            time.sleep(30)
                            continue
                        elif side == 'short' and price <= short_target:
                            trading_client.submit_order(MarketOrderRequest(
                                symbol=symbol,
                                qty=abs(int(shares)),
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY
                            ))
                            profit = (buy_price - price) * abs(shares)
                            trade_type = "BREAKOUT TARGET" if is_breakout else "TARGET"
                            log(f">>> {trade_type} SHORT {symbol} at ${price:.2f} | Profit: ${profit:.2f}")
                            active_position = None
                            buy_price = None
                            time.sleep(30)
                            continue
                    unrealized = (price - buy_price) * abs(shares) if side == 'long' else (buy_price - price) * abs(shares)
                    log(f"Holding {side.upper()} {symbol} | Price: ${price:.2f} | Entry: ${buy_price:.2f} | Unrealized: ${unrealized:.2f}")
                except Exception as e:
                    print(f"Error managing position: {e}")

            if active_position is None:
                best_score = -1
                best_setup = None
                for symbol in symbols:
                    if cooldown.get(symbol, False):
                        continue
                    if symbol in earnings_skip:
                        continue
                    if symbol not in boxes:
                        continue
                    box = boxes[symbol]
                    try:
                        signal, price, sl = get_hourly_signal(symbol, box['high'], box['low'])
                        if signal == 0 or price is None:
                            continue
                        if market_regime == 'bull' and signal in [-1, -2]:
                            continue
                        if market_regime == 'bear' and signal in [1, 2]:
                            continue
                        sentiment = sentiment_scores.get(symbol, 0)
                        if sentiment <= -5 and signal in [1, 2]:
                            log(f"Skipping LONG {symbol} - bearish sentiment {sentiment}")
                            continue
                        if sentiment >= 5 and signal in [-1, -2]:
                            log(f"Skipping SHORT {symbol} - bullish sentiment {sentiment}")
                            continue
                        box_range = box['high'] - box['low']
                        if signal == 1:
                            closeness = 1 - ((price - box['low']) / max(box['low'] * 0.003, 0.001))
                        elif signal == -1:
                            closeness = 1 - ((box['high'] - price) / max(box['high'] * 0.003, 0.001))
                        elif signal == 2:
                            # Breakout long - score based on how far above box
                            closeness = (price - box['high']) / max(box['high'] * 0.003, 0.001)
                            closeness = min(closeness, 1.0)
                        elif signal == -2:
                            # Breakout short - score based on how far below box
                            closeness = (box['low'] - price) / max(box['low'] * 0.003, 0.001)
                            closeness = min(closeness, 1.0)
                        box_range_pct = box_range / box['low']
                        score = (closeness * 0.5) + (box_range_pct * 100 * 0.5)
                        if score > best_score:
                            best_score = score
                            best_setup = {'symbol': symbol, 'signal': signal, 'price': price, 'sl': sl, 'score': score}
                    except Exception as e:
                        print(f"Error scanning {symbol}: {e}")
                if best_setup is not None:
                    symbol = best_setup['symbol']
                    signal = best_setup['signal']
                    price = best_setup['price']
                    side = 'long' if signal in [1, 2] else 'short'
                    if symbol in earnings_skip:
                        log(f"Skipping {symbol} - earnings on {earnings_skip[symbol]}")
                        time.sleep(30)
                        continue
                    if entry_count.get(symbol, 0) >= 3:
                        log(f"Max entries reached for {symbol} today - skipping")
                        time.sleep(30)
                        continue
                    if side == 'short' and (now.hour > 18 or (now.hour == 18 and now.minute >= 30)):
                        log(f'Skipping SHORT {symbol} - too late in day')
                        time.sleep(30)
                        continue
                    try:
                        account = trading_client.get_account()
                        capital = float(account.cash)
                        invest = min(max_investment, capital * 0.80)
                        shares = get_shares(invest, price, side)
                        if signal in [1, 2]:
                            trading_client.submit_order(MarketOrderRequest(
                                symbol=symbol,
                                qty=shares,
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY
                            ))
                            fill_price = get_fill_price(symbol, 'long') or price
                            buy_price = fill_price
                            best_price = fill_price
                            trailing_stop_active = False
                            active_position = {'symbol': symbol, 'side': 'long', 'shares': shares, 'breakout': signal == 2}
                            trade_type = "BREAKOUT LONG" if signal == 2 else "BUY"
                            box_range = boxes[symbol]['high'] - boxes[symbol]['low']
                            target = boxes[symbol]['high'] + box_range if signal == 2 else boxes[symbol]['high']
                            log(f">>> {trade_type} {symbol} | {shares} shares at ${fill_price:.2f} | Invested: ${shares*fill_price:.2f} | Target: ${target:.2f} | SL: ${best_setup['sl']:.2f}")
                            entry_count[symbol] = entry_count.get(symbol, 0) + 1
                        elif signal in [-1, -2]:
                            trading_client.submit_order(MarketOrderRequest(
                                symbol=symbol,
                                qty=shares,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY
                            ))
                            fill_price = get_fill_price(symbol, 'short') or price
                            buy_price = fill_price
                            best_price = fill_price
                            trailing_stop_active = False
                            active_position = {'symbol': symbol, 'side': 'short', 'shares': shares, 'breakout': signal == -2}
                            trade_type = "BREAKOUT SHORT" if signal == -2 else "SHORT"
                            box_range = boxes[symbol]['high'] - boxes[symbol]['low']
                            target = boxes[symbol]['low'] - box_range if signal == -2 else boxes[symbol]['low']
                            log(f">>> {trade_type} {symbol} | {shares} shares at ${fill_price:.2f} | Invested: ${shares*fill_price:.2f} | Target: ${target:.2f} | SL: ${best_setup['sl']:.2f}")
                            entry_count[symbol] = entry_count.get(symbol, 0) + 1
                    except Exception as e:
                        print(f"Failed to place order: {e}")
                else:
                    status = []
                    for symbol in symbols:
                        if symbol in boxes:
                            b = boxes[symbol]
                            status.append(f"{symbol}: ${b['low']:.2f}-${b['high']:.2f}")
                    log(f"WAIT | {' | '.join(status)}")
        except Exception as e:
            log(f"Error: {e} - restarting in 30s")
        time.sleep(30)

run_agent()
