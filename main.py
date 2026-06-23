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

# Force stdout to be line-buffered so logs appear instantly on Render/Docker
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

# Configure Logging (using force=True overrides any loggers initialized by imported libraries)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("teletrader.log", encoding="utf-8")
    ],
    force=True
)
logger = logging.getLogger("TeleTrader.main")

class HealthCheckHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "healthy"}')

    def log_message(self, format, *args):
        logger.info(f"[HEALTH CHECK] Ping received: {format % args}")

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
        entry_price_high = signal.get("entry_price_high")
        stop_loss = signal.get("stop_loss")
        take_profits = signal.get("take_profits", [])
        channel_id = signal.get("channel_id")
        
        # Resolve static Stop Loss points if configured for this channel
        channel_settings = self.config.get("telegram", {}).get("channel_settings", {})
        chan_conf = {}
        if channel_id is not None:
            chan_conf = channel_settings.get(str(channel_id), {}) or channel_settings.get(channel_id, {})
            
        static_sl_points = chan_conf.get("static_sl_points")
        if static_sl_points is not None and entry_price is not None:
            action_lower = action.lower() if action else ""
            if "buy" in action_lower:
                stop_loss = entry_price - float(static_sl_points)
            elif "sell" in action_lower:
                stop_loss = entry_price + float(static_sl_points)
            logger.info(f"Applying channel-specific static SL for channel {channel_id}: {stop_loss} ({static_sl_points} points)")
            
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
        
        # --- Opposite Position & Order Auto-Close (Contradiction Mitigation) ---
        if action_lower in ["buy", "sell", "buy limit", "sell limit", "buy stop", "sell stop"]:
            side = "buy" if "buy" in action_lower else "sell"
            opposite_side = "sell" if side == "buy" else "buy"
            
            # 1. Close active opposite positions
            active_pos_ids = list(self.risk_manager.tracked_trades.keys())
            for pos_key in active_pos_ids:
                trade = self.risk_manager.tracked_trades[pos_key]
                if trade["side"] == opposite_side:
                    pos_id = int(pos_key)
                    logger.info(f"Signal contradiction detected! Fully closing opposite {opposite_side.upper()} position {pos_id} before entering {action.upper()}.")
                    self.tl_client.close_position_fully(pos_id)
                    self.risk_manager.tracked_trades.pop(pos_key)
                    
            # 2. Cancel pending opposite orders
            pending_order_keys = list(self.risk_manager.pending_orders.keys())
            for order_key in pending_order_keys:
                pending_order = self.risk_manager.pending_orders[order_key]
                if pending_order["side"] == opposite_side:
                    order_id = int(order_key)
                    logger.info(f"Signal contradiction detected! Cancelling opposite pending {opposite_side.upper()} order {order_id}.")
                    try:
                        self.tl_client.client.delete_order(order_id)
                    except Exception as e:
                        logger.error(f"Failed to delete pending order {order_id}: {e}")
                    self.risk_manager.pending_orders.pop(order_key)
                    
            self.risk_manager.save_state()
        
        # 1. Market Orders (Buy, Sell)
        if action_lower in ["buy", "sell"]:
            side = "buy" if action_lower == "buy" else "sell"
            
            # Slippage / bad entry check
            try:
                quotes = self.tl_client.get_quotes()
                current_price = quotes["ask"] if side == "buy" else quotes["bid"]
            except Exception as e:
                logger.error(f"Failed to fetch current quotes for slippage check: {e}. Proceeding with market order.")
                current_price = None
                
            entry_boundary = entry_price_high if entry_price_high is not None else entry_price
            slippage_tolerance = float(self.config.get("risk", {}).get("slippage_tolerance", 1.0))
            
            should_place_market = True
            if current_price is not None:
                if side == "buy" and current_price > entry_boundary + slippage_tolerance:
                    should_place_market = False
                    logger.warning(f"Slippage limit exceeded: Current Ask ({current_price}) is too far above entry high ({entry_boundary}). Falling back to a limit order.")
                elif side == "sell" and current_price < entry_boundary - slippage_tolerance:
                    should_place_market = False
                    logger.warning(f"Slippage limit exceeded: Current Bid ({current_price}) is too far below entry low ({entry_boundary}). Falling back to a limit order.")

            if should_place_market:
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
            else:
                # Fallback: Place a pending limit order at the entry boundary price
                logger.info(f"Placing pending LIMIT order at {entry_boundary} instead of market order to prevent bad entry.")
                order_id = self.tl_client.execute_pending_order(
                    type_="limit",
                    side=side,
                    quantity=quantity,
                    price=entry_boundary,
                    stop_loss=stop_loss,
                    take_profit=take_profit_target
                )
                if order_id:
                    self.risk_manager.track_pending_order(
                        order_id=order_id,
                        entry_price=entry_boundary,
                        side=side,
                        total_qty=quantity,
                        tp_levels=take_profits,
                        sl_level=stop_loss
                    )
                
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
