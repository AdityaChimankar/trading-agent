from research.walk_forward_optimizer import run_walk_forward
for sym in ['RELIANCE', 'TCS', 'INFY']:
    result = run_walk_forward(sym, verbose=True)
    if result:
        print(f'{sym}: optimized={result["optimized"]}, default={result["default"]}')