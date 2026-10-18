with open('src/trading_agent/core/quant_indicators.py', 'r') as f:
    content = f.read()

compute_di_func = '''
def compute_di(df: pd.DataFrame, period: int = 14) -> tuple:
    """+DI/-DI (directional indicators) - ADX measures trend STRENGTH
    only, not direction; +DI vs -DI is what gives the direction. Kept
    separate from compute_adx() rather than changing its return type, since
    existing callers (backtest.py, decision_agent.py) depend on
    compute_adx() returning a single Series."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
    atr = compute_atr(df, period).replace(0, 1e-9)
    plus_di = 100 * plus_dm.rolling(period).mean() / atr
    minus_di = 100 * minus_dm.rolling(period).mean() / atr
    return plus_di, minus_di


'''

# Insert before the if __name__ block
new_content = content.replace('if __name__ == "__main__":', compute_di_func + 'if __name__ == "__main__":')

with open('src/trading_agent/core/quant_indicators.py', 'w') as f:
    f.write(new_content)
print('Done')