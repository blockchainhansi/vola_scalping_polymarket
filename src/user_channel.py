"""
Polymarket Box Spread Market Maker - USER Channel WebSocket Manager

Handles authenticated WebSocket connection to Polymarket USER channel
for real-time fill/trade notifications.

The USER channel requires authentication (apiKey, secret, passphrase)
and provides:
- trade events: when orders are matched/filled
- order events: placement/update/cancellation confirmations
"""

import asyncio
import json
import threading
from typing import Optional, Callable, Set, Dict, Any
from datetime import datetime

from websocket import WebSocketApp

from config import Config, get_config
from models import Outcome
from logger import get_logger

logger = get_logger(__name__)


class UserChannelManager:
    """
    Manages authenticated WebSocket connection to Polymarket USER channel.
    
    Detects fills for our orders by:
    1. Connecting to wss://ws-subscriptions-clob.polymarket.com/ws/user
    2. Authenticating with API credentials (apiKey, secret, passphrase)
    3. Subscribing to trade events
    4. Matching incoming trades against tracked order IDs
    
    Events from USER channel:
    - trade: {event_type: "trade", taker_order_id, maker_orders: [{order_id, ...}], price, size, status}
    - order: {event_type: "order", id, type: "PLACEMENT"|"UPDATE"|"CANCELLATION", size_matched}
    """
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        config: Optional[Config] = None,
        on_fill: Optional[Callable[[str, Outcome, float, float], None]] = None,
    ):
        self.config = config or get_config()
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.on_fill = on_fill  # Callback: (order_id, outcome, price, size)
        
        # WebSocket state
        self._ws: Optional[WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        
        # Track our order IDs for fill detection
        self._tracked_order_ids: Set[str] = set()
        
        # Stats
        self._message_count = 0
        self._last_fill: Optional[datetime] = None
        self._connected_at: Optional[datetime] = None
        
        # Build WebSocket URL
        ws_base = self.config.clob_ws_url.rstrip("/")
        if ws_base.endswith("/ws"):
            ws_base = ws_base[:-3]
        self._ws_url = f"{ws_base}/ws/user"
    
    @property
    def is_connected(self) -> bool:
        return self._connected
    
    def set_fill_callback(self, callback: Callable[[str, Outcome, float, float], None]):
        """Set the callback for fill detection: (order_id, outcome, price, size)."""
        self.on_fill = callback
    
    def track_order(self, order_id: str):
        """Start tracking an order ID for fill detection."""
        self._tracked_order_ids.add(order_id)
        logger.debug(f"[USER] Tracking order {order_id[:16]}... ({len(self._tracked_order_ids)} total)")
    
    def untrack_order(self, order_id: str):
        """Stop tracking an order ID."""
        self._tracked_order_ids.discard(order_id)
        logger.debug(f"[USER] Untracked order {order_id[:16]}... ({len(self._tracked_order_ids)} total)")
    
    def start(self):
        """Start the USER channel WebSocket connection in a background thread."""
        if self._running:
            logger.warning("[USER] Already running")
            return
        
        self._running = True
        logger.info(f"[USER] Starting connection to {self._ws_url}")
        
        # Create WebSocketApp
        self._ws = WebSocketApp(
            self._ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        
        # Run in background thread (WebSocketApp.run_forever() blocks)
        self._ws_thread = threading.Thread(target=self._run_ws, daemon=True)
        self._ws_thread.start()
    
    def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        
        if self._ws:
            self._ws.close()
            self._ws = None
        
        if self._ws_thread:
            self._ws_thread.join(timeout=2)
            self._ws_thread = None
        
        self._connected = False
        logger.info("[USER] Stopped")
    
    def _run_ws(self):
        """Run WebSocket in background thread with auto-reconnect."""
        while self._running:
            try:
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"[USER] WebSocket error: {e}")
            
            if self._running:
                logger.info("[USER] Reconnecting in 5s...")
                asyncio.run(asyncio.sleep(5))
    
    def _on_open(self, ws):
        """Handle WebSocket connection opened."""
        logger.info("[USER] WebSocket connected, authenticating...")
        self._connected_at = datetime.now()
        
        # Send authentication and subscription message
        # Format: {"type": "user", "markets": [...], "auth": {apiKey, secret, passphrase}}
        auth_msg = {
            "type": "user",
            "markets": [],  # Empty = subscribe to all markets for this user
            "auth": {
                "apiKey": self.api_key,
                "secret": self.api_secret,
                "passphrase": self.api_passphrase,
            }
        }
        
        ws.send(json.dumps(auth_msg))
        logger.info("[USER] Auth message sent")
        self._connected = True
        
        # Start ping thread
        threading.Thread(target=self._ping_loop, args=(ws,), daemon=True).start()
    
    def _ping_loop(self, ws):
        """Send periodic pings to keep connection alive."""
        import time
        while self._running and self._connected:
            try:
                ws.send("PING")
            except Exception:
                break
            time.sleep(10)
    
    def _on_message(self, ws, message: str):
        """Handle incoming WebSocket message."""
        self._message_count += 1
        
        # Skip PONG responses
        message = message.strip()
        if message == "PONG" or not message.startswith("{"):
            return
        
        try:
            data = json.loads(message)
            event_type = data.get("event_type", "")
            
            if event_type == "trade":
                self._handle_trade(data)
            elif event_type == "order":
                self._handle_order_update(data)
            else:
                if self._message_count <= 5:
                    logger.debug(f"[USER] Unknown event: {event_type}")
                    
        except json.JSONDecodeError:
            logger.warning(f"[USER] Invalid JSON: {message[:100]}")
        except Exception as e:
            logger.error(f"[USER] Message handling error: {e}")
    
    def _handle_trade(self, data: Dict[str, Any]):
        """
        Handle a trade event from USER channel.
        
        Trade message format:
        {
            "event_type": "trade",
            "taker_order_id": "...",
            "maker_orders": [{"order_id": "...", ...}],
            "asset_id": "...",
            "price": "0.55",
            "size": "10",
            "status": "MATCHED" | "MINED" | "CONFIRMED"
        }
        """
        # DEBUG: Log full trade message to understand structure
        logger.debug(f"[USER] RAW TRADE: {data}")
        
        status = data.get("status", "")
        
        # We care about MATCHED (immediate fill confirmation)
        # Could also track MINED/CONFIRMED for on-chain confirmation
        if status not in ("MATCHED", "MINED", "CONFIRMED"):
            return
        
        asset_id = data.get("asset_id", "")
        price = float(data.get("price", 0))
        size = float(data.get("size", 0))
        
        if price <= 0 or size <= 0:
            return
        
        # Check if taker_order_id is ours
        taker_order_id = data.get("taker_order_id", "")
        if taker_order_id in self._tracked_order_ids:
            self._process_fill(taker_order_id, asset_id, price, size, "taker", status)
        
        # Check maker_orders for our order IDs
        maker_orders = data.get("maker_orders", [])
        for maker in maker_orders:
            maker_order_id = maker.get("order_id", "")
            if maker_order_id in self._tracked_order_ids:
                # Maker fill - use the maker's own price/size/asset_id
                maker_price = float(maker.get("price", price))
                # IMPORTANT: maker uses 'matched_amount', not 'size'
                maker_size = float(maker.get("matched_amount", maker.get("size", size)))
                # IMPORTANT: maker has its own asset_id (can differ from trade-level)
                maker_asset_id = maker.get("asset_id", asset_id)
                logger.debug(f"[USER] Maker order details: {maker}")
                logger.debug(f"[USER] Using asset_id: {maker_asset_id}, size: {maker_size} (trade was: {asset_id}, {size})")
                self._process_fill(maker_order_id, maker_asset_id, maker_price, maker_size, "maker", status)
    
    def _handle_order_update(self, data: Dict[str, Any]):
        """
        Handle an order update event from USER channel.
        
        Order message format:
        {
            "event_type": "order",
            "id": "order_id",
            "type": "PLACEMENT" | "UPDATE" | "CANCELLATION",
            "size_matched": "5.0",
            "asset_id": "...",
            ...
        }
        """
        order_id = data.get("id", "")
        update_type = data.get("type", "")
        
        if order_id not in self._tracked_order_ids:
            return
        
        if update_type == "CANCELLATION":
            logger.debug(f"[USER] Order {order_id[:16]}... cancelled")
            self._tracked_order_ids.discard(order_id)
        
        elif update_type in ("PLACEMENT", "UPDATE"):
            size_matched = float(data.get("size_matched", 0))
            if size_matched > 0:
                # Partial or full fill via order update
                asset_id = data.get("asset_id", "")
                # Note: order updates may not include price - use 0 as placeholder
                price = float(data.get("price", 0))
                logger.info(f"[USER] Order {order_id[:16]}... matched {size_matched}")
    
    def _process_fill(
        self, 
        order_id: str, 
        asset_id: str, 
        price: float, 
        size: float,
        role: str,
        status: str
    ):
        """Process a confirmed fill and trigger callback."""
        # Determine outcome from asset_id
        # Debug: Log the comparison
        logger.debug(f"[USER] Fill asset_id: {asset_id}")
        logger.debug(f"[USER] Config YES token: {self.config.token_id_yes}")
        logger.debug(f"[USER] Config NO token: {self.config.token_id_no}")
        
        if asset_id == self.config.token_id_yes:
            outcome = Outcome.YES
        elif asset_id == self.config.token_id_no:
            outcome = Outcome.NO
        else:
            logger.warning(f"[USER] Unknown asset_id in fill: {asset_id[:32]}...")
            logger.warning(f"[USER] Expected YES: {self.config.token_id_yes[:32]}...")
            logger.warning(f"[USER] Expected NO: {self.config.token_id_no[:32]}...")
            return
        
        self._last_fill = datetime.now()
        
        logger.info(
            f"[USER FILL] {role.upper()} order {order_id[:16]}... filled: "
            f"{outcome.value} {size} @ {price} (status={status})"
        )
        
        # Remove from tracked orders (full fill assumed)
        # For partial fills, we'd need to track remaining size
        self._tracked_order_ids.discard(order_id)
        
        # Trigger callback
        if self.on_fill:
            try:
                self.on_fill(order_id, outcome, price, size)
            except Exception as e:
                logger.error(f"[USER] Fill callback error: {e}")
    
    def _on_error(self, ws, error):
        """Handle WebSocket error."""
        logger.error(f"[USER] WebSocket error: {error}")
        self._connected = False
    
    def _on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket connection closed."""
        logger.warning(f"[USER] WebSocket closed: {close_status_code} - {close_msg}")
        self._connected = False
    
    def get_stats(self) -> Dict[str, Any]:
        """Get connection statistics."""
        return {
            "connected": self._connected,
            "message_count": self._message_count,
            "tracked_orders": len(self._tracked_order_ids),
            "last_fill": self._last_fill.isoformat() if self._last_fill else None,
            "connected_at": self._connected_at.isoformat() if self._connected_at else None,
        }


# Singleton instance
_user_channel: Optional[UserChannelManager] = None


def init_user_channel(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    config: Optional[Config] = None,
    on_fill: Optional[Callable[[str, Outcome, float, float], None]] = None,
) -> UserChannelManager:
    """Initialize the global UserChannelManager."""
    global _user_channel
    _user_channel = UserChannelManager(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        config=config,
        on_fill=on_fill,
    )
    return _user_channel


def get_user_channel() -> Optional[UserChannelManager]:
    """Get the global UserChannelManager instance."""
    return _user_channel
