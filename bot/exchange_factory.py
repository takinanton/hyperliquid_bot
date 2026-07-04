"""Exchange client factory — выбирает client по EXCHANGE env."""
from __future__ import annotations
import logging

from bot.config import Settings

log = logging.getLogger(__name__)


def get_exchange_client(settings: Settings):
    """Returns HLClient | KrakenClient | NadoClient based on settings.exchange."""
    name = settings.exchange.lower()
    if name == "hyperliquid":
        from bot.exchange import HLClient
        log.info("Exchange: Hyperliquid")
        return HLClient(settings)
    elif name == "kraken":
        from bot.exchange_kraken import KrakenClient
        log.info("Exchange: Kraken Futures")
        return KrakenClient(settings)
    elif name == "nado":
        from bot.exchange_nado import NadoClient
        log.info("Exchange: Nado (Ink L2)")
        return NadoClient(settings)
    elif name == "pacifica":
        from bot.exchange_pacifica import PacificaClient
        log.info("Exchange: Pacifica (Solana)")
        return PacificaClient(settings)
    elif name == "extended":
        from bot.exchange_extended import ExtendedClient
        log.info("Exchange: Extended (Starknet)")
        return ExtendedClient(settings)
    elif name == "ib":
        from bot.exchange_ib import IBClient
        log.info("Exchange: Interactive Brokers")
        return IBClient(settings)
    else:
        raise ValueError(f"Unknown EXCHANGE={name}. Use 'hyperliquid' / 'kraken' / 'nado' / 'pacifica' / 'extended' / 'ib'.")
