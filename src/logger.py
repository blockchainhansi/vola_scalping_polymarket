"""
Polymarket Box Spread Market Maker - Logging Configuration

Structured logging setup with colored console output.
"""

import logging
import sys
from datetime import datetime
from typing import Optional


class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors for console output."""
    
    COLORS = {
        'DEBUG': '\033[36m',     # Cyan
        'INFO': '\033[32m',      # Green
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[35m',  # Magenta
    }
    RESET = '\033[0m'
    BOLD = '\033[1m'
    
    EMOJIS = {
        'DEBUG': '🔍',
        'INFO': '📊',
        'WARNING': '⚠️',
        'ERROR': '❌',
        'CRITICAL': '💥',
    }
    
    def format(self, record: logging.LogRecord) -> str:
        # Add color and emoji based on level
        color = self.COLORS.get(record.levelname, '')
        emoji = self.EMOJIS.get(record.levelname, '')
        
        # Format timestamp
        timestamp = datetime.fromtimestamp(record.created).strftime('%H:%M:%S.%f')[:-3]
        
        # Build the message
        level_str = f"{color}{record.levelname:8}{self.RESET}"
        
        # Format: [HH:MM:SS.mmm] LEVEL    emoji message
        formatted = f"[{timestamp}] {level_str} {emoji} {record.getMessage()}"
        
        # Add exception info if present
        if record.exc_info:
            formatted += f"\n{self.formatException(record.exc_info)}"
        
        return formatted


class FileFormatter(logging.Formatter):
    """Plain formatter for file output."""
    
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        formatted = f"[{timestamp}] {record.levelname:8} {record.name}: {record.getMessage()}"
        
        if record.exc_info:
            formatted += f"\n{self.formatException(record.exc_info)}"
        
        return formatted


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None
) -> logging.Logger:
    """
    Set up logging configuration.
    
    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional file path to write logs to
        
    Returns:
        The root logger for the application
    """
    # Get the root logger for our application
    logger = logging.getLogger("mm_strategy")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Clear any existing handlers
    logger.handlers.clear()
    
    # Console handler with colors
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColoredFormatter())
    logger.addHandler(console_handler)
    
    # File handler if specified
    if log_file:
        file_handler = logging.FileHandler(log_file, mode='a')
        file_handler.setFormatter(FileFormatter())
        logger.addHandler(file_handler)
    
    # Don't propagate to root logger
    logger.propagate = False
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger for a specific module.
    
    Args:
        name: Module name (usually __name__)
        
    Returns:
        Logger instance
    """
    return logging.getLogger(f"mm_strategy.{name}")


# Strategy-specific log helpers
class StrategyLogger:
    """Helper class for strategy-specific logging with consistent formatting."""
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
    
    def mode_change(self, old_mode: str, new_mode: str):
        """Log a strategy mode change."""
        self.logger.info(f"Mode change: {old_mode} → {new_mode}")
    
    def order_placed(self, side: str, outcome: str, price: float, size: float, order_id: str):
        """Log an order placement."""
        emoji = "🟢" if side == "BUY" else "🔴"
        self.logger.info(f"{emoji} {side} {outcome} @ ${price:.4f} × {size:.2f} (ID: {order_id[:8]}...)")
    
    def order_filled(self, side: str, outcome: str, price: float, size: float):
        """Log an order fill."""
        emoji = "✅"
        self.logger.info(f"{emoji} FILLED: {side} {outcome} @ ${price:.4f} × {size:.2f}")
    
    def order_cancelled(self, order_id: str, reason: str = ""):
        """Log an order cancellation."""
        msg = f"Order successfully cancelled: {order_id[:8]}..."
        if reason:
            msg += f" ({reason})"
        self.logger.info(msg)
    
    def inventory_update(self, q_yes: float, q_no: float, mu_yes: float, mu_no: float):
        """Log inventory state."""
        delta = q_yes - q_no
        combined = mu_yes + mu_no
        self.logger.debug(
            f"Inventory: YES={q_yes:.2f} @ ${mu_yes:.4f}, NO={q_no:.2f} @ ${mu_no:.4f} | "
            f"ΔQ={delta:+.2f}, μ_sum=${combined:.4f}"
        )
    
    def orderbook_update(self, outcome: str, bid: float, ask: float, spread: float):
        """Log orderbook state."""
        self.logger.debug(f"Book {outcome}: bid=${bid:.4f}, ask=${ask:.4f}, spread=${spread:.4f}")
    
    def trap_prices(self, yes_price: Optional[float], no_price: Optional[float]):
        """Log calculated trap prices."""
        yes_str = f"${yes_price:.4f}" if yes_price else "NO_QUOTE"
        no_str = f"${no_price:.4f}" if no_price else "NO_QUOTE"
        self.logger.debug(f"Trap prices: YES={yes_str}, NO={no_str}")
    
    def profit_locked(self, amount: float, total: float):
        """Log profit lock event."""
        self.logger.info(f"💰 Profit locked: +${amount:.4f} (Total: ${total:.4f})")
    
    def emergency(self, reason: str, exposure: float):
        """Log emergency event."""
        self.logger.warning(f"🚨 EMERGENCY: {reason} (Exposure: {exposure:.2f})")
    
    def heartbeat(self, mode: str, delta_q: float, locked_profit: float):
        """Periodic status heartbeat."""
        self.logger.info(f"💓 Mode={mode}, ΔQ={delta_q:+.2f}, Locked=${locked_profit:.4f}")
