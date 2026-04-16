"""
Visualization tools for the trading robot
Generates charts and analysis reports
"""

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
import numpy as np


def plot_equity_curve(trades_df, initial_balance=10000, save_path='equity_curve.png'):
    """Plot equity curve over time"""
    
    if len(trades_df) == 0:
        print("No trades to plot")
        return
    
    # Calculate cumulative P&L
    trades_df = trades_df.copy()
    trades_df['cumulative_pnl'] = trades_df['pnl'].cumsum()
    trades_df['equity'] = initial_balance + trades_df['cumulative_pnl']
    
    # Create figure
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10))
    
    # Plot equity curve
    ax1.plot(trades_df['exit_time'], trades_df['equity'], 
             linewidth=2, color='#2E86AB', label='Equity')
    ax1.axhline(y=initial_balance, color='gray', linestyle='--', 
                alpha=0.5, label='Initial Balance')
    ax1.fill_between(trades_df['exit_time'], initial_balance, trades_df['equity'],
                     where=(trades_df['equity'] >= initial_balance), 
                     alpha=0.3, color='green', label='Profit')
    ax1.fill_between(trades_df['exit_time'], initial_balance, trades_df['equity'],
                     where=(trades_df['equity'] < initial_balance), 
                     alpha=0.3, color='red', label='Loss')
    
    ax1.set_title('Equity Curve', fontsize=16, fontweight='bold')
    ax1.set_ylabel('Account Balance ($)', fontsize=12)
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45)
    
    # Plot individual trade P&L
    colors = ['green' if pnl > 0 else 'red' for pnl in trades_df['pnl']]
    ax2.bar(range(len(trades_df)), trades_df['pnl'], color=colors, alpha=0.6)
    ax2.axhline(y=0, color='black', linestyle='-', linewidth=0.8)
    ax2.set_title('Individual Trade P&L', fontsize=16, fontweight='bold')
    ax2.set_xlabel('Trade Number', fontsize=12)
    ax2.set_ylabel('Profit/Loss ($)', fontsize=12)
    ax2.grid(True, alpha=0.3, axis='y')
    
    plt.tight_layout()
    plt.savefig(save_path, dpi=300, bbox_inches='tight')
    print(f"✅ Equity curve saved to {save_path}")
    
    return fig


def plot_drawdown(trades_df, initial_balance=10000, save_path='drawdown.png'):
    """Plot drawdown chart"""
    
    if len(trades_df) == 0:
        print("No trades to plot")
        return
    
    # Calculate equity and drawdown
    trades_df = trades_df.copy()
    trades_df['cumulative_pnl'] = trades_df['pnl'].cumsum()
    trades_df['equity'] = initial_balance + trades_df['cumulative_pnl']
    trades_df['peak'] = trades_df['equity'].cummax()
    trades_df['drawdown'] = (trades_df['equity'] - trades_df['peak']) / trades_df['peak'] * 100
    
    # Create figure
    fig, ax = plt.subplots(figsize=(14, 6))
    
    ax.fill_between(trades_df['exit_time'], 0, trades_df['drawdown'],
                    color='red', alpha=0.4)
    ax.plot(trades_df['exit_time'], trades_df['drawdown'], 
            color='darkred', linewidth=2)
    
    # Mark maximum drawdown
    max_dd_idx = trades_df['drawdown'].idxmin()
    max_dd_value = trades_df.loc[max_dd_idx, 'drawdown']
    max_dd_date = trades_df.loc[max_dd_idx, 'exit_time']
    
    ax.plot(max_dd_date, max_dd_value, 'o', color='black', markersize=10)
    ax.annotate(f'Max DD: {max_dd_value:.2f}%',
                xy=(max_dd_date, max_dd_value),
                xytext=(10, -20), textcoords='offset points',
                bbox=dict(boxstyle='round,pad=0.5', fc='yellow', alpha=0.7),
                arrowprops=dict(arrowstyle='->', connectionstyle='arc3,rad=0'))
    
    ax.set_title('Drawdown Over Time', fontsize=16, fontweight='bold')
    ax.set_xlabel('Date', fontsize=12)
    ax.set_ylabel('Drawdown (%)', fontsize=12)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45)
    
    plt.tight_layout()
    plt.savefig(save_path, dpi=300, bbox_inches='tight')
    print(f"✅ Drawdown chart saved to {save_path}")
    
    return fig


