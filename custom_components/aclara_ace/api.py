"""Async client for the Aclara / SilverBlaze "ACE" customer portal.

The portal (acewebsite.silverblaze.com) authenticates through an Azure AD B2C
custom policy hosted at acelogin.silverblaze.com. After login the portal issues
ASP.NET Core session cookies plus an opaque encrypted ``Parameters`` blob that
every widget API call must carry (base64-encoded) in the query string.

Usage data comes from ``api/DataDownload/ConsumptionData`` which returns a flat
list of hourly readings in the utility's local time zone.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import aiohttp

_LOGGER = logging.getLogger(__name__)

PORTAL_URL = "https://acewebsite.silverblaze.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) "
    "Gecko/20100101 Firefox/130.0"
)

# The portal serves usage in whole days; a single request for eight months
# worked fine, but keep chunks bounded so a very long backfill can't time out.
MAX_DAYS_PER_REQUEST = 180

_SETTINGS_RE = re.compile(r"var SETTINGS = (\{.*?\});", re.S)
_FORM_ACTION_RE = re.compile(r"<form[^>]*action=[\"']([^\"']+)[\"']")
_FORM_INPUT_RE = re.compile(
    r"<input[^>]*name=[\"']([^\"']+)[\"'][^>]*value=[\"']([^\"']*)[\"']"
)


class AclaraAceError(Exception):
    """Base error."""


class AuthError(AclaraAceError):
    """Login failed (bad credentials, changed login flow, ...)."""


class ApiError(AclaraAceError):
    """The portal returned something unexpected."""


@dataclass(frozen=True)
class Meter:
    """A metered service point."""

    meter_id: str
    service_id: str
    commodity: str  # "water", "electric", "gas"
    account_id: str
    premise_id: str
    address: str
    rate_class: str = ""
    meter_type: str = ""


@dataclass(frozen=True)
class Account:
    """Customer account as reported by the portal."""

    customer_id: str
    account_id: str
    name: str
    meters: list[Meter] = field(default_factory=list)
    timezone: str = "America/Chicago"


@dataclass(frozen=True)
class Reading:
    """One interval reading. ``start`` is timezone-aware."""

    start: datetime
    quantity: float
    unit: str
    interval: str


@dataclass(frozen=True)
class MeterSpan:
    """Range of AMI data the portal holds for a meter, in UTC."""

    meter_id: str
    interval: str
    unit: str
    oldest: datetime
    newest: datetime


class AclaraAceClient:
    """Client for one customer login."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        client_id: int,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._client_id = client_id
        self._login_lock = asyncio.Lock()
        self._dashboard_enc: str | None = None
        self._params_b64: str | None = None
        self._timezone = "America/Chicago"
        # Azure B2C sets cookies whose values contain "/", "+" and "=".
        # aiohttp's cookie jar re-emits those quoted, which B2C rejects with
        # 400 Bad Request, so cookies for the login host are tracked by hand.
        self._b2c_cookies: dict[str, str] = {}
        self._b2c_host: str | None = None

    # ------------------------------------------------------------------ auth

    def _harvest_b2c_cookies(self, resp: aiohttp.ClientResponse) -> None:
        """Record raw Set-Cookie values from the B2C host and drop them from the jar."""
        for r in (*resp.history, resp):
            if r.url.host != self._b2c_host:
                continue
            for raw in r.headers.getall("Set-Cookie", []):
                pair = raw.split(";", 1)[0]
                if "=" in pair:
                    name, value = pair.split("=", 1)
                    self._b2c_cookies[name.strip()] = value.strip()
        host = self._b2c_host
        self._session.cookie_jar.clear(lambda c: host is not None and host.endswith(c["domain"].lstrip(".")))

    def _b2c_headers(self, extra: dict[str, str]) -> dict[str, str]:
        cookie = "; ".join(f"{k}={v}" for k, v in self._b2c_cookies.items())
        return {"User-Agent": USER_AGENT, "Cookie": cookie, **extra}

    @property
    def logged_in(self) -> bool:
        """Return True once a session and API parameters were obtained."""
        return self._params_b64 is not None

    async def async_login(self) -> None:
        """Run the B2C login flow and prime the API parameters."""
        async with self._login_lock:
            await self._login_locked()

    async def _login_locked(self) -> None:
        self._params_b64 = None
        self._session.cookie_jar.clear()
        headers = {"User-Agent": USER_AGENT}

        # 1. Start the OIDC challenge; the portal redirects to the B2C page.
        async with self._session.get(
            f"{PORTAL_URL}/B2C",
            params={"clientId": self._client_id},
            headers=headers,
        ) as resp:
            auth_url = str(resp.url)
            page = await resp.text()
            self._b2c_host = resp.url.host
            self._b2c_cookies = {}
            self._harvest_b2c_cookies(resp)
        match = _SETTINGS_RE.search(page)
        if not match:
            raise AuthError("B2C login page did not contain SETTINGS")
        settings = json.loads(match.group(1))
        csrf = settings["csrf"]
        tx = settings["transId"]
        tenant = settings["hosts"]["tenant"]
        policy = settings["hosts"]["policy"]
        b2c_origin = f"{urlparse(auth_url).scheme}://{urlparse(auth_url).netloc}"
        base = f"{b2c_origin}{tenant}"

        # 2. Post credentials.
        async with self._session.post(
            f"{base}/SelfAsserted",
            params={"tx": tx, "p": policy},
            data={
                "request_type": "RESPONSE",
                "signInName": self._username,
                "password": self._password,
            },
            headers=self._b2c_headers(
                {
                    "X-CSRF-TOKEN": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": auth_url,
                    "Origin": b2c_origin,
                }
            ),
        ) as resp:
            text = await resp.text()
            self._harvest_b2c_cookies(resp)
        try:
            result = json.loads(text)
        except ValueError as err:
            raise AuthError(f"Unexpected SelfAsserted response: {text[:200]}") from err
        if str(result.get("status")) != "200":
            raise AuthError(
                result.get("message") or f"Login rejected: {json.dumps(result)[:200]}"
            )

        # 3. Confirm; B2C answers with an auto-submitting form (response_mode=form_post).
        async with self._session.get(
            f"{base}/api/CombinedSigninAndSignup/confirmed",
            params={"rememberMe": "false", "csrf_token": csrf, "tx": tx, "p": policy},
            headers=self._b2c_headers({"Referer": auth_url}),
            allow_redirects=False,
        ) as resp:
            confirmed = await resp.text()
            self._harvest_b2c_cookies(resp)
        action = _FORM_ACTION_RE.search(confirmed)
        fields = {k: html.unescape(v) for k, v in _FORM_INPUT_RE.findall(confirmed)}
        if not action or "code" not in fields:
            raise AuthError("B2C did not return an authorization code")

        # 4. Post the code back to the portal; it sets the session cookies and
        #    finally lands on Page?enc=<dashboard token>.
        async with self._session.post(
            html.unescape(action.group(1)),
            data=fields,
            headers={**headers, "Referer": f"{b2c_origin}/", "Origin": b2c_origin},
        ) as resp:
            final_url = str(resp.url)
            await resp.read()
        enc = parse_qs(urlparse(final_url).query).get("enc")
        if not enc:
            raise AuthError(f"Portal did not land on a dashboard page: {final_url[:200]}")
        self._dashboard_enc = enc[0]

        # 5. Find the usage page and fetch its API parameters blob.
        menu = await self._get_json(
            "/Menu/GetMenuWithAllSubTabs", {"enc": self._dashboard_enc}, retry=False
        )
        usage_enc = None
        for tab in menu.get("Tabs", []):
            for sub in tab.get("SubTabs", []):
                if sub.get("Key") == "tab.usagesubnav":
                    usage_enc = parse_qs(urlparse(sub["Url"]).query)["enc"][0]
        page_enc = usage_enc or self._dashboard_enc
        page_params = await self._get_json(
            "/api/Page/GetPageParameters", {"enc": page_enc}, retry=False
        )
        params = page_params.get("Layout", {}).get("Parameters")
        if not params:
            raise AuthError("Portal page did not expose API parameters")
        self._params_b64 = base64.b64encode(params.encode()).decode()
        _LOGGER.debug("Logged in to ACE portal as %s", self._username)

    # ------------------------------------------------------------------ http

    async def _get_json(
        self, path: str, params: dict[str, Any], *, retry: bool = True
    ) -> Any:
        """GET a JSON endpoint, re-logging in once if the session is gone."""
        if retry and not self.logged_in:
            await self.async_login()
        async with self._session.get(
            f"{PORTAL_URL}{path}",
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"},
        ) as resp:
            ctype = resp.headers.get("Content-Type", "")
            text = await resp.text()
        if resp.status == 200 and "json" in ctype:
            return json.loads(text)
        if retry:
            # Expired sessions come back as a 302 to login or a 500 HTML page.
            _LOGGER.debug("ACE call %s returned %s %s; re-authenticating", path, resp.status, ctype)
            await self.async_login()
            return await self._api_get_no_retry(path, params)
        raise ApiError(f"{path} returned HTTP {resp.status} ({ctype})")

    async def _api_get_no_retry(self, path: str, params: dict[str, Any]) -> Any:
        params = {**params, "parameters": self._params_b64}
        return await self._get_json(path, params, retry=False)

    async def _api_get(self, path: str, params: dict[str, Any]) -> Any:
        if not self.logged_in:
            await self.async_login()
        return await self._get_json(path, {**params, "parameters": self._params_b64})

    # ------------------------------------------------------------------ data

    async def async_get_account(self) -> Account:
        """Return the customer account with its meters."""
        data = await self._api_get(
            "/api/CustomerInfo/GetAccountPremise", {"tabkey": "tab.usagesubnav"}
        )
        meters: list[Meter] = []
        account_id = data.get("AccountId", "")
        for acct in data.get("AccountList", []):
            for premise in acct.get("Premises", []):
                address = ", ".join(
                    p
                    for p in (
                        premise.get("Addr1"),
                        premise.get("Addr2"),
                        premise.get("City"),
                        premise.get("StateProvince"),
                        premise.get("PostalCode"),
                    )
                    if p
                )
                for service in premise.get("Service", []):
                    for sp in service.get("ServicePoints", []):
                        for m in sp.get("Meters", []):
                            if str(m.get("IsDeleted", "0")) == "1":
                                continue
                            meters.append(
                                Meter(
                                    meter_id=str(m["Id"]),
                                    service_id=str(service.get("Id", "")),
                                    commodity=str(
                                        m.get("CommodityKey")
                                        or service.get("CommodityKey")
                                        or "water"
                                    ),
                                    account_id=str(acct.get("Id", account_id)),
                                    premise_id=str(premise.get("Id", "")),
                                    address=address,
                                    rate_class=str(m.get("RateClass", "")),
                                    meter_type=str(m.get("MeterType", "")),
                                )
                            )
        init = await self._api_get("/api/ConsumptionV3/Init", {"tabKey": "tab.usagesubnav"})
        self._timezone = init.get("IanaTimezone") or self._timezone
        return Account(
            customer_id=str(data.get("CustomerId", "")),
            account_id=str(account_id),
            name=str(data.get("AccountName") or f"{data.get('FirstName','')} {data.get('LastName','')}").strip(),
            meters=meters,
            timezone=self._timezone,
        )

    async def async_get_meter_spans(self) -> list[MeterSpan]:
        """Return the available AMI date range per meter and interval."""
        meta = await self._api_get(
            "/api/DataDownload/Meta", {"tabkey": "null", "customerByAccountId": "true"}
        )
        spans: list[MeterSpan] = []
        for coll in (meta.get("body") or {}).get("Metadata", []):
            for ts in coll.get("TimeSeries", []):
                span = ts.get("AmiTimeSpan") or {}
                if not span.get("Oldest") or not span.get("Newest"):
                    continue
                spans.append(
                    MeterSpan(
                        meter_id=str(ts.get("CollectionId") or coll.get("CollectionId")),
                        interval=str(ts.get("Interval")),
                        unit=str((ts.get("SeriesSpec") or {}).get("uom", "gal")),
                        oldest=datetime.fromisoformat(span["Oldest"]),
                        newest=datetime.fromisoformat(span["Newest"]),
                    )
                )
        return spans

    async def async_get_hourly_usage(
        self,
        meter: Meter,
        start: date,
        end: date,
        *,
        timezone: str | tzinfo | None = None,
    ) -> list[Reading]:
        """Return hourly usage for ``meter`` between two local dates (inclusive).

        Timestamps are converted from the utility's local time to aware
        datetimes. Rows come back sorted; DST-fold duplicates are disambiguated.
        """
        tz = timezone if isinstance(timezone, tzinfo) else ZoneInfo(timezone or self._timezone)
        readings: list[Reading] = []
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=MAX_DAYS_PER_REQUEST - 1))
            rows = await self._api_get(
                "/api/DataDownload/ConsumptionData",
                {
                    "tabkey": "null",
                    # endDate is exclusive on this endpoint.
                    "startDate": chunk_start.isoformat(),
                    "endDate": (chunk_end + timedelta(days=1)).isoformat(),
                    "meters": f"{meter.meter_id}|{meter.commodity}|60min:usage|{meter.meter_id}",
                    "netBillingStartDate": "",
                    "collectionType": "MeterId",
                },
            )
            if not isinstance(rows, list):
                raise ApiError(f"ConsumptionData returned {type(rows).__name__}")
            readings.extend(_parse_rows(rows, tz))
            chunk_start = chunk_end + timedelta(days=1)
        readings.sort(key=lambda r: r.start)
        return readings


def _parse_rows(rows: list[dict[str, Any]], tz: tzinfo) -> list[Reading]:
    out: list[Reading] = []
    seen: set[str] = set()
    for row in rows:
        if str(row.get("Kind", "Usage")).lower() != "usage":
            continue
        raw = str(row["DateOfUse"])
        naive = datetime.strptime(raw, "%m/%d/%Y %H:%M")
        # The fall-back DST hour appears twice with identical wall time; the
        # second occurrence is the later (standard-time) one.
        fold = 1 if raw in seen else 0
        seen.add(raw)
        start = naive.replace(tzinfo=tz, fold=fold)
        out.append(
            Reading(
                start=start,
                quantity=float(row.get("Quantity") or 0),
                unit=str(row.get("Unit") or "gal"),
                interval=str(row.get("Interval") or "60min"),
            )
        )
    return out
