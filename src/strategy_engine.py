"""
Strategy engine implementing the continuous box-spread legging logic.
"""

import asyncio
from datetime import datetime
from typing import Optional

from config import Config, get_config
from logger import get_logger, StrategyLogger
from models import (
    StrategyState,
    StrategyMode,
    Outcome,
    Side,
    LiveOrder,
    OrderBook,
)
from polymarket_client import PolymarketClient, get_client
from orderbook_manager import OrderBookManager, get_orderbook_manager
from user_channel import get_user_channel
from safety import exposure_exceeded, is_in_expiry_buffer, is_in_final_exit

logger = get_logger(__name__)


class StrategyEngine:
    """Continuous market maker based on the box-spread invariant."""

    def __init__(
        self,
        config: Optional[Config] = None,
        client: Optional[PolymarketClient] = None,
        ob_manager: Optional[OrderBookManager] = None,
    ):
        self.config = config or get_config()
        self.client = client
        self.ob_manager = ob_manager
        self.state = StrategyState()
        self.log = StrategyLogger(logger)
        self._lock = asyncio.Lock()
        self._last_trap_update: Optional[datetime] = None
        self._loop = None  # Capture event loop on start
        self._placing_traps = False  # Guard against duplicate trap placement
        self._placing_hedge = False  # Guard against duplicate hedge placement
        self._order_outcome: dict[str, Outcome] = {}  # Track intended outcome by order_id
        self._accepted_imbalance = False  # Track if we've logged accepting small imbalance

    async def start(self):
        self._loop = asyncio.get_running_loop()
        if self.client is None:
            self.client = await get_client()
        if self.ob_manager is None:
            self.ob_manager = get_orderbook_manager()
        
        # Set up callbacks
        self.ob_manager.set_callback(self.on_orderbook)
        # Note: Fill detection is handled by USER channel WebSocket, not MARKET channel
        # self.ob_manager.set_fill_callback(self.on_fill)  # Commented out - fills come from user_channel
        
        await self.ob_manager.start()
        self.state.mode = StrategyMode.OPEN
        self.state.started_at = datetime.now()
        self.log.mode_change("INIT", self.state.mode.value)
    
    def _remember_order(self, order_id: str, outcome: Outcome):
        """Remember intended outcome for an order_id to disambiguate fills."""
        self._order_outcome[order_id] = outcome

    def _forget_order(self, order_id: str):
        """Forget stored metadata for an order_id."""
        self._order_outcome.pop(order_id, None)

    def _track_order(self, order_id: str):
        """Track an order for fill detection via USER channel WebSocket."""
        user_channel = get_user_channel()
        if user_channel:
            user_channel.track_order(order_id)
        # Also track in orderbook manager as backup
        if self.ob_manager:
            self.ob_manager.track_order(order_id)
    
    def _untrack_order(self, order_id: str):
        """Stop tracking an order."""
        user_channel = get_user_channel()
        if user_channel:
            user_channel.untrack_order(order_id)
        if self.ob_manager:
            self.ob_manager.untrack_order(order_id)
        self._forget_order(order_id)

    def _switch_mode(self, new_mode: StrategyMode):
        """Switch strategy mode and log the transition."""
        if self.state.mode != new_mode:
            self.log.mode_change(self.state.mode.value, new_mode.value)
            self.state.mode = new_mode

    async def cancel_all_orders(self):
        """Cancel ALL open orders - use when stopping or switching markets."""
        logger.info("🧹 Cancelling all open orders...")
        try:
            # Cancel via CLOB API (catches any orders we lost track of)
            cancelled = await self.client.cancel_all_orders()
            logger.info(f"   Cancelled {cancelled} orders via API")
            
            # Clear local references
            self.state.trap_order_yes = None
            self.state.trap_order_no = None
            self.state.hedge_order = None
            self._placing_traps = False
            self._placing_hedge = False
        except Exception as e:
            logger.error(f"   Error cancelling orders: {e}")

    async def flatten_position(self):
        """
        Market sell any unhedged inventory on shutdown.
        
        If ΔQ > 0: We're long YES, sell YES at market
        If ΔQ < 0: We're long NO, sell NO at market
        """
        inv = self.state.inventory
        delta_q = inv.delta_q
        
        if abs(delta_q) < 0.01:
            logger.info("📊 Position already flat, nothing to sell")
            return
        
        logger.info(f"🔄 Flattening position: ΔQ={delta_q:.2f}")
        
        try:
            if delta_q > 0:
                # Long YES - sell YES tokens at market
                size = delta_q
                token_id = self.config.token_id_yes
                # Get best bid, default to 0.01 if orderbook unavailable
                best_bid = 0.01
                if self.state.orderbook_yes and self.state.orderbook_yes.best_bid:
                    best_bid = self.state.orderbook_yes.best_bid
                price = max(0.01, best_bid - 0.02)  # Slightly below bid to ensure fill
                logger.info(f"   Selling {size:.2f} YES @ market (bid ~{best_bid:.2f})")
                
                order = await self.client.place_market_order(
                    token_id=token_id,
                    side=Side.SELL,
                    size=size,
                    price=price,
                )
                
                if order:
                    logger.info(f"   ✅ Sold {size:.2f} YES - position flattened")
                    inv.q_yes -= size
                else:
                    logger.warning(f"   ⚠️ Market sell failed - position still open!")
                    
            else:
                # Long NO - sell NO tokens at market
                size = abs(delta_q)
                token_id = self.config.token_id_no
                # Get best bid, default to 0.01 if orderbook unavailable
                best_bid = 0.01
                if self.state.orderbook_no and self.state.orderbook_no.best_bid:
                    best_bid = self.state.orderbook_no.best_bid
                price = max(0.01, best_bid - 0.02)
                logger.info(f"   Selling {size:.2f} NO @ market (bid ~{best_bid:.2f})")
                
                order = await self.client.place_market_order(
                    token_id=token_id,
                    side=Side.SELL,
                    size=size,
                    price=price,
                )
                
                if order:
                    logger.info(f"   ✅ Sold {size:.2f} NO - position flattened")
                    inv.q_no -= size
                else:
                    logger.warning(f"   ⚠️ Market sell failed - position still open!")
                    
        except Exception as e:
            logger.error(f"   ❌ Flatten error: {e}")

    async def stop(self):
        # Cancel all orders before stopping
        await self.cancel_all_orders()
        
        # Flatten any open position
        await self.flatten_position()
        
        if self.ob_manager:
            await self.ob_manager.stop()
        self.state.mode = StrategyMode.STOPPED

    async def on_orderbook(self, outcome: Outcome, book: OrderBook):
        """Entry point for every book update (continuous)."""
        async with self._lock:
            if outcome == Outcome.YES:
                self.state.orderbook_yes = book
            else:
                self.state.orderbook_no = book

            # Wait until both books are present
            if not (self.state.orderbook_yes and self.state.orderbook_no):
                return

            await self._step()

    async def _step(self):
        inv = self.state.inventory
        yes_book = self.state.orderbook_yes
        no_book = self.state.orderbook_no
        
        # Log current market state periodically (every 5 seconds)
        now = datetime.now()
        if not hasattr(self, '_last_status_log') or (now - self._last_status_log).total_seconds() >= 5:
            self._last_status_log = now
            yes_bid = yes_book.best_bid if yes_book else None
            yes_ask = yes_book.best_ask if yes_book else None
            no_bid = no_book.best_bid if no_book else None
            no_ask = no_book.best_ask if no_book else None
            
            logger.info(f"📈 Market: YES bid/ask={yes_bid:.2f}/{yes_ask:.2f}, NO bid/ask={no_bid:.2f}/{no_ask:.2f}" if yes_bid and yes_ask and no_bid and no_ask else "📈 Market: Waiting for orderbook data...")
            logger.info(f"📦 Inventory: ΔQ={inv.delta_q:.2f}, Q_yes={inv.q_yes:.2f}, Q_no={inv.q_no:.2f}, μ_yes={inv.mu_yes:.4f}, μ_no={inv.mu_no:.4f}")
            logger.info(f"💰 P&L: Locked={inv.locked_profit:.4f} USDC, Rounds={inv.completed_rounds}")
            
            # Log active orders
            trap_yes_info = f"YES@{self.state.trap_order_yes.price:.2f}" if self.state.trap_order_yes else "None"
            trap_no_info = f"NO@{self.state.trap_order_no.price:.2f}" if self.state.trap_order_no else "None"
            hedge_info = f"{self.state.hedge_order.outcome.value}@{self.state.hedge_order.price:.2f}" if self.state.hedge_order else "None"
            logger.info(f"📋 Orders: Trap YES={trap_yes_info}, Trap NO={trap_no_info}, Hedge={hedge_info}")
            
            # Log time to expiry and market status
            time_left = self.config.time_until_expiry().total_seconds()
            # Check if market is in tradeable range (configured range)
            in_range = (yes_ask and no_ask and 
                        self.config.range_min <= yes_ask <= self.config.range_max and 
                        self.config.range_min <= no_ask <= self.config.range_max)
            range_status = "✅ In range" if in_range else f"⏸️ Waiting for {int(self.config.range_min*100)}-{int(self.config.range_max*100)}% range"
            logger.info(f"⏱️  Time to expiry: {time_left:.0f}s | Mode: {self.state.mode.value} | {range_status}")

        # Final exit: cancel everything, stop trading
        if is_in_final_exit(self.config):
            await self._final_exit()
            return

        # Expiry buffer: only hedge, no new traps
        if is_in_expiry_buffer(self.config):
            if not hasattr(self, '_expiry_buffer_logged') or not self._expiry_buffer_logged:
                logger.info(f"⚠️  Entered expiry buffer ({self.config.expiry_buffer_seconds}s) - only hedging, no new traps")
                self._expiry_buffer_logged = True
            if abs(inv.delta_q) > 0.01:
                if inv.delta_q > 0:
                    await self._mode_hedge(long_side=Outcome.YES)
                else:
                    await self._mode_hedge(long_side=Outcome.NO)
            return

        # Max exposure check: aggressive hedge only (no new traps)
        if exposure_exceeded(inv, self.config):
            self.log.emergency("EXPOSURE_CAP", inv.delta_q)
            logger.warning(f"🚨 EXPOSURE EXCEEDED: |ΔQ|={abs(inv.delta_q):.2f} > E_max={self.config.max_exposure}")
            # Eq 9: |ΔQ| > E_max triggers hedging (no extra aggression mode)
            if inv.delta_q > 0:
                await self._mode_hedge(long_side=Outcome.YES)
            else:
                await self._mode_hedge(long_side=Outcome.NO)
            return

        # Lock profits when approximately balanced
        if abs(inv.delta_q) < 0.01:
            inv.lock_profit(self.config.c_target)
                # Eq 3 invariant enforced via locked profit when balanced

        # Decide mode based on imbalance
        # IMPORTANT: If imbalance is small (< half of min order size), treat as balanced
        # to avoid ping-pong over-hedging that flips position back and forth
        MIN_HEDGE_THRESHOLD = 2.5  # Half of Polymarket minimum (5.0)
        
        if abs(inv.delta_q) < MIN_HEDGE_THRESHOLD:
            # Small imbalance - accept it and go back to placing traps
            # Over-hedging would flip us to the other side and create a loop
            if abs(inv.delta_q) >= 0.01:
                # Only log once when we accept the imbalance
                if not hasattr(self, '_accepted_imbalance') or not self._accepted_imbalance:
                    logger.info(f"   ℹ️ Accepting small imbalance (ΔQ={inv.delta_q:.2f}) - below hedge threshold {MIN_HEDGE_THRESHOLD}")
                    self._accepted_imbalance = True
            else:
                self._accepted_imbalance = False
            await self._mode_open()
        elif inv.delta_q > 0:
            self._accepted_imbalance = False
            await self._mode_hedge(long_side=Outcome.YES)
        else:
            self._accepted_imbalance = False
            await self._mode_hedge(long_side=Outcome.NO)

    async def _mode_open(self):
        """Mode A: Balanced inventory, place symmetric traps ONCE and wait for fill."""
        self._switch_mode(StrategyMode.OPEN)
        
        # Clear any stale hedge order from previous round
        self.state.hedge_order = None
        self._placing_hedge = False

        # If we already have BOTH traps placed, just wait - don't place more
        if self.state.trap_order_yes and self.state.trap_order_no:
            return  # Traps already in place, wait for fill
        
        # Guard against concurrent trap placement
        if self._placing_traps:
            return
        self._placing_traps = True
        
        try:
            yes_book = self.state.orderbook_yes
            no_book = self.state.orderbook_no
            if not yes_book or not no_book:
                return

            # Get market prices
            yes_ask = yes_book.best_ask or 0.0
            no_ask = no_book.best_ask or 0.0
            
            # Compute trap prices using Eq 7: π_limit = C_target - P_opposing_ask
            yes_trap = self.config.compute_trap_price(
                opposing_ask=no_ask,
                own_ask=yes_ask,
            )
            no_trap = self.config.compute_trap_price(
                opposing_ask=yes_ask,
                own_ask=no_ask,
            )
            
            # If both traps are None (market outside 40-60%), don't spam logs
            if yes_trap is None and no_trap is None:
                return

            # Log trap prices only when we can actually place them
            logger.info(f"🎯 Trap calc: YES trap = C_target({self.config.c_target}) - NO_ask({no_ask:.2f}) = {yes_trap}")
            logger.info(f"🎯 Trap calc: NO trap = C_target({self.config.c_target}) - YES_ask({yes_ask:.2f}) = {no_trap}")

            # Place YES trap (only if we don't have one)
            if yes_trap and not self.state.trap_order_yes:
                await self._place_trap(Outcome.YES, yes_trap)

            # Place NO trap (only if we don't have one)
            if no_trap and not self.state.trap_order_no:
                await self._place_trap(Outcome.NO, no_trap)
        finally:
            self._placing_traps = False

    async def _mode_hedge(self, long_side: Outcome):
        """
        Mode B: Exposed on one side, hedge the opposite leg.
        
        Places GTC limit order at hedge_price (Eq 8), sitting in the book.
        Per PDF: Always place the order, even if market ask > hedge_price.
        """
        # FIRST: Check guards before ANY operations to prevent duplicates
        # Skip if we already have a hedge order placed or are currently placing one
        if self.state.hedge_order and self.state.hedge_order.is_active:
            return  # Hedge already in place, wait for fill
        if self._placing_hedge:
            return  # Already placing hedge
        
        # Set flag immediately to block concurrent calls
        self._placing_hedge = True
        
        try:
            new_mode = StrategyMode.HEDGE_YES if long_side == Outcome.YES else StrategyMode.HEDGE_NO
            inv = self.state.inventory
            
            # Only log mode entry once (when actually switching)
            if self.state.mode != new_mode:
                self._switch_mode(new_mode)
                logger.info(f"🔄 HEDGE MODE: Long {long_side.value}, need to buy {Outcome.NO.value if long_side == Outcome.YES else Outcome.YES.value}")
                logger.info(f"   Imbalance: ΔQ={inv.delta_q:.2f}, VWAP_{long_side.value}={inv.mu_yes if long_side == Outcome.YES else inv.mu_no:.4f}")

            # Cancel traps on both sides while hedging (only if they exist)
            if self.state.trap_order_yes:
                trap = self.state.trap_order_yes
                self.state.trap_order_yes = None
                await self._cancel_trap(trap)
            if self.state.trap_order_no:
                trap = self.state.trap_order_no
                self.state.trap_order_no = None
                await self._cancel_trap(trap)

            yes_book = self.state.orderbook_yes
            no_book = self.state.orderbook_no
            if not yes_book or not no_book:
                return

            qty = abs(inv.delta_q)
            
            # Polymarket minimum order size is 5 tokens
            # If we have < 5, we need to hedge with 5 (over-hedge slightly)
            # If we have >= 5, use exact amount
            MIN_POLYMARKET_SIZE = 5.0
            needs_overhedge = qty < MIN_POLYMARKET_SIZE
            if needs_overhedge:
                # Can't place order smaller than 5, so hedge with minimum
                # This may slightly over-hedge but better than being stuck
                logger.info(f"   ⚠️ Small imbalance ({qty:.2f} tokens) → using minimum size {MIN_POLYMARKET_SIZE}")
                qty = MIN_POLYMARKET_SIZE

            if long_side == Outcome.YES:
                # Need to buy NO
                # Eq 8: π_hedge = C_target - μ_own
                hedge_price = self.config.compute_hedge_price(inv.mu_yes)
                market_ask = no_book.best_ask or 1.0
                logger.info(f"   Hedge price: C_target({self.config.c_target}) - μ_YES({inv.mu_yes:.4f}) = {hedge_price:.2f}")
                logger.info(f"   Market NO ask: {market_ask:.2f} | Our bid: {hedge_price:.2f} | {'WILL FILL' if hedge_price >= market_ask else 'WAITING'}")
                # Place GTC limit at hedge_price - sits in book until filled
                await self._place_hedge(Outcome.NO, hedge_price, qty)
            else:
                # Need to buy YES
                # Eq 8: π_hedge = C_target - μ_own
                hedge_price = self.config.compute_hedge_price(inv.mu_no)
                market_ask = yes_book.best_ask or 1.0
                logger.info(f"   Hedge price: C_target({self.config.c_target}) - μ_NO({inv.mu_no:.4f}) = {hedge_price:.2f}")
                logger.info(f"   Market YES ask: {market_ask:.2f} | Our bid: {hedge_price:.2f} | {'WILL FILL' if hedge_price >= market_ask else 'WAITING'}")
                await self._place_hedge(Outcome.YES, hedge_price, qty)
        except Exception as e:
            logger.error(f"   ❌ Hedge placement failed: {e}")
            raise
        finally:
            # Always reset the flag when done (success or failure)
            self._placing_hedge = False
    
    async def _final_exit(self):
        """Final exit: cancel all orders, stop trading."""
        if self.state.mode == StrategyMode.STOPPED:
            return
        
        self.log.mode_change(self.state.mode.value, "STOPPED")
        self._switch_mode(StrategyMode.STOPPED)
        
        # Cancel all orders
        if self.state.trap_order_yes:
            trap = self.state.trap_order_yes
            self.state.trap_order_yes = None
            await self._cancel_trap(trap)
            
        if self.state.trap_order_no:
            trap = self.state.trap_order_no
            self.state.trap_order_no = None
            await self._cancel_trap(trap)
            
        await self._cancel_hedge()
        
        self.log.emergency("FINAL_EXIT", self.state.inventory.delta_q)

    async def _place_hedge(self, outcome: Outcome, price: float, size: float):
        """
        Place a GTC limit order to hedge exposure.
        
        The order sits in the book until filled - we don't use IOC or market orders.
        """
        token = self.config.token_id_yes if outcome == Outcome.YES else self.config.token_id_no
        price = round(max(0.01, min(0.99, price)), 2)
        
        # Check if we already have a hedge order at this price
        if self.state.hedge_order and self.state.hedge_order.is_active:
            if (self.state.hedge_order.outcome == outcome and 
                abs(self.state.hedge_order.price - price) < 0.005):
                return  # Already have correct hedge order
            # Cancel old hedge order if price/side changed
            await self._cancel_hedge()
        
        order = await self.client.place_limit_order(
            token_id=token,
            side=Side.BUY,
            price=price,
            size=size,
            time_in_force="GTC",  # GTC - sit in book until filled
        )
        if order:
            self.log.order_placed("BUY", outcome.value, price, size, order.order_id)
            self._remember_order(order.order_id, outcome)
            self.state.hedge_order = order
            # Track this order for fill detection via USER channel
            self._track_order(order.order_id)

    async def _cancel_hedge(self):
        """Cancel the current hedge order."""
        order = self.state.hedge_order
        if order and order.is_active:
            self.state.hedge_order = None  # Clear immediately to prevent race conditions
            order_id = order.order_id
            await self.client.cancel_order(order_id)
            self.log.order_cancelled(order_id, "Hedge")
            # Untrack the cancelled order
            self._untrack_order(order_id)

    async def _place_trap(self, outcome: Outcome, price: float):
        """Place a single trap order (no updates, just place once)."""
        token = self.config.token_id_yes if outcome == Outcome.YES else self.config.token_id_no
        price = round(max(0.01, min(0.99, price)), 2)

        order = await self.client.place_limit_order(
            token_id=token,
            side=Side.BUY,
            price=price,
            size=self.config.trap_order_size,
            time_in_force="GTC",
        )
        if order:
            self.log.order_placed("BUY", outcome.value, price, self.config.trap_order_size, order.order_id)
            self._remember_order(order.order_id, outcome)
            if outcome == Outcome.YES:
                self.state.trap_order_yes = order
            else:
                self.state.trap_order_no = order
            # Track this order for fill detection via USER channel
            self._track_order(order.order_id)
    
    async def _cancel_trap(self, order: Optional[LiveOrder]):
        """Cancel a trap order."""
        if order and order.is_active:
            await self.client.cancel_order(order.order_id)
            self.log.order_cancelled(order.order_id, "Trap")
            # Untrack the cancelled order
            self._untrack_order(order.order_id)

    # ---------------------------------------------------------
    # Fill Handling (called when order fills are detected)
    # ---------------------------------------------------------
    def on_fill(self, order_id: str, outcome: Outcome, price: float, size: float):
        """
        Handle a fill event from WebSocket.
        
        Updates inventory and clears the filled order reference.
        Triggers immediate strategy step to react (e.g. hedge).
        """
        inv = self.state.inventory
        
        # Determine what type of order was filled AND validate the outcome matches
        order_type = "UNKNOWN"
        expected_outcome = None
        
        if self.state.trap_order_yes and self.state.trap_order_yes.order_id == order_id:
            order_type = "TRAP_YES"
            expected_outcome = Outcome.YES
        elif self.state.trap_order_no and self.state.trap_order_no.order_id == order_id:
            order_type = "TRAP_NO"
            expected_outcome = Outcome.NO
        elif self.state.hedge_order and self.state.hedge_order.order_id == order_id:
            order_type = "HEDGE"
            # Hedge outcome depends on which side we're hedging
            if self.state.mode == StrategyMode.HEDGE_YES:
                expected_outcome = Outcome.NO  # Long YES, buying NO
            else:
                expected_outcome = Outcome.YES  # Long NO, buying YES
        
        # CRITICAL: Ignore unknown fills - they're likely stale orders from previous rounds
        if order_type == "UNKNOWN":
            logger.warning(f"⚠️ Ignoring fill from unknown/stale order: {order_id[:16]}...")
            logger.warning(f"   This may be a leftover order from a previous round")
            return  # Don't update inventory for unknown orders!
        
        # Prefer our locally remembered intent if present (disambiguates asset_id issues)
        mapped_outcome = self._order_outcome.get(order_id)
        if mapped_outcome and outcome != mapped_outcome:
            logger.info(f"   ℹ️ Using locally stored outcome {mapped_outcome.value} for order {order_id[:16]}... (WS reported {outcome.value})")
            outcome = mapped_outcome

        # Validate outcome matches what we expected
        if expected_outcome and outcome != expected_outcome:
            logger.warning(f"⚠️ Fill outcome mismatch! Expected {expected_outcome.value}, got {outcome.value}")
            logger.warning(f"   Order type: {order_type}, Order ID: {order_id[:16]}...")
            logger.warning(f"   This may indicate a tracking bug - using actual outcome from fill")
        
        logger.info(f"")
        logger.info(f"{'='*60}")
        logger.info(f"🎉 FILL DETECTED: {order_type}")
        logger.info(f"   Bought {size:.2f} {outcome.value} @ ${price:.2f}")
        logger.info(f"   Cost: ${price * size:.2f} USDC")
        
        inv.record_fill(outcome, Side.BUY, price, size)
        self.log.order_filled("BUY", outcome.value, price, size)
        
        # Clear the specific order reference that matched the order_id (source-of-truth)
        if order_type == "TRAP_YES":
            self.state.trap_order_yes = None
        elif order_type == "TRAP_NO":
            self.state.trap_order_no = None
        elif order_type == "HEDGE":
            self.state.hedge_order = None
        self._forget_order(order_id)

        # Now log the position based on ACTUAL fill outcome (trust the fill asset_id)
        if outcome == Outcome.YES:
            logger.info(f"   → Now LONG YES, need to hedge with NO")
            logger.info(f"   → Inventory: ΔQ={inv.delta_q:.2f}, μ_YES={inv.mu_yes:.4f}")
        elif outcome == Outcome.NO:
            logger.info(f"   → Now LONG NO, need to hedge with YES")
            logger.info(f"   → Inventory: ΔQ={inv.delta_q:.2f}, μ_NO={inv.mu_no:.4f}")
        
        # Check if this was a hedge fill that balanced us
        if order_type == "HEDGE":
            if abs(inv.delta_q) < 0.5:  # Use 0.5 tolerance for "balanced"
                logger.info(f"   → Hedge complete! Inventory approximately balanced.")
                # Lock profit
                inv.lock_profit(self.config.c_target)
                logger.info(f"   💵 PROFIT LOCKED: Round #{inv.completed_rounds}, Total locked: ${inv.locked_profit:.4f}")
            else:
                logger.info(f"   → Hedge partially filled, still unbalanced: ΔQ={inv.delta_q:.2f}")
        
        logger.info(f"{'='*60}")
        logger.info(f"")
            
        # Trigger strategy step immediately on the event loop
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._step()))

    def get_active_order_ids(self) -> set:
        """Return set of active order IDs for fill detection."""
        order_ids = set()
        if self.state.trap_order_yes and self.state.trap_order_yes.is_active:
            order_ids.add(self.state.trap_order_yes.order_id)
        if self.state.trap_order_no and self.state.trap_order_no.is_active:
            order_ids.add(self.state.trap_order_no.order_id)
        if self.state.hedge_order and self.state.hedge_order.is_active:
            order_ids.add(self.state.hedge_order.order_id)
        return order_ids


def build_engine() -> StrategyEngine:
    return StrategyEngine()