def plot_trade_distribution(trades_df, save_path='trade_distribution.png'):
    """Plot trade P&L distribution"""
    
    if len(trades_df) == 0:
        print("No trades to plot")
        return
    
    fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(14, 10))
    
    # 1. P&L Histogram
    ax1.hist(trades_df['pnl'], bins=20, color='steelblue', alpha=0.7, edgecolor='black')
    ax1.axvline(x=0, color='red', linestyle='--', linewidth=2)
    ax1.axvline(x=trades_df['pnl'].mean(), color='green', linestyle='--', 
                linewidth=2, label=f"Mean: ${trades_df['pnl'].mean():.2f}")
    ax1.set_title('Trade P&L Distribution', fontsize=14, fontweight='bold')
    ax1.set_xlabel('Profit/Loss ($)', fontsize=12)
    ax1.set_ylabel('Frequency', fontsize=12)
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    # 2. Win/Loss by Type
    long_trades = trades_df[trades_df['type'] == 'long']
    short_trades = trades_df[trades_df['type'] == 'short']
    
    long_wins = len(long_trades[long_trades['pnl'] > 0])
    long_losses = len(long_trades[long_trades['pnl'] <= 0])
    short_wins = len(short_trades[short_trades['pnl'] > 0])
    short_losses = len(short_trades[short_trades['pnl'] <= 0])
    
    x = np.arange(2)
    width = 0.35
    
    wins = [long_wins, short_wins]
    losses = [long_losses, short_losses]
    
    ax2.bar(x - width/2, wins, width, label='Wins', color='green', alpha=0.7)
    ax2.bar(x + width/2, losses, width, label='Losses', color='red', alpha=0.7)
    
    ax2.set_title('Win/Loss by Trade Type', fontsize=14, fontweight='bold')
    ax2.set_ylabel('Number of Trades', fontsize=12)
    ax2.set_xticks(x)
    ax2.set_xticklabels(['Long', 'Short'])
    ax2.legend()
    ax2.grid(True, alpha=0.3, axis='y')
    
    # 3. Trade Duration
    trades_df['duration'] = (trades_df['exit_time'] - trades_df['entry_time']).dt.total_seconds() / 60
    
    ax3.hist(trades_df['duration'], bins=20, color='purple', alpha=0.7, edgecolor='black')
    ax3.axvline(x=trades_df['duration'].mean(), color='red', linestyle='--', 
                linewidth=2, label=f"Mean: {trades_df['duration'].mean():.1f} min")
    ax3.set_title('Trade Duration Distribution', fontsize=14, fontweight='bold')
    ax3.set_xlabel('Duration (minutes)', fontsize=12)
    ax3.set_ylabel('Frequency', fontsize=12)
    ax3.legend()
    ax3.grid(True, alpha=0.3)
    
    # 4. Cumulative Win Rate
    trades_df['win'] = (trades_df['pnl'] > 0).astype(int)
    trades_df['cumulative_wins'] = trades_df['win'].cumsum()
    trades_df['trade_number'] = range(1, len(trades_df) + 1)
    trades_df['cumulative_win_rate'] = trades_df['cumulative_wins'] / trades_df['trade_number'] * 100
    
    ax4.plot(trades_df['trade_number'], trades_df['cumulative_win_rate'], 
             linewidth=2, color='darkgreen')
    ax4.axhline(y=50, color='gray', linestyle='--', alpha=0.5, label='50%')
    ax4.fill_between(trades_df['trade_number'], 50, trades_df['cumulative_win_rate'],
                     where=(trades_df['cumulative_win_rate'] >= 50), 
                     alpha=0.3, color='green')
    ax4.fill_between(trades_df['trade_number'], 50, trades_df['cumulative_win_rate'],
                     where=(trades_df['cumulative_win_rate'] < 50), 
                     alpha=0.3, color='red')
    
    ax4.set_title('Cumulative Win Rate', fontsize=14, fontweight='bold')
    ax4.set_xlabel('Trade Number', fontsize=12)
    ax4.set_ylabel('Win Rate (%)', fontsize=12)
    ax4.legend()
    ax4.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(save_path, dpi=300, bbox_inches='tight')
    print(f"✅ Trade distribution saved to {save_path}")
    
    return fig


