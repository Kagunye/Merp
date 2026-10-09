import asyncio
import nltk
nltk.download('vader_lexicon', quiet=True)
import nest_asyncio
import json
import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier, VotingClassifier, AdaBoostClassifier
from xgboost import XGBClassifier
from lightgbm import LGBMClassifier
from sklearn.model_selection import train_test_split, RandomizedSearchCV, StratifiedKFold, TimeSeriesSplit
from sklearn.preprocessing import StandardScaler, RobustScaler
from sklearn.metrics import log_loss, roc_auc_score, f1_score, accuracy_score, precision_score, recall_score
from sklearn.pipeline import Pipeline
from sklearn.decomposition import PCA
from sklearn.feature_selection import SelectKBest, f_classif
from sklearn.impute import SimpleImputer
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.svm import SVC
from sklearn.neural_network import MLPClassifier
from sklearn.utils.class_weight import compute_class_weight
from sklearn.metrics import classification_report, confusion_matrix
from datetime import datetime, timedelta
import logging
import joblib
import time
from scipy.stats import norm
from scipy.optimize import minimize
import requests
from textblob import TextBlob
from nltk.sentiment import SentimentIntensityAnalyzer
from sklearn.calibration import CalibratedClassifierCV
import ta
from statistics import mode
from tensorflow import keras
from tensorflow.keras import layers
import os
import glob
from typing import Dict, Optional, List, Tuple
import tkinter as tk
from tkinter import ttk
import pickle
import aiohttp
import feedparser
import ssl
import re
import math

try:
    from email_alerts import send_trade_alert
except ImportError:
    def send_trade_alert(signal, price_data, advice, mt5_trade=False):
        logging.info(f"Trade alert (fallback): {signal['direction']} signal with strength {signal['signal_strength']:.2f}")
        return True

import MetaTrader5 as mt5
import pytz
import itertools

nest_asyncio.apply()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ============================================================
# Configuration — credentials from environment variables
# ============================================================
MT5_LOGIN = int(os.environ.get('MT5_LOGIN', '25305222'))
MT5_PASSWORD = os.environ.get('MT5_PASSWORD', '')
MT5_SERVER = os.environ.get('MT5_SERVER', 'Deriv-Demo')
FINNHUB_API_KEY = os.environ.get('FINNHUB_API_KEY', '')

MT5_PATHS = [
    r"C:\Program Files\MetaTrader 5 Terminal\terminal64.exe",
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe"
]
MT5_PATH = MT5_PATHS[0]

SELECTED_MARKET = None
NEWS_CACHE_FILE = 'news_cache.pkl'
NEWS_CACHE_DURATION = 3600
LAST_NEWS_FETCH = 0
RISK_PERCENT_PER_TRADE = 1.0

AVAILABLE_MARKETS: Dict[str, str] = {
    'frxXAUUSD': 'Gold/USD', 'frxXAGUSD': 'Silver/USD',
    'frxBRENT': 'Brent Crude Oil', 'frxWTI': 'WTI Crude Oil',
    'XAUUSD': 'Gold/USD', 'XAGUSD': 'Silver/USD',
    'BRENT': 'Brent Crude Oil', 'WTI': 'WTI Crude Oil',
    'CRASH_1000': 'Crash 1000 Index', 'CRASH_500': 'Crash 500 Index',
    'BOOM_1000': 'Boom 1000 Index', 'BOOM_500': 'Boom 500 Index',
    'STPRD': 'Step Index',
    'Jump_75': 'Jump 75 Index', 'Jump_100': 'Jump 100 Index', 'Jump_50': 'Jump 50 Index',
    'frxGBPJPY': 'GBP/JPY', 'frxEURUSD': 'EUR/USD', 'frxUSDJPY': 'USD/JPY',
    'frxGBPUSD': 'GBP/USD', 'frxEURGBP': 'EUR/GBP', 'frxAUDUSD': 'AUD/USD',
    'frxNZDUSD': 'NZD/USD', 'frxUSDCAD': 'USD/CAD', 'frxEURAUD': 'EUR/AUD',
    'frxAUDJPY': 'AUD/JPY', 'frxEURJPY': 'EUR/JPY', 'frxUSDCHF': 'USD/CHF',
    'frxEURCHF': 'EUR/CHF',
    'EURUSD': 'EUR/USD', 'GBPUSD': 'GBP/USD', 'USDJPY': 'USD/JPY',
    'AUDUSD': 'AUD/USD', 'NZDUSD': 'NZD/USD', 'USDCAD': 'USD/CAD',
    'EURGBP': 'EUR/GBP', 'EURAUD': 'EUR/AUD', 'AUDJPY': 'AUD/JPY',
    'EURJPY': 'EUR/JPY', 'USDCHF': 'USD/CHF', 'EURCHF': 'EUR/CHF',
    'GBPJPY': 'GBP/JPY',
    'R_75': 'Volatility 75 Index', 'R_100': 'Volatility 100 Index',
    'R_50': 'Volatility 50 Index', 'R_25': 'Volatility 25 Index',
    'R_10': 'Volatility 10 Index',
    '1HZ10V': 'Volatility 10 (1s) Index', '1HZ25V': 'Volatility 25 (1s) Index',
    '1HZ50V': 'Volatility 50 (1s) Index', '1HZ75V': 'Volatility 75 (1s) Index',
    '1HZ100V': 'Volatility 100 (1s) Index',
}

# Killzone session windows (UTC hours)
KILLZONES = {
    'asian': (0, 3),
    'london': (7, 10),
    'new_york': (12, 15),
    'london_close': (15, 17),
}


# ============================================================
# Market Selector GUI
# ============================================================
class MarketSelector:
    def __init__(self):
        self.selected_market = None
        self.window = tk.Tk()
        self.window.title("Market Selector")
        self.window.geometry("400x600")

        self.search_var = tk.StringVar()
        self.search_var.trace('w', self.filter_markets)
        search_entry = ttk.Entry(self.window, textvariable=self.search_var)
        search_entry.pack(pady=10, padx=5, fill='x')

        self.filter_frame = ttk.Frame(self.window)
        self.filter_frame.pack(pady=5, fill='x')

        self.filter_var = tk.StringVar(value="all")
        for label, val in [("All", "all"), ("Forex", "forex"), ("Synthetic", "synthetic"),
                           ("Volatility", "volatility"), ("Commodities", "commodities")]:
            ttk.Radiobutton(self.filter_frame, text=label, value=val,
                           variable=self.filter_var, command=self.filter_markets).pack(side='left')

        self.market_listbox = tk.Listbox(self.window, width=50)
        self.market_listbox.pack(pady=10, padx=5, fill='both', expand=True)
        self.filter_markets()

        select_button = ttk.Button(self.window, text="Select Market", command=self.on_select)
        select_button.pack(pady=10)
        self.window.mainloop()

    def filter_markets(self, *args):
        self.market_listbox.delete(0, tk.END)
        search_text = self.search_var.get().lower()
        market_filter = self.filter_var.get()

        for symbol, name in AVAILABLE_MARKETS.items():
            if search_text and search_text not in name.lower() and search_text not in symbol.lower():
                continue
            if market_filter == "forex":
                if not (symbol.startswith("frx") and not any(x in symbol for x in ["XAU", "XAG", "BRENT", "WTI"])):
                    if not symbol in ['EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'NZDUSD', 'USDCAD',
                                      'EURGBP', 'EURAUD', 'AUDJPY', 'EURJPY', 'USDCHF', 'EURCHF', 'GBPJPY']:
                        continue
            elif market_filter == "synthetic" and not any(x in symbol for x in ["CRASH", "BOOM", "STPRD", "Jump"]):
                continue
            elif market_filter == "volatility" and not any(x in symbol for x in ["R_", "1HZ"]):
                continue
            elif market_filter == "commodities":
                if not any(x in symbol for x in ["XAU", "XAG", "BRENT", "WTI"]):
                    continue
            self.market_listbox.insert(tk.END, f"{symbol} - {name}")

    def on_select(self):
        selection = self.market_listbox.curselection()
        if selection:
            selected_item = self.market_listbox.get(selection[0])
            self.selected_market = selected_item.split(" - ")[0]
            self.window.quit()
            self.window.destroy()


def get_selected_market() -> Optional[str]:
    global SELECTED_MARKET
    if SELECTED_MARKET is None:
        selector = MarketSelector()
        SELECTED_MARKET = selector.selected_market
    return SELECTED_MARKET


def get_market_type(symbol):
    if not symbol:
        return 'general'
    if symbol.startswith('frx'):
        if any(x in symbol for x in ["XAU", "XAG", "BRENT", "WTI"]):
            return 'commodities'
        return 'forex'
    elif any(x in symbol for x in ['CRASH', 'BOOM', 'Jump', 'STPRD']):
        return 'synthetic'
    elif any(x in symbol for x in ['R_', '1HZ']):
        return 'volatility'
    return 'general'


# ============================================================
# Session / Killzone Timing
# ============================================================

def get_current_killzone() -> Optional[str]:
    utc_hour = datetime.utcnow().hour
    for name, (start, end) in KILLZONES.items():
        if start <= utc_hour < end:
            return name
    return None


def is_in_killzone(symbol: str) -> bool:
    market_type = get_market_type(symbol)
    if market_type in ('synthetic', 'volatility'):
        return True
    kz = get_current_killzone()
    return kz in ('london', 'new_york')


def get_session_volatility_multiplier() -> float:
    kz = get_current_killzone()
    multipliers = {
        'london': 1.2,
        'new_york': 1.3,
        'london_close': 0.8,
        'asian': 0.6,
    }
    return multipliers.get(kz, 0.5)


# ============================================================
# Smart Money / ICT Strategy Engine — Enhanced
# ============================================================

def detect_swing_points(df: pd.DataFrame, lookback: int = 5) -> pd.DataFrame:
    df = df.copy()
    df['swing_high'] = False
    df['swing_low'] = False
    df['swing_high_price'] = np.nan
    df['swing_low_price'] = np.nan

    highs = df['high'].values
    lows = df['low'].values

    for i in range(lookback, len(df) - lookback):
        window_highs = highs[i - lookback:i + lookback + 1]
        window_lows = lows[i - lookback:i + lookback + 1]
        if highs[i] == max(window_highs):
            df.iloc[i, df.columns.get_loc('swing_high')] = True
            df.iloc[i, df.columns.get_loc('swing_high_price')] = highs[i]
        if lows[i] == min(window_lows):
            df.iloc[i, df.columns.get_loc('swing_low')] = True
            df.iloc[i, df.columns.get_loc('swing_low_price')] = lows[i]

    df['swing_high_price'] = df['swing_high_price'].ffill()
    df['swing_low_price'] = df['swing_low_price'].ffill()

    return df


def detect_break_of_structure(df: pd.DataFrame) -> Dict:
    last_swing_high = None
    last_swing_low = None
    bos = {'direction': 'none', 'level': 0.0, 'bar_index': -1, 'strength': 0}

    for i in range(len(df)):
        if df['swing_high'].iloc[i]:
            last_swing_high = (i, df['high'].iloc[i])
        if df['swing_low'].iloc[i]:
            last_swing_low = (i, df['low'].iloc[i])

    current_close = df['close'].iloc[-1]
    atr = df['ATR'].iloc[-1] if 'ATR' in df.columns else 0.001

    if last_swing_high and current_close > last_swing_high[1]:
        penetration = (current_close - last_swing_high[1]) / atr if atr > 0 else 0
        bos = {'direction': 'bullish', 'level': last_swing_high[1],
               'bar_index': last_swing_high[0], 'strength': min(penetration, 3.0)}
    if last_swing_low and current_close < last_swing_low[1]:
        penetration = (last_swing_low[1] - current_close) / atr if atr > 0 else 0
        bos = {'direction': 'bearish', 'level': last_swing_low[1],
               'bar_index': last_swing_low[0], 'strength': min(penetration, 3.0)}

    return bos


