"""
Firestore Client for Options Trade Journaling.

This module provides a centralized Firestore client for logging all options trade
executions to Google Cloud Firestore for historical analysis and journaling.

Trades are tagged with mode='live' for production and mode='paper' for testnet.
"""

import os
from typing import Optional, Dict, Any, List
from datetime import datetime
from core.logger import get_logger

logger = get_logger(__name__)

# Global Firestore client instance (singleton pattern)
_firestore_client = None
_firestore_enabled = False
_firestore_collection = "options"


def initialize_firestore(service_account_path: str, collection_name: str = "options", enabled: bool = True):
    """Initialize Firestore client with service account credentials.

    Args:
        service_account_path: Path to Firebase Admin SDK service account JSON file
        collection_name: Firestore collection name for trades
        enabled: Whether Firestore journaling is enabled

    Returns:
        bool: True if initialization successful, False otherwise
    """
    global _firestore_client, _firestore_enabled, _firestore_collection

    _firestore_enabled = enabled
    _firestore_collection = collection_name

    if not enabled:
        logger.info("Firestore trade journaling is disabled in configuration")
        return False

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        # Check if service account file exists
        if not os.path.exists(service_account_path):
            logger.error(f"Firestore service account file not found: {service_account_path}")
            _firestore_enabled = False
            return False

        # Initialize Firebase Admin SDK (only once)
        if not firebase_admin._apps:
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin SDK initialized successfully")

        # Get Firestore client
        _firestore_client = firestore.client()
        logger.info(f"Firestore client initialized. Collection: '{collection_name}'")

        return True

    except ImportError as e:
        logger.error(f"Firebase Admin SDK not installed. Run: pip install firebase-admin. Error: {e}")
        _firestore_enabled = False
        return False
    except Exception as e:
        logger.error(f"Failed to initialize Firestore client: {e}", exc_info=True)
        _firestore_enabled = False
        return False


def journal_straddle_entry(
    trade_id: str,
    underlying: str,
    strategy_name: str,
    mode: str,
    spot_price: float,
    atm_strike: float,
    call_product_id: int,
    put_product_id: int,
    call_symbol: str,
    put_symbol: str,
    call_order_id: Optional[str],
    put_order_id: Optional[str],
    call_premium: float,
    put_premium: float,
    total_premium: float,
    lot_size: int,
    leverage: int,
    entry_time: str,
    **kwargs,
) -> Optional[str]:
    """Journal a straddle entry (both legs) to Firestore.

    Args:
        trade_id: Unique trade identifier (e.g., 'straddle_BTC_20260711_170000')
        underlying: Underlying asset (e.g., 'BTC')
        strategy_name: Strategy name (e.g., 'short_straddle')
        mode: 'live' for production, 'paper' for testnet
        spot_price: Spot price at entry time
        atm_strike: ATM strike price selected
        call_product_id: Exchange product ID for the call option
        put_product_id: Exchange product ID for the put option
        call_symbol: Call option symbol
        put_symbol: Put option symbol
        call_order_id: Order ID for call leg
        put_order_id: Order ID for put leg
        call_premium: Premium received for call leg
        put_premium: Premium received for put leg
        total_premium: Total premium collected (call + put)
        lot_size: Number of contracts per leg
        leverage: Leverage used
        entry_time: Entry timestamp as ISO string
        **kwargs: Additional fields

    Returns:
        Document ID if successful, None otherwise
    """
    global _firestore_client, _firestore_enabled, _firestore_collection

    if not _firestore_enabled or _firestore_client is None:
        logger.debug("Firestore journaling disabled, skipping trade journal")
        return None

    try:
        trade_doc = {
            # Trade identification
            "trade_id": trade_id,
            "status": "OPEN",

            # Metadata
            "entry_timestamp": datetime.utcnow(),
            "underlying": underlying,
            "strategy_name": strategy_name,
            "mode": mode,

            # Market data at entry
            "spot_price": spot_price,
            "atm_strike": atm_strike,

            # Call leg
            "call_product_id": call_product_id,
            "call_symbol": call_symbol,
            "call_order_id": call_order_id,
            "call_premium": call_premium,

            # Put leg
            "put_product_id": put_product_id,
            "put_symbol": put_symbol,
            "put_order_id": put_order_id,
            "put_premium": put_premium,

            # Combined
            "total_premium_collected": total_premium,
            "lot_size": lot_size,
            "leverage": leverage,
            "entry_time_ist": entry_time,

            # Stop-loss tracking
            "sl_threshold": total_premium * 0.5,  # 50% of premium

            # Events array for lifecycle tracking
            "events": [{
                "timestamp": datetime.utcnow(),
                "action": "ENTRY",
                "spot_price": spot_price,
                "call_premium": call_premium,
                "put_premium": put_premium,
                "total_premium": total_premium,
            }],

            # Exit fields (to be populated on exit)
            "exit_timestamp": None,
            "exit_reason": None,
            "exit_call_premium": None,
            "exit_put_premium": None,
            "realized_pnl": None,
            "max_mtm_loss": None,
        }

        # Add any additional fields
        trade_doc.update(kwargs)

        # Remove None values
        trade_doc = {k: v for k, v in trade_doc.items() if v is not None}

        doc_ref = _firestore_client.collection(_firestore_collection).document(trade_id)
        doc_ref.set(trade_doc)

        logger.info(
            f"[OK] Straddle OPENED in Firestore: {trade_id} | "
            f"{underlying} Strike={atm_strike} | Premium=${total_premium:.2f}"
        )

        return trade_id

    except Exception as e:
        logger.error(f"Failed to journal straddle entry to Firestore: {e}", exc_info=True)
        return None