def generate_performance_report(trades_df, initial_balance=10000, strategy_name="Gold Scalper"):
    """Generate a comprehensive performance report"""
    
    if len(trades_df) == 0:
        print("No trades to analyze")
        return
    
    # Calculate metrics
    trades_df['cumulative_pnl'] = trades_df['pnl'].cumsum()
    trades_df['equity'] = initial_balance + trades_df['cumulative_pnl']
    
    winning_trades = trades_df[trades_df['pnl'] > 0]
    losing_trades = trades_df[trades_df['pnl'] <= 0]
    
    total_trades = len(trades_df)
    wins = len(winning_trades)
    losses = len(losing_trades)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    
    gross_profit = winning_trades['pnl'].sum()
    gross_loss = abs(losing_trades['pnl'].sum())
    net_profit = trades_df['pnl'].sum()
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0
    
    avg_win = winning_trades['pnl'].mean() if wins > 0 else 0
    avg_loss = losing_trades['pnl'].mean() if losses > 0 else 0
    
    largest_win = trades_df['pnl'].max()
    largest_loss = trades_df['pnl'].min()
    
    # Drawdown
    trades_df['peak'] = trades_df['equity'].cummax()
    trades_df['drawdown'] = (trades_df['equity'] - trades_df['peak']) / trades_df['peak'] * 100
    max_drawdown = trades_df['drawdown'].min()
    
    # Returns
    total_return = ((trades_df['equity'].iloc[-1] / initial_balance) - 1) * 100
    
    # Trade duration
    trades_df['duration'] = (trades_df['exit_time'] - trades_df['entry_time']).dt.total_seconds() / 60
    avg_duration = trades_df['duration'].mean()
    
    # Long vs Short
    long_trades = trades_df[trades_df['type'] == 'long']
    short_trades = trades_df[trades_df['type'] == 'short']
    
    long_wins = len(long_trades[long_trades['pnl'] > 0])
    short_wins = len(short_trades[short_trades['pnl'] > 0])
    
    # Generate report
    report = f"""
{'='*80}
TRADING PERFORMANCE REPORT - {strategy_name}
{'='*80}

OVERVIEW
--------
Report Date:         {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Strategy:            {strategy_name}
Test Period:         {trades_df['entry_time'].min()} to {trades_df['exit_time'].max()}
Total Trading Days:  {(trades_df['exit_time'].max() - trades_df['entry_time'].min()).days}

ACCOUNT SUMMARY
---------------
Initial Balance:     ${initial_balance:,.2f}
Final Balance:       ${trades_df['equity'].iloc[-1]:,.2f}
Net Profit/Loss:     ${net_profit:,.2f}
Total Return:        {total_return:.2f}%

TRADE STATISTICS
----------------
Total Trades:        {total_trades}
Winning Trades:      {wins} ({win_rate:.2f}%)
Losing Trades:       {losses} ({100-win_rate:.2f}%)

Gross Profit:        ${gross_profit:,.2f}
Gross Loss:          ${gross_loss:,.2f}
Profit Factor:       {profit_factor:.2f}

Average Win:         ${avg_win:,.2f}
Average Loss:        ${avg_loss:,.2f}
Avg Win/Loss Ratio:  {abs(avg_win/avg_loss) if avg_loss != 0 else 0:.2f}

Largest Win:         ${largest_win:,.2f}
Largest Loss:        ${largest_loss:,.2f}

TRADE DURATION
--------------
Average Duration:    {avg_duration:.1f} minutes
Min Duration:        {trades_df['duration'].min():.1f} minutes
Max Duration:        {trades_df['duration'].max():.1f} minutes

TRADE TYPE ANALYSIS
-------------------
Long Trades:         {len(long_trades)} (Win Rate: {(long_wins/len(long_trades)*100) if len(long_trades) > 0 else 0:.2f}%)
Short Trades:        {len(short_trades)} (Win Rate: {(short_wins/len(short_trades)*100) if len(short_trades) > 0 else 0:.2f}%)

RISK METRICS
------------
Maximum Drawdown:    {max_drawdown:.2f}%
Average Trade:       ${trades_df['pnl'].mean():,.2f}
Std Deviation:       ${trades_df['pnl'].std():,.2f}

CONSECUTIVE TRADES
------------------
Max Consecutive Wins:   {max_consecutive_wins(trades_df)}
Max Consecutive Losses: {max_consecutive_losses(trades_df)}

EXPECTANCY
----------
Win Probability:     {win_rate/100:.4f}
Loss Probability:    {(100-win_rate)/100:.4f}
Expectancy per Trade: ${(win_rate/100 * avg_win) + ((100-win_rate)/100 * avg_loss):,.2f}

{'='*80}

⚠️  IMPORTANT NOTES:
- Past performance does not guarantee future results
- Always test on demo account before live trading
- Consider transaction costs, slippage, and spreads
- Results may vary significantly in live trading conditions

{'='*80}
    """
    
    return report


