"""
Polymarket Box Spread Market Maker - Market Discovery

Automatically discovers BTC 15-minute markets (Up/Down) from Polymarket's Gamma API
and selects the next upcoming one.
"""

import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
import httpx

from config import Config, get_config
from logger import get_logger

logger = get_logger(__name__)

# Gamma API base URL
GAMMA_API_URL = "https://gamma-api.polymarket.com"


class MarketDiscovery:
    """
    Discovers BTC 15-minute Up/Down markets from Polymarket's Gamma API.
    
    These markets have:
    - Slug pattern: btc-updown-15m-{timestamp}
    - Outcomes: ["Up", "Down"] (not YES/NO)
    - 15-minute windows throughout the trading day
    """
    
    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self._client = httpx.AsyncClient(timeout=30.0)
    
    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()
    
    async def find_next_btc_15min_market(self) -> Optional[Dict[str, Any]]:
        """
        Find the next upcoming BTC 15-minute market that's accepting orders.
        
        Returns:
            Market dict with:
                - condition_id: str
                - token_id_up: str (for "Up" outcome)
                - token_id_down: str (for "Down" outcome)
                - end_date: datetime
                - title: str
                - slug: str
            or None if no suitable market found.
        """
        logger.info("Searching for BTC 15-minute markets via Gamma API...")
        
        try:
            # Use current time for filtering
            now = datetime.now(timezone.utc)
            end_date_min = now.isoformat().replace("+00:00", "Z")
            
            # Fetch markets directly, ordered by end date
            response = await self._client.get(
                f"{GAMMA_API_URL}/markets",
                params={
                    "end_date_min": end_date_min,
                    "order": "endDate",
                    "ascending": "true",
                    "closed": "false",
                    "limit": 100,
                }
            )
            response.raise_for_status()
            markets = response.json()
            
            logger.info(f"Fetched {len(markets)} markets from Gamma API")
            
            # Filter for BTC 15-minute markets
            btc_15m_markets = self._filter_btc_15min_markets(markets)
            logger.info(f"Found {len(btc_15m_markets)} BTC 15-min markets")
            
            if not btc_15m_markets:
                logger.warning("No BTC 15-minute markets found")
                return None
            
            # Select the next upcoming market that's accepting orders
            selected = self._select_next_market(btc_15m_markets)
            
            return selected
            
        except Exception as e:
            logger.error(f"Error fetching markets from Gamma API: {e}")
            return None
    
    def _filter_btc_15min_markets(self, markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter markets for BTC 15-minute Up/Down markets."""
        filtered = []
        
        for market in markets:
            slug = market.get("slug", "")
            
            # Match the btc-updown-15m pattern
            if "btc-updown-15m" not in slug:
                continue
            
            # Skip if not accepting orders
            if not market.get("acceptingOrders"):
                continue
            
            # Parse token IDs
            clob_token_ids = market.get("clobTokenIds", "[]")
            if isinstance(clob_token_ids, str):
                try:
                    clob_token_ids = json.loads(clob_token_ids)
                except:
                    continue
            
            if len(clob_token_ids) < 2:
                continue
            
            # Parse outcomes to map tokens correctly
            outcomes = market.get("outcomes", "[]")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except:
                    outcomes = ["Up", "Down"]
            
            # Map token IDs to outcomes (Up/Down)
            token_id_up = None
            token_id_down = None
            for i, outcome in enumerate(outcomes):
                if i < len(clob_token_ids):
                    if outcome.lower() == "up":
                        token_id_up = clob_token_ids[i]
                    elif outcome.lower() == "down":
                        token_id_down = clob_token_ids[i]
            
            if not token_id_up or not token_id_down:
                # Fallback: assume first is Up, second is Down
                token_id_up = clob_token_ids[0]
                token_id_down = clob_token_ids[1]
            
            # Parse end date
            end_date_str = market.get("endDate")
            if not end_date_str:
                continue
            
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except:
                continue
            
            filtered.append({
                "condition_id": market.get("conditionId"),
                "title": market.get("question", slug), # Use question as title if available
                "slug": slug,
                "end_date": end_date,
                "end_date_iso": end_date_str,
                "token_id_up": token_id_up,
                "token_id_down": token_id_down,
                "outcomes": outcomes,
                "accepting_orders": market.get("acceptingOrders"),
                "minimum_order_size": market.get("orderMinSize", 5),
                "tick_size": market.get("orderPriceMinTickSize", 0.01),
            })
        
        return filtered
    
    def _select_next_market(self, markets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Select the next upcoming market.
        
        Selects the market with:
        - End date in the future (not expired)
        - Currently accepting orders
        - Soonest end date
        - At least 2 minutes remaining (to avoid immediate final exit)
        """
        now = datetime.now(timezone.utc)
        min_time_remaining = timedelta(seconds=120)  # Skip markets with < 2 min remaining
        
        # Filter for unexpired, accepting orders, with enough time to trade
        upcoming = [
            m for m in markets
            if m["end_date"] > now + min_time_remaining and m.get("accepting_orders")
        ]
        
        if not upcoming:
            logger.warning("No upcoming markets with sufficient time remaining")
            return None
        
        # Sort by end_date (soonest first)
        upcoming.sort(key=lambda m: m["end_date"])
        
        return upcoming[0]
    
    async def get_market_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch a specific market by its slug."""
        try:
            response = await self._client.get(
                f"{GAMMA_API_URL}/events/slug/{slug}"
            )
            response.raise_for_status()
            event = response.json()
            
            if not event:
                return None
            
            # Process the event
            markets = self._filter_btc_15min_events([event])
            return markets[0] if markets else None
            
        except Exception as e:
            logger.error(f"Error fetching market {slug}: {e}")
            return None


async def discover_market(config: Optional[Config] = None) -> Tuple[str, str, str]:
    """
    Discover the next BTC 15-minute market.
    
    Returns:
        Tuple of (condition_id, token_id_up, token_id_down)
        
    Raises:
        RuntimeError: If no suitable market found
    """
    discovery = MarketDiscovery(config)
    try:
        market = await discovery.find_next_btc_15min_market()
        
        if not market:
            raise RuntimeError("No BTC 15-minute market found")
        
        return (
            market["condition_id"],
            market["token_id_up"],
            market["token_id_down"],
        )
    finally:
        await discovery.close()


# CLI test
if __name__ == "__main__":
    async def main():
        from config import init_config
        cfg = init_config()
        
        discovery = MarketDiscovery(cfg)
        try:
            market = await discovery.find_next_btc_15min_market()
            
            if market:
                print(f"\n=== Selected Market ===")
                print(f"Title: {market['title']}")
                print(f"Slug: {market['slug']}")
                print(f"End Date: {market['end_date']}")
                print(f"Condition ID: {market['condition_id']}")
                print(f"Up Token: {market['token_id_up']}")
                print(f"Down Token: {market['token_id_down']}")
                print(f"Outcomes: {market['outcomes']}")
            else:
                print("No suitable market found")
        finally:
            await discovery.close()
    
    asyncio.run(main())