def journal_straddle_exit(
    trade_id: str,
    exit_reason: str,
    exit_call_premium: float,
    exit_put_premium: float,
    realized_pnl: float,
    max_mtm_loss: Optional[float] = None,
    call_exit_order_id: Optional[str] = None,
    put_exit_order_id: Optional[str] = None,
    **kwargs,
) -> Optional[str]:
    """Journal a straddle exit to Firestore.

    Args:
        trade_id: Trade ID from entry
        exit_reason: Reason for exit ('scheduled_exit', 'stop_loss_hit', 'manual')
        exit_call_premium: Call premium at exit
        exit_put_premium: Put premium at exit
        realized_pnl: Realized P&L
        max_mtm_loss: Maximum MTM loss observed during the trade
        call_exit_order_id: Order ID for call exit
        put_exit_order_id: Order ID for put exit
        **kwargs: Additional fields

    Returns:
        Document ID if successful, None otherwise
    """
    global _firestore_client, _firestore_enabled, _firestore_collection

    if not _firestore_enabled or _firestore_client is None:
        logger.debug("Firestore journaling disabled, skipping exit journal")
        return None

    try:
        from firebase_admin import firestore

        exit_timestamp = datetime.utcnow()

        exit_event = {
            "timestamp": exit_timestamp,
            "action": "EXIT",
            "reason": exit_reason,
            "exit_call_premium": exit_call_premium,
            "exit_put_premium": exit_put_premium,
            "realized_pnl": realized_pnl,
        }
        exit_event = {k: v for k, v in exit_event.items() if v is not None}

        update_data = {
            "status": "CLOSED",
            "exit_timestamp": exit_timestamp,
            "exit_reason": exit_reason,
            "exit_call_premium": exit_call_premium,
            "exit_put_premium": exit_put_premium,
            "realized_pnl": realized_pnl,
            "max_mtm_loss": max_mtm_loss,
            "call_exit_order_id": call_exit_order_id,
            "put_exit_order_id": put_exit_order_id,
            "events": firestore.ArrayUnion([exit_event]),
        }

        update_data.update(kwargs)
        update_data = {k: v for k, v in update_data.items() if v is not None}

        doc_ref = _firestore_client.collection(_firestore_collection).document(trade_id)
        doc_ref.set(update_data, merge=True)

        logger.info(
            f"[OK] Straddle CLOSED in Firestore: {trade_id} | "
            f"Reason: {exit_reason} | PnL: ${realized_pnl:+,.2f}"
        )

        return trade_id

    except Exception as e:
        logger.error(f"Failed to journal straddle exit to Firestore: {e}", exc_info=True)
        return None


def get_open_trades(underlying: str) -> List[Dict[str, Any]]:
    """Get all open trades for a given underlying asset from Firestore.

    Args:
        underlying: The underlying asset (e.g., 'BTC')

    Returns:
        List of trade dictionaries
    """
    global _firestore_client, _firestore_enabled, _firestore_collection

    if not _firestore_enabled or _firestore_client is None:
        logger.debug("Firestore journaling disabled, cannot query open trades")
        return []

    try:
        docs = _firestore_client.collection(_firestore_collection) \
            .where("status", "==", "OPEN") \
            .where("underlying", "==", underlying) \
            .stream()
        return [doc.to_dict() for doc in docs]
    except Exception as e:
        logger.error(f"Failed to query open trades from Firestore: {e}", exc_info=True)
        return []


def get_firestore_status() -> Dict[str, Any]:
    """Get Firestore client status.

    Returns:
        dict: Status information including enabled state, collection name, and connection status
    """
    global _firestore_client, _firestore_enabled, _firestore_collection

    return {
        "enabled": _firestore_enabled,
        "connected": _firestore_client is not None,
        "collection": _firestore_collection,
    }


