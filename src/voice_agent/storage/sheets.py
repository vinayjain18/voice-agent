"""Minimal async Google Sheets client.

Only the four operations the appointment store needs, over the REST API with
aiohttp. `google-api-python-client` would pull httplib2, google-api-core and a
second protobuf into a serverless bundle that also ships livekit-api, for a
handful of calls we can spell out.

Writes go through batchUpdate with insertDimension + updateCells rather than
values.append. Appends write into whatever cells currently follow the data, so
two concurrent appends can target the same row and silently overwrite each
other - four simultaneous appends can produce three rows. Inserting at a fixed
index is atomic and needs no read to find the end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
from google.auth import crypt, jwt

logger = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"

_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


class SheetsError(RuntimeError):
    """A Sheets call failed after exhausting retries."""


class SheetsClient:
    def __init__(self, service_account_json: str, spreadsheet_id: str) -> None:
        try:
            self._info: dict[str, Any] = json.loads(service_account_json)
        except ValueError as exc:
            raise SheetsError(f"service account JSON is not valid JSON: {exc}") from exc

        for key in ("client_email", "private_key"):
            if not self._info.get(key):
                raise SheetsError(f"service account JSON is missing '{key}'.")

        self._signer = crypt.RSASigner.from_service_account_info(self._info)
        self.spreadsheet_id = spreadsheet_id
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._tab_ids: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @property
    def client_email(self) -> str:
        return str(self._info["client_email"])

    async def _access_token(self, session: aiohttp.ClientSession) -> str:
        async with self._lock:
            if self._token and time.time() < self._token_expires_at - 60:
                return self._token

            now = int(time.time())
            assertion = jwt.encode(
                self._signer,
                {
                    "iss": self.client_email,
                    "scope": SCOPE,
                    "aud": TOKEN_URL,
                    "iat": now,
                    "exp": now + 3600,
                },
            )
            payload = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion.decode("utf-8"),
            }
            async with session.post(TOKEN_URL, data=payload) as response:
                body = await response.text()
                if response.status != 200:
                    raise SheetsError(
                        f"could not get a Google access token ({response.status}): {body[:300]}"
                    )
                data = json.loads(body)

            self._token = data["access_token"]
            self._token_expires_at = time.time() + float(data.get("expires_in", 3600))
            return self._token

    async def _request(
        self, method: str, url: str, *, params: dict | None = None, body: dict | None = None
    ) -> dict:
        last_error = ""
        for attempt in range(_MAX_ATTEMPTS):
            try:
                async with aiohttp.ClientSession() as session:
                    token = await self._access_token(session)
                    headers = {"Authorization": f"Bearer {token}"}
                    async with session.request(
                        method, url, params=params, json=body, headers=headers
                    ) as response:
                        text = await response.text()
                        if response.status < 300:
                            return json.loads(text) if text else {}
                        if response.status == 401:
                            # Token rejected mid-flight; drop it and try again.
                            self._token = None
                        if response.status not in _RETRY_STATUS and response.status != 401:
                            raise SheetsError(
                                f"{method} {url} failed ({response.status}): {text[:300]}"
                            )
                        last_error = f"{response.status}: {text[:200]}"
            except aiohttp.ClientError as exc:
                last_error = str(exc)

            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.5 * 2**attempt)

        raise SheetsError(f"{method} {url} failed after {_MAX_ATTEMPTS} attempts. {last_error}")

    async def tab_id(self, tab: str) -> int:
        """The numeric sheetId, which insertDimension and updateCells need."""
        if tab in self._tab_ids:
            return self._tab_ids[tab]

        data = await self._request(
            "GET",
            f"{API_ROOT}/{self.spreadsheet_id}",
            params={"fields": "sheets.properties(sheetId,title)"},
        )
        for sheet in data.get("sheets", []):
            properties = sheet.get("properties", {})
            self._tab_ids[properties.get("title", "")] = int(properties.get("sheetId", 0))

        if tab not in self._tab_ids:
            available = ", ".join(sorted(self._tab_ids)) or "none"
            raise SheetsError(f"the spreadsheet has no tab named '{tab}'. Tabs: {available}.")
        return self._tab_ids[tab]

    async def read_rows(self, tab: str) -> list[list[str]]:
        """Every row of the tab, as displayed.

        FORMATTED_VALUE rather than the raw value: a date a human typed by hand
        comes back as the text on screen instead of a serial number.
        """
        data = await self._request(
            "GET",
            f"{API_ROOT}/{self.spreadsheet_id}/values/{tab}",
            params={"valueRenderOption": "FORMATTED_VALUE", "majorDimension": "ROWS"},
        )
        return [[str(cell) for cell in row] for row in data.get("values", [])]

    async def batch_update(self, requests: list[dict]) -> dict:
        return await self._request(
            "POST",
            f"{API_ROOT}/{self.spreadsheet_id}:batchUpdate",
            body={"requests": requests},
        )

    async def insert_row(self, tab: str, values: list[str], *, at: int = 1) -> None:
        """Insert one row at a given index, atomically.

        Callers pass the end of the data, so existing rows keep their index.
        See the note on _snapshot in storage/appointments.py: inserting anywhere
        above them shifts every row below and turns a later in-place amendment
        into an overwrite of somebody else's appointment.
        """
        sheet_id = await self.tab_id(tab)
        await self.batch_update(
            [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": at,
                            "endIndex": at + 1,
                        },
                        "inheritFromBefore": False,
                    }
                },
                {
                    "updateCells": {
                        "rows": [{"values": [_cell(value) for value in values]}],
                        "fields": "userEnteredValue",
                        "start": {"sheetId": sheet_id, "rowIndex": at, "columnIndex": 0},
                    }
                },
            ]
        )

    async def write_row(self, tab: str, row_index: int, values: list[str]) -> None:
        """Overwrite one row in place. row_index is 0-based, header included."""
        sheet_id = await self.tab_id(tab)
        await self.batch_update(
            [
                {
                    "updateCells": {
                        "rows": [{"values": [_cell(value) for value in values]}],
                        "fields": "userEnteredValue",
                        "start": {"sheetId": sheet_id, "rowIndex": row_index, "columnIndex": 0},
                    }
                }
            ]
        )


def _cell(value: str) -> dict:
    # stringValue stores the literal text, so "2026-09-12" is not silently
    # reinterpreted as a date serial the way a typed-in value would be.
    return {"userEnteredValue": {"stringValue": "" if value is None else str(value)}}
