import asyncio
import logging
import sys
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
from typing import Dict, Any, Optional

from tradelocker_client import TradeLockerClient
from scraper import TelegramScraper
from risk import RiskManager

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("teletrader.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("TeleTrader.main")

class HealthCheckHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "healthy"}')

    def log_message(self, format, *args):
        # Override to suppress default HTTP server access logs
        pass

def run_health_check_server():
    try:
        port = int(os.environ.get("PORT", 8080))
        print(f"[HEALTH CHECK] Starting server on port {port}...", flush=True)
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        print(f"[HEALTH CHECK] Server successfully bound to port {port}. Running forever...", flush=True)
        server.serve_forever()
    except Exception as e:
        print(f"[HEALTH CHECK CRITICAL ERROR] Failed to start web server: {e}", flush=True)

class TeleTraderBot:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        self.config: Dict[str, Any] = {}
        self.tl_client: Optional[TradeLockerClient] = None
        self.risk_manager: Optional[RiskManager] = None
        self.scraper: Optional[TelegramScraper] = None

    def load_config(self):
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
        with open(self.config_path, "r") as f:
            self.config = json.load(f)
        logger.info("Configuration file loaded successfully.")

    async def handle_parsed_signal(self, signal: Dict[str, Any]):
        """
        Callback triggered when a valid Gold signal is parsed from Telegram.
        """
        logger.info(f"Processing new parsed signal: {signal}")
        
        action = signal.get("action")
        entry_price = signal.get("entry_price")
        stop_loss = signal.get("stop_loss")
        take_profits = signal.get("take_profits", [])
        
        if not action or entry_price is None or stop_loss is None:
            logger.error(f"Signal is missing required fields. Action: {action}, Entry: {entry_price}, SL: {stop_loss}")
            return
            
        # Calculate dynamic lot sizing
        try:
            quantity = self.tl_client.calculate_lot_size(entry_price, stop_loss)
        except Exception as e:
            logger.error(f"Error calculating lot size: {e}. Defaulting to minimum 0.01.")
            quantity = 0.01
            
        # Determine last TP to set as broker target (safety net)
        take_profit_target = take_profits[-1] if take_profits else None
        
        action_lower = action.lower()
        
        # 1. Market Orders (Buy, Sell)
        if action_lower in ["buy", "sell"]:
            side = "buy" if action_lower == "buy" else "sell"
            order_id = self.tl_client.execute_market_order(
                side=side,
                quantity=quantity,
                stop_loss=stop_loss,
                take_profit=take_profit_target
            )
            
            if order_id:
                logger.info(f"Market order placed successfully. Order ID: {order_id}. Waiting for fill match...")
                # Wait briefly for execution engine to process
                await asyncio.sleep(2.0)
                
                try:
                    position_id = self.tl_client.client.get_position_id_from_order_id(order_id)
                    if position_id is not None:
                        self.risk_manager.track_trade(
                            position_id=position_id,
                            order_id=order_id,
                            entry_price=entry_price,
                            side=side,
                            total_qty=quantity,
                            tp_levels=take_profits,
                            sl_level=stop_loss
                        )
                    else:
                        logger.warning(f"Could not immediately find position for Order ID {order_id}. Adding to pending checks.")
                        # Add to pending orders so the RiskManager polling loop matches it when filled
                        self.risk_manager.track_pending_order(
                            order_id=order_id,
                            entry_price=entry_price,
                            side=side,
                            total_qty=quantity,
                            tp_levels=take_profits,
                            sl_level=stop_loss
                        )
                except Exception as e:
                    logger.error(f"Error querying position for order {order_id}: {e}")
            else:
                logger.error("Market order placement failed.")
                
        # 2. Pending Orders (Limit or Stop)
        elif action_lower in ["buy limit", "sell limit", "buy stop", "sell stop"]:
            parts = action_lower.split()
            side = parts[0]  # "buy" or "sell"
            type_ = parts[1] # "limit" or "stop"
            
            order_id = self.tl_client.execute_pending_order(
                type_=type_,
                side=side,
                quantity=quantity,
                price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit_target
            )
            
            if order_id:
                # Add to pending orders queue in RiskManager
                self.risk_manager.track_pending_order(
                    order_id=order_id,
                    entry_price=entry_price,
                    side=side,
                    total_qty=quantity,
                    tp_levels=take_profits,
                    sl_level=stop_loss
                )
            else:
                logger.error("Pending order placement failed.")
        else:
            logger.error(f"Unsupported action: {action}")

    async def run(self):
        # 0. Start health check server for Render compatibility
        threading.Thread(target=run_health_check_server, daemon=True).start()
        
        # 1. Load config
        self.load_config()
        
        # 2. Initialize TradeLocker Client
        self.tl_client = TradeLockerClient(self.config)
        self.tl_client.connect()
        
        # 3. Initialize Risk Manager and start task loop
        self.risk_manager = RiskManager(self.config, self.tl_client)
        asyncio.create_task(self.risk_manager.run_loop())
        
        # 4. Initialize Telegram Scraper and run
        self.scraper = TelegramScraper(self.config, self.handle_parsed_signal)
        await self.scraper.start()

def main():
    bot = TeleTraderBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Bot execution stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Bot terminated due to unhandled error: {e}", exc_info=True)

if __name__ == "__main__":
    main()
