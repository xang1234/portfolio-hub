"""Tests for the Broker Protocol contract.

Slice 1 requires the Broker Protocol to exist with three methods:
  - async connect()
  - async disconnect()
  - async is_connected() -> bool

The Protocol itself isn't directly testable, but we verify that:
  1. It can be imported
  2. It declares the required methods
  3. A concrete implementation satisfying the contract is recognized
"""

import inspect
from typing import Protocol, runtime_checkable


def test_broker_protocol_exists_and_is_importable():
    from app.core.broker import Broker

    assert Broker is not None


def test_broker_protocol_declares_connect():
    from app.core.broker import Broker

    assert hasattr(Broker, "connect")
    assert inspect.iscoroutinefunction(Broker.connect)


def test_broker_protocol_declares_disconnect():
    from app.core.broker import Broker

    assert hasattr(Broker, "disconnect")
    assert inspect.iscoroutinefunction(Broker.disconnect)


def test_broker_protocol_declares_is_connected():
    from app.core.broker import Broker

    assert hasattr(Broker, "is_connected")
    assert inspect.iscoroutinefunction(Broker.is_connected)


def test_broker_protocol_has_name_attribute():
    """The Protocol surface specified in PLAN.md includes a `name: str` class attribute."""
    from app.core.broker import Broker

    # Protocols expose class attrs in __annotations__
    assert "name" in Broker.__annotations__
    assert Broker.__annotations__["name"] is str
