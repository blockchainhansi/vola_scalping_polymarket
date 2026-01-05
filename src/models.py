"""
Polymarket Box Spread Market Maker - Data Models

Pydantic models for inventory state, orders, and orderbook data.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any
import json
from pathlib import Path


class Side(str, Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


class Outcome(str, Enum):
    """Binary market outcome."""
    YES = "YES"
    NO = "NO"


class OrderStatus(str, Enum):
    """Order lifecycle status."""
    PENDING = "PENDING"
    LIVE = "LIVE"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class StrategyMode(str, Enum):
    """Current strategy mode."""
    OPEN = "OPEN"  # Mode A: Balanced, placing traps
    HEDGE_YES = "HEDGE_YES"  # Mode B: Long YES, need to buy NO
    HEDGE_NO = "HEDGE_NO"  # Mode B: Long NO, need to buy YES
    EMERGENCY = "EMERGENCY"  # Exposure exceeded, closing positions
    COOLDOWN = "COOLDOWN"  # After emergency, waiting to resume
    STOPPED = "STOPPED"  # Strategy stopped (manual or expiry)


@dataclass
class OrderBookLevel:
    """Single level in the order book."""
    price: float
    size: float


@dataclass
class OrderBook:
    """Order book snapshot for one outcome."""
    asset_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    
    @property
    def best_bid(self) -> Optional[float]:
        """Best bid price (highest)."""
        if not self.bids:
            return None
        return max(level.price for level in self.bids)
    
    @property
    def best_ask(self) -> Optional[float]:
        """Best ask price (lowest)."""
        if not self.asks:
            return None
        return min(level.price for level in self.asks)
    
    @property
    def mid_price(self) -> Optional[float]:
        """Mid price."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2
    
    @property
    def spread(self) -> Optional[float]:
        """Bid-ask spread."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid
    
    def get_best_bid_level(self) -> Optional[OrderBookLevel]:
        """Get the best bid level."""
        if not self.bids:
            return None
        return max(self.bids, key=lambda x: x.price)
    
    def get_best_ask_level(self) -> Optional[OrderBookLevel]:
        """Get the best ask level."""
        if not self.asks:
            return None
        return min(self.asks, key=lambda x: x.price)


@dataclass
class LiveOrder:
    """Tracks a live order on the exchange."""
    order_id: str
    asset_id: str
    outcome: Outcome
    side: Side
    price: float
    size: float
    filled_size: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    @property
    def remaining_size(self) -> float:
        return self.size - self.filled_size
    
    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.LIVE, OrderStatus.PARTIALLY_FILLED)


@dataclass
class InventoryState:
    """
    Portfolio inventory state.
    
    S_t = {Q_yes, C_yes, Q_no, C_no}
    
    where:
    - Q_i: Total quantity of shares held for outcome i
    - C_i: Total cost basis (USD spent) for outcome i
    """
    # YES side
    q_yes: float = 0.0  # Quantity of YES shares
    c_yes: float = 0.0  # Cost basis for YES (total USD spent)
    
    # NO side
    q_no: float = 0.0  # Quantity of NO shares
    c_no: float = 0.0  # Cost basis for NO (total USD spent)
    
    # Locked profit (completed box spreads)
    locked_profit: float = 0.0
    locked_quantity: float = 0.0  # min(Q_yes, Q_no) at lock time
    completed_rounds: int = 0  # Number of completed box spread rounds
    
    # Tracking
    total_trades: int = 0
    total_volume: float = 0.0
    
    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    @property
    def mu_yes(self) -> float:
        """VWAP for YES position."""
        if self.q_yes == 0:
            return 0.0
        return self.c_yes / self.q_yes
    
    @property
    def mu_no(self) -> float:
        """VWAP for NO position."""
        if self.q_no == 0:
            return 0.0
        return self.c_no / self.q_no
    
    @property
    def delta_q(self) -> float:
        """Inventory imbalance: ΔQ = Q_yes - Q_no"""
        # Eq 6
        return self.q_yes - self.q_no
    
    @property
    def combined_vwap(self) -> float:
        """Combined VWAP: μ_yes + μ_no"""
        return self.mu_yes + self.mu_no
    
    @property
    def is_balanced(self) -> bool:
        """Check if inventory is approximately balanced."""
        return abs(self.delta_q) < 0.01
    
    @property
    def potential_profit(self) -> float:
        """
        Potential profit if we could close the box at current VWAPs.
        P_lock = min(Q_yes, Q_no) × (1.0 - combined_vwap)
        """
        lockable = min(self.q_yes, self.q_no)
        if lockable == 0 or self.combined_vwap >= 1.0:
            return 0.0
        return lockable * (1.0 - self.combined_vwap)
    
    def record_fill(self, outcome: Outcome, side: Side, price: float, size: float):
        """
        Record a fill and update inventory.
        
        Args:
            outcome: YES or NO
            side: BUY or SELL
            price: Fill price
            size: Fill size (tokens)
        """
        self.total_trades += 1
        self.total_volume += price * size
        self.updated_at = datetime.now()
        
        if outcome == Outcome.YES:
            if side == Side.BUY:
                self.c_yes += price * size
                self.q_yes += size
            else:  # SELL
                # Reduce position, proportionally reduce cost basis
                if self.q_yes > 0:
                    avg_cost = self.c_yes / self.q_yes
                    self.c_yes -= avg_cost * min(size, self.q_yes)
                    self.q_yes = max(0, self.q_yes - size)
        else:  # NO
            if side == Side.BUY:
                self.c_no += price * size
                self.q_no += size
            else:  # SELL
                if self.q_no > 0:
                    avg_cost = self.c_no / self.q_no
                    self.c_no -= avg_cost * min(size, self.q_no)
                    self.q_no = max(0, self.q_no - size)
    
    def lock_profit(self, c_target: float):
        """
        Lock in profit from completed box spreads.
        
        When we have balanced inventory (Q_yes ≈ Q_no), the profit is locked.
        Box invariant uses C_target = 1 - profit_margin (Eq 3).
        """
        lockable = min(self.q_yes, self.q_no)
        if lockable <= self.locked_quantity:
            return  # Nothing new to lock
        
        new_locked = lockable - self.locked_quantity
        profit_per_share = 1.0 - self.combined_vwap
        
        if profit_per_share > 0:
            self.locked_profit += new_locked * profit_per_share
            self.locked_quantity = lockable
            self.completed_rounds += 1
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for persistence."""
        return {
            "q_yes": self.q_yes,
            "c_yes": self.c_yes,
            "q_no": self.q_no,
            "c_no": self.c_no,
            "locked_profit": self.locked_profit,
            "locked_quantity": self.locked_quantity,
            "total_trades": self.total_trades,
            "total_volume": self.total_volume,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InventoryState":
        """Deserialize from dictionary."""
        return cls(
            q_yes=data.get("q_yes", 0.0),
            c_yes=data.get("c_yes", 0.0),
            q_no=data.get("q_no", 0.0),
            c_no=data.get("c_no", 0.0),
            locked_profit=data.get("locked_profit", 0.0),
            locked_quantity=data.get("locked_quantity", 0.0),
            total_trades=data.get("total_trades", 0),
            total_volume=data.get("total_volume", 0.0),
            created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.now(),
            updated_at=datetime.fromisoformat(data["updated_at"]) if "updated_at" in data else datetime.now(),
        )
    
    def save(self, filepath: str):
        """Save state to JSON file."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
    
    @classmethod
    def load(cls, filepath: str) -> "InventoryState":
        """Load state from JSON file."""
        path = Path(filepath)
        if not path.exists():
            return cls()
        with open(path, "r") as f:
            data = json.load(f)
        return cls.from_dict(data)


@dataclass
class StrategyState:
    """Overall strategy state."""
    mode: StrategyMode = StrategyMode.STOPPED
    inventory: InventoryState = field(default_factory=InventoryState)
    
    # Active orders
    trap_order_yes: Optional[LiveOrder] = None
    trap_order_no: Optional[LiveOrder] = None
    hedge_order: Optional[LiveOrder] = None
    
    # Order books (updated continuously)
    orderbook_yes: Optional[OrderBook] = None
    orderbook_no: Optional[OrderBook] = None
    
    # Timing
    started_at: Optional[datetime] = None
    emergency_at: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None
    
    # Market info
    market_expiry: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for persistence."""
        return {
            "mode": self.mode.value,
            "inventory": self.inventory.to_dict(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "emergency_at": self.emergency_at.isoformat() if self.emergency_at else None,
            "market_expiry": self.market_expiry.isoformat() if self.market_expiry else None,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StrategyState":
        """Deserialize from dictionary."""
        state = cls()
        state.mode = StrategyMode(data.get("mode", "STOPPED"))
        state.inventory = InventoryState.from_dict(data.get("inventory", {}))
        if data.get("started_at"):
            state.started_at = datetime.fromisoformat(data["started_at"])
        if data.get("emergency_at"):
            state.emergency_at = datetime.fromisoformat(data["emergency_at"])
        if data.get("market_expiry"):
            state.market_expiry = datetime.fromisoformat(data["market_expiry"])
        return state
    
    def save(self, filepath: str):
        """Save state to JSON file."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
    
    @classmethod
    def load(cls, filepath: str) -> "StrategyState":
        """Load state from JSON file."""
        path = Path(filepath)
        if not path.exists():
            return cls()
        with open(path, "r") as f:
            data = json.load(f)
        return cls.from_dict(data)
