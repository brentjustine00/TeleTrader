import logging
import re
import json
from typing import Optional, Dict, Any, List, Callable
from pydantic import BaseModel, Field
from telethon import TelegramClient, events
from google import genai
from google.genai import types

logger = logging.getLogger("TeleTrader.scraper")

# Pydantic schema for structured output from Gemini
class GoldSignalSchema(BaseModel):
    is_gold_signal: bool = Field(
        description="True if the message is a trading signal specifically for Gold/XAUUSD, otherwise False"
    )
    action: Optional[str] = Field(
        description="Type of order: Buy, Sell, Buy Limit, Sell Limit, Buy Stop, Sell Stop. Must be one of these exact values, or null if not a signal."
    )
    entry_price: Optional[float] = Field(
        description="The entry price value or the starting/lower price of the entry range if a range is specified."
    )
    entry_price_high: Optional[float] = Field(
        description="The upper limit of the entry range if a range is specified, otherwise null."
    )
    stop_loss: Optional[float] = Field(
        description="Stop Loss price value."
    )
    take_profits: List[float] = Field(
        description="Ordered list of Take Profit price targets (TP1, TP2, TP3, TP4, etc.)."
    )

class TelegramScraper:
    def __init__(self, config: Dict[str, Any], signal_callback: Callable[[Dict[str, Any]], Any]):
        """
        Initializes the Telegram Scraper.
        """
        self.telegram_config = config.get("telegram", {})
        self.gemini_config = config.get("gemini", {})
        self.signal_callback = signal_callback
        
        self.api_id = int(self.telegram_config.get("api_id", 0))
        self.api_hash = self.telegram_config.get("api_hash", "")
        self.session_name = self.telegram_config.get("session_name", "gold_scraper")
        self.channel_ids = self.telegram_config.get("channel_ids", [])
        
        import os
        self.gemini_key = os.environ.get("GEMINI_API_KEY") or self.gemini_config.get("api_key")
        if self.gemini_key == "your_gemini_api_key_here":
            self.gemini_key = None
            
        self.gemini_model = self.gemini_config.get("model", "gemini-2.5-flash")
        
        self.client: Optional[TelegramClient] = None

    async def start(self):
        """
        Starts the Telethon Telegram Client and listens to channels.
        """
        if not self.api_id or not self.api_hash:
            raise ValueError("Telegram api_id and api_hash must be configured.")
            
        logger.info("Initializing Telegram Client...")
        string_session = self.telegram_config.get("string_session")
        if string_session:
            from telethon.sessions import StringSession
            logger.info("Using StringSession for Telegram authentication.")
            self.client = TelegramClient(StringSession(string_session), self.api_id, self.api_hash)
        else:
            logger.info("Using FileSession for Telegram authentication.")
            self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
        
        # Add event listener for new messages on specified channels
        @self.client.on(events.NewMessage(chats=self.channel_ids))
        async def handler(event):
            message_text = event.message.message
            sender_id = event.chat_id
            logger.info(f"Received raw message from chat {sender_id}:\n{message_text}")
            
            # Run the parser pipeline
            parsed_signal = self.parse_message(message_text)
            if parsed_signal:
                logger.info(f"Successfully parsed Gold signal: {parsed_signal}")
                # Dispatch signal to orchestrator
                await self.signal_callback(parsed_signal)
            else:
                logger.info("Message did not contain a valid Gold trading signal.")
                
        await self.client.start()
        logger.info(f"Telegram client started. Listening to channels: {self.channel_ids}")
        await self.client.run_until_disconnected()

    def parse_message(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Parsing pipeline trying heuristic NLP first, then falling back to Gemini.
        """
        # 1. Try heuristic parser
        heuristic_result = self.parse_heuristic(text)
        
        if heuristic_result:
            # Validate if we got all core metrics: asset, action, entry, SL, and at least 2 TPs
            has_core = (
                heuristic_result.get("action") is not None and
                heuristic_result.get("entry_price") is not None and
                heuristic_result.get("stop_loss") is not None and
                len(heuristic_result.get("take_profits", [])) >= 2
            )
            if has_core:
                logger.info("Signal successfully parsed via Heuristic Engine.")
                return heuristic_result
            else:
                logger.info("Heuristic parser output is incomplete. Falling back to Gemini LLM...")
        else:
            logger.info("Heuristic parser found no signal matches. Falling back to Gemini LLM...")

        # 2. Try Gemini API fallback if key is configured
        if self.gemini_key:
            try:
                return self.parse_llm(text)
            except Exception as e:
                logger.error(f"Gemini LLM parser encountered an error: {e}")
                return None
        else:
            logger.warning("Gemini API key not configured. Fallback skipped.")
            return heuristic_result  # Return whatever heuristic managed to parse (even if incomplete)

    def parse_heuristic(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Resilient heuristic NLP parser.
        """
        clean_text = text.lower().strip()
        
        # Verify it is Gold/XAUUSD
        is_gold = any(x in clean_text for x in ["xau", "gold", "xauusd", "xau/usd"])
        if not is_gold:
            return None
            
        # Detect Action
        action = None
        if "buy limit" in clean_text:
            action = "Buy Limit"
        elif "sell limit" in clean_text:
            action = "Sell Limit"
        elif "buy stop" in clean_text:
            action = "Buy Stop"
        elif "sell stop" in clean_text:
            action = "Sell Stop"
        elif "buy" in clean_text:
            action = "Buy"
        elif "sell" in clean_text:
            action = "Sell"
            
        if not action:
            return None

        # Clean lines and extract numbers from relevant lines
        lines = [line.strip() for line in clean_text.split('\n') if line.strip()]
        
        entry_price = None
        entry_price_high = None
        stop_loss = None
        take_profits = []
        
        # Helper to extract numbers between 1000 and 10000 (standard gold prices)
        def extract_gold_prices(s: str) -> List[float]:
            found = re.findall(r'\b\d{3,5}(?:\.\d+)?\b', s)
            return [float(x) for x in found if 1000 <= float(x) <= 10000]

        for line in lines:
            # Check Stop Loss
            if any(term in line for term in ["sl", "stoploss", "stop loss", "stop"]):
                prices = extract_gold_prices(line)
                if prices:
                    stop_loss = prices[0]
                    continue
            
            # Check Take Profits
            if any(term in line for term in ["tp", "target", "take profit", "take_profit"]):
                prices = extract_gold_prices(line)
                take_profits.extend(prices)
                continue
                
            # Check Entry
            if any(term in line for term in ["entry", "open", "at", "price", "limit", "stop"]):
                prices = extract_gold_prices(line)
                if prices:
                    if len(prices) >= 2:
                        entry_price = min(prices)
                        entry_price_high = max(prices)
                    else:
                        entry_price = prices[0]
                    continue

        # Fallback order-based parsing if keywords missed
        all_prices = extract_gold_prices(clean_text)
        
        if entry_price is None and all_prices:
            entry_price = all_prices[0]
            # Check if second number is entry range
            if len(all_prices) > 1 and action in ["Buy Limit", "Sell Limit", "Buy Stop", "Sell Stop"]:
                # If they are very close, it could be a range
                if abs(all_prices[1] - all_prices[0]) < 10.0:
                    entry_price_high = all_prices[1]
                    
        # Remove duplicates and sort TPs
        take_profits = sorted(list(set(take_profits)))
        
        # Validate logic: SL should be opposite of action relative to entry
        if entry_price and stop_loss:
            if "buy" in action.lower() and stop_loss >= entry_price:
                # Invert if heuristic mixed them up
                logger.warning("SL is higher than entry on a BUY. Heuristic may have swapped them.")
            elif "sell" in action.lower() and stop_loss <= entry_price:
                logger.warning("SL is lower than entry on a SELL. Heuristic may have swapped them.")

        return {
            "asset": "XAUUSD",
            "action": action,
            "entry_price": entry_price,
            "entry_price_high": entry_price_high,
            "stop_loss": stop_loss,
            "take_profits": take_profits
        }

    def parse_llm(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Calls Gemini API with structured JSON Schema to parse trading signals.
        """
        logger.info(f"Invoking Gemini Model ({self.gemini_model}) for layout-adaptive parsing...")
        client = genai.Client(api_key=self.gemini_key)
        
        prompt = (
            f"Parse the following Telegram channel message for a Gold/XAUUSD trading signal. "
            f"Extract the action, entry price or range, stop loss, and take profit targets.\n\n"
            f"Message Content:\n\"\"\"\n{text}\n\"\"\""
        )
        
        response = client.models.generate_content(
            model=self.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GoldSignalSchema,
                system_instruction=(
                    "You are a layout-adaptive trade signal parser. You translate messy channel updates "
                    "into standard signal details. Verify the asset is strictly GOLD/XAUUSD. "
                    "If it is not a Gold signal, set is_gold_signal to false. "
                    "The action MUST be one of: Buy, Sell, Buy Limit, Sell Limit, Buy Stop, Sell Stop. "
                    "Take profits must be a list of target prices sorted in order away from the entry price."
                )
            )
        )
        
        try:
            result = json.loads(response.text)
            if not result.get("is_gold_signal"):
                logger.info("Gemini parsed signal: not a Gold/XAUUSD signal.")
                return None
                
            return {
                "asset": "XAUUSD",
                "action": result.get("action"),
                "entry_price": result.get("entry_price"),
                "entry_price_high": result.get("entry_price_high"),
                "stop_loss": result.get("stop_loss"),
                "take_profits": sorted(list(set(result.get("take_profits", []))))
            }
        except Exception as e:
            logger.error(f"Error decoding Gemini structured JSON output: {e}. Output was:\n{response.text}")
            raise e
