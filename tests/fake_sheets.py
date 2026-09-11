"""An in-memory stand-in for SheetsClient.

Models the two behaviours the store depends on: inserting at an index shifts
everything below it down, and a write lands on whatever row index it is given.
"""

from __future__ import annotations


class FakeSheetsClient:
    def __init__(self, rows: list[list[str]] | None = None) -> None:
        self.rows: list[list[str]] = [list(row) for row in (rows or [])]
        self.spreadsheet_id = "fake-sheet"
        self.reads = 0
        self.writes = 0

    async def read_rows(self, tab: str) -> list[list[str]]:
        self.reads += 1
        return [list(row) for row in self.rows]

    async def insert_row(self, tab: str, values: list[str], *, at: int = 1) -> None:
        self.writes += 1
        self.rows.insert(at, list(values))

    async def write_row(self, tab: str, row_index: int, values: list[str]) -> None:
        self.writes += 1
        while len(self.rows) <= row_index:
            self.rows.append([])
        self.rows[row_index] = list(values)

    async def tab_id(self, tab: str) -> int:
        return 0

    async def ensure_tab(self, tab: str) -> None:
        return None
