"""AI Trading Agent core engine.

The engine is framework-agnostic: it does not import Django. Persistence is
injected through the :class:`trading_engine.journal.Journal` protocol and broker
access through the :class:`trading_engine.gateway.MT5Gateway` protocol.

Layering (each layer only depends on the layers above it):

    models / constants        - plain data types, MT5 enum values
    gateway                   - broker access (real MT5, remote bridge, mock)
    indicators / analyzer     - pure feature computation, no side effects
    strategy / ai engine      - produce *proposals*, never send orders
    risk manager              - deterministic gate, signs approvals
    executor / positions      - only component that sends trade requests
    agent                     - orchestration, modes, kill switch
"""

__version__ = "0.1.0"