def journal_orb_entry(
    trade_id: str,
    symbol: str,
    action: str,
    side: str,
    price: float,
    order_size: int,
    leverage: int,
    mode: str,
    strategy_name: str,
    reason: str,
    orb_h1: float,
    orb_l1: float,
    tp_price: float,
    sl_price: float,
    entry_time_ist: str,
    **kwargs,
) -> Optional[str]:
    """Journal a Gold ORB entry trade to Firestore.

    Args:
        trade_id: Unique trade identifier
        symbol: Trading symbol (e.g. 'XAUTUSD')
        action: Trade action ('ENTRY_LONG' or 'ENTRY_SHORT')
        side: Order side ('buy' or 'sell')
        price: Entry price
        order_size: Number of contracts (e.g. 1000)
        leverage: Leverage used (e.g. 100)
        mode: Execution mode ('live' or 'paper')
        strategy_name: Name of strategy ('Gold ORB')
        reason: Signal reason
        orb_h1: ORB High
        orb_l1: ORB Low
        tp_price: Take profit price
        sl_price: Stop loss price
        entry_time_ist: IST time string

    Returns:
        Document ID if successful, None otherwise
    """
    global _firestore_client, _firestore_enabled, _firestore_collection

    if not _firestore_enabled or _firestore_client is None:
        logger.debug("Firestore journaling disabled, skipping trade journal")
        return None

    try:
        trade_doc = {
            "trade_id": trade_id,
            "status": "OPEN",
            "entry_timestamp": datetime.utcnow(),
            "symbol": symbol,
            "underlying": "XAUT",
            "strategy_name": strategy_name,
            "mode": mode,
            "action": action,
            "side": side,
            "entry_price": price,
            "order_size": order_size,
            "leverage": leverage,
            "orb_h1": orb_h1,
            "orb_l1": orb_l1,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "entry_time_ist": entry_time_ist,
            "reason": reason,
            "events": [{
                "timestamp": datetime.utcnow(),
                "action": action,
                "price": price,
                "reason": reason,
            }],
        }
        trade_doc.update(kwargs)
        trade_doc = {k: v for k, v in trade_doc.items() if v is not None}

        doc_ref = _firestore_client.collection(_firestore_collection).document(trade_id)
        doc_ref.set(trade_doc)
        logger.info(f"[OK] Gold ORB OPENED in Firestore: {trade_id} | {symbol} {action} @ ${price:.2f}")
        return trade_id
    except Exception as e:
        logger.error(f"Failed to journal Gold ORB entry to Firestore: {e}", exc_info=True)
        return None


def journal_orb_exit(
    trade_id: str,
    action: str,
    side: str,
    exit_price: float,
    realized_pnl: float,
    reason: str,
    exit_time_ist: str,
    **kwargs,
) -> Optional[str]:
    """Journal a Gold ORB exit trade to Firestore.

    Args:
        trade_id: Trade ID from entry
        action: Exit action ('EXIT_LONG' or 'EXIT_SHORT')
        side: Order side ('sell' or 'buy')
        exit_price: Price at exit
        realized_pnl: Realized P&L
        reason: Exit reason ('TP hit' or 'SL hit')
        exit_time_ist: IST time string

    Returns:
        Document ID if successful, None otherwise
    """
    global _firestore_client, _firestore_enabled, _firestore_collection

    if not _firestore_enabled or _firestore_client is None:
        logger.debug("Firestore journaling disabled, skipping exit journal")
        return None

    try:
        from firebase_admin import firestore

        exit_timestamp = datetime.utcnow()
        exit_event = {
            "timestamp": exit_timestamp,
            "action": action,
            "reason": reason,
            "exit_price": exit_price,
            "realized_pnl": realized_pnl,
        }

        update_data = {
            "status": "CLOSED",
            "exit_timestamp": exit_timestamp,
            "exit_action": action,
            "exit_price": exit_price,
            "realized_pnl": realized_pnl,
            "exit_reason": reason,
            "exit_time_ist": exit_time_ist,
            "events": firestore.ArrayUnion([exit_event]),
        }
        update_data.update(kwargs)
        update_data = {k: v for k, v in update_data.items() if v is not None}

        doc_ref = _firestore_client.collection(_firestore_collection).document(trade_id)
        doc_ref.set(update_data, merge=True)
        logger.info(f"[OK] Gold ORB CLOSED in Firestore: {trade_id} | {reason} | PnL: ${realized_pnl:+,.2f}")
        return trade_id
    except Exception as e:
        logger.error(f"Failed to journal Gold ORB exit to Firestore: {e}", exc_info=True)
        return None

