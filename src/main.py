"""
Entry point for the Polymarket box-spread market maker.

Automatically discovers BTC 15-minute markets and runs continuously,
switching to the next market after each expiry.
"""

import asyncio
import signal
import sys

from config import init_config
from logger import setup_logging, get_logger
from polymarket_client import init_client
from orderbook_manager import init_orderbook_manager
from user_channel import init_user_channel
from market_discovery import MarketDiscovery
from strategy_engine import StrategyEngine


async def discover_and_set_market(cfg, log):
    """
    Discover the next BTC 15-minute market and update config.
    
    Note: BTC 15-min markets use "Up"/"Down" outcomes, which we map to YES/NO
    for the box spread strategy (Up = YES, Down = NO).
    
    Returns:
        dict: Market info with title, end_date, etc.
    """
    log.info("Discovering BTC 15-minute markets...")
    
    discovery = MarketDiscovery(cfg)
    try:
        market = await discovery.find_next_btc_15min_market()
        
        if not market:
            raise RuntimeError("No BTC 15-minute market found on Polymarket")
        
        # Update config with discovered market IDs
        # Map Up -> YES, Down -> NO for box spread strategy
        cfg.condition_id = market["condition_id"]
        cfg.token_id_yes = market["token_id_up"]    # Up outcome
        cfg.token_id_no = market["token_id_down"]   # Down outcome
        cfg.active_market_end_date = market["end_date"]
        
        log.info(f"Selected market: {market['title']}")
        log.info(f"  Slug: {market['slug']}")
        log.info(f"  Condition ID: {cfg.condition_id}")
        log.info(f"  End time: {market['end_date']}")
        log.info(f"  Up (YES) token: {cfg.token_id_yes[:32]}...")
        log.info(f"  Down (NO) token: {cfg.token_id_no[:32]}...")
        
        return market
    finally:
        await discovery.close()


async def run_single_market(cfg, client, log, stop_event):
    """
    Run the strategy on a single market until expiry or stop signal.
    
    Returns:
        bool: True if should continue to next market, False if stopped
    """
    # CRITICAL: Cancel any stale orders from previous markets before starting
    log.info("🧹 Cleaning up any stale orders before starting...")
    try:
        cancelled = await client.cancel_all_orders()
        if cancelled and cancelled > 0:
            log.info(f"   Cancelled {cancelled} stale orders")
        else:
            log.info("   No stale orders to cancel")
    except Exception as e:
        log.warning(f"   Could not cancel stale orders: {e}")
    
    # Initialize orderbook manager (MARKET channel - no auth required)
    ob_manager = init_orderbook_manager(cfg)
    
    # Build strategy engine
    engine = StrategyEngine(config=cfg, client=client, ob_manager=ob_manager)
    
    # Get API credentials for authenticated WebSocket
    api_key, api_secret, api_passphrase = client.get_api_credentials()
    
    # Initialize USER channel for fill detection (authenticated)
    user_channel = init_user_channel(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        config=cfg,
        on_fill=engine.on_fill,
    )
    
    # Start components
    user_channel.start()
    await engine.start()
    
    log.info("Strategy engine running on current market...")
    
    # Wait until stopped or market expires
    try:
        while not stop_event.is_set():
            # Check if market has expired (final exit triggered)
            if cfg.is_in_final_exit():
                log.info("Market entering final exit window, preparing for next market...")
                break
            
            # Sleep briefly then check again
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    
    # Cleanup
    log.info("Stopping current market session...")
    user_channel.stop()
    await engine.stop()
    
    # Return whether to continue (not stopped by user)
    return not stop_event.is_set()


async def main():
    # Load config and logger
    cfg = init_config()
    setup_logging(level=cfg.log_level)
    log = get_logger(__name__)

    log.info("=" * 60)
    log.info("Polymarket Box-Spread Market Maker - BTC 15min Auto-Discovery")
    log.info("=" * 60)
    log.info(f"Wallet: {cfg.wallet_address}")
    log.info(f"C_TARGET={cfg.c_target:.4f}, PROFIT_MARGIN={cfg.profit_margin:.4f}")
    log.info(f"MAX_EXPOSURE={cfg.max_exposure}, TRAP_SIZE={cfg.trap_order_size}")

    # Initialize CLOB client (once, reused across markets)
    client = await init_client(cfg)
    log.info("CLOB client initialized")

    # Graceful shutdown handling
    stop_event = asyncio.Event()

    def _handle_signal():
        log.info("Received stop signal, shutting down...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass

    # Main loop: discover market -> trade -> repeat
    market_count = 0
    while not stop_event.is_set():
        market_count += 1
        log.info(f"\n{'='*60}")
        log.info(f"Market Session #{market_count}")
        log.info(f"{'='*60}")
        
        try:
            # Discover next market
            market = await discover_and_set_market(cfg, log)
            
            # Run strategy on this market
            should_continue = await run_single_market(cfg, client, log, stop_event)
            
            if not should_continue:
                break
            
            # Immediately discover next market (no delay)
            # The market discovery will find the next upcoming market
            
        except Exception as e:
            log.error(f"Error in market session: {e}")
            if not stop_event.is_set():
                log.info("Retrying in 10 seconds...")
                await asyncio.sleep(10)

    # Final cleanup
    await client.close()
    log.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
