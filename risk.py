import asyncio
import logging
import json
import os
from typing import Dict, Any, List, Set, Optional
from tradelocker.exceptions import TLAPIException
from tradelocker.types import ModificationParamsType
from tradelocker.utils import get_nested_key

logger = logging.getLogger("TeleTrader.risk")

class RiskManager:
    def __init__(self, config: Dict[str, Any], tl_client):
        """
        Initializes the Cascading Risk Manager.
        """
        self.config = config
        self.tl_client = tl_client
        self.risk_config = config.get("risk", {})
        
        self.polling_interval = float(self.risk_config.get("polling_interval", 2.0))
        self.breakeven_buffer = float(self.risk_config.get("breakeven_buffer", 0.2))
        self.partial_close_pcts = self.risk_config.get("partial_close_percentages", [0.25, 0.25, 0.25, 0.25])
        
        self.state_file = "active_trades.json"
        self.tracked_trades: Dict[str, Dict[str, Any]] = {}
        self.pending_orders: Dict[str, Dict[str, Any]] = {}
        
        # Load any existing state from file
        self.load_state()

    def load_state(self):
        """
        Loads tracked trades and pending orders from the local JSON state file.
        """
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    # Support legacy files that only contain tracked_trades
                    if "tracked_trades" in data or "pending_orders" in data:
                        self.tracked_trades = data.get("tracked_trades", {})
                        self.pending_orders = data.get("pending_orders", {})
                    else:
                        self.tracked_trades = data
                        self.pending_orders = {}
                logger.info(f"Loaded {len(self.tracked_trades)} trades and {len(self.pending_orders)} pending orders from {self.state_file}.")
            except Exception as e:
                logger.error(f"Failed to load state file {self.state_file}: {e}")
                self.tracked_trades = {}
                self.pending_orders = {}
        else:
            self.tracked_trades = {}
            self.pending_orders = {}

    def save_state(self):
        """
        Saves tracked trades and pending orders to the local JSON state file.
        """
        try:
            data = {
                "tracked_trades": self.tracked_trades,
                "pending_orders": self.pending_orders
            }
            with open(self.state_file, "w") as f:
                json.dump(data, f, indent=4)
            logger.debug(f"Saved state to {self.state_file}.")
        except Exception as e:
            logger.error(f"Failed to save state to {self.state_file}: {e}")

    def track_pending_order(self, order_id: int, entry_price: float, side: str, total_qty: float, tp_levels: List[float], sl_level: float):
        """
        Registers a pending order (limit/stop) for matching/fill tracking.
        """
        order_key = str(order_id)
        self.pending_orders[order_key] = {
            "order_id": order_id,
            "entry_price": entry_price,
            "side": side.lower(),
            "total_qty": total_qty,
            "tp_levels": sorted(tp_levels),
            "sl_level": sl_level
        }
        self.save_state()
        logger.info(f"Added pending order to tracking: OrderID {order_id} | Side: {side} | Qty: {total_qty} | Entry: {entry_price} | TPs: {tp_levels} | SL: {sl_level}")

    def track_trade(self, position_id: int, order_id: Optional[int], entry_price: float, side: str, total_qty: float, tp_levels: List[float], sl_level: float):
        """
        Adds a new active trade to position-level tracking.
        """
        pos_key = str(position_id)
        self.tracked_trades[pos_key] = {
            "position_id": position_id,
            "order_id": order_id,
            "entry_price": entry_price,
            "side": side.lower(),
            "total_qty": total_qty,
            "current_qty": total_qty,
            "tp_levels": sorted(tp_levels),
            "sl_level": sl_level,
            "highest_tp_hit": -1,
            "partially_closed_tps": []
        }
        self.save_state()
        logger.info(f"Added active trade to tracking: PosID {position_id} | Side: {side} | Qty: {total_qty} | Entry: {entry_price} | TPs: {tp_levels} | SL: {sl_level}")

    async def run_loop(self):
        """
        The continuous polling loop that tracks active positions, matches pending orders, and handles risk.
        """
        logger.info(f"Risk Manager polling loop started. Interval: {self.polling_interval} seconds.")
        while True:
            try:
                # 1. Match pending orders
                if self.pending_orders:
                    await self.check_pending_orders()
                
                # 2. Monitor active positions
                if self.tracked_trades:
                    await self.monitor_trades()
            except Exception as e:
                logger.error(f"Error in Risk Manager monitoring loop: {e}", exc_info=True)
            
            await asyncio.sleep(self.polling_interval)

    async def check_pending_orders(self):
        """
        Checks if any pending orders have been filled and promotes them to active positions,
        or cancels them if market price hits TP targets before filling.
        """
        quotes = None
        try:
            quotes = self.tl_client.get_quotes()
        except Exception as e:
            logger.error(f"Failed to fetch quotes for pending order check: {e}")
            
        pending_keys = list(self.pending_orders.keys())
        for order_key in pending_keys:
            order_id = int(order_key)
            trade_info = self.pending_orders[order_key]
            
            # 1. Check if the order was filled first
            try:
                position_id = self.tl_client.get_position_id_from_order_id(order_id)
                if position_id is not None:
                    logger.info(f"Pending Order {order_id} has been filled! Promoting to Active Position {position_id}.")
                    # Start tracking position
                    self.track_trade(
                        position_id=position_id,
                        order_id=order_id,
                        entry_price=trade_info["entry_price"],
                        side=trade_info["side"],
                        total_qty=trade_info["total_qty"],
                        tp_levels=trade_info["tp_levels"],
                        sl_level=trade_info["sl_level"]
                    )
                    # Remove from pending list
                    self.pending_orders.pop(order_key)
                    self.save_state()
                    continue
            except Exception as e:
                logger.error(f"Error checking pending order fill status for order {order_id}: {e}")
                
            # 2. If not filled, check if any TP target has already been reached
            if quotes and trade_info.get("tp_levels"):
                tp_levels = trade_info["tp_levels"]
                side = trade_info["side"]
                current_price = quotes["bid"] if side == "buy" else quotes["ask"]
                
                # Check against first TP target (TP1)
                tp1 = tp_levels[0]
                tp_hit = False
                if side == "buy" and current_price >= tp1:
                    tp_hit = True
                elif side == "sell" and current_price <= tp1:
                    tp_hit = True
                    
                if tp_hit:
                    logger.info(f"Pending Order {order_id} cancelled: Market price ({current_price}) reached TP target ({tp1}) before order was filled.")
                    try:
                        self.tl_client.client.delete_order(order_id)
                    except Exception as e:
                        logger.error(f"Failed to delete pending order {order_id} on broker: {e}")
                    self.pending_orders.pop(order_key)
                    self.save_state()

    async def monitor_trades(self):
        """
        Polls positions and quotes, applying cascading risk logic.
        """
        # 1. Fetch active positions from TradeLocker
        try:
            api_positions = self.tl_client.get_active_positions()
        except Exception as e:
            logger.error(f"Failed to fetch active positions: {e}")
            return
            
        api_pos_map = {pos["id"]: pos for pos in api_positions}
        
        # 2. Get current Gold quotes (bid and ask)
        try:
            quotes = self.tl_client.get_quotes()
            bid = quotes["bid"]
            ask = quotes["ask"]
        except Exception as e:
            logger.error(f"Failed to fetch live quotes: {e}")
            return

        # 3. Clean up closed trades (untracked from local state if closed on broker)
        local_keys = list(self.tracked_trades.keys())
        for pos_key in local_keys:
            pos_id = int(pos_key)
            if pos_id not in api_pos_map:
                logger.info(f"Position {pos_id} is no longer open in TradeLocker. Removing from tracking.")
                self.tracked_trades.pop(pos_key)
                self.save_state()

        # 4. Process each tracked trade
        for pos_key, trade in list(self.tracked_trades.items()):
            pos_id = int(pos_key)
            api_pos = api_pos_map.get(pos_id)
            if not api_pos:
                continue
                
            side = trade["side"]
            entry_price = trade["entry_price"]
            tp_levels = trade["tp_levels"]
            highest_tp_hit = trade["highest_tp_hit"]
            
            # Synchronize current remaining qty and actual average price from API
            trade["current_qty"] = api_pos["qty"]
            trade["avg_price"] = api_pos["avg_price"]
            
            # The exit price depends on trade direction:
            # - For a BUY position, we sell to close -> exit price is Bid
            # - For a SELL position, we buy to close -> exit price is Ask
            exit_price = bid if side == "buy" else ask
            
            # Check each TP level
            for tp_idx, tp_price in enumerate(tp_levels):
                tp_hit = False
                if side == "buy" and exit_price >= tp_price:
                    tp_hit = True
                elif side == "sell" and exit_price <= tp_price:
                    tp_hit = True
                    
                if tp_hit and tp_idx > highest_tp_hit:
                    logger.info(f"Target TP{tp_idx+1} ({tp_price}) hit for position {pos_id}! (Current Price: {exit_price})")
                    trade["highest_tp_hit"] = tp_idx
                    self.save_state()
                    
                    # Apply cascading risk rule for the newly hit TP
                    await self.apply_risk_rule(pos_id, trade, tp_idx)

    async def apply_risk_rule(self, pos_id: int, trade: Dict[str, Any], tp_idx: int):
        """
        Executes partial close and stop loss modification according to cascading risk logic.
        """
        side = trade["side"]
        entry_price = trade.get("avg_price", trade["entry_price"])
        tp_levels = trade["tp_levels"]
        total_qty = trade["total_qty"]
        current_qty = trade["current_qty"]
        
        # 1. Execute Partial Close (if configured)
        if tp_idx not in trade["partially_closed_tps"]:
            pct = 0.0
            if tp_idx < len(self.partial_close_pcts):
                pct = self.partial_close_pcts[tp_idx]
                
            if pct > 0.0:
                quantity_to_close = round(total_qty * pct, 2)
                if quantity_to_close >= 0.01:
                    if quantity_to_close > current_qty:
                        quantity_to_close = current_qty
                        
                    logger.info(f"TP{tp_idx+1} Hit: Initiating partial close of {quantity_to_close} lots for position {pos_id}.")
                    success = self.tl_client.close_position_partial(pos_id, quantity_to_close)
                    if success:
                        trade["partially_closed_tps"].append(tp_idx)
                        trade["current_qty"] = max(0.0, current_qty - quantity_to_close)
                        self.save_state()
                    else:
                        logger.error(f"Partial close of {quantity_to_close} lots failed for position {pos_id}.")
                else:
                    logger.debug(f"Calculated partial close quantity {quantity_to_close} is below minimum lot size (0.01). Skipping partial close.")
            else:
                logger.debug(f"No partial close configured for TP{tp_idx+1}. Skipping.")

        # 2. Modify Stop Loss based on Cascading Risk Rules
        new_sl = None
        rule_desc = ""
        
        if tp_idx == 0:
            logger.info(f"TP1 Hit: Keeping original SL of {trade['sl_level']} active.")
            return
            
        elif tp_idx == 1:
            if side == "buy":
                new_sl = entry_price + self.breakeven_buffer
            else:
                new_sl = entry_price - self.breakeven_buffer
            rule_desc = "Breakeven"
            
        elif tp_idx == 2:
            new_sl = tp_levels[0]
            rule_desc = "TP1 Lock"
            
        elif tp_idx >= 3:
            new_sl = tp_levels[tp_idx - 2]
            rule_desc = f"TP{tp_idx-1} Lock"

        if new_sl is not None:
            logger.info(f"TP{tp_idx+1} Hit: Cascading SL modification triggered ({rule_desc}). Modifying SL of position {pos_id} to {new_sl}.")
            success = await self.modify_position_sl_with_retry(pos_id, new_sl)
            if success:
                trade["sl_level"] = new_sl
                self.save_state()

    async def modify_position_sl_with_retry(self, pos_id: int, new_sl: float, max_retries: int = 5, base_delay: float = 1.0) -> bool:
        """
        Modifies a position's Stop Loss with an exponential backoff retry loop.
        """
        delay = base_delay
        for attempt in range(1, max_retries + 1):
            try:
                success = self.tl_client.modify_position_sl(pos_id, new_sl)
                if success:
                    logger.info(f"SL modification succeeded for position {pos_id} on attempt {attempt}.")
                    return True
                else:
                    logger.warning(f"SL modification rejected by TradeLocker for position {pos_id} on attempt {attempt}.")
            except Exception as e:
                logger.error(f"Error modifying SL for position {pos_id} on attempt {attempt}: {e}")
                
            if attempt < max_retries:
                logger.info(f"Retrying SL modification in {delay:.2f} seconds...")
                await asyncio.sleep(delay)
                delay *= 2  # Exponential backoff
                
        logger.critical(f"Failed to modify SL for position {pos_id} to {new_sl} after {max_retries} attempts.")
        return False
