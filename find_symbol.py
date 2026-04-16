#!/usr/bin/env python3
"""
Symbol Finder Tool
==================
Lists all available symbols in MetaTrader 5 to help you find 
the correct Gold/XAUUSD symbol for your broker.

Usage:
    python find_symbol.py           # List all gold-related symbols
    python find_symbol.py --all     # List ALL available symbols
"""

import sys

try:
    import MetaTrader5 as mt5
except ImportError:
    print("MetaTrader5 not installed. Run: pip install MetaTrader5")
    sys.exit(1)


def main():
    # Check for --all flag
    show_all = '--all' in sys.argv
    
    # Initialize MT5
    if not mt5.initialize():
        print("ERROR: Could not connect to MetaTrader 5")
        print("Make sure MT5 is running and logged into your account.")
        return 1
    
    # Get account info
    account = mt5.account_info()
    if account:
        print(f"\nConnected to: {account.server}")
        print(f"Account: {account.login}")
        print(f"Balance: ${account.balance:,.2f}")
    
    # Get all symbols
    symbols = mt5.symbols_get()
    
    if not symbols:
        print("\nNo symbols found!")
        mt5.shutdown()
        return 1
    
    print(f"\nTotal symbols available: {len(symbols)}")
    
    if show_all:
        print("\nAll available symbols:")
        print("-" * 40)
        for sym in sorted(symbols, key=lambda x: x.name):
            print(f"  {sym.name}")
    else:
        # Find gold-related symbols
        gold_keywords = ['XAU', 'GOLD', 'GLD']
        gold_symbols = []
        
        for sym in symbols:
            name_upper = sym.name.upper()
            if any(keyword in name_upper for keyword in gold_keywords):
                gold_symbols.append(sym)
        
        if gold_symbols:
            print("\nGold-related symbols found:")
            print("-" * 60)
            print(f"{'Symbol':<20} {'Spread':>10} {'Min Lot':>10} {'Max Lot':>10}")
            print("-" * 60)
            
            for sym in sorted(gold_symbols, key=lambda x: x.name):
                print(f"{sym.name:<20} {sym.spread:>10} {sym.volume_min:>10} {sym.volume_max:>10}")
            
            print("-" * 60)
            print(f"\nRecommended symbol: {gold_symbols[0].name}")
            print(f"\nUpdate TRADING_CONFIG['symbol'] in config.py to: '{gold_symbols[0].name}'")
        else:
            print("\nNo Gold/XAU symbols found!")
            print("\nTry running with --all flag to see all symbols:")
            print("  python find_symbol.py --all")
    
    mt5.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