def detect_change_of_character(df: pd.DataFrame) -> Dict:
    """
    CHoCH — a structural shift that signals potential reversal.
    Unlike BOS which confirms trend continuation, CHoCH breaks
    the OPPOSING structure (e.g., first lower low in an uptrend).
    """
    choch = {'detected': False, 'direction': 'none', 'level': 0.0}

    swing_highs = df[df['swing_high']]['high'].values
    swing_lows = df[df['swing_low']]['low'].values

    if len(swing_highs) < 3 or len(swing_lows) < 3:
        return choch

    hh_sequence = swing_highs[-1] > swing_highs[-2] > swing_highs[-3]
    hl_sequence = swing_lows[-1] > swing_lows[-2] > swing_lows[-3]
    lh_sequence = swing_highs[-1] < swing_highs[-2] < swing_highs[-3]
    ll_sequence = swing_lows[-1] < swing_lows[-2] < swing_lows[-3]

    current_close = df['close'].iloc[-1]

    if hh_sequence and hl_sequence:
        if current_close < swing_lows[-1]:
            choch = {'detected': True, 'direction': 'bearish', 'level': swing_lows[-1]}

    if lh_sequence and ll_sequence:
        if current_close > swing_highs[-1]:
            choch = {'detected': True, 'direction': 'bullish', 'level': swing_highs[-1]}

    return choch


def detect_displacement(df: pd.DataFrame, lookback: int = 5) -> Dict:
    """
    Displacement — large-bodied candles that show institutional intent.
    These are candles with bodies > 2x ATR, indicating aggressive
    institutional order flow.
    """
    disp = {'detected': False, 'direction': 'none', 'strength': 0, 'index': -1}

    if 'ATR' not in df.columns:
        return disp

    for i in range(max(1, len(df) - lookback), len(df)):
        body = abs(df['close'].iloc[i] - df['open'].iloc[i])
        atr = df['ATR'].iloc[i]
        if atr <= 0:
            continue

        body_ratio = body / atr
        if body_ratio > 2.0:
            direction = 'bullish' if df['close'].iloc[i] > df['open'].iloc[i] else 'bearish'
            if body_ratio > disp['strength']:
                disp = {
                    'detected': True,
                    'direction': direction,
                    'strength': body_ratio,
                    'index': i
                }

    return disp


def detect_order_blocks(df: pd.DataFrame, lookback: int = 20) -> List[Dict]:
    order_blocks = []
    closes = df['close'].values
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    atr_vals = df['ATR'].values if 'ATR' in df.columns else np.full(len(df), 0.001)

    start = max(0, len(df) - lookback)
    for i in range(start + 1, len(df) - 1):
        body_curr = abs(closes[i] - opens[i])
        atr = max(atr_vals[i], 0.0001)

        is_bullish_candle = closes[i] > opens[i]
        is_bearish_candle = closes[i] < opens[i]
        is_impulsive = body_curr > atr * 1.5

        if is_impulsive and is_bullish_candle and closes[i - 1] < opens[i - 1]:
            mitigated = any(lows[j] <= closes[i - 1] for j in range(i + 1, min(i + 5, len(df))))
            order_blocks.append({
                'type': 'bullish',
                'top': opens[i - 1],
                'bottom': closes[i - 1],
                'index': i - 1,
                'strength': body_curr / atr,
                'mitigated': mitigated,
            })
        elif is_impulsive and is_bearish_candle and closes[i - 1] > opens[i - 1]:
            mitigated = any(highs[j] >= closes[i - 1] for j in range(i + 1, min(i + 5, len(df))))
            order_blocks.append({
                'type': 'bearish',
                'top': closes[i - 1],
                'bottom': opens[i - 1],
                'index': i - 1,
                'strength': body_curr / atr,
                'mitigated': mitigated,
            })

    return [ob for ob in order_blocks if not ob['mitigated']]


def detect_fair_value_gaps(df: pd.DataFrame, lookback: int = 20) -> List[Dict]:
    fvgs = []
    start = max(2, len(df) - lookback)
    for i in range(start, len(df)):
        high_2_back = df['high'].iloc[i - 2]
        low_current = df['low'].iloc[i]
        low_2_back = df['low'].iloc[i - 2]
        high_current = df['high'].iloc[i]

        if low_current > high_2_back:
            filled = any(df['low'].iloc[j] <= high_2_back for j in range(i + 1, len(df)))
            fvgs.append({
                'type': 'bullish', 'top': low_current, 'bottom': high_2_back,
                'index': i, 'size': low_current - high_2_back, 'filled': filled,
            })
        elif high_current < low_2_back:
            filled = any(df['high'].iloc[j] >= low_2_back for j in range(i + 1, len(df)))
            fvgs.append({
                'type': 'bearish', 'top': low_2_back, 'bottom': high_current,
                'index': i, 'size': low_2_back - high_current, 'filled': filled,
            })

    return [f for f in fvgs if not f['filled']]


def detect_liquidity_sweep(df: pd.DataFrame, lookback: int = 10) -> Dict:
    sweep = {'type': 'none', 'level': 0.0, 'wick_depth': 0}

    recent = df.iloc[-lookback:]
    swing_highs = recent[recent['swing_high']]['high']
    swing_lows = recent[recent['swing_low']]['low']

    last = df.iloc[-1]
    atr = df['ATR'].iloc[-1] if 'ATR' in df.columns else 0.001

    if len(swing_highs) > 0:
        recent_sh = swing_highs.max()
        if last['high'] > recent_sh and last['close'] < recent_sh:
            wick = (last['high'] - recent_sh) / atr if atr > 0 else 0
            sweep = {'type': 'bearish_sweep', 'level': recent_sh, 'wick_depth': wick}

    if len(swing_lows) > 0:
        recent_sl = swing_lows.min()
        if last['low'] < recent_sl and last['close'] > recent_sl:
            wick = (recent_sl - last['low']) / atr if atr > 0 else 0
            sweep = {'type': 'bullish_sweep', 'level': recent_sl, 'wick_depth': wick}

    return sweep


def calculate_market_structure(df: pd.DataFrame) -> str:
    swing_highs = df[df['swing_high']]['high'].values
    swing_lows = df[df['swing_low']]['low'].values

    if len(swing_highs) < 3 or len(swing_lows) < 3:
        return 'ranging'

    hh_count = sum(1 for i in range(1, min(4, len(swing_highs))) if swing_highs[-i] > swing_highs[-i - 1])
    hl_count = sum(1 for i in range(1, min(4, len(swing_lows))) if swing_lows[-i] > swing_lows[-i - 1])
    lh_count = sum(1 for i in range(1, min(4, len(swing_highs))) if swing_highs[-i] < swing_highs[-i - 1])
    ll_count = sum(1 for i in range(1, min(4, len(swing_lows))) if swing_lows[-i] < swing_lows[-i - 1])

    if hh_count >= 2 and hl_count >= 2:
        return 'uptrend'
    elif lh_count >= 2 and ll_count >= 2:
        return 'downtrend'
    return 'ranging'


def calculate_premium_discount(df: pd.DataFrame) -> Dict:
    """
    Premium/discount zone analysis using recent swing range.
    Institutional traders buy at discount (below 50% of range)
    and sell at premium (above 50% of range).
    OTE (Optimal Trade Entry) sits at the 62-79% retracement zone.
    """
    swing_highs = df[df['swing_high']]['high']
    swing_lows = df[df['swing_low']]['low']

    if len(swing_highs) == 0 or len(swing_lows) == 0:
        return {'zone': 'equilibrium', 'fib_level': 0.5, 'in_ote': False}

    range_high = swing_highs.iloc[-1]
    range_low = swing_lows.iloc[-1]
    current = df['close'].iloc[-1]

    total_range = range_high - range_low
    if total_range <= 0:
        return {'zone': 'equilibrium', 'fib_level': 0.5, 'in_ote': False}

    position = (current - range_low) / total_range

    fib_618 = range_low + total_range * 0.382
    fib_786 = range_low + total_range * 0.214

    if position > 0.5:
        zone = 'premium'
    elif position < 0.5:
        zone = 'discount'
    else:
        zone = 'equilibrium'

    in_ote = 0.214 <= position <= 0.382 or 0.618 <= position <= 0.786

    return {
        'zone': zone,
        'fib_level': position,
        'in_ote': in_ote,
        'range_high': range_high,
        'range_low': range_low,
    }


def detect_institutional_candle_patterns(df: pd.DataFrame) -> Dict:
    """
    Detect high-probability institutional candle patterns at key levels.
    """
    patterns = {'bullish': [], 'bearish': []}

    if len(df) < 3:
        return patterns

    last = df.iloc[-1]
    prev = df.iloc[-2]
    body_last = abs(last['close'] - last['open'])
    body_prev = abs(prev['close'] - prev['open'])
    range_last = last['high'] - last['low']
    atr = df['ATR'].iloc[-1] if 'ATR' in df.columns else range_last

    if range_last == 0 or atr == 0:
        return patterns

    lower_wick = min(last['close'], last['open']) - last['low']
    upper_wick = last['high'] - max(last['close'], last['open'])

    if lower_wick > body_last * 2.5 and upper_wick < body_last * 0.5:
        patterns['bullish'].append('hammer')
    if upper_wick > body_last * 2.5 and lower_wick < body_last * 0.5:
        patterns['bearish'].append('shooting_star')

    if (last['close'] > last['open'] and prev['close'] < prev['open']
            and body_last > body_prev * 1.3
            and last['close'] > prev['open'] and last['open'] < prev['close']):
        patterns['bullish'].append('engulfing')
    if (last['close'] < last['open'] and prev['close'] > prev['open']
            and body_last > body_prev * 1.3
            and last['close'] < prev['open'] and last['open'] > prev['close']):
        patterns['bearish'].append('engulfing')

    if body_last > atr * 1.8:
        if last['close'] > last['open']:
            patterns['bullish'].append('marubozu')
        else:
            patterns['bearish'].append('marubozu')

    if len(df) >= 4:
        three_back = df.iloc[-3]
        if (prev['close'] < prev['open'] and three_back['close'] < three_back['open']
                and last['close'] > last['open']
                and last['close'] > three_back['open']):
            patterns['bullish'].append('morning_star')
        if (prev['close'] > prev['open'] and three_back['close'] > three_back['open']
                and last['close'] < last['open']
                and last['close'] < three_back['open']):
            patterns['bearish'].append('evening_star')

    return patterns


def calculate_volume_profile(df: pd.DataFrame, num_levels: int = 10) -> Dict:
    price_min = df['low'].min()
    price_max = df['high'].max()
    level_size = (price_max - price_min) / num_levels

    if level_size == 0:
        return {'poc': df['close'].iloc[-1], 'value_area_high': price_max, 'value_area_low': price_min}

    volumes = {}
    for _, row in df.iterrows():
        vol = row.get('volume', 1)
        for level in range(num_levels):
            level_price = price_min + level * level_size
            if row['low'] <= level_price + level_size and row['high'] >= level_price:
                volumes[level] = volumes.get(level, 0) + vol

    if not volumes:
        return {'poc': df['close'].iloc[-1], 'value_area_high': price_max, 'value_area_low': price_min}

    poc_level = max(volumes, key=volumes.get)
    poc_price = price_min + poc_level * level_size + level_size / 2

    total_vol = sum(volumes.values())
    sorted_levels = sorted(volumes.items(), key=lambda x: x[1], reverse=True)
    cumulative = 0
    va_levels = []
    for level, vol in sorted_levels:
        cumulative += vol
        va_levels.append(level)
        if cumulative >= total_vol * 0.7:
            break

    va_prices = [price_min + l * level_size for l in va_levels]
    return {
        'poc': poc_price,
        'value_area_high': max(va_prices) + level_size if va_prices else price_max,
        'value_area_low': min(va_prices) if va_prices else price_min,
    }


def get_multi_timeframe_bias(symbol: str) -> Dict:
    """
    Multi-timeframe analysis: H1 + H4 bias confirmation.
    Both must agree for a strong bias.
    """
    result = {'h1': 'neutral', 'h4': 'neutral', 'combined': 'neutral', 'strength': 0}
    try:
        mt5_symbol = map_deriv_to_mt5_symbol(symbol)

        for tf_name, tf_const, tf_key in [('H1', mt5.TIMEFRAME_H1, 'h1'), ('H4', mt5.TIMEFRAME_H4, 'h4')]:
            rates = mt5.copy_rates_from_pos(mt5_symbol, tf_const, 0, 100)
            if rates is None or len(rates) < 50:
                continue

            df_htf = pd.DataFrame(rates)
            close = df_htf['close']
            ema_20 = close.ewm(span=20, adjust=False).mean()
            ema_50 = close.ewm(span=50, adjust=False).mean()

            last_close = close.iloc[-1]
            last_ema20 = ema_20.iloc[-1]
            last_ema50 = ema_50.iloc[-1]

            if last_close > last_ema20 > last_ema50:
                result[tf_key] = 'bullish'
            elif last_close < last_ema20 < last_ema50:
                result[tf_key] = 'bearish'

        if result['h1'] == result['h4'] and result['h1'] != 'neutral':
            result['combined'] = result['h1']
            result['strength'] = 2
        elif result['h1'] != 'neutral' and result['h4'] == 'neutral':
            result['combined'] = result['h1']
            result['strength'] = 1
        elif result['h4'] != 'neutral' and result['h1'] == 'neutral':
            result['combined'] = result['h4']
            result['strength'] = 1

    except Exception as e:
        logging.error(f"Error getting MTF bias: {e}")

    return result


