"""
Polymarket Box Spread Market Maker - CLOB Client

Uses py-clob-client for order signing and execution, matching the reference TS implementation.
"""

import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from config import Config, get_config
from models import Side, Outcome, OrderStatus, LiveOrder, OrderBook, OrderBookLevel
from logger import get_logger

logger = get_logger(__name__)

# Thread pool for running sync clob client methods
_executor = ThreadPoolExecutor(max_workers=4)


def _run_sync(func, *args, **kwargs):
    """Run a sync function in a thread pool."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_executor, lambda: func(*args, **kwargs))


class PolymarketClient:
    """
    Async wrapper around py-clob-client.
    
    Handles:
    - API key generation/derivation (EOA signature type)
    - Order placement (limit GTC for traps, market FOK for emergency/hedge)
    - Order cancellation
    - Position queries
    - Order book fetching (HTTP fallback)
    """
    
    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self._clob: Optional[ClobClient] = None
        self._creds: Optional[ApiCreds] = None
        self._initialized = False
    
    @property
    def wallet_address(self) -> str:
        if self._clob:
            return self._clob.get_address()
        # Derive from private key before client init
        from eth_account import Account
        return Account.from_key(self.config.private_key).address
    
    async def initialize(self):
        """Initialize the CLOB client and get API credentials."""
        if self._initialized:
            return
        
        # Create initial client without creds to derive/create API key
        # Note: signature_type=0 (POLY_PROXY) is required for Polymarket wallets
        # signature_type=2 (EOA) would be for direct EOA wallets without proxy
        self._clob = ClobClient(
            host=self.config.clob_http_url,
            chain_id=self.config.chain_id,
            key=self.config.private_key,
            signature_type=0,  # POLY_PROXY - required for Polymarket accounts
        )
        
        # Try to create API key first, fall back to derive
        try:
            self._creds = await _run_sync(self._clob.create_api_key)
            if self._creds and self._creds.api_key:
                logger.info(f"API key created for {self.wallet_address[:10]}...")
            else:
                raise ValueError("No API key returned")
        except Exception:
            try:
                self._creds = await _run_sync(self._clob.derive_api_key)
                logger.info(f"API key derived for {self.wallet_address[:10]}...")
            except Exception as e:
                logger.error(f"Failed to create/derive API key: {e}")
                raise
        
        # Recreate client with credentials
        self._clob = ClobClient(
            host=self.config.clob_http_url,
            chain_id=self.config.chain_id,
            key=self.config.private_key,
            signature_type=0,  # POLY_PROXY
            creds=self._creds,
        )
        
        self._initialized = True
        logger.info("Polymarket CLOB client initialized")
    
    def get_api_credentials(self) -> tuple[str, str, str]:
        """
        Get API credentials for authenticated WebSocket connections.
        
        Returns:
            Tuple of (api_key, api_secret, api_passphrase)
            
        Raises:
            RuntimeError: If client not initialized or credentials unavailable
        """
        if not self._initialized or not self._creds:
            raise RuntimeError("Client not initialized - call initialize() first")
        
        return (
            self._creds.api_key,
            self._creds.api_secret,
            self._creds.api_passphrase,
        )
    
    async def close(self):
        """Clean up resources."""
        self._clob = None
        self._creds = None
        self._initialized = False
    
    async def get_orderbook(self, token_id: str) -> OrderBook:
        """
        Fetch order book for a token via HTTP.
        
        This is a fallback - primary updates come via WebSocket.
        """
        if not self._clob:
            raise RuntimeError("Client not initialized")
        
        data = await _run_sync(self._clob.get_order_book, token_id)
        
        bids = [
            OrderBookLevel(price=float(level["price"]), size=float(level["size"]))
            for level in data.get("bids", [])
        ]
        asks = [
            OrderBookLevel(price=float(level["price"]), size=float(level["size"]))
            for level in data.get("asks", [])
        ]
        
        return OrderBook(
            asset_id=token_id,
            bids=bids,
            asks=asks,
            timestamp=datetime.now(),
        )
    
    async def place_limit_order(
        self,
        token_id: str,
        side: Side,
        price: float,
        size: float,
        time_in_force: str = "GTC",  # Good-Til-Cancelled for traps
    ) -> Optional[LiveOrder]:
        """
        Place a limit order (GTC for traps, IOC for immediate hedges).
        
        Args:
            token_id: The token ID to trade
            side: BUY or SELL
            price: Limit price (0.01 to 0.99)
            size: Order size in tokens (SELL) or USDC (BUY)
            time_in_force: GTC (maker trap), IOC (taker hedge)
            
        Returns:
            LiveOrder if successful, None if failed
        """
        if not self._clob:
            raise RuntimeError("Client not initialized")
        
        # Round price to 2 decimals (Polymarket tick size is 0.01)
        price = round(price, 2)
        
        # Map side to py-clob-client constants
        clob_side = BUY if side == Side.BUY else SELL
        
        # Build order args
        order_args = OrderArgs(
            token_id=token_id,
            side=clob_side,
            price=price,
            size=size,
        )
        
        # Choose order type: GTC for passive traps, FOK/IOC for immediate execution
        if time_in_force == "GTC":
            order_type = OrderType.GTC
        elif time_in_force == "IOC":
            order_type = OrderType.IOC
        else:
            order_type = OrderType.FOK
        
        try:
            # Create and sign the order (limit order path)
            signed_order = await _run_sync(self._clob.create_order, order_args)
            
            # Post the order
            resp = await _run_sync(self._clob.post_order, signed_order, order_type)
            
            if resp.get("success"):
                order_id = resp.get("orderID") or resp.get("order_id", "")
                logger.debug(f"Limit order placed: {order_id}")
                
                outcome = (
                    Outcome.YES if token_id == self.config.token_id_yes else Outcome.NO
                )
                
                return LiveOrder(
                    order_id=order_id,
                    asset_id=token_id,
                    outcome=outcome,
                    side=side,
                    price=price,
                    size=size,
                    status=OrderStatus.LIVE,
                )
            else:
                logger.error(f"Limit order failed: {resp}")
                return None
                
        except Exception as e:
            logger.error(f"Limit order error: {e}")
            return None
    
    async def place_market_order(
        self,
        token_id: str,
        side: Side,
        size: float,
        price: Optional[float] = None,
    ) -> Optional[LiveOrder]:
        """
        Place a market order (FOK - Fill or Kill).
        
        Used for emergency closes and immediate hedges when price is acceptable.
        
        Args:
            token_id: Token to trade
            side: BUY or SELL
            size: Amount (USDC for BUY, tokens for SELL)
            price: Price to use for sweep (defaults to worst case 0.99/0.01)
        """
        if not self._clob:
            raise RuntimeError("Client not initialized")
        
        # Default to worst-case price if not specified
        if price is None:
            price = 0.99 if side == Side.BUY else 0.01
        
        clob_side = BUY if side == Side.BUY else SELL
        
        order_args = OrderArgs(
            token_id=token_id,
            side=clob_side,
            price=round(price, 2),
            size=size,
        )
        
        try:
            # Use create_order (not create_market_order) with FOK type
            signed_order = await _run_sync(self._clob.create_order, order_args)
            resp = await _run_sync(self._clob.post_order, signed_order, OrderType.FOK)
            
            if resp.get("success"):
                order_id = resp.get("orderID") or resp.get("order_id", "")
                outcome = (
                    Outcome.YES if token_id == self.config.token_id_yes else Outcome.NO
                )
                
                return LiveOrder(
                    order_id=order_id,
                    asset_id=token_id,
                    outcome=outcome,
                    side=side,
                    price=price,
                    size=size,
                    status=OrderStatus.FILLED,
                )
            else:
                logger.error(f"Market order failed: {resp}")
                return None
                
        except Exception as e:
            logger.error(f"Market order error: {e}")
            return None
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by ID."""
        if not self._clob:
            raise RuntimeError("Client not initialized")
        
        try:
            resp = await _run_sync(self._clob.cancel, order_id)
            
            if resp.get("success") or resp.get("canceled"):
                logger.debug(f"Order cancelled: {order_id}")
                return True
            else:
                # Check if it's just "already canceled" - not a real error
                not_canceled = resp.get("not_canceled", {})
                if not_canceled:
                    reason = list(not_canceled.values())[0] if not_canceled else ""
                    if "already canceled" in reason or "already matched" in reason:
                        logger.debug(f"Order already gone: {order_id[:16]}...")
                        return True  # Not an error - order is already cancelled/filled
                logger.warning(f"Cancel failed: {resp}")
                return False
                
        except Exception as e:
            logger.error(f"Cancel error: {e}")
            return False
    
    async def cancel_all_orders(self, token_id: Optional[str] = None) -> int:
        """
        Cancel all orders, optionally filtered by token.
        
        Returns:
            Number of orders cancelled
        """
        if not self._clob:
            raise RuntimeError("Client not initialized")
        
        try:
            # First, try to cancel ALL orders across all markets
            resp = await _run_sync(self._clob.cancel_all)
            
            # Handle both list and int responses
            cancelled_data = resp.get("canceled", []) if isinstance(resp, dict) else []
            cancelled = len(cancelled_data) if isinstance(cancelled_data, list) else cancelled_data
            
            # Also cancel specifically for our current market tokens (belt and suspenders)
            if self.config.token_id_yes:
                try:
                    resp2 = await _run_sync(self._clob.cancel_market_orders, self.config.token_id_yes)
                    extra = len(resp2.get("canceled", [])) if isinstance(resp2, dict) else 0
                    cancelled += extra
                except Exception:
                    pass
            
            if self.config.token_id_no:
                try:
                    resp3 = await _run_sync(self._clob.cancel_market_orders, self.config.token_id_no)
                    extra = len(resp3.get("canceled", [])) if isinstance(resp3, dict) else 0
                    cancelled += extra
                except Exception:
                    pass
            
            logger.info(f"Cancelled {cancelled} orders")
            return cancelled
            
        except Exception as e:
            logger.error(f"Cancel all error: {e}")
            return 0
    
    async def get_open_orders(self, token_id: Optional[str] = None) -> List[LiveOrder]:
        """Get all open orders."""
        if not self._clob:
            raise RuntimeError("Client not initialized")
        
        try:
            if token_id:
                data = await _run_sync(self._clob.get_orders, token_id)
            else:
                data = await _run_sync(self._clob.get_orders)
            
            orders = []
            for order_data in data if isinstance(data, list) else data.get("orders", []):
                asset_id = order_data.get("asset_id") or order_data.get("token_id", "")
                outcome = (
                    Outcome.YES 
                    if asset_id == self.config.token_id_yes 
                    else Outcome.NO
                )
                
                orders.append(LiveOrder(
                    order_id=order_data.get("id", ""),
                    asset_id=asset_id,
                    outcome=outcome,
                    side=Side(order_data.get("side", "BUY").upper()),
                    price=float(order_data.get("price", 0)),
                    size=float(order_data.get("original_size") or order_data.get("size", 0)),
                    filled_size=float(order_data.get("size_matched", 0)),
                    status=OrderStatus.LIVE,
                ))
            
            return orders
            
        except Exception as e:
            logger.error(f"Get orders error: {e}")
            return []
    
    async def get_positions(self) -> Dict[str, float]:
        """
        Get current positions for our tokens.
        
        Returns:
            Dict mapping token_id to position size
        """
        if not self._clob:
            raise RuntimeError("Client not initialized")
        
        try:
            # Use the balances endpoint
            data = await _run_sync(self._clob.get_balances)
            
            positions = {}
            for pos in data if isinstance(data, list) else []:
                token_id = pos.get("asset_id") or pos.get("token_id", "")
                size = float(pos.get("size") or pos.get("balance", 0))
                if token_id in (self.config.token_id_yes, self.config.token_id_no):
                    positions[token_id] = size
            
            return positions
            
        except Exception as e:
            logger.error(f"Get positions error: {e}")
            return {}


# Singleton instance
_client: Optional[PolymarketClient] = None


async def get_client() -> PolymarketClient:
    """Get the global client instance."""
    global _client
    if _client is None:
        _client = PolymarketClient()
        await _client.initialize()
    return _client


async def init_client(config: Optional[Config] = None) -> PolymarketClient:
    """Initialize the global client with optional config."""
    global _client
    _client = PolymarketClient(config)
    await _client.initialize()
    return _client
