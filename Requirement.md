i would like to create a delta exchange algo application . This application implmenmted option trading in delat exchange. Symbol is BTC/ETH and strike price are available in delta exchange.

D:\Workspace\delta-exchange-alog This project already trades in delta exchange futures. so use this application as refernce to understand the application structure and API integration.
Follow same principles followed in the project like discord channel and configuration 


My options strategy is simple. it is short straddle . Every day evening at 5pm (17:00), sell ATM Call and Put of BTC. and number of the Lot is 250 with leverage of 200 and exit the position at 5.25pm (17:25) on same day. Also COMBINED STOP-LOSS(Exit the whole strategy when an overall loss is hit. Pick dollars (Max loss / MTM) or percent of the entry premium collected.) with Stop-loss type = % of entry premium with value of 50% 


All entry should be take in Market price

Combined stop-loss: Total premium SL % (a percent of the premium you collected) or Overall MTM SL (a dollar drawdown on open P&L). MTM means mark to market, your live open P&L.

Max loss = an absolute dollar drawdown. Percent of premium = loss as a percent of the premium collected at entry.