def detect_wyckoff_phase(df: pd.DataFrame) -> str:
    """
    Simplified Wyckoff phase detection based on volume and price behavior.
    Accumulation: price consolidating at lows with declining volume then spike
    Distribution: price consolidating at highs with declining volume then spike
    """
    if len(df) < 40 or 'volume' not in df.columns:
        return 'unknown'

    recent_30 = df.tail(30)
    price_range = recent_30['high'].max() - recent_30['low'].min()
    atr = df['ATR'].iloc[-1] if 'ATR' in df.columns else price_range * 0.05

    if price_range < atr * 5:
        vol_first_half = recent_30['volume'].iloc[:15].mean()
        vol_second_half = recent_30['volume'].iloc[15:].mean()

        longer_trend = df.tail(60)
        trend_start = longer_trend['close'].iloc[0]
        consolidation_mid = recent_30['close'].mean()

        if consolidation_mid < trend_start and vol_second_half > vol_first_half * 1.3:
            return 'accumulation'
        elif consolidation_mid > trend_start and vol_second_half > vol_first_half * 1.3:
            return 'distribution'
        return 'consolidation'

    return 'trending'


# ============================================================
# Preprocessing & Indicators
# ============================================================

def preprocess_data(df):
    try:
        if isinstance(df, list):
            df = pd.DataFrame(df)

        column_mapping = {
            'time': 'timestamp', 'open_price': 'open', 'high_price': 'high',
            'low_price': 'low', 'close_price': 'close', 'tick_volume': 'volume'
        }
        df = df.rename(columns=column_mapping)

        required_columns = ['open', 'high', 'low', 'close']
        if not all(col in df.columns for col in required_columns):
            logging.error(f"Missing required columns. Available: {df.columns.tolist()}")
            return df

        if 'epoch' in df.columns:
            df['timestamp'] = pd.to_datetime(df['epoch'], unit='s')
        if 'timestamp' in df.columns and not isinstance(df.index, pd.DatetimeIndex):
            df.set_index('timestamp', inplace=True)

        try:
            if len(df) > 100 and isinstance(df.index, pd.DatetimeIndex):
                df = df.resample('5min').agg({
                    'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
                }).dropna()
        except Exception as e:
            logging.warning(f"Resampling failed, using original data: {e}")

        df = add_smart_money_indicators(df)
        df = df.ffill().bfill()

        if isinstance(df.index, pd.DatetimeIndex):
            df.reset_index(inplace=True)

        return df
    except Exception as e:
        logging.error(f"Error in data preprocessing: {e}")
        return df