def max_consecutive_wins(trades_df):
    """Calculate maximum consecutive wins"""
    wins = (trades_df['pnl'] > 0).astype(int)
    return max((wins * (wins.groupby((wins != wins.shift()).cumsum()).cumcount() + 1)).max(), 0)


def max_consecutive_losses(trades_df):
    """Calculate maximum consecutive losses"""
    losses = (trades_df['pnl'] <= 0).astype(int)
    return max((losses * (losses.groupby((losses != losses.shift()).cumsum()).cumcount() + 1)).max(), 0)


def create_full_report(backtester, save_folder='reports'):
    """Create comprehensive visual report"""
    import os
    
    if not os.path.exists(save_folder):
        os.makedirs(save_folder)
    
    trades_df = backtester.get_trades_dataframe()
    
    if len(trades_df) == 0:
        print("No trades to generate report")
        return
    
    print("\n📊 Generating comprehensive report...\n")
    
    # Generate plots
    plot_equity_curve(trades_df, backtester.risk_manager.account_balance, 
                     f'{save_folder}/equity_curve.png')
    plot_drawdown(trades_df, backtester.risk_manager.account_balance,
                 f'{save_folder}/drawdown.png')
    plot_trade_distribution(trades_df, f'{save_folder}/trade_distribution.png')
    
    # Generate text report
    report = generate_performance_report(trades_df, backtester.risk_manager.account_balance)
    
    with open(f'{save_folder}/performance_report.txt', 'w') as f:
        f.write(report)
    
    print(report)
    print(f"\n✅ Full report saved to {save_folder}/")
    print(f"   - equity_curve.png")
    print(f"   - drawdown.png")
    print(f"   - trade_distribution.png")
    print(f"   - performance_report.txt")


# Example usage
if __name__ == "__main__":
    from gold_scalper import ScalpingStrategy, RiskManager, Backtester, generate_sample_data
    
    print("Generating visualization report...\n")
    
    # Run backtest
    df = generate_sample_data(days=30)
    strategy = ScalpingStrategy()
    risk_manager = RiskManager()
    backtester = Backtester(strategy, risk_manager)
    results = backtester.run(df)
    
    # Create full report
    create_full_report(backtester)