Nora

Portfolio P&L Calendar & Backtesting Dashboard

Nora is a backtesting tool for robinhood designed to answer a simple question:

“If I held this portfolio, how would it have performed over time?”

I originally wanted to build a live P&L calendar around Robinhood. I explored multiple approaches, including Robinhood MCP, OAuth, SDK-based integrations, and other connection methods, but none provided a reliable way to access the portfolio data without either creating compliance/ToS concerns or risking inaccurate results.

Instead of forcing an unreliable Robinhood integration, I built Nora around Alpaca’s paper-trading environment.

How Nora Works

Nora takes a portfolio and essentially recreates it inside an Alpaca paper account, allowing the portfolio to be tracked without using real money.

The dashboard then uses the portfolio’s historical performance to generate a calendar-style P&L view.

Your Portfolio
      ↓
Portfolio Holdings
      ↓
Alpaca Paper Trading
      ↓
Portfolio Simulation
      ↓
Historical Performance
      ↓
Nora P&L Calendar

This makes Nora less of a traditional live portfolio tracker and more of a portfolio backtesting and performance visualization tool.

What It Is

* Portfolio P&L calendar
* Historical performance tool
* Stock portfolio backtesting tool
* Crypto portfolio backtesting tool
* Alpaca paper/live trading 
* Daily profit/loss visualization
* Portfolio performance dashboard
* Alternative to manually calculating historical P&L

What It Isn’t

Nora is based in alpaca you can also use live alapaca trading if you do choose so but i made this enviorment to test robinhood portfolios through alpaca.

The results are based on owning the same portfolio as you would in robinhood.

It is better described as:

“A way to view a portfolio without buying it with cash first to visualize how it would have performed over time.”

The goal was to find a reliable way to recreate the portfolio and calculate performance without depending on an unofficial or unreliable Robinhood connection.

After experimenting with:

* Robinhood MCP
* OAuth
* SDK approaches
* Other connection/integration methods

Alpaca’s paper-trading environment ended up being the most practical alternative.

Because the portfolio can be recreated using paper trading, Nora can observe the simulated account and build the P&L calendar around that data.

Example Use Case

Imagine a portfolio containing:

VOO
AAPL
NVDA
SCHD
PLTR
BTC
ETH

Nora can recreate the portfolio in a simulated environment and use the resulting performance data to build a calendar such as:

        AUGUST 2026
 MON    TUE    WED    THU    FRI
 ─────────────────────────────────
 +$42   -$18   +$76   +$31   -$12
 +0.4%  -0.2%  +0.7%  +0.3%  -0.1%
        Monthly P&L
          +$119

The goal is to make portfolio performance easy to understand at a glance, rather than forcing users to analyze a long list of transactions or account statements.