def add_smart_money_indicators(df: pd.DataFrame) -> pd.DataFrame:
    try:
        df['RSI'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        df['RSI_fast'] = ta.momentum.RSIIndicator(df['close'], window=7).rsi()

        macd = ta.trend.MACD(df['close'], window_fast=12, window_slow=26, window_sign=9)
        df['MACD'] = macd.macd()
        df['MACD_Signal'] = macd.macd_signal()
        df['MACD_Histogram'] = macd.macd_diff()

        stoch = ta.momentum.StochasticOscillator(df['high'], df['low'], df['close'], window=14, smooth_window=3)
        df['Stochastic_K'] = stoch.stoch()
        df['Stochastic_D'] = stoch.stoch_signal()

        df['EMA8'] = ta.trend.EMAIndicator(df['close'], window=8).ema_indicator()
        df['EMA21'] = ta.trend.EMAIndicator(df['close'], window=21).ema_indicator()
        df['EMA50'] = ta.trend.EMAIndicator(df['close'], window=50).ema_indicator()
        df['EMA200'] = ta.trend.EMAIndicator(df['close'], window=200).ema_indicator()

        df['ATR'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
        df['ATR_fast'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=7).average_true_range()

        bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
        df['BB_upper'] = bb.bollinger_hband()
        df['BB_lower'] = bb.bollinger_lband()
        df['BB_mid'] = bb.bollinger_mavg()
        df['BB_width'] = (df['BB_upper'] - df['BB_lower']) / df['BB_mid']
        df['BB_pct'] = bb.bollinger_pband()

        adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14)
        df['ADX'] = adx.adx()
        df['DI_plus'] = adx.adx_pos()
        df['DI_minus'] = adx.adx_neg()

        df['body_size'] = abs(df['close'] - df['open'])
        df['upper_wick'] = df['high'] - df[['close', 'open']].max(axis=1)
        df['lower_wick'] = df[['close', 'open']].min(axis=1) - df['low']
        df['candle_range'] = df['high'] - df['low']

        df['returns'] = df['close'].pct_change()
        df['volatility'] = df['returns'].rolling(window=20).std()
        df['returns_zscore'] = (df['returns'] - df['returns'].rolling(50).mean()) / df['returns'].rolling(50).std()

        df['RSI_divergence'] = 0
        price_diff = df['close'].diff(5)
        rsi_diff = df['RSI'].diff(5)
        df.loc[(price_diff < 0) & (rsi_diff > 0), 'RSI_divergence'] = 1
        df.loc[(price_diff > 0) & (rsi_diff < 0), 'RSI_divergence'] = -1

        df['VWAP'] = (df['close'] * df.get('volume', pd.Series(1, index=df.index))).cumsum() / df.get('volume', pd.Series(1, index=df.index)).cumsum()

        # Legacy indicators for model compatibility
        df['SMA_20'] = df['close'].rolling(window=20).mean()
        df['EMA_12'] = df['close'].ewm(span=12, adjust=False).mean()
        df['EMA_26'] = df['close'].ewm(span=26, adjust=False).mean()
        df['Momentum'] = df['close'].diff(4)
        df['Bollinger_Upper'] = df['BB_upper']
        df['Bollinger_Lower'] = df['BB_lower']
        df['mean'] = df['close'].rolling(window=20).mean()
        df['median'] = df['close'].rolling(window=20).median()
        df['std'] = df['close'].rolling(window=20).std()
        df['Volume_SMA'] = df['close'].rolling(window=7).mean()

        df = detect_swing_points(df, lookback=5)

    except Exception as e:
        logging.error(f"Error calculating indicators: {e}")

    return df


def calculate_volatility(df):
    df['returns'] = df['close'].pct_change()
    df['volatility'] = df['returns'].rolling(window=20).std()
    df.dropna(inplace=True)
    return df


def add_technical_indicators(df):
    df['SMA_20'] = df['close'].rolling(window=20).mean()
    df['EMA_12'] = df['close'].ewm(span=12, adjust=False).mean()
    df['RSI'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    macd = ta.trend.MACD(df['close'])
    df['MACD'] = macd.macd()
    bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
    df['Bollinger_Upper'] = bb.bollinger_hband()
    df['Bollinger_Lower'] = bb.bollinger_lband()
    df['Momentum'] = df['close'].diff(4)
    df['EMA_26'] = df['close'].ewm(span=26, adjust=False).mean()
    df['MACD_Histogram'] = df['MACD'] - df['EMA_26']
    df['Stochastic_K'] = ((df['close'] - df['low'].rolling(window=14).min()) /
                          (df['high'].rolling(window=14).max() - df['low'].rolling(window=14).min())) * 100
    df['Stochastic_D'] = df['Stochastic_K'].rolling(window=3).mean()
    df['mean'] = df['close'].rolling(window=20).mean()
    df['median'] = df['close'].rolling(window=20).median()
    df['std'] = df['close'].rolling(window=20).std()
    df['ATR'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
    df.dropna(inplace=True)
    return df


# ============================================================
# Enhanced Smart Money Signal Generation
# ============================================================

def generate_trade_signals(model, scaler, df, symbol):
    """
    Enhanced Smart Money / ICT signal generation with 12-layer confluence.

    Layers:
    1. Market structure (trend direction via swing analysis)
    2. Break of Structure / Change of Character
    3. Order block proximity (unmitigated)
    4. Fair value gap proximity (unfilled)
    5. Liquidity sweep detection
    6. Multi-timeframe bias (H1 + H4)
    7. Premium/discount zone + OTE
    8. Displacement candle confirmation
    9. Institutional candle patterns
    10. Momentum (RSI divergence + MACD + Stochastic)
    11. ADX trend strength + BB squeeze
    12. Session/killzone timing

    Minimum 6/16 score required, winning side must be > 1.5x opposing.
    """
    try:
        if len(df) < 50:
            return {'direction': 'Neutral', 'signal_strength': 0}

        current_price = float(df['close'].iloc[-1])
        atr = float(df['ATR'].iloc[-1]) if 'ATR' in df.columns else current_price * 0.001

        # === Compute all SMC layers ===
        structure = calculate_market_structure(df)
        bos = detect_break_of_structure(df)
        choch = detect_change_of_character(df)
        displacement = detect_displacement(df, lookback=5)

        order_blocks = detect_order_blocks(df, lookback=30)
        bullish_obs = [ob for ob in order_blocks if ob['type'] == 'bullish']
        bearish_obs = [ob for ob in order_blocks if ob['type'] == 'bearish']

        at_bullish_ob = any(
            ob['bottom'] - atr * 0.5 <= current_price <= ob['top'] + atr * 0.5
            for ob in bullish_obs
        )
        at_bearish_ob = any(
            ob['bottom'] - atr * 0.5 <= current_price <= ob['top'] + atr * 0.5
            for ob in bearish_obs
        )
        best_bull_ob_strength = max((ob['strength'] for ob in bullish_obs), default=0)
        best_bear_ob_strength = max((ob['strength'] for ob in bearish_obs), default=0)

        fvgs = detect_fair_value_gaps(df, lookback=30)
        bullish_fvgs = [f for f in fvgs if f['type'] == 'bullish']
        bearish_fvgs = [f for f in fvgs if f['type'] == 'bearish']

        near_bullish_fvg = any(
            f['bottom'] - atr * 0.3 <= current_price <= f['top'] + atr * 0.3
            for f in bullish_fvgs
        )
        near_bearish_fvg = any(
            f['bottom'] - atr * 0.3 <= current_price <= f['top'] + atr * 0.3
            for f in bearish_fvgs
        )

        sweep = detect_liquidity_sweep(df, lookback=10)
        mtf_bias = get_multi_timeframe_bias(symbol)
        pd_zone = calculate_premium_discount(df)
        candle_patterns = detect_institutional_candle_patterns(df)
        wyckoff = detect_wyckoff_phase(df)

        rsi = float(df['RSI'].iloc[-1]) if 'RSI' in df.columns else 50
        rsi_div = int(df['RSI_divergence'].iloc[-1]) if 'RSI_divergence' in df.columns else 0
        macd_hist = float(df['MACD_Histogram'].iloc[-1]) if 'MACD_Histogram' in df.columns else 0
        prev_macd_hist = float(df['MACD_Histogram'].iloc[-2]) if 'MACD_Histogram' in df.columns else 0
        stoch_k = float(df['Stochastic_K'].iloc[-1]) if 'Stochastic_K' in df.columns else 50
        stoch_d = float(df['Stochastic_D'].iloc[-1]) if 'Stochastic_D' in df.columns else 50

        vp = calculate_volume_profile(df.tail(100))

        adx = float(df['ADX'].iloc[-1]) if 'ADX' in df.columns else 20
        di_plus = float(df['DI_plus'].iloc[-1]) if 'DI_plus' in df.columns else 25
        di_minus = float(df['DI_minus'].iloc[-1]) if 'DI_minus' in df.columns else 25
        bb_width = float(df['BB_width'].iloc[-1]) if 'BB_width' in df.columns else 0.02
        bb_squeeze = bb_width < df['BB_width'].rolling(50).mean().iloc[-1] * 0.75 if 'BB_width' in df.columns else False

        in_killzone = is_in_killzone(symbol)
        session_mult = get_session_volatility_multiplier()

        # === Score long conditions ===
        long_score = 0.0
        long_reasons = []

        if structure == 'uptrend':
            long_score += 1.5
            long_reasons.append('uptrend_structure')
        if bos['direction'] == 'bullish':
            long_score += 1.5 + min(bos['strength'] * 0.3, 0.5)
            long_reasons.append(f'bullish_BOS({bos["strength"]:.1f})')
        if choch['detected'] and choch['direction'] == 'bullish':
            long_score += 2.0
            long_reasons.append('bullish_CHoCH')
        if at_bullish_ob:
            score_add = min(1.5 + best_bull_ob_strength * 0.2, 2.0)
            long_score += score_add
            long_reasons.append('at_bullish_OB')
        if near_bullish_fvg:
            long_score += 1.0
            long_reasons.append('near_bullish_FVG')
        if sweep['type'] == 'bullish_sweep':
            long_score += 1.5 + min(sweep['wick_depth'] * 0.3, 0.5)
            long_reasons.append('bullish_sweep')
        if mtf_bias['combined'] == 'bullish':
            long_score += 1.0 + mtf_bias['strength'] * 0.5
            long_reasons.append(f'MTF_bullish(str={mtf_bias["strength"]})')
        if pd_zone['zone'] == 'discount':
            long_score += 1.0
            long_reasons.append('discount_zone')
            if pd_zone['in_ote']:
                long_score += 0.5
                long_reasons.append('OTE_entry')
        if displacement['detected'] and displacement['direction'] == 'bullish':
            long_score += 1.0
            long_reasons.append(f'bullish_displacement({displacement["strength"]:.1f})')
        if candle_patterns['bullish']:
            long_score += 0.5 * len(candle_patterns['bullish'])
            long_reasons.extend([f'pattern:{p}' for p in candle_patterns['bullish']])
        if rsi_div == 1:
            long_score += 1.0
            long_reasons.append('bullish_RSI_divergence')
        if 35 < rsi < 65 and macd_hist > prev_macd_hist:
            long_score += 0.5
            long_reasons.append('momentum_rising')
        if stoch_k > stoch_d and stoch_k < 80:
            long_score += 0.5
            long_reasons.append('stoch_bullish')
        if current_price < vp['poc']:
            long_score += 0.5
            long_reasons.append('below_POC')
        if adx > 20 and di_plus > di_minus:
            long_score += 1.0
            long_reasons.append('ADX_bullish')
        if bb_squeeze:
            long_score += 0.5
            long_reasons.append('BB_squeeze')
        if in_killzone:
            long_score += 0.5
            long_reasons.append('in_killzone')
        if wyckoff == 'accumulation':
            long_score += 1.0
            long_reasons.append('wyckoff_accumulation')

        # === Score short conditions ===
        short_score = 0.0
        short_reasons = []

        if structure == 'downtrend':
            short_score += 1.5
            short_reasons.append('downtrend_structure')
        if bos['direction'] == 'bearish':
            short_score += 1.5 + min(bos['strength'] * 0.3, 0.5)
            short_reasons.append(f'bearish_BOS({bos["strength"]:.1f})')
        if choch['detected'] and choch['direction'] == 'bearish':
            short_score += 2.0
            short_reasons.append('bearish_CHoCH')
        if at_bearish_ob:
            score_add = min(1.5 + best_bear_ob_strength * 0.2, 2.0)
            short_score += score_add
            short_reasons.append('at_bearish_OB')
        if near_bearish_fvg:
            short_score += 1.0
            short_reasons.append('near_bearish_FVG')
        if sweep['type'] == 'bearish_sweep':
            short_score += 1.5 + min(sweep['wick_depth'] * 0.3, 0.5)
            short_reasons.append('bearish_sweep')
        if mtf_bias['combined'] == 'bearish':
            short_score += 1.0 + mtf_bias['strength'] * 0.5
            short_reasons.append(f'MTF_bearish(str={mtf_bias["strength"]})')
        if pd_zone['zone'] == 'premium':
            short_score += 1.0
            short_reasons.append('premium_zone')
            if pd_zone['in_ote']:
                short_score += 0.5
                short_reasons.append('OTE_entry')
        if displacement['detected'] and displacement['direction'] == 'bearish':
            short_score += 1.0
            short_reasons.append(f'bearish_displacement({displacement["strength"]:.1f})')
        if candle_patterns['bearish']:
            short_score += 0.5 * len(candle_patterns['bearish'])
            short_reasons.extend([f'pattern:{p}' for p in candle_patterns['bearish']])
        if rsi_div == -1:
            short_score += 1.0
            short_reasons.append('bearish_RSI_divergence')
        if 35 < rsi < 65 and macd_hist < prev_macd_hist:
            short_score += 0.5
            short_reasons.append('momentum_falling')
        if stoch_k < stoch_d and stoch_k > 20:
            short_score += 0.5
            short_reasons.append('stoch_bearish')
        if current_price > vp['poc']:
            short_score += 0.5
            short_reasons.append('above_POC')
        if adx > 20 and di_minus > di_plus:
            short_score += 1.0
            short_reasons.append('ADX_bearish')
        if bb_squeeze:
            short_score += 0.5
            short_reasons.append('BB_squeeze')
        if in_killzone:
            short_score += 0.5
            short_reasons.append('in_killzone')
        if wyckoff == 'distribution':
            short_score += 1.0
            short_reasons.append('wyckoff_distribution')

        # === Decision ===
        max_possible = 16.0
        min_entry_score = 6.0

        signal = {
            'direction': 'Neutral',
            'signal_strength': 0,
            'entry_type': '',
            'scalp_target': None,
            'confirmation_count': 0,
            'setup_quality': 0,
            'market': AVAILABLE_MARKETS.get(symbol, symbol),
            'structure': structure,
            'bos': bos['direction'],
            'choch': choch['direction'] if choch['detected'] else 'none',
            'htf_bias': mtf_bias['combined'],
            'pd_zone': pd_zone['zone'],
            'wyckoff': wyckoff,
            'reasons': [],
            'rsi': rsi,
            'macd_hist': macd_hist,
            'ema8': float(df['EMA8'].iloc[-1]) if 'EMA8' in df.columns else 0,
            'ema21': float(df['EMA21'].iloc[-1]) if 'EMA21' in df.columns else 0,
            'session_mult': session_mult,
        }

        if long_score >= min_entry_score and long_score > short_score * 1.5:
            strength = min(long_score / max_possible, 1.0)
            tp_mult = 3.0 if long_score >= 10 else 2.5
            signal.update({
                'direction': 'Buy',
                'signal_strength': strength,
                'entry_type': 'SMC_Premium' if long_score >= 10 else 'SMC_Long' if long_score >= 8 else 'Confluence_Long',
                'confirmation_count': len(long_reasons),
                'setup_quality': (long_score / max_possible) * 100,
                'scalp_target': current_price + atr * tp_mult,
                'reasons': long_reasons,
            })
        elif short_score >= min_entry_score and short_score > long_score * 1.5:
            strength = min(short_score / max_possible, 1.0)
            tp_mult = 3.0 if short_score >= 10 else 2.5
            signal.update({
                'direction': 'Sell',
                'signal_strength': strength,
                'entry_type': 'SMC_Premium' if short_score >= 10 else 'SMC_Short' if short_score >= 8 else 'Confluence_Short',
                'confirmation_count': len(short_reasons),
                'setup_quality': (short_score / max_possible) * 100,
                'scalp_target': current_price - atr * tp_mult,
                'reasons': short_reasons,
            })

        logging.info(
            f"=== SMC Signal === {symbol} | Price: {current_price:.5f} | "
            f"Structure: {structure} | BOS: {bos['direction']} | CHoCH: {choch['direction'] if choch['detected'] else 'none'} | "
            f"MTF: {mtf_bias['combined']} | Zone: {pd_zone['zone']} | Wyckoff: {wyckoff} | "
            f"Long: {long_score:.1f}/{max_possible} | Short: {short_score:.1f}/{max_possible} | "
            f"Direction: {signal['direction']} | Strength: {signal['signal_strength']:.2f} | "
            f"Type: {signal['entry_type']} | KZ: {in_killzone}"
        )

        return signal

    except Exception as e:
        logging.error(f"Error in signal generation: {str(e)}")
        return {'direction': 'Neutral', 'signal_strength': 0}


def analyze_daily_candle(df, symbol):
    try:
        if len(df) < 30:
            return {'direction': 'Neutral', 'signal_strength': 0}

        current_price = float(df['close'].iloc[-1])
        atr = float(df['ATR'].iloc[-1]) if 'ATR' in df.columns else current_price * 0.001

        structure = calculate_market_structure(df)
        bos = detect_break_of_structure(df)
        choch = detect_change_of_character(df)
        order_blocks = detect_order_blocks(df, lookback=30)
        fvgs = detect_fair_value_gaps(df, lookback=20)
        sweep = detect_liquidity_sweep(df, lookback=10)
        pd_zone = calculate_premium_discount(df)
        candle_patterns = detect_institutional_candle_patterns(df)

        rsi = float(df['RSI'].iloc[-1]) if 'RSI' in df.columns else 50
        macd_hist = float(df['MACD_Histogram'].iloc[-1]) if 'MACD_Histogram' in df.columns else 0
        adx = float(df['ADX'].iloc[-1]) if 'ADX' in df.columns else 20

        signal = {
            'direction': 'Neutral', 'signal_strength': 0, 'pattern': '',
            'structure': structure, 'market': AVAILABLE_MARKETS.get(symbol, symbol),
            'confirmation_count': 0, 'risk_reward': 0,
            'ema8': float(df['EMA8'].iloc[-1]) if 'EMA8' in df.columns else 0,
            'ema21': float(df['EMA21'].iloc[-1]) if 'EMA21' in df.columns else 0,
            'rsi': rsi, 'macd_hist': macd_hist, 'confirmations': [],
        }

        score = 0
        confirmations = []

        if structure == 'uptrend' and bos['direction'] == 'bullish':
            score += 3
            confirmations.append('bullish_structure+BOS')
            signal['direction'] = 'Buy'
            signal['pattern'] = 'Bullish_BOS'
        elif structure == 'downtrend' and bos['direction'] == 'bearish':
            score += 3
            confirmations.append('bearish_structure+BOS')
            signal['direction'] = 'Sell'
            signal['pattern'] = 'Bearish_BOS'
        elif choch['detected']:
            score += 2
            if choch['direction'] == 'bullish':
                signal['direction'] = 'Buy'
                signal['pattern'] = 'Bullish_CHoCH'
                confirmations.append('bullish_CHoCH')
            else:
                signal['direction'] = 'Sell'
                signal['pattern'] = 'Bearish_CHoCH'
                confirmations.append('bearish_CHoCH')

        if signal['direction'] == 'Neutral':
            return signal

        relevant_obs = [ob for ob in order_blocks if ob['type'] == ('bullish' if signal['direction'] == 'Buy' else 'bearish')]
        if any(ob['bottom'] - atr <= current_price <= ob['top'] + atr for ob in relevant_obs):
            score += 2
            confirmations.append('at_order_block')

        relevant_fvgs = [f for f in fvgs if f['type'] == ('bullish' if signal['direction'] == 'Buy' else 'bearish')]
        if any(f['bottom'] - atr * 0.5 <= current_price <= f['top'] + atr * 0.5 for f in relevant_fvgs):
            score += 1
            confirmations.append('near_FVG')

        if (signal['direction'] == 'Buy' and sweep['type'] == 'bullish_sweep') or \
           (signal['direction'] == 'Sell' and sweep['type'] == 'bearish_sweep'):
            score += 2
            confirmations.append('liquidity_sweep')

        if (signal['direction'] == 'Buy' and pd_zone['zone'] == 'discount') or \
           (signal['direction'] == 'Sell' and pd_zone['zone'] == 'premium'):
            score += 1
            confirmations.append(f'{pd_zone["zone"]}_zone')

        if signal['direction'] == 'Buy' and macd_hist > 0 and rsi > 45:
            score += 1
            confirmations.append('momentum_confirms')
        elif signal['direction'] == 'Sell' and macd_hist < 0 and rsi < 55:
            score += 1
            confirmations.append('momentum_confirms')

        if adx > 25:
            score += 1
            confirmations.append('strong_trend')

        relevant_patterns = candle_patterns['bullish'] if signal['direction'] == 'Buy' else candle_patterns['bearish']
        if relevant_patterns:
            score += 1
            confirmations.extend([f'pattern:{p}' for p in relevant_patterns])

        max_score = 13
        signal['confirmation_count'] = len(confirmations)
        signal['signal_strength'] = min(score / max_score, 1.0)
        signal['confirmations'] = confirmations
        signal['setup_quality'] = (score / max_score) * 100

        if score < 4:
            signal['direction'] = 'Neutral'
            signal['signal_strength'] = 0

        logging.info(
            f"=== Daily SMC === {symbol} | {signal['direction']} | "
            f"Structure: {structure} | BOS: {bos['direction']} | "
            f"Score: {score}/{max_score} | Strength: {signal['signal_strength']:.2f} | "
            f"Confirmations: {', '.join(confirmations)}"
        )

        return signal

    except Exception as e:
        logging.error(f"Error in daily analysis: {e}")
        return {'direction': 'Neutral', 'signal_strength': 0}


# ============================================================
# Execution Filters — Enhanced
# ============================================================

def should_execute_trade(signal, news):
    try:
        if signal['direction'] not in ['Buy', 'Sell']:
            return False

        if signal.get('signal_strength', 0) < 0.45:
            logging.info(f"Rejected: strength {signal.get('signal_strength', 0):.2f} < 0.45")
            return False

        if signal.get('confirmation_count', 0) < 3:
            logging.info(f"Rejected: {signal.get('confirmation_count', 0)} confirmations < 3")
            return False

        structure = signal.get('structure', 'ranging')
        if signal['direction'] == 'Buy' and structure == 'downtrend':
            if signal.get('choch') != 'bullish':
                logging.info("Rejected: Buy in downtrend without bullish CHoCH")
                return False
        if signal['direction'] == 'Sell' and structure == 'uptrend':
            if signal.get('choch') != 'bearish':
                logging.info("Rejected: Sell in uptrend without bearish CHoCH")
                return False

        htf = signal.get('htf_bias', 'neutral')
        if htf != 'neutral':
            if signal['direction'] == 'Buy' and htf == 'bearish':
                logging.info("Rejected: Buy against bearish HTF")
                return False
            if signal['direction'] == 'Sell' and htf == 'bullish':
                logging.info("Rejected: Sell against bullish HTF")
                return False

        pd_zone = signal.get('pd_zone', 'equilibrium')
        if signal['direction'] == 'Buy' and pd_zone == 'premium':
            logging.info("Rejected: Buy in premium zone")
            return False
        if signal['direction'] == 'Sell' and pd_zone == 'discount':
            logging.info("Rejected: Sell in discount zone")
            return False

        if news:
            high_impact = [n for n in news if n.get('impact_level') == 'high']
            if high_impact:
                avg_sentiment = sum(n.get('sentiment_score', 0) for n in high_impact) / len(high_impact)
                if signal['direction'] == 'Buy' and avg_sentiment < -0.5:
                    logging.info("Rejected: Buy against strongly bearish news")
                    return False
                if signal['direction'] == 'Sell' and avg_sentiment > 0.5:
                    logging.info("Rejected: Sell against strongly bullish news")
                    return False

        reasons = signal.get('reasons', signal.get('confirmations', []))
        logging.info(
            f"Trade Approved: {signal['direction']} | Strength: {signal['signal_strength']:.2f} | "
            f"Confirmations: {signal['confirmation_count']} | Structure: {structure} | "
            f"Reasons: {', '.join(reasons)}"
        )
        return True

    except Exception as e:
        logging.error(f"Error in execution filter: {e}")
        return False


def check_trend_alignment(signal):
    try:
        ema8 = signal.get('ema8', 0)
        ema21 = signal.get('ema21', 0)
        rsi = signal.get('rsi', 50)
        macd_hist = signal.get('macd_hist', 0)
        structure = signal.get('structure', 'ranging')

        if signal['direction'] == 'Buy':
            return (ema8 > ema21 and rsi > 40 and macd_hist > 0 and structure != 'downtrend')
        else:
            return (ema8 < ema21 and rsi < 60 and macd_hist < 0 and structure != 'uptrend')
    except Exception as e:
        logging.error(f"Error checking trend alignment: {e}")
        return False


def verify_volume_conditions(signal):
    return True


def check_volatility_conditions(signal):
    try:
        atr = signal.get('atr', 0)
        avg_atr = signal.get('avg_atr', 0)
        if atr == 0 or avg_atr == 0:
            return True
        volatility_ratio = atr / avg_atr
        return 0.5 <= volatility_ratio <= 2.5
    except Exception as e:
        logging.error(f"Error checking volatility: {e}")
        return False


# ============================================================
# News Fetching
# ============================================================

async def fetch_market_news():
    try:
        if not FINNHUB_API_KEY:
            logging.warning("FINNHUB_API_KEY not set — skipping news fetch")
            return get_fallback_news()

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)

        urls = [
            f"https://finnhub.io/api/v1/news?category=forex&token={FINNHUB_API_KEY}",
            f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_API_KEY}"
        ]

        all_articles = []
        async with aiohttp.ClientSession(connector=connector) as session:
            for url in urls:
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        if response.status == 200:
                            news = await response.json()
                            if isinstance(news, list):
                                all_articles.extend(news)
                except Exception as e:
                    logging.error(f"Error fetching news: {e}")
                    continue

        if all_articles:
            formatted_articles = []
            for article in all_articles:
                if isinstance(article, dict):
                    formatted_articles.append({
                        'title': article.get('headline', article.get('title', '')),
                        'description': article.get('summary', article.get('description', '')),
                        'url': article.get('url', ''),
                        'publishedAt': (
                            datetime.fromtimestamp(article.get('datetime', 0)).strftime('%Y-%m-%dT%H:%M:%SZ')
                            if article.get('datetime')
                            else datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')
                        ),
                        'content': article.get('summary', article.get('content', ''))
                    })
            return formatted_articles

        return get_fallback_news()
    except Exception as e:
        logging.error(f"Error in fetch_market_news: {e}")
        return get_fallback_news()


def get_fallback_news():
    current_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{
        'title': 'Market Analysis: Technical Indicators Update',
        'description': 'General market analysis shows mixed signals across different assets.',
        'url': '',
        'publishedAt': current_time,
        'content': 'Markets are showing interesting technical setups with risk management remaining key.'
    }]


