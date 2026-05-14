"""Broker abstraction layer.

Slice 1 scope: Protocol stub with lifecycle methods only. Position/AccountSummary
dataclasses and the get_positions / get_account_summary / get_company_name methods
arrive in slice 2 and beyond.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Broker(Protocol):
    name: str

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_connected(self) -> bool: ...
