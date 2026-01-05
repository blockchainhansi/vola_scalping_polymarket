"""
Polymarket Box Spread Market Maker - Configuration Module

Loads and validates all environment variables and hyperparameters.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv


@dataclass
class Config:
    """Configuration container for the market maker strategy."""
    
    # API & Network
    private_key: str
    clob_http_url: str
    clob_ws_url: str
    rpc_url: str
    chain_id: int
    
    # Strategy Hyperparameters (Simplified)
    profit_margin: float  # xi
    c_target: float  # 1.0 - xi
    max_exposure: float  # E_max
    trap_order_size: float
    min_order_size: float
    range_min: float  # Lower bound of active trading range (e.g. 0.40)
    range_max: float  # Upper bound of active trading range (e.g. 0.60)
    
    # Market Selection (15-minute markets)
    condition_id: str
    token_id_yes: str
    token_id_no: str
    market_duration_minutes: int
    
    # Safety Parameters
    expiry_buffer_seconds: int
    final_exit_seconds: int
    emergency_cooldown: int
    
    # Operational
    log_level: str
    state_file: str
    state_persist_interval: int
    ws_reconnect_delay: int
    order_refresh_interval: float
    
    # Derived wallet address (computed after loading)
    wallet_address: str = field(default="")
    
    # Active market end date (set by discovery)
    active_market_end_date: Optional[datetime] = None
    
    def compute_risk_premium(self, probability: float) -> Optional[float]:
        """
        Compute the risk premium R(p) for a given probability.

        Simplified: Only trading in configured range with zero risk premium.
        
        Returns:
            float: Always 0.0 in the active range
            None: If probability outside active range
        """
        if probability >= self.range_min and probability <= self.range_max:
            return 0.0
        else:
            # Outside active range - do not quote
            return None
    
    def compute_trap_price(
        self, 
        opposing_ask: float, 
        own_ask: float
    ) -> Optional[float]:
        """
        Compute the trap (limit bid) price for one side.
        
        Simplified: π_limit = C_target - P_opposing_ask (no risk premium)
        Only active in configured range for both sides.
        
        Args:
            opposing_ask: Best ask price of the opposing outcome
            own_ask: Best ask price of our outcome
            
        Returns:
            float: The limit price to place our trap bid
            None: If outside active range (don't place order)
        """
        # Check if both sides are in active range
        if own_ask < self.range_min or own_ask > self.range_max or opposing_ask < self.range_min or opposing_ask > self.range_max:
            return None  # Outside active range
        
        # Simple formula: π_limit = C_target - P_opposing_ask
        trap_price = self.c_target - opposing_ask
        
        # Ensure price is valid (positive and <= 0.99)
        if trap_price <= 0.01 or trap_price > 0.99:
            return None
            
        return round(trap_price, 2)
    
    def compute_hedge_price(self, own_vwap: float) -> float:
        """
        Compute the maximum price we can pay for the hedge leg.
        
        Eq 8: π_hedge = C_target - μ_own
        
        Args:
            own_vwap: Our VWAP for the side we're already long
            
        Returns:
            float: Maximum price to pay for the hedge
        """
        hedge_price = self.c_target - own_vwap
        return round(max(0.01, min(0.99, hedge_price)), 2)
    
    def get_market_expiry(self) -> datetime:
        """
        Calculate market expiry time for 15-minute markets.
        Uses the discovered market end date if available, otherwise
        calculates the next quarter-hour boundary.

        Returns:
            datetime: When the market expires
        """
        if self.active_market_end_date:
            # Ensure timezone awareness compatibility
            if self.active_market_end_date.tzinfo is None:
                return self.active_market_end_date
            # If active_market_end_date is timezone-aware (e.g. UTC), 
            # we should return a naive datetime if the rest of the app expects naive,
            # or ensure comparisons are consistent.
            # The app uses datetime.now() which is naive local time usually.
            # Let's convert to naive local time for consistency with datetime.now()
            return self.active_market_end_date.astimezone().replace(tzinfo=None)

        now = datetime.now()
        minute = (now.minute // 15) * 15
        current_slot = now.replace(minute=minute, second=0, microsecond=0)
        next_expiry = current_slot + timedelta(minutes=15)
        return next_expiry
    
    def time_until_expiry(self) -> timedelta:
        """Get time remaining until market expiry."""
        return self.get_market_expiry() - datetime.now()
    
    def is_in_expiry_buffer(self) -> bool:
        """Check if we're within the expiry buffer (stop placing traps)."""
        return self.time_until_expiry().total_seconds() <= self.expiry_buffer_seconds
    
    def is_in_final_exit(self) -> bool:
        """Check if we're in final exit window (cancel all, stop trading)."""
        return self.time_until_expiry().total_seconds() <= self.final_exit_seconds


def load_config(env_path: Optional[str] = None) -> Config:
    """
    Load configuration from environment variables.
    
    Args:
        env_path: Optional path to .env file. If None, looks in current dir.
        
    Returns:
        Config: Validated configuration object
        
    Raises:
        ValueError: If required configuration is missing or invalid
    """
    # Load .env file
    if env_path:
        load_dotenv(env_path)
    else:
        # Try current directory, then src directory
        if Path(".env").exists():
            load_dotenv(".env")
        elif Path("src/.env").exists():
            load_dotenv("src/.env")
        else:
            load_dotenv()  # Load from default locations
    
    def get_required(key: str) -> str:
        value = os.getenv(key)
        if not value:
            raise ValueError(f"Required environment variable {key} is not set")
        return value
    
    def get_float(key: str, default: float) -> float:
        value = os.getenv(key)
        if value is None:
            return default
        return float(value)
    
    def get_int(key: str, default: int) -> int:
        value = os.getenv(key)
        if value is None:
            return default
        return int(value)
    
    def get_str(key: str, default: str) -> str:
        return os.getenv(key, default)
    
    # Load profit margin and compute c_target
    profit_margin = get_float("PROFIT_MARGIN", 0.02)
    c_target_override = os.getenv("C_TARGET")
    c_target = float(c_target_override) if c_target_override else (1.0 - profit_margin)
    
    # Market IDs are optional - can be discovered automatically
    condition_id = get_str("CONDITION_ID", "")
    token_id_yes = get_str("TOKEN_ID_YES", "")
    token_id_no = get_str("TOKEN_ID_NO", "")
    
    config = Config(
        # API & Network
        private_key=get_required("PRIVATE_KEY"),
        clob_http_url=get_str("CLOB_HTTP_URL", "https://clob.polymarket.com"),
        clob_ws_url=get_str("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws"),
        rpc_url=get_str("RPC_URL", ""),
        chain_id=get_int("CHAIN_ID", 137),
        
        # Strategy Hyperparameters (Simplified)
        profit_margin=profit_margin,
        c_target=c_target,
        max_exposure=get_float("MAX_EXPOSURE", 100.0),
        trap_order_size=get_float("TRAP_ORDER_SIZE", 10.0),
        min_order_size=get_float("MIN_ORDER_SIZE", 1.0),
        range_min=get_float("RANGE_MIN", 0.40),
        range_max=get_float("RANGE_MAX", 0.60),
        
        # Market Selection (15-minute markets) - can be empty for auto-discovery
        condition_id=condition_id,
        token_id_yes=token_id_yes,
        token_id_no=token_id_no,
        market_duration_minutes=get_int("MARKET_DURATION_MINUTES", 15),
        
        # Safety Parameters
        expiry_buffer_seconds=get_int("EXPIRY_BUFFER_SECONDS", 60),
        final_exit_seconds=get_int("FINAL_EXIT_SECONDS", 10),
        emergency_cooldown=get_int("EMERGENCY_COOLDOWN", 30),
        
        # Operational
        log_level=get_str("LOG_LEVEL", "INFO"),
        state_file=get_str("STATE_FILE", "mm_state.json"),
        state_persist_interval=get_int("STATE_PERSIST_INTERVAL", 30),
        ws_reconnect_delay=get_int("WS_RECONNECT_DELAY", 5),
        order_refresh_interval=get_float("ORDER_REFRESH_INTERVAL", 0.0),
    )
    
    # Derive wallet address from private key
    try:
        from eth_account import Account
        account = Account.from_key(config.private_key)
        config.wallet_address = account.address
    except Exception as e:
        raise ValueError(f"Invalid private key: {e}")
    
    # Validate configuration
    if config.profit_margin <= 0 or config.profit_margin >= 1:
        raise ValueError(f"PROFIT_MARGIN must be between 0 and 1, got {config.profit_margin}")
    
    if config.max_exposure <= 0:
        raise ValueError(f"MAX_EXPOSURE must be positive, got {config.max_exposure}")
    
    if config.c_target <= 0 or config.c_target >= 1:
        raise ValueError(f"C_TARGET must be between 0 and 1, got {config.c_target}")
    
    return config


# Singleton config instance
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def init_config(env_path: Optional[str] = None) -> Config:
    """Initialize the global configuration from a specific path."""
    global _config
    _config = load_config(env_path)
    return _config