def filter_market_news(news_articles, symbol):
    if not news_articles:
        return []

    filtered_news = []
    market_type = get_market_type(symbol)
    keywords = {
        'forex': {
            'primary': [symbol[3:6].lower(), symbol[6:].lower(), 'forex', 'currency'] if symbol.startswith('frx') else ['forex'],
            'secondary': ['central bank', 'interest rate', 'inflation', 'economic', 'fed', 'ecb', 'monetary policy',
                         'nfp', 'cpi', 'gdp', 'employment', 'fomc']
        },
        'commodities': {
            'primary': ['gold', 'silver', 'oil', 'crude', 'brent', 'wti', 'xau'],
            'secondary': ['commodity', 'precious metal', 'opec', 'supply', 'demand']
        },
        'synthetic': {
            'primary': ['synthetic', 'index', 'technical', 'trend'],
            'secondary': ['market', 'trading', 'momentum', 'volatility']
        },
        'volatility': {
            'primary': ['volatility', 'vix', 'market volatility'],
            'secondary': ['risk', 'sentiment', 'market', 'trading']
        }
    }

    market_keywords = keywords.get(market_type, {'primary': ['market'], 'secondary': ['trend']})
    for article in news_articles:
        try:
            content = ' '.join(filter(None, [
                article.get('title', ''), article.get('description', ''), article.get('content', '')
            ])).lower()
            if not content:
                continue
            relevance_score = 0
            matched_keywords = []
            for kw in market_keywords['primary']:
                if kw in content:
                    relevance_score += 3
                    matched_keywords.append(kw)
            for kw in market_keywords['secondary']:
                if kw in content:
                    relevance_score += 1
                    matched_keywords.append(kw)
            if relevance_score >= 2:
                sia = SentimentIntensityAnalyzer()
                sentiment = sia.polarity_scores(content)
                filtered_news.append({
                    'title': article['title'],
                    'description': article.get('description', ''),
                    'url': article.get('url', ''),
                    'publishedAt': article['publishedAt'],
                    'relevance_score': relevance_score,
                    'sentiment_score': sentiment['compound'],
                    'impact_level': 'high' if relevance_score >= 6 else 'medium',
                    'market_type': market_type,
                    'matched_keywords': matched_keywords
                })
        except Exception:
            continue

    filtered_news.sort(key=lambda x: x['relevance_score'], reverse=True)
    return filtered_news[:5]


def print_news_summary(filtered_news):
    if not filtered_news:
        print("\nNo relevant market news found.")
        return
    print("\n" + "=" * 50)
    print("MARKET NEWS SUMMARY")
    print("=" * 50)
    for i, article in enumerate(filtered_news, 1):
        try:
            sentiment = article['sentiment_score']
            icon = "+" if sentiment > 0.2 else "-" if sentiment < -0.2 else "~"
            print(f"\n{i}. [{icon}] {article['title']}")
            print(f"   Impact: {article['impact_level'].upper()} | Score: {article['relevance_score']}")
            print(f"   Sentiment: {sentiment:.2f}")
        except Exception:
            continue
    print("\n" + "=" * 50)


# ============================================================
# Model Training — Enhanced with walk-forward validation
# ============================================================

def build_tf_model(input_shape):
    model = keras.Sequential([
        layers.Dense(128, activation='relu', input_shape=input_shape,
                     kernel_regularizer=keras.regularizers.l2(0.001)),
        layers.Dropout(0.3),
        layers.Dense(64, activation='relu', kernel_regularizer=keras.regularizers.l2(0.001)),
        layers.BatchNormalization(),
        layers.Dropout(0.2),
        layers.Dense(32, activation='relu'),
        layers.BatchNormalization(),
        layers.Dropout(0.2),
        layers.Dense(16, activation='relu'),
        layers.Dense(1, activation='sigmoid')
    ])
    optimizer = keras.optimizers.Adam(learning_rate=0.0005)
    model.compile(optimizer=optimizer, loss='binary_crossentropy', metrics=['accuracy', 'AUC'])
    return model


def cleanup_old_models(keep_latest=3):
    model_files = glob.glob('volatility_model_*.keras')
    model_files.sort(key=os.path.getmtime, reverse=True)
    for old_file in model_files[keep_latest:]:
        try:
            os.remove(old_file)
            timestamp = old_file.replace('volatility_model_', '').replace('.keras', '')
            scaler_file = f'scaler_{timestamp}.joblib'
            if os.path.exists(scaler_file):
                os.remove(scaler_file)
        except Exception as e:
            logging.error(f"Error removing old files: {e}")


