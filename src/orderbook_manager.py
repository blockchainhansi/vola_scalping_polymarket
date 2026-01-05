"""
Polymarket Box Spread Market Maker - WebSocket Order Book Manager

Maintains continuous order book updates via WebSocket connection.
Triggers strategy updates on every change (continuous mode).
"""

import asyncio
import json
from typing import Optional, Callable, Awaitable, Dict, Any
from datetime import datetime
import websockets
from websockets.client import WebSocketClientProtocol

from config import Config, get_config
from models import OrderBook, OrderBookLevel, Outcome
from logger import get_logger

logger = get_logger(__name__)


class OrderBookManager:
    """
    Manages WebSocket connection for real-time order book updates.
    
    Features:
    - Subscribes to both YES and NO token order books
    - Caches latest order book state
    - Triggers callback on every update (continuous strategy execution)
    - Detects fills via trade events
    - Auto-reconnects on disconnect
    """
    
    def __init__(
        self, 
        config: Optional[Config] = None,
        on_update: Optional[Callable[[Outcome, OrderBook], Awaitable[None]]] = None,
        on_fill: Optional[Callable[[str, Outcome, float, float], None]] = None,
    ):
        self.config = config or get_config()
        self.on_update = on_update
        self.on_fill = on_fill  # Callback: (order_id, outcome, price, size)
        
        # Order book cache
        self._book_yes: Optional[OrderBook] = None
        self._book_no: Optional[OrderBook] = None
        
        # WebSocket state
        self._ws: Optional[WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_task: Optional[asyncio.Task] = None
        
        # Track our order IDs for fill detection
        self._tracked_order_ids: set = set()
        
        # Stats
        self._message_count = 0
        self._last_update: Optional[datetime] = None
        self._connected_at: Optional[datetime] = None
    
    @property
    def book_yes(self) -> Optional[OrderBook]:
        return self._book_yes
    
    @property
    def book_no(self) -> Optional[OrderBook]:
        return self._book_no
    
    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.open
    
    @property
    def has_data(self) -> bool:
        """Check if we have order book data for both sides."""
        return self._book_yes is not None and self._book_no is not None
    
    def set_callback(self, callback: Callable[[Outcome, OrderBook], Awaitable[None]]):
        """Set the callback for order book updates."""
        self.on_update = callback
    
    def set_fill_callback(self, callback: Callable[[str, Outcome, float, float], None]):
        """Set the callback for fill detection: (order_id, outcome, price, size)."""
        self.on_fill = callback
    
    def track_order(self, order_id: str):
        """Start tracking an order ID for fill detection."""
        self._tracked_order_ids.add(order_id)
    
    def untrack_order(self, order_id: str):
        """Stop tracking an order ID."""
        self._tracked_order_ids.discard(order_id)
    
    async def start(self):
        """Start the WebSocket connection and subscription."""
        if self._running:
            logger.warning("OrderBookManager already running")
            return
        
        self._running = True
        logger.info("Starting OrderBookManager...")
        
        await self._connect()
    
    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None
        
        if self._ws:
            await self._ws.close()
            self._ws = None
        
        logger.info("OrderBookManager stopped")
    
    async def _connect(self):
        """Establish WebSocket connection."""
        ws_url = self.config.clob_ws_url
        if not ws_url.endswith("/market"):
            ws_url = f"{ws_url}/market"
        
        try:
            logger.info(f"Connecting to {ws_url}...")
            self._ws = await websockets.connect(
                ws_url,
                ping_interval=30,
                ping_timeout=10,
            )
            self._connected_at = datetime.now()
            logger.info("WebSocket connected")
            
            # Subscribe to both tokens
            await self._subscribe()
            
            # Start message handler
            asyncio.create_task(self._message_loop())
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            await self._schedule_reconnect()
    
    async def _subscribe(self):
        """Subscribe to order book updates for both tokens."""
        if not self._ws:
            return
        
        # Polymarket WS subscription format
        subscribe_msg = {
            "type": "MARKET",
            "assets_ids": [
                self.config.token_id_yes,
                self.config.token_id_no,
            ]
        }
        
        await self._ws.send(json.dumps(subscribe_msg))
        logger.info(f"Subscribed to YES ({self.config.token_id_yes[:16]}...) and NO ({self.config.token_id_no[:16]}...)")
    
    async def _message_loop(self):
        """Process incoming WebSocket messages."""
        if not self._ws:
            return
        
        try:
            async for message in self._ws:
                if not self._running:
                    break
                
                await self._handle_message(message)
                
        except websockets.ConnectionClosed as e:
            logger.warning(f"WebSocket closed: {e}")
            if self._running:
                await self._schedule_reconnect()
        except Exception as e:
            logger.error(f"Message loop error: {e}")
            if self._running:
                await self._schedule_reconnect()
    
    async def _handle_message(self, raw: str):
        """Parse and process a WebSocket message."""
        try:
            # Skip non-JSON messages (like "PONG")
            raw = raw.strip()
            if not raw.startswith("{") and not raw.startswith("["):
                return
            
            data = json.loads(raw)
            self._message_count += 1
            
            # Handle array of book snapshots
            if isinstance(data, list):
                for book_data in data:
                    await self._process_book_data(book_data)
                return
            
            # Handle single book update
            event_type = data.get("event_type") or data.get("type")
            
            if event_type == "book" or ("asset_id" in data and ("bids" in data or "asks" in data)):
                await self._process_book_data(data)
            elif event_type in ("trade", "fill", "order_fill"):
                # Handle trade/fill events for fill detection
                self._process_fill_event(data)
            elif event_type in ("price_change", "last_trade_price"):
                # Ignore these - we only care about full book updates
                pass
            else:
                # Log unknown messages occasionally
                if self._message_count <= 5:
                    logger.debug(f"Unknown WS message type: {event_type}")
                    
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON: {raw[:100]}")
        except Exception as e:
            logger.error(f"Message handling error: {e}")
    
    async def _process_book_data(self, data: Dict[str, Any]):
        """Process an order book update."""
        asset_id = (
            data.get("asset_id") or 
            data.get("assetId") or 
            data.get("token_id") or 
            data.get("tokenId")
        )
        
        if not asset_id:
            return
        
        # Determine which book this is
        if asset_id == self.config.token_id_yes:
            outcome = Outcome.YES
        elif asset_id == self.config.token_id_no:
            outcome = Outcome.NO
        else:
            return  # Not our tokens
        
        # Parse bids and asks
        raw_bids = data.get("bids") or data.get("buys") or []
        raw_asks = data.get("asks") or data.get("sells") or []
        
        bids = [
            OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in raw_bids
        ]
        asks = [
            OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in raw_asks
        ]
        
        # Create order book
        book = OrderBook(
            asset_id=asset_id,
            bids=bids,
            asks=asks,
            timestamp=datetime.now(),
        )
        
        # Update cache
        if outcome == Outcome.YES:
            self._book_yes = book
        else:
            self._book_no = book
        
        self._last_update = datetime.now()
        
        # Log occasionally
        if self._message_count <= 3 or self._message_count % 100 == 0:
            logger.debug(
                f"Book {outcome.value}: {len(bids)} bids, {len(asks)} asks | "
                f"best bid={book.best_bid}, best ask={book.best_ask}"
            )
        
        # Trigger callback (continuous strategy execution)
        if self.on_update:
            try:
                await self.on_update(outcome, book)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    def _process_fill_event(self, data: Dict[str, Any]):
        """
        Process a trade/fill event to detect when our orders are filled.
        
        Polymarket WS may send fills in different formats - we check for our order IDs.
        """
        # Extract order ID from various possible fields
        order_id = (
            data.get("order_id") or 
            data.get("orderId") or 
            data.get("maker_order_id") or
            data.get("taker_order_id")
        )
        
        if not order_id or order_id not in self._tracked_order_ids:
            return  # Not our order
        
        # Extract fill details
        asset_id = (
            data.get("asset_id") or 
            data.get("assetId") or 
            data.get("token_id")
        )
        price = float(data.get("price", 0))
        size = float(data.get("size") or data.get("amount") or data.get("quantity", 0))
        
        if not asset_id or price <= 0 or size <= 0:
            logger.warning(f"Invalid fill event data: {data}")
            return
        
        # Determine outcome
        if asset_id == self.config.token_id_yes:
            outcome = Outcome.YES
        elif asset_id == self.config.token_id_no:
            outcome = Outcome.NO
        else:
            return
        
        logger.info(f"[WS FILL] Order {order_id[:16]}... filled: {outcome.value} {size} @ {price}")
        
        # Remove from tracked orders
        self._tracked_order_ids.discard(order_id)
        
        # Trigger fill callback
        if self.on_fill:
            try:
                self.on_fill(order_id, outcome, price, size)
            except Exception as e:
                logger.error(f"Fill callback error: {e}")
    
    async def _schedule_reconnect(self):
        """Schedule a reconnection attempt."""
        if not self._running:
            return
        
        if self._reconnect_task and not self._reconnect_task.done():
            return
        
        delay = self.config.ws_reconnect_delay
        logger.info(f"Reconnecting in {delay}s...")
        
        async def reconnect():
            await asyncio.sleep(delay)
            if self._running:
                await self._connect()
        
        self._reconnect_task = asyncio.create_task(reconnect())
    
    def get_stats(self) -> Dict[str, Any]:
        """Get connection statistics."""
        return {
            "connected": self.is_connected,
            "has_data": self.has_data,
            "message_count": self._message_count,
            "last_update": self._last_update.isoformat() if self._last_update else None,
            "connected_at": self._connected_at.isoformat() if self._connected_at else None,
            "yes_book": {
                "best_bid": self._book_yes.best_bid if self._book_yes else None,
                "best_ask": self._book_yes.best_ask if self._book_yes else None,
            } if self._book_yes else None,
            "no_book": {
                "best_bid": self._book_no.best_bid if self._book_no else None,
                "best_ask": self._book_no.best_ask if self._book_no else None,
            } if self._book_no else None,
        }


# Singleton instance
_manager: Optional[OrderBookManager] = None


def get_orderbook_manager() -> OrderBookManager:
    """Get the global OrderBookManager instance."""
    global _manager
    if _manager is None:
        _manager = OrderBookManager()
    return _manager


def init_orderbook_manager(
    config: Optional[Config] = None,
    on_update: Optional[Callable[[Outcome, OrderBook], Awaitable[None]]] = None,
    on_fill: Optional[Callable[[str, Outcome, float, float], None]] = None,
) -> OrderBookManager:
    """Initialize the global OrderBookManager."""
    global _manager
    _manager = OrderBookManager(config, on_update, on_fill)
    return _manager
