import logging
import math
from typing import Optional, Literal, Dict, Any, List
import pandas as pd

from tradelocker import TLAPI
from tradelocker.exceptions import TLAPIException, TLAPIOrderException

logger = logging.getLogger("TeleTrader.tradelocker_client")

class TradeLockerClient:
    def __init__(self, config: Dict[str, Any]):
        """
        Initializes the TradeLocker API Client wrapper.
        """
        self.tl_config = config.get("tradelocker", {})
        self.risk_config = config.get("risk", {})
        
        self.environment = self.tl_config.get("environment", "https://demo.tradelocker.com")
        self.username = self.tl_config.get("username")
        self.password = self.tl_config.get("password")
        self.server = self.tl_config.get("server")
        self.access_token = self.tl_config.get("access_token")
        self.refresh_token = self.tl_config.get("refresh_token")
        self.account_id = int(self.tl_config.get("account_id", 0))
        self.acc_num = int(self.tl_config.get("acc_num", 0))
        
        self.client: Optional[TLAPI] = None
        self.gold_instrument_id: Optional[int] = None
        self.gold_tradable_instrument_id: Optional[int] = None
        self.gold_symbol_name: Optional[str] = None

    def connect(self):
        """
        Establishes connection to TradeLocker and authenticates.
        """
        logger.info(f"Connecting to TradeLocker at {self.environment} on server {self.server}...")
        try:
            if self.access_token and self.refresh_token:
                # Monkeypatch TLAPI class method to avoid crash during init and subsequent refreshes
                TLAPI.refresh_access_tokens = lambda self: logger.warning(
                    "Token refresh skipped (Social Login session, using provided access token)"
                )
                
                self.client = TLAPI(
                    environment=self.environment,
                    access_token=self.access_token,
                    refresh_token=self.refresh_token,
                    account_id=self.account_id,
                    acc_num=self.acc_num,
                    log_level="info"
                )
            else:
                self.client = TLAPI(
                    environment=self.environment,
                    username=self.username,
                    password=self.password,
                    server=self.server,
                    account_id=self.account_id,
                    acc_num=self.acc_num,
                    log_level="info"
                )
            # Fetch all accounts to verify connection and populate account details
            accounts = self.client.get_all_accounts()
            logger.info(f"Connected successfully. Available accounts:\n{accounts}")
            
            # If account_id is 0, select the first account
            if self.account_id == 0 and not accounts.empty:
                self.account_id = int(accounts.iloc[0]["id"])
                self.acc_num = int(accounts.iloc[0]["accNum"])
                self.client.account_id = self.account_id
                self.client.acc_num = self.acc_num
                logger.info(f"Selected default account ID: {self.account_id}, Account Number: {self.acc_num}")
                
            self._resolve_gold_instrument()
        except Exception as e:
            err_msg = str(e)
            is_auth_error = any(word in err_msg.lower() for word in [
                "401", "403", "unauthorized", "forbidden", "token", "jwt", "validation failed", "expired"
            ])
            if is_auth_error:
                logger.critical(
                    "\n" + "="*80 + "\n"
                    "AUTHENTICATION ERROR: Your TradeLocker access token has expired or is invalid.\n"
                    "Please perform the following steps to update it:\n"
                    "1. Log in to TradeLocker in your browser (Chrome/Edge/Firefox).\n"
                    "2. Open Developer Tools (F12) -> Network tab.\n"
                    "3. Filter by 'jwt' or refresh the page, look for a request to 'jwt' or 'accounts'.\n"
                    "4. Copy the new 'accessToken' from the Request Headers / Response JSON.\n"
                    "5. Open config.json and update BOTH the 'access_token' and 'refresh_token' fields.\n"
                    "6. Restart the bot.\n"
                    "Reference error details: " + err_msg + "\n" +
                    "="*80 + "\n"
                )
            else:
                logger.critical(f"Failed to connect to TradeLocker: {e}")
            raise e

    def _resolve_gold_instrument(self):
        """
        Resolves Gold instrument details.
        """
        if not self.client:
            raise RuntimeError("Client is not connected.")
            
        logger.info("Resolving Gold (XAU/USD) instrument...")
        try:
            instruments_df = self.client.get_all_instruments()
            # 1. First priority: Exact standard XAUUSD symbols
            for _, row in instruments_df.iterrows():
                symbol_name = str(row["name"]).upper()
                if symbol_name in ["XAUUSD", "GOLD", "XAU/USD"]:
                    self.gold_instrument_id = int(row["id"])
                    self.gold_tradable_instrument_id = int(row["tradableInstrumentId"])
                    self.gold_symbol_name = str(row["name"])
                    logger.info(f"Found Gold instrument: {self.gold_symbol_name} (ID: {self.gold_instrument_id}, Tradable ID: {self.gold_tradable_instrument_id})")
                    return
            
            # 2. Second priority: Standard Gold symbols containing USD (e.g. XAUUSD.pro)
            for _, row in instruments_df.iterrows():
                symbol_name = str(row["name"]).upper()
                if "XAU" in symbol_name and "USD" in symbol_name:
                    self.gold_instrument_id = int(row["id"])
                    self.gold_tradable_instrument_id = int(row["tradableInstrumentId"])
                    self.gold_symbol_name = str(row["name"])
                    logger.info(f"Found Gold instrument: {self.gold_symbol_name} (ID: {self.gold_instrument_id}, Tradable ID: {self.gold_tradable_instrument_id})")
                    return
                    
            # 3. Third priority: Fallback to any generic XAU symbol
            for _, row in instruments_df.iterrows():
                symbol_name = str(row["name"]).upper()
                if "XAU" in symbol_name:
                    self.gold_instrument_id = int(row["id"])
                    self.gold_tradable_instrument_id = int(row["tradableInstrumentId"])
                    self.gold_symbol_name = str(row["name"])
                    logger.info(f"Found Gold instrument: {self.gold_symbol_name} (ID: {self.gold_instrument_id}, Tradable ID: {self.gold_tradable_instrument_id})")
                    return
            
            raise ValueError("Gold (XAU/USD) instrument could not be found in the asset list.")
        except Exception as e:
            logger.error(f"Error resolving Gold instrument: {e}")
            raise e

    def get_balance(self) -> float:
        """
        Fetches the current account balance.
        """
        if not self.client:
            raise RuntimeError("Client not connected.")
        try:
            state = self.client.get_account_state()
            return float(state.get("balance", 0.0))
        except Exception as e:
            logger.error(f"Error fetching account state: {e}")
            return 0.0

    def calculate_lot_size(self, entry_price: float, stop_loss: float) -> float:
        """
        Dynamically calculates lot size based on account balance, risk parameter, and SL distance.
        """
        risk_type = self.risk_config.get("risk_type", "fixed")
        fixed_lot = self.risk_config.get("fixed_lot_size", 0.01)
        
        if risk_type == "fixed":
            logger.debug(f"Using fixed lot size: {fixed_lot}")
            return fixed_lot

        sl_distance = abs(entry_price - stop_loss)
        if sl_distance <= 0:
            logger.warning("Stop loss distance is zero or negative. Using minimum lot size 0.01.")
            return 0.01

        balance = self.get_balance()
        risk_pct = self.risk_config.get("risk_percentage", 1.0)
        contract_size = self.risk_config.get("gold_contract_size", 100.0)
        
        risk_usd = balance * (risk_pct / 100.0)
        calculated_lot = risk_usd / (contract_size * sl_distance)
        
        # Round to 2 decimal places (standard step is 0.01 lot)
        lot_size = round(calculated_lot, 2)
        if lot_size < 0.01:
            lot_size = 0.01
            
        logger.info(f"Balance: ${balance:.2f} | Risk USD: ${risk_usd:.2f} | SL Distance: {sl_distance:.2f} | Calculated Lot: {calculated_lot:.4f} -> Final Lot: {lot_size}")
        return lot_size

    def get_quotes(self) -> Dict[str, float]:
        """
        Returns the latest Bid and Ask price for Gold.
        """
        if not self.client or self.gold_instrument_id is None:
            raise RuntimeError("Client not connected or Gold instrument not resolved.")
        try:
            quotes = self.client.get_quotes(self.gold_instrument_id)
            return {
                "ask": float(quotes.get("ap", 0.0)),
                "bid": float(quotes.get("bp", 0.0))
            }
        except Exception as e:
            logger.error(f"Error fetching quotes for Gold: {e}")
            raise e

    def execute_market_order(self, side: Literal["buy", "sell"], quantity: float, stop_loss: float, take_profit: Optional[float] = None) -> Optional[int]:
        """
        Executes a market order for Gold with SL and optional TP.
        """
        if not self.client or self.gold_instrument_id is None:
            raise RuntimeError("Client not connected or Gold instrument not resolved.")
            
        logger.info(f"Placing market {side.upper()} order for {quantity} lots (SL: {stop_loss}, TP: {take_profit})...")
        try:
            order_id = self.client.create_order(
                instrument_id=self.gold_instrument_id,
                quantity=quantity,
                side=side,
                type_="market",
                validity="IOC",
                stop_loss=stop_loss,
                stop_loss_type="absolute",
                take_profit=take_profit,
                take_profit_type="absolute" if take_profit else None
            )
            logger.info(f"Market order successfully placed. Order ID: {order_id}")
            return order_id
        except TLAPIOrderException as oe:
            logger.error(f"Broker rejected the order: {oe.response_json}")
            return None
        except Exception as e:
            logger.error(f"Failed to place market order: {e}")
            return None

    def execute_pending_order(self, type_: Literal["limit", "stop"], side: Literal["buy", "sell"], quantity: float, price: float, stop_loss: float, take_profit: Optional[float] = None) -> Optional[int]:
        """
        Executes a pending order (limit or stop) for Gold.
        """
        if not self.client or self.gold_instrument_id is None:
            raise RuntimeError("Client not connected or Gold instrument not resolved.")
            
        logger.info(f"Placing pending {type_.upper()} {side.upper()} order at {price} for {quantity} lots (SL: {stop_loss}, TP: {take_profit})...")
        try:
            kwargs = {}
            if type_ == "stop":
                kwargs["stop_price"] = price
                kwargs["price"] = price
            else:
                kwargs["price"] = price
                
            order_id = self.client.create_order(
                instrument_id=self.gold_instrument_id,
                quantity=quantity,
                side=side,
                type_=type_,
                validity="GTC",
                stop_loss=stop_loss,
                stop_loss_type="absolute",
                take_profit=take_profit,
                take_profit_type="absolute" if take_profit else None,
                **kwargs
            )
            logger.info(f"Pending order successfully placed. Order ID: {order_id}")
            return order_id
        except TLAPIOrderException as oe:
            logger.error(f"Broker rejected pending order: {oe.response_json}")
            return None
        except Exception as e:
            logger.error(f"Failed to place pending order: {e}")
            return None

    def get_active_positions(self) -> List[Dict[str, Any]]:
        """
        Retrieves all currently active open positions for Gold.
        """
        if not self.client:
            raise RuntimeError("Client not connected.")
        try:
            positions_df = self.client.get_all_positions()
            if positions_df.empty:
                return []
                
            gold_pos = positions_df[positions_df["tradableInstrumentId"] == self.gold_tradable_instrument_id]
            positions = []
            for _, row in gold_pos.iterrows():
                positions.append({
                    "id": int(row["id"]),
                    "side": str(row["side"]),
                    "qty": float(row["qty"]),
                    "avg_price": float(row["avgPrice"]),
                    "stop_loss_id": int(row["stopLossId"]) if not pd.isna(row["stopLossId"]) else None,
                    "take_profit_id": int(row["takeProfitId"]) if not pd.isna(row["takeProfitId"]) else None,
                    "open_date": int(row["openDate"]),
                    "unrealized_pnl": float(row["unrealizedPl"])
                })
            return positions
        except Exception as e:
            logger.error(f"Error fetching active positions: {e}")
            return []

    def get_position_stop_loss(self, position_id: int) -> Optional[float]:
        """
        Fetches the current stop loss price of an active position by searching orders history.
        """
        if not self.client:
            raise RuntimeError("Client not connected.")
        try:
            orders = self.client.get_all_orders(history=False)
            if not orders.empty:
                matched = orders[(orders["positionId"] == position_id) & (orders["type"] == "stop")]
                if not matched.empty:
                    return float(matched.iloc[0]["stopPrice"])
            
            hist_orders = self.client.get_all_orders(history=True)
            if not hist_orders.empty:
                matched = hist_orders[(hist_orders["positionId"] == position_id) & (hist_orders["type"] == "stop")]
                if not matched.empty:
                    return float(matched.iloc[0]["stopPrice"])
            
            return None
        except Exception as e:
            logger.error(f"Error fetching stop loss for position {position_id}: {e}")
            return None

    def modify_position_sl(self, position_id: int, new_sl: float) -> bool:
        """
        Modifies the Stop Loss of an open position using PATCH.
        """
        if not self.client:
            raise RuntimeError("Client not connected.")
        logger.info(f"Modifying position {position_id} Stop Loss to {new_sl}...")
        try:
            params = {
                "stopLoss": new_sl,
                "stopLossType": "absolute"
            }
            success = self.client.modify_position(position_id, params)
            if success:
                logger.info(f"Successfully modified Stop Loss for position {position_id} to {new_sl}")
                return True
            else:
                logger.warning(f"Stop Loss modification returned False for position {position_id}")
                return False
        except Exception as e:
            logger.error(f"Error modifying Stop Loss for position {position_id}: {e}")
            raise e

    def close_position_partial(self, position_id: int, quantity_to_close: float) -> bool:
        """
        Partially closes an open position.
        """
        if not self.client:
            raise RuntimeError("Client not connected.")
        logger.info(f"Partially closing position {position_id} by {quantity_to_close} lots...")
        try:
            success = self.client.close_position(position_id=position_id, close_quantity=quantity_to_close)
            if success:
                logger.info(f"Successfully partially closed position {position_id} by {quantity_to_close} lots.")
                return True
            else:
                logger.warning(f"Partial close returned False for position {position_id}")
                return False
        except Exception as e:
            logger.error(f"Error partially closing position {position_id}: {e}")
            return False

    def close_position_fully(self, position_id: int) -> bool:
        """
        Fully closes an open position.
        """
        if not self.client:
            raise RuntimeError("Client not connected.")
        logger.info(f"Fully closing position {position_id}...")
        try:
            success = self.client.close_position(position_id=position_id, close_quantity=0)
            if success:
                logger.info(f"Successfully fully closed position {position_id}")
                return True
            else:
                logger.warning(f"Full close returned False for position {position_id}")
                return False
        except Exception as e:
            logger.error(f"Error fully closing position {position_id}: {e}")
            return False