def train_model(df):
    features = [
        'open', 'high', 'low', 'close', 'volatility',
        'SMA_20', 'RSI', 'MACD', 'Bollinger_Upper', 'Bollinger_Lower',
        'Momentum', 'ATR', 'EMA_12', 'EMA_26', 'MACD_Histogram',
        'Stochastic_K', 'Stochastic_D', 'mean', 'median', 'std'
    ]

    available_features = [f for f in features if f in df.columns]
    X = df[available_features].fillna(0)
    y = np.where(df['close'].pct_change().shift(-1).abs() > 0.015, 1, 0)

    tscv = TimeSeriesSplit(n_splits=5)
    scores = []

    scaler = RobustScaler()
    X_scaled = scaler.fit_transform(X)

    for train_idx, test_idx in tscv.split(X_scaled):
        X_train, X_test = X_scaled[train_idx], X_scaled[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]

        model = build_tf_model((X_train.shape[1],))
        early_stopping = keras.callbacks.EarlyStopping(
            monitor='val_auc', patience=10, restore_best_weights=True, mode='max')
        reduce_lr = keras.callbacks.ReduceLROnPlateau(
            monitor='val_loss', factor=0.5, patience=5, min_lr=1e-6)

        class_weights = compute_class_weight('balanced', classes=np.unique(y_train), y=y_train)
        class_weight_dict = dict(enumerate(class_weights))

        model.fit(X_train, y_train, epochs=100, batch_size=32, validation_split=0.2,
                  callbacks=[early_stopping, reduce_lr], class_weight=class_weight_dict, verbose=0)

        _, _, auc = model.evaluate(X_test, y_test, verbose=0)
        scores.append(auc)

    logging.info(f"Walk-forward AUC scores: {[f'{s:.3f}' for s in scores]} | Mean: {np.mean(scores):.3f}")

    model = build_tf_model((len(available_features),))
    early_stopping = keras.callbacks.EarlyStopping(
        monitor='val_auc', patience=10, restore_best_weights=True, mode='max')
    reduce_lr = keras.callbacks.ReduceLROnPlateau(
        monitor='val_loss', factor=0.5, patience=5, min_lr=1e-6)
    class_weights = compute_class_weight('balanced', classes=np.unique(y), y=y)
    class_weight_dict = dict(enumerate(class_weights))

    model.fit(X_scaled, y, epochs=100, batch_size=32, validation_split=0.15,
              callbacks=[early_stopping, reduce_lr], class_weight=class_weight_dict, verbose=1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    model.save(f'volatility_model_{timestamp}.keras')
    joblib.dump(scaler, f'scaler_{timestamp}.joblib')
    cleanup_old_models()

    return model, scaler, df


def load_latest_model():
    try:
        model_files = glob.glob('volatility_model_*.keras')
        if not model_files:
            return None, None
        latest = max(model_files, key=os.path.getmtime)
        timestamp = latest.replace('volatility_model_', '').replace('.keras', '')
        model = keras.models.load_model(latest)
        scaler_file = f'scaler_{timestamp}.joblib'
        if not os.path.exists(scaler_file):
            return None, None
        scaler = joblib.load(scaler_file)
        return model, scaler
    except Exception as e:
        logging.error(f"Error loading latest model: {e}")
        return None, None


def predict_next_move(model, scaler, latest_data):
    features = [
        'open', 'high', 'low', 'close', 'volatility',
        'SMA_20', 'RSI', 'MACD', 'Bollinger_Upper', 'Bollinger_Lower',
        'Momentum', 'ATR', 'EMA_12', 'EMA_26', 'MACD_Histogram',
        'Stochastic_K', 'Stochastic_D', 'mean', 'median', 'std'
    ]
    available = [f for f in features if f in latest_data.columns]
    X_new = latest_data[available].fillna(0)
    X_new_scaled = scaler.transform(X_new)
    prediction_proba = model.predict(X_new_scaled)
    confidence_threshold = 0.6
    if prediction_proba > confidence_threshold:
        return 1, prediction_proba[0][0]
    elif prediction_proba < (1 - confidence_threshold):
        return 0, prediction_proba[0][0]
    return None, prediction_proba[0][0]


# ============================================================
# Position Sizing & Risk Management — Enhanced
# ============================================================

def calculate_position_size(risk_percentage=None, account_balance=0, stop_loss_pips=0, atr=None, avg_atr=None):
    try:
        if risk_percentage is None:
            risk_percentage = RISK_PERCENT_PER_TRADE
        risk_decimal = risk_percentage / 100
        max_risk_amount = account_balance * risk_decimal
        pip_value = 10
        if stop_loss_pips == 0:
            stop_loss_pips = 1
        position_size = max_risk_amount / (stop_loss_pips * pip_value)

        if account_balance >= 10000:
            position_size = min(position_size * 1.2, account_balance * 0.05 / 100000)
        elif account_balance >= 5000:
            position_size = min(position_size * 1.1, account_balance * 0.04 / 100000)
        elif account_balance >= 1000:
            position_size = min(position_size, account_balance * 0.03 / 100000)
        elif account_balance >= 500:
            position_size = min(position_size * 0.9, account_balance * 0.02 / 100000)
        else:
            position_size = min(position_size * 0.8, account_balance * 0.01 / 100000)

        if avg_atr and atr:
            volatility_ratio = atr / avg_atr
            if volatility_ratio > 1.5:
                position_size *= 0.7
            elif volatility_ratio > 1.2:
                position_size *= 0.85
            elif volatility_ratio < 0.7:
                position_size *= 1.1

        max_size_limit = account_balance * 0.03 / 10000
        position_size = min(position_size, max_size_limit)
        position_size = max(position_size, 0.001)
        position_size = round(position_size, 3)
        return position_size
    except Exception as e:
        logging.error(f"Error calculating position size: {e}")
        return 0.001


def get_market_specific_lot_size(symbol, base_lot_size, account_balance):
    try:
        volatility_multipliers = {
            '1HZ10V': 1.0, '1HZ25V': 0.4, '1HZ50V': 0.2, '1HZ75V': 0.1, '1HZ100V': 0.05,
            'R_10': 0.8, 'R_25': 0.5, 'R_50': 0.3, 'R_75': 0.15, 'R_100': 0.1,
            'CRASH_1000': 0.2, 'CRASH_500': 0.15, 'BOOM_1000': 0.2, 'BOOM_500': 0.15,
            'Jump_75': 0.2, 'Jump_100': 0.15, 'Jump_50': 0.3, 'STPRD': 0.4,
        }
        multiplier = volatility_multipliers.get(symbol, 1.0)
        if account_balance < 10:
            if symbol in ['1HZ50V', '1HZ75V', '1HZ100V', 'R_75', 'R_100', 'CRASH_500', 'Jump_100']:
                return 0.001
            if symbol in ['1HZ25V', 'R_50', 'CRASH_1000', 'BOOM_1000']:
                if base_lot_size > 0.001:
                    return 0.001
        elif account_balance < 50 and symbol in volatility_multipliers:
            if base_lot_size > 0.01 and multiplier < 0.5:
                return 0.01

        adjusted = round(max(0.001, base_lot_size * multiplier), 3)
        return adjusted
    except Exception as e:
        logging.error(f"Error adjusting market-specific lot size: {e}")
        return base_lot_size


def optimize_lot_size(symbol, account_balance, risk_percentage, stop_loss_price, entry_price, signal_strength=0.75):
    try:
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return 0.001
        account_info = mt5.account_info()
        if account_info is None:
            return 0.001
        actual_balance = account_info.balance

        market_type = 'other'
        if symbol.startswith('frx'):
            major_pairs = ['EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'AUDUSD', 'USDCAD']
            market_type = 'forex_major' if any(m in symbol for m in major_pairs) else 'forex'
        elif any(x in symbol for x in ['R_', '1HZ']):
            market_type = 'volatility'
        elif any(x in symbol for x in ['CRASH', 'BOOM', 'Jump']):
            market_type = 'synthetic'

        account_protection = None
        for var in globals().values():
            if isinstance(var, AccountProtection):
                account_protection = var
                break

        if account_protection and getattr(account_protection, 'force_nano_mode', False):
            return get_adjusted_nano_lot_size(signal_strength, market_type)

        if actual_balance <= 5:
            lot = get_adjusted_nano_lot_size(signal_strength, market_type)
            return min(lot, 0.002)

        return get_scaled_lot_size(actual_balance, signal_strength, market_type)
    except Exception as e:
        logging.error(f"Error optimizing lot size: {e}")
        return 0.001


def get_adjusted_nano_lot_size(signal_strength, market_type='forex'):
    try:
        if signal_strength >= 0.98 and market_type == 'forex_major':
            return 0.003
        elif signal_strength >= 0.98 and market_type == 'forex':
            return 0.002
        elif signal_strength >= 0.95 and market_type == 'forex_major':
            return 0.002
        return 0.001
    except Exception:
        return 0.001


def get_scaled_lot_size(account_balance, signal_strength, market_type='forex'):
    try:
        tiers = [
            (5, 0.001), (10, 0.002), (20, 0.003), (50, 0.005),
            (100, 0.01), (200, 0.02), (500, 0.03), (1000, 0.05),
            (2000, 0.1), (5000, 0.2), (10000, 0.3), (float('inf'), 0.5)
        ]

        if account_balance <= 5:
            return get_adjusted_nano_lot_size(signal_strength, market_type)

        base_lot = tiers[0][1]
        for tier_bal, tier_lot in tiers:
            if account_balance <= tier_bal:
                base_lot = tier_lot
                break

        if signal_strength >= 0.95:
            adj = base_lot
        elif signal_strength >= 0.90:
            adj = base_lot * 0.9
        elif signal_strength >= 0.85:
            adj = base_lot * 0.8
        elif signal_strength >= 0.80:
            adj = base_lot * 0.7
        else:
            adj = base_lot * 0.5

        if market_type == 'volatility':
            adj *= 0.7
        elif market_type == 'synthetic':
            adj *= 0.8
        elif market_type != 'forex_major':
            adj *= 0.9

        return max(0.001, round(adj, 3))
    except Exception:
        return 0.001


def calculate_dynamic_sl_tp(current_price, direction, atr, signal):
    """
    Dynamic SL/TP based on signal quality and structure.
    Higher confluence = tighter SL with wider TP (better R:R).
    """
    quality = signal.get('setup_quality', 50)

    if quality >= 80:
        sl_mult = 1.2
        tp_mult = 4.0
    elif quality >= 60:
        sl_mult = 1.5
        tp_mult = 3.0
    else:
        sl_mult = 1.8
        tp_mult = 2.5

    session_mult = signal.get('session_mult', 1.0)
    sl_mult *= (0.8 + session_mult * 0.2)

    if direction == 'Buy':
        stop_loss = current_price - (atr * sl_mult)
        take_profit = current_price + (atr * tp_mult)
    else:
        stop_loss = current_price + (atr * sl_mult)
        take_profit = current_price - (atr * tp_mult)

    return stop_loss, take_profit


def calculate_stop_loss_and_take_profit(current_price, risk_percentage, account_balance, symbol='frxEURUSD', symbol_info=None, df=None):
    try:
        if symbol_info is None or df is None:
            return None, None
        symbol_data = symbol_info.get(symbol, symbol_info.get('frxEURUSD'))
        atr = df['ATR'].iloc[-1]
        pip_size = symbol_data['point']
        sl_distance = atr * 1.5
        tp_distance = sl_distance * 3.0
        stop_loss = round(current_price - sl_distance, symbol_data['digits'])
        take_profit = round(current_price + tp_distance, symbol_data['digits'])
        return stop_loss, take_profit
    except Exception as e:
        logging.error(f"Error in SL/TP calculation: {e}")
        return None, None


def get_trade_parameters(signal, current_price, account_balance, atr, avg_atr=None, symbol='frxEURUSD'):
    try:
        account_info = mt5.account_info()
        actual_balance = account_info.balance if account_info else account_balance
        is_micro_account = actual_balance < 20

        if is_micro_account:
            risk_percentage = 0.5
            position_size = 0.01
        elif actual_balance < 50:
            risk_percentage = 0.8
        elif actual_balance < 100:
            risk_percentage = 1.0
        else:
            risk_percentage = 1.2

        stop_loss, take_profit = calculate_dynamic_sl_tp(current_price, signal['direction'], atr, signal)

        stop_loss_pips = abs(current_price - stop_loss) / 0.00001

        if not is_micro_account:
            position_size = calculate_position_size(risk_percentage, actual_balance, stop_loss_pips, atr, avg_atr)

        signal_strength = signal.get('signal_strength', 0)
        if is_micro_account and signal_strength < 0.7:
            return None

        return {
            'type': signal['direction'],
            'position_size': position_size,
            'entry_price': current_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'risk_percentage': risk_percentage,
            'account_balance': actual_balance,
            'setup_quality': signal_strength * 100,
            'entry_type': signal.get('entry_type', 'Standard'),
            'symbol': symbol,
            'risk_level': 'ultra_conservative' if is_micro_account else 'conservative',
            'market_name': AVAILABLE_MARKETS.get(symbol, symbol),
        }
    except Exception as e:
        logging.error(f"Error calculating trade parameters: {e}")
        return None


def advisory_decision(direction, price, tp, sl, balance, news=None):
    risk_reward = abs(tp - price) / abs(price - sl) if abs(price - sl) > 0 else 0
    news_impact = "No significant news impact"
    if news:
        sentiment = sum(n.get('sentiment_score', n.get('sentiment', 0)) for n in news) / len(news)
        if abs(sentiment) > 0.3:
            news_impact = f"News sentiment: {'Bullish' if sentiment > 0 else 'Bearish'} ({sentiment:.2f})"
    return (
        f"Trade Advisory: {direction} | Entry: {price:.5f} | SL: {sl:.5f} | TP: {tp:.5f} | "
        f"R:R: {risk_reward:.2f} | Balance: {balance:.2f} | {news_impact}"
    )


# ============================================================
# MT5 Connection & Trade Execution
# ============================================================

def initialize_mt5():
    try:
        mt5.shutdown()
        if not MT5_PASSWORD:
            logging.error("MT5_PASSWORD not set in environment variables")
            return False
        for mt5_path in MT5_PATHS:
            if os.path.exists(mt5_path):
                init_result = mt5.initialize(path=mt5_path, login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER, timeout=30000)
                if init_result:
                    global MT5_PATH
                    MT5_PATH = mt5_path
                    account_info = mt5.account_info()
                    if account_info is not None:
                        logging.info(f"MT5 Connected: Login={account_info.login}, Balance=${account_info.balance:.2f}")
                        return True
        error_code = mt5.last_error()
        logging.error(f"MT5 initialization failed: {error_code}")
        return False
    except Exception as e:
        logging.error(f"MT5 initialization error: {e}")
        return False


def map_deriv_to_mt5_symbol(deriv_symbol):
    try:
        if not deriv_symbol:
            return "EURUSD"

        mapping = {
            '1HZ10V': 'Volatility 10 (1s) Index', '1HZ25V': 'Volatility 25 (1s) Index',
            '1HZ50V': 'Volatility 50 (1s) Index', '1HZ75V': 'Volatility 75 (1s) Index',
            '1HZ100V': 'Volatility 100 (1s) Index',
            'R_10': 'Volatility 10 Index', 'R_25': 'Volatility 25 Index',
            'R_50': 'Volatility 50 Index', 'R_75': 'Volatility 75 Index', 'R_100': 'Volatility 100 Index',
            'BOOM300': 'Boom 300 Index', 'BOOM500': 'Boom 500 Index', 'BOOM1000': 'Boom 1000 Index',
            'CRASH300': 'Crash 300 Index', 'CRASH500': 'Crash 500 Index', 'CRASH1000': 'Crash 1000 Index',
            'STPRNG': 'Step Index', 'STPIND': 'Step Index',
            'JUMP10': 'Jump 10 Index', 'JUMP25': 'Jump 25 Index', 'JUMP50': 'Jump 50 Index',
            'JUMP75': 'Jump 75 Index', 'JUMP100': 'Jump 100 Index',
            'frxXAUUSD': 'XAUUSD', 'frxXAGUSD': 'XAGUSD', 'frxBRENT': 'Brent', 'frxWTI': 'WTI',
            'XAUUSD': 'XAUUSD', 'XAGUSD': 'XAGUSD', 'BRENT': 'Brent', 'WTI': 'WTI',
            'frxEURUSD': 'EURUSD', 'frxGBPUSD': 'GBPUSD', 'frxUSDJPY': 'USDJPY',
            'frxAUDUSD': 'AUDUSD', 'frxUSDCAD': 'USDCAD', 'frxUSDCHF': 'USDCHF',
            'frxEURGBP': 'EURGBP', 'frxEURJPY': 'EURJPY', 'frxGBPJPY': 'GBPJPY',
            'frxNZDUSD': 'NZDUSD', 'frxEURAUD': 'EURAUD', 'frxEURCAD': 'EURCAD',
            'frxAUDJPY': 'AUDJPY', 'frxEURCHF': 'EURCHF',
            'EURUSD': 'EURUSD', 'GBPUSD': 'GBPUSD', 'USDJPY': 'USDJPY',
        }

        if deriv_symbol in mapping:
            return mapping[deriv_symbol]
        if deriv_symbol.startswith('frx'):
            std = deriv_symbol[3:]
            return mapping.get(std, std)
        if 'R_' in deriv_symbol:
            return f"Volatility {deriv_symbol.replace('R_', '')} Index"
        return deriv_symbol
    except Exception as e:
        logging.error(f"Error mapping symbol {deriv_symbol}: {e}")
        return "EURUSD"


def fetch_mt5_historical_data(symbol, timeframe, num_bars=500):
    try:
        mt5_symbol = map_deriv_to_mt5_symbol(symbol)
        symbol_info = mt5.symbol_info(mt5_symbol)
        if symbol_info is None:
            logging.error(f"Symbol {mt5_symbol} not found in MT5")
            return []

        timeframe_map = {
            'M1': mt5.TIMEFRAME_M1, 'M5': mt5.TIMEFRAME_M5, 'M15': mt5.TIMEFRAME_M15,
            'M30': mt5.TIMEFRAME_M30, 'H1': mt5.TIMEFRAME_H1, 'H4': mt5.TIMEFRAME_H4,
            'D1': mt5.TIMEFRAME_D1, 'W1': mt5.TIMEFRAME_W1, 'MN1': mt5.TIMEFRAME_MN1
        }
        mt5_timeframe = timeframe_map.get(timeframe, mt5.TIMEFRAME_M5)
        rates = mt5.copy_rates_from_pos(mt5_symbol, mt5_timeframe, 0, num_bars)

        if rates is None or len(rates) == 0:
            logging.error(f"Failed to fetch MT5 rates for {mt5_symbol}: {mt5.last_error()}")
            return []

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df = df.rename(columns={'time': 'timestamp', 'tick_volume': 'volume'})
        return df.to_dict('records')
    except Exception as e:
        logging.error(f"Error fetching MT5 historical data: {e}")
        return []


def execute_mt5_trade(trade_params):
    try:
        account_info = mt5.account_info()
        if account_info is None:
            return False

        is_nano_account = account_info.balance < 10
        is_micro_account = account_info.balance < 20 and not is_nano_account

        positions = mt5.positions_get()
        if positions is None:
            return False
        if len(positions) >= 1:
            logging.warning(f"Position limit reached: {len(positions)}/1")
            return False

        mt5_symbol = map_deriv_to_mt5_symbol(trade_params.get('symbol', 'EURUSD'))
        symbol_info = mt5.symbol_info(mt5_symbol)
        if symbol_info is None:
            return False

        if not symbol_info.visible:
            if not mt5.symbol_select(mt5_symbol, True):
                return False

        tick = mt5.symbol_info_tick(mt5_symbol)
        if tick is None:
            return False

        order_type = mt5.ORDER_TYPE_BUY if trade_params.get('type') == 'Buy' else mt5.ORDER_TYPE_SELL
        price = tick.ask if trade_params.get('type') == 'Buy' else tick.bid

        stop_loss = float(trade_params.get('stop_loss', 0))
        take_profit = float(trade_params.get('take_profit', 0))

        risk_percentage = float(trade_params.get('risk_percentage', 2.0))
        volume = optimize_lot_size(mt5_symbol, account_info.balance, risk_percentage, stop_loss, price)
        if volume <= 0:
            volume = float(trade_params.get('position_size', 0.001))

        if is_nano_account:
            volume = 0.001
        elif is_micro_account:
            volume = min(volume, 0.01)

        min_volume = symbol_info.volume_min
        volume_step = symbol_info.volume_step
        if volume < min_volume:
            volume = min_volume
        volume = math.floor(volume / volume_step) * volume_step
        if volume < min_volume:
            volume = min_volume

        entry_type = str(trade_params.get('entry_type', 'SMC'))[:8]
        entry_type = ''.join(c for c in entry_type if c.isalnum() or c in ' -_')
        comment = f"SMC-{entry_type}"[:31]

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": mt5_symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": stop_loss,
            "tp": take_profit,
            "deviation": 20,
            "magic": 234000,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK
        }

        result = mt5.order_send(request)
        if result is None:
            logging.error(f"Order failed: {mt5.last_error()}")
            return False
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"Order failed: {result.comment} (code: {result.retcode})")
            return False

        logging.info(f"Trade executed: {mt5_symbol} {'Buy' if order_type == mt5.ORDER_TYPE_BUY else 'Sell'} {volume} @ {price}")
        return True
    except Exception as e:
        logging.error(f"Error executing trade: {e}")
        return False


