i would like to create a delta exchange algo application . This application implmenmted option trading in delat exchange. Symbol is BTC/ETH and strike price are available in delta exchange.

D:\Workspace\delta-exchange-alog This project already trades in delta exchange futures. so use this application as refernce to understand the application structure and API integration.
Follow same principles followed in the project like discord channel and configuration 


My options strategy is simple. it is short straddle . Every day evening at 5pm (17:00), sell ATM Call and Put of BTC. and number of the Lot is 250 with leverage of 200 and exit the position at 5.25pm (17:25) on same day. Also COMBINED STOP-LOSS(Exit the whole strategy when an overall loss is hit. Pick dollars (Max loss / MTM) or percent of the entry premium collected.) with Stop-loss type = % of entry premium with value of 50% 


All entry should be take in Market price

Combined stop-loss: Total premium SL % (a percent of the premium you collected) or Overall MTM SL (a dollar drawdown on open P&L). MTM means mark to market, your live open P&L.

Max loss = an absolute dollar drawdown. Percent of premium = loss as a percent of the premium collected at entry.




2. Structural Improvements (Live Bot Optimization)
Beyond adjusting parameters, we can implement these three structural improvements:

A. Hold to Expiry (Auto-Settlement)
Currently: We exit the straddle at 17:25 IST (5 minutes before option expiry). This requires placing two market orders (Taker fees + slippage).
Improvement: Hold the straddle until 17:30 IST expiry (auto-settlement):
For legs that expire Out-of-the-Money (worthless), we pay $0.00 in trading fees and $0.00 in slippage upon settlement.
For legs that expire In-the-Money, they are auto-settled exactly at the index price (no slippage).
Since short straddles decay to zero most of the time, this saves ~50% of your exit fees and 100% of your exit slippage.
B. Limit Orders (Maker Rebates)
Currently: The bot uses market orders, paying the standard 0.03% Taker fee.
Improvement: Change the live execution to use limit orders at the bid/ask:
Delta Exchange Maker fees are lower (0.02% or less), which would reduce the total fee drag over hundreds of trades.
C. pre-17:00 IST Trend/Momentum Filter
Short straddles perform poorly on days with large, directional price movements.
Improvement: Avoid entering trades if BTC exhibits strong momentum right before 17:00 IST (e.g., if BTC has moved more than 1% in the 2 hours preceding entry).