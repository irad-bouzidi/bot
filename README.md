# Nadaraya-Watson Envelope MT5 Trading Bot

This is a professional automated trading bot for MetaTrader 5 (MT5) that implements the Nadaraya-Watson Envelope strategy based on LuxAlgo's indicators.

## 📈 Strategy Overview

The bot uses a non-repainting Gaussian kernel estimator to determine a smoothed mean of the price. It then constructs an envelope around this mean using the Mean Absolute Error (MAE).

- **Long Entry**: Opens a BUY position when the price closes below the lower envelope band.
- **Short Entry**: Opens a SELL position when the price closes above the upper envelope band.
- **Exit**: Closes positions when the price returns to the smoothed mean (the center line).

## 🚀 Getting Started

### Prerequisites
- **MetaTrader 5 Terminal**: Must be installed and logged into your trading account.
- **Python 3.8+**: Ensure Python is installed on your system.
- **Trading Account**: An MT5 account that supports the symbols `BTCUSDm` or `XAUUSDm`.

### Installation
1. Clone or download this repository to your local machine.
2. Install the required Python libraries:
   ```bash
   pip install -r requirements.txt
   ```

## 🛠 Usage

The bot can be run for either Bitcoin (`BTCUSDm`) or Gold (`XAUUSDm`) using command-line arguments.

### Run for BTCUSDm (Default)
```bash
python bot.py
# OR
python bot.py BTCUSDm
```

### Run for XAUUSDm
```bash
python bot.py XAUUSDm
```

## ⚙️ Configuration
You can modify the following constants inside `bot.py` to tune the bot:
- `BANDWIDTH`: Controls the smoothness of the estimator (Higher = smoother).
- `MULT`: Multiplier for the envelope width (Higher = wider bands).
- `WINDOW_SIZE`: The number of bars used for the kernel calculation (default: 500).
- `LOT_SIZE`: The volume of the trade (default: 0.01).
- `TIMEFRAME`: Currently set to 5-minute (`TIMEFRAME_M5`).

## ⚠️ Disclaimer
Trading financial instruments involves significant risk of loss. This bot is provided for educational and research purposes. Always test strategies on a **Demo Account** before deploying capital to a live environment.

***
*Based on LuxAlgo - Nadaraya-Watson Envelope and Smoothers.*