def get_mt5_error_description(error_code):
    mt5_error_map = {
        10004: 'No connection', 10006: 'Trade context busy', 10007: 'Trade timeout',
        10008: 'Invalid price', 10009: 'Invalid stops', 10010: 'Invalid volume',
        10011: 'Market closed', 10012: 'Trade disabled', 10013: 'Not enough money',
        10014: 'Price changed', 10015: 'Off quotes', 10016: 'Broker busy',
        10017: 'Requote', 10018: 'Order locked',
    }
    return mt5_error_map.get(error_code, f'Unknown: {error_code}')


# ============================================================
# Account Protection
# ============================================================

class AccountProtection:
    def __init__(self):
        self.is_active = True
        self.emergency_stop = False
        self.daily_loss_limit_reached = False
        self.nano_account_threshold = 10.0
        self.micro_account_threshold = 20.0
        self.nano_loss_limit_percent = 1.0
        self.micro_loss_limit_percent = 2.0
        self.small_loss_limit_percent = 5.0
        self.starting_balance = None
        self.lowest_balance = None
        self.highest_balance = None
        self.last_check_time = datetime.now()
        self.check_interval = 60
        self.daily_stats_reset_time = None
        self.sequential_losses = 0
        self.max_sequential_losses = 1
        self.nano_max_daily_trades = 2
        self.micro_max_daily_trades = 3
        self.recovery_mode = False
        self.allowed_symbols_micro = ['frxEURUSD', 'frxGBPUSD', 'frxUSDJPY', 'frxUSDCAD', 'frxAUDUSD', 'frxEURGBP']
        self.allowed_symbols_nano = ['frxEURUSD', 'frxGBPUSD']
        self.daily_profit = 0.0
        self.daily_losses = 0.0
        self.daily_trades = 0
        self.daily_win_count = 0
        self.daily_loss_count = 0
        self.force_nano_mode = True
        self.trade_history = []

    def initialize(self):
        try:
            account_info = mt5.account_info()
            if account_info is None:
                return False
            self.starting_balance = account_info.balance
            self.lowest_balance = account_info.balance
            self.highest_balance = account_info.balance
            self.daily_stats_reset_time = datetime.now().date()
            return True
        except Exception as e:
            logging.error(f"Error initializing account protection: {e}")
            return False

    def check_account_status(self):
        try:
            current_time = datetime.now()
            if (current_time - self.last_check_time).total_seconds() < self.check_interval:
                return True
            self.last_check_time = current_time

            if self.daily_stats_reset_time != current_time.date():
                self.reset_daily_stats()

            account_info = mt5.account_info()
            if account_info is None:
                return False

            current_balance = account_info.balance
            if current_balance < self.lowest_balance:
                self.lowest_balance = current_balance
            if current_balance > self.highest_balance:
                self.highest_balance = current_balance

            daily_change = current_balance - self.starting_balance
            daily_change_pct = (daily_change / self.starting_balance) * 100 if self.starting_balance else 0

            is_nano = self.force_nano_mode or current_balance < self.nano_account_threshold
            max_loss_pct = self.nano_loss_limit_percent if is_nano else self.micro_loss_limit_percent if current_balance < self.micro_account_threshold else self.small_loss_limit_percent

            if daily_change_pct < -max_loss_pct:
                self.emergency_stop = True
                logging.warning(f"Emergency stop: daily loss {daily_change_pct:.2f}% exceeds limit {max_loss_pct}%")
                return False

            if self.sequential_losses >= self.max_sequential_losses:
                self.emergency_stop = True
                logging.warning(f"Emergency stop: {self.sequential_losses} sequential losses")
                return False

            return not self.emergency_stop
        except Exception as e:
            logging.error(f"Error checking account protection: {e}")
            return False

    def reset_daily_stats(self):
        account_info = mt5.account_info()
        if account_info:
            self.starting_balance = account_info.balance
            self.highest_balance = account_info.balance
            self.lowest_balance = account_info.balance
        self.daily_stats_reset_time = datetime.now().date()
        self.daily_profit = 0.0
        self.daily_losses = 0.0
        self.daily_trades = 0
        self.daily_win_count = 0
        self.daily_loss_count = 0
        self.daily_loss_limit_reached = False
        self.emergency_stop = False

    def record_trade_result(self, profit):
        self.daily_trades += 1
        self.trade_history.append({
            'profit': profit,
            'time': datetime.now(),
            'balance_after': (mt5.account_info().balance if mt5.account_info() else 0)
        })
        if profit > 0:
            self.daily_profit += profit
            self.daily_win_count += 1
            self.sequential_losses = 0
            self.recovery_mode = False
        else:
            self.daily_losses += abs(profit)
            self.daily_loss_count += 1
            self.sequential_losses += 1
            self.recovery_mode = True
            if self.sequential_losses >= self.max_sequential_losses:
                self.emergency_stop = True
        self.check_account_status()

    def get_performance_stats(self) -> Dict:
        if not self.trade_history:
            return {'win_rate': 0, 'avg_rr': 0, 'expectancy': 0}
        wins = [t for t in self.trade_history if t['profit'] > 0]
        losses = [t for t in self.trade_history if t['profit'] <= 0]
        win_rate = len(wins) / len(self.trade_history) if self.trade_history else 0
        avg_win = sum(t['profit'] for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t['profit'] for t in losses) / len(losses)) if losses else 1
        avg_rr = avg_win / avg_loss if avg_loss > 0 else 0
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
        return {'win_rate': win_rate, 'avg_rr': avg_rr, 'expectancy': expectancy, 'total_trades': len(self.trade_history)}

    def can_open_trade(self, symbol, signal_strength=0):
        if not self.is_active:
            return True
        if self.emergency_stop or self.daily_loss_limit_reached:
            return False

        account_info = mt5.account_info()
        if account_info is None:
            return False

        if self.force_nano_mode:
            if self.daily_trades >= 1:
                return False
            if signal_strength < 0.45:
                return False
            return self.check_account_status()

        is_nano = account_info.balance < self.nano_account_threshold
        if is_nano:
            if symbol not in self.allowed_symbols_nano:
                return False
            if signal_strength < 0.9:
                return False
            if self.daily_trades >= self.nano_max_daily_trades:
                return False
        elif account_info.balance < self.micro_account_threshold:
            if signal_strength < 0.85:
                return False
            if self.daily_trades >= self.micro_max_daily_trades:
                return False

        return self.check_account_status()


# ============================================================
# Trade Manager — Enhanced with partial TP and structure-based trailing
# ============================================================

class TradeManager:
    def __init__(self):
        self.initialized = False
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 3
        self.max_positions = 1
        self.breakeven_pips = 4
        self.trailing_stop_pips = 7
        self.min_profit_to_trail = 1.0
        self.profit_lock_levels = [3, 5, 8, 13, 21]
        self.allow_new_trades = True
        self.profit_threshold = 0.5
        self.closed_position_groups = set()
        self.max_drawdown_percent = 3
        self.consecutive_losses = 0
        self.micro_account_mode = True
        self.max_daily_trades = 3
        self.daily_trades_count = 0
        self.last_day_reset = datetime.now().date()
        self.total_profit_threshold = 2.0
        self.trailing_stop_multiplier = 1.5
        self.last_trailing_update = 0
        self.trailing_update_interval = 1
        self.account_protection = AccountProtection()
        self.account_protection.initialize()

        if getattr(self.account_protection, 'force_nano_mode', False):
            self.max_daily_trades = 1

    def check_connection(self):
        try:
            if not self.initialized:
                if self.reconnect_attempts >= self.max_reconnect_attempts:
                    time.sleep(60)
                    self.reconnect_attempts = 0
                    return False
                if not initialize_mt5():
                    self.reconnect_attempts += 1
                    time.sleep(5)
                    return False
                self.initialized = True
                self.reconnect_attempts = 0
            if mt5.account_info() is None:
                self.initialized = False
                return False
            return True
        except Exception as e:
            logging.error(f"Connection check error: {e}")
            return False

    def can_open_new_trade(self):
        if not self.allow_new_trades:
            return False
        positions = mt5.positions_get()
        if positions is not None:
            active = len([p for p in positions if p.ticket not in self.closed_position_groups])
            if active >= self.max_positions:
                return False
        return True

    async def manage_trades(self):
        while True:
            try:
                if not self.check_connection():
                    await asyncio.sleep(5)
                    continue

                positions = mt5.positions_get()
                if positions is None:
                    await asyncio.sleep(1)
                    continue

                active = [p for p in positions if p.ticket not in self.closed_position_groups]
                total_profit = sum(pos.profit for pos in active)

                if total_profit >= self.total_profit_threshold and len(active) > 0:
                    await self.close_all_profitable_positions(active)
                    continue

                current_time = time.time()
                if current_time - self.last_trailing_update >= self.trailing_update_interval:
                    for pos in active:
                        if pos.profit >= self.min_profit_to_trail:
                            await self.update_trailing_stop(pos)
                    self.last_trailing_update = current_time

                for pos in active:
                    await self.manage_position(pos)
                    await asyncio.sleep(0.1)

                await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Error in trade manager: {e}")
                await asyncio.sleep(5)

    async def close_all_profitable_positions(self, positions):
        try:
            for pos in positions:
                if pos.profit <= 0:
                    continue
                tick = mt5.symbol_info_tick(pos.symbol)
                if tick is None:
                    continue
                price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
                request = {
                    "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
                    "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                    "position": pos.ticket, "price": price, "deviation": 20, "magic": 234000,
                    "comment": "ProfitTarget", "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_FOK
                }
                result = mt5.order_send(request)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.closed_position_groups.add(pos.ticket)
                    self.account_protection.record_trade_result(pos.profit)
                    stats = self.account_protection.get_performance_stats()
                    logging.info(f"Position closed | Profit: {pos.profit:.2f} | WR: {stats['win_rate']:.1%} | Expectancy: {stats['expectancy']:.4f}")
        except Exception as e:
            logging.error(f"Error closing positions: {e}")

    async def update_trailing_stop(self, position):
        try:
            symbol_info = mt5.symbol_info(position.symbol)
            tick = mt5.symbol_info_tick(position.symbol)
            if not symbol_info or not tick:
                return

            if position.type == mt5.ORDER_TYPE_BUY:
                current_price = tick.bid
                profit_pips = (current_price - position.price_open) / symbol_info.point
            else:
                current_price = tick.ask
                profit_pips = (position.price_open - current_price) / symbol_info.point

            if profit_pips <= 0:
                return

            rates = mt5.copy_rates_from_pos(position.symbol, mt5.TIMEFRAME_M5, 0, 100)
            if rates is not None:
                df_temp = pd.DataFrame(rates)
                atr = ta.volatility.AverageTrueRange(df_temp['high'], df_temp['low'], df_temp['close'], window=14).average_true_range().iloc[-1]

                trail_mult = self.trailing_stop_multiplier
                if profit_pips > 20:
                    trail_mult = 1.0
                elif profit_pips > 10:
                    trail_mult = 1.2

                trail = atr * trail_mult

                if position.type == mt5.ORDER_TYPE_BUY:
                    new_sl = current_price - trail
                    if position.sl == 0 or new_sl > position.sl:
                        await self.update_stop_loss(position, new_sl)
                else:
                    new_sl = current_price + trail
                    if position.sl == 0 or new_sl < position.sl:
                        await self.update_stop_loss(position, new_sl)
        except Exception as e:
            logging.error(f"Error updating trailing stop: {e}")

    async def update_stop_loss(self, position, new_sl):
        try:
            symbol_info = mt5.symbol_info(position.symbol)
            tick = mt5.symbol_info_tick(position.symbol)
            if not symbol_info or not tick:
                return
            new_sl = round(new_sl, symbol_info.digits)
            request = {
                "action": mt5.TRADE_ACTION_SLTP, "symbol": position.symbol,
                "sl": new_sl, "tp": position.tp, "position": position.ticket
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                logging.info(f"Updated SL for {position.ticket} to {new_sl}")
        except Exception as e:
            logging.error(f"Error updating SL: {e}")

    async def manage_position(self, position):
        try:
            symbol_info = mt5.symbol_info(position.symbol)
            tick = mt5.symbol_info_tick(position.symbol)
            if not symbol_info or not tick:
                return

            if position.type == mt5.ORDER_TYPE_BUY:
                current_price = tick.bid
                profit_pips = (current_price - position.price_open) / symbol_info.point
            else:
                current_price = tick.ask
                profit_pips = (position.price_open - current_price) / symbol_info.point

            if profit_pips >= self.breakeven_pips:
                if position.type == mt5.ORDER_TYPE_BUY:
                    be_sl = position.price_open + (0.5 * symbol_info.point)
                    if position.sl < be_sl:
                        await self.update_stop_loss(position, be_sl)
                else:
                    be_sl = position.price_open - (0.5 * symbol_info.point)
                    if position.sl == 0 or position.sl > be_sl:
                        await self.update_stop_loss(position, be_sl)

            account_info = mt5.account_info()
            if account_info and account_info.balance < 20:
                position_age = (datetime.now() - datetime.fromtimestamp(position.time)).total_seconds() / 60
                should_cut = False
                if profit_pips <= -5:
                    should_cut = True
                elif position_age > 5 and profit_pips <= -3:
                    should_cut = True
                elif position_age > 20 and profit_pips < 0:
                    should_cut = True

                if should_cut:
                    request = {
                        "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol, "volume": position.volume,
                        "type": mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                        "position": position.ticket, "price": current_price, "deviation": 20, "magic": 234000,
                        "comment": "QuickCut", "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_FOK
                    }
                    result = mt5.order_send(request)
                    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                        logging.warning(f"Quick cut position {position.ticket}: {profit_pips:.1f} pips")
                        self.account_protection.record_trade_result(position.profit)
                        self.closed_position_groups.add(position.ticket)
        except Exception as e:
            logging.error(f"Error managing position {position.ticket}: {e}")


# ============================================================
# Main Trading Loop — Enhanced
# ============================================================

async def main(symbol, interval):
    if not initialize_mt5():
        logging.error("Failed to initialize MT5")
        return

    try:
        if not is_in_killzone(symbol):
            logging.info(f"Outside killzone for {symbol} — skipping")
            return

        historical_data = fetch_mt5_historical_data(symbol, interval, num_bars=500)
        if not historical_data:
            logging.error("No historical data from MT5")
            return

        df = pd.DataFrame(historical_data)
        df = preprocess_data(df)

        if len(df) < 50:
            logging.error("Not enough data after preprocessing")
            return

        signal = generate_trade_signals(None, None, df, symbol)

        news = await fetch_market_news()
        filtered_news = filter_market_news(news, symbol)
        if filtered_news:
            print_news_summary(filtered_news)

        if signal['direction'] in ['Buy', 'Sell']:
            current_price = df['close'].iloc[-1]
            account_balance = mt5.account_info().balance
            atr = df['ATR'].iloc[-1]
            avg_atr = df['ATR'].rolling(50).mean().iloc[-1]

            stop_loss, take_profit = calculate_dynamic_sl_tp(
                current_price, signal['direction'], atr, signal)

            advice = advisory_decision(signal['direction'], current_price,
                                       take_profit, stop_loss, account_balance, filtered_news)

            if should_execute_trade(signal, filtered_news):
                trade_params = {
                    'symbol': symbol,
                    'type': signal['direction'],
                    'position_size': 0.01,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'entry_type': signal.get('entry_type', 'SMC'),
                    'setup_quality': signal.get('setup_quality', 70),
                    'risk_percentage': 1.0,
                    'market_name': AVAILABLE_MARKETS.get(symbol, symbol),
                }

                if execute_mt5_trade(trade_params):
                    rr = abs(take_profit - current_price) / abs(current_price - stop_loss) if abs(current_price - stop_loss) > 0 else 0
                    logging.info(
                        f"SMC Trade Executed: {signal['direction']} | "
                        f"Entry: {current_price:.5f} | SL: {stop_loss:.5f} | TP: {take_profit:.5f} | "
                        f"R:R: {rr:.1f} | Type: {signal.get('entry_type')} | "
                        f"Reasons: {', '.join(signal.get('reasons', signal.get('confirmations', [])))}"
                    )
                    send_trade_alert(signal,
                                     {'entry': current_price, 'stop_loss': stop_loss,
                                      'take_profit': take_profit, 'market_name': symbol},
                                     advice, mt5_trade=True)

        trade_manager = TradeManager()
        asyncio.create_task(trade_manager.manage_trades())

    except Exception as e:
        logging.error(f"Error in main: {e}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    try:
        if not initialize_mt5():
            logging.error("Failed to initialize MT5.")
            exit(1)

        account_protection = AccountProtection()
        if not account_protection.initialize():
            logging.error("Failed to initialize account protection")
            exit(1)

        selector = MarketSelector()
        selected_symbol = selector.selected_market
        if not selected_symbol:
            logging.error("No market selected.")
            exit(1)

        logging.info(f"Selected market: {selected_symbol}")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def trading_loop():
            trade_manager = TradeManager()
            last_status_check = 0
            cycle_count = 0

            while True:
                try:
                    current_time = time.time()
                    if current_time - last_status_check > 60:
                        if not account_protection.check_account_status():
                            logging.warning("Account protection activated — pausing 5 min")
                            await asyncio.sleep(300)
                            continue
                        last_status_check = current_time

                        if cycle_count % 10 == 0:
                            stats = account_protection.get_performance_stats()
                            logging.info(
                                f"Performance: WR={stats['win_rate']:.1%} | "
                                f"R:R={stats['avg_rr']:.2f} | Exp={stats['expectancy']:.4f} | "
                                f"Trades={stats['total_trades']}"
                            )

                    await main(selected_symbol, 'M5')
                    cycle_count += 1
                    await asyncio.sleep(2)
                except Exception as e:
                    logging.error(f"Error in trading loop: {e}")
                    await asyncio.sleep(5)

        try:
            loop.run_until_complete(trading_loop())
        except KeyboardInterrupt:
            logging.info("Bot stopped by user")
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            mt5.shutdown()

    except Exception as e:
        logging.error(f"Critical error: {e}")
