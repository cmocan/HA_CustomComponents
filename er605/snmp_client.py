# custom_components/er605/snmp_client.py
# ─────────────────────────────────────────────────────────────────────────────
# Reusable async SNMPv2c client for the TP-Link ER605.
#
# The public interface (get / get_many / walk) and the exception hierarchy
# (SnmpConnectionError / SnmpTimeoutError) are designed so coordinator code
# can work against this client consistently.
#
# Auth difference: CommunityData(community, mpModel=1) instead of UsmUserData.
# mpModel=1 = SNMPv2c. mpModel=0 = SNMPv1 (not used here).
#
# Note on SnmpAuthError: retained for API parity but NEVER raised by this
# client. With SNMPv2c a wrong community string causes the agent to silently
# drop the packet — the caller receives SnmpTimeoutError instead.
#
# API note: written for pysnmp >= 7.x (snake_case API).
#   get_cmd / bulk_cmd   — coroutines (single await, return tuple)
#   walk_cmd             — async generator (GETNEXT-based walk)
#   UdpTransportTarget   — must be constructed via await .create()
#
# No homeassistant.* imports. This file runs as plain Python.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    from pysnmp.hlapi.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        get_cmd,
        walk_cmd,
    )
    from pysnmp.proto.errind import ErrorIndication
except ImportError:  # pysnmp not installed (unit-test environment)
    pass  # The client class is never instantiated in tests that mock it.


# ── Exception hierarchy ───────────────────────────────────────────────────────

class SnmpConnectionError(Exception):
    """Raised when the router cannot be reached or returns an SNMP error."""


class SnmpTimeoutError(SnmpConnectionError):
    """Raised when an SNMP operation times out (no response from agent).

    With SNMPv2c, a wrong community string also produces a timeout because the
    agent silently drops packets with an unknown community. This exception
    therefore covers both 'unreachable host' and 'wrong community string' —
    they are indistinguishable at the UDP level.
    """


class SnmpAuthError(SnmpConnectionError):
    """Retained for API parity with the v3 client. Never raised by this client.

    SNMPv2c does not return explicit authentication error PDUs. Wrong community
    strings result in a timeout (SnmpTimeoutError), not this exception.
    """


# ── Internal helpers ──────────────────────────────────────────────────────────

_TIMEOUT_INDICATORS = frozenset([
    "No SNMP response received",
    "No Response",
    "requestTimedOut",
])

_AUTH_INDICATORS = frozenset([
    "wrongDigest",
    "unknownUserName",
    "notInTimeWindow",
    "authenticationFailure",
])


def _classify_error(error_indication: object) -> SnmpConnectionError:
    """Map a pysnmp ErrorIndication to our exception hierarchy."""
    msg = str(error_indication)
    if any(t in msg for t in _TIMEOUT_INDICATORS):
        return SnmpTimeoutError(msg)
    if any(a in msg for a in _AUTH_INDICATORS):
        return SnmpAuthError(msg)
    return SnmpConnectionError(msg)


# ── Client ────────────────────────────────────────────────────────────────────

class ER605SnmpClient:
    """Async SNMPv2c client for the TP-Link ER605 using pysnmplib.

    Parameters
    ----------
    host:       Router IP address.
    port:       SNMP UDP port (default 161).
    community:  SNMPv2c community string.
    timeout:    Seconds to wait per SNMP operation.
    retries:    Number of UDP retries on timeout.
    logger:     Optional logger for DEBUG tracing.
    """

    def __init__(
        self,
        host: str,
        port: int = 161,
        community: str = "public",
        timeout: int = 5,
        retries: int = 1,
        logger: logging.Logger | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._retries = retries
        self._log = logger or logging.getLogger(__name__)
        self._community_data = CommunityData(community, mpModel=1)
        self._engine: SnmpEngine | None = None

    async def _get_engine(self) -> SnmpEngine:
        """Return a cached SnmpEngine, creating it in a thread on first call.

        SnmpEngine() reads MIB files from disk (os.listdir / open) during
        construction so it must not be called directly on the event loop.
        """
        if self._engine is None:
            loop = asyncio.get_running_loop()
            self._engine = await loop.run_in_executor(None, SnmpEngine)
        return self._engine

    async def _transport(self) -> UdpTransportTarget:
        """Create a UdpTransportTarget. pysnmp 7.x requires await .create()."""
        return await UdpTransportTarget.create(
            (self._host, self._port),
            timeout=self._timeout,
            retries=self._retries,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def get(self, oid: str) -> Any:
        """Perform a single SNMP GET. Returns the raw value from the VarBind."""
        self._log.debug("SNMP GET %s -> %s", self._host, oid)
        engine = await self._get_engine()
        error_indication, error_status, error_index, var_binds = await get_cmd(
            engine,
            self._community_data,
            await self._transport(),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
        if error_indication:
            raise _classify_error(error_indication)
        if error_status:
            raise SnmpConnectionError(
                f"SNMP GET {oid} error: {error_status.prettyPrint()} "
                f"at {error_index and var_binds[int(error_index) - 1][0] or '?'}"
            )
        oid_obj, value = var_binds[0]
        result = value.prettyPrint() if hasattr(value, "prettyPrint") else value
        self._log.debug("SNMP GET result: %s = %r", oid, result)
        return result

    async def get_many(self, oids: list[str]) -> dict[str, Any]:
        """Perform a multi-OID SNMP GET in a single PDU.

        Returns {oid_string: value} for each requested OID.
        """
        self._log.debug("SNMP GET-MANY %s -> %d OIDs", self._host, len(oids))
        engine = await self._get_engine()
        var_bind_objects = [ObjectType(ObjectIdentity(oid)) for oid in oids]
        error_indication, error_status, error_index, var_binds = await get_cmd(
            engine,
            self._community_data,
            await self._transport(),
            ContextData(),
            *var_bind_objects,
        )
        if error_indication:
            raise _classify_error(error_indication)
        if error_status:
            raise SnmpConnectionError(
                f"SNMP GET-MANY error: {error_status.prettyPrint()}"
            )
        result = {
            str(vb[0]): (vb[1].prettyPrint() if hasattr(vb[1], "prettyPrint") else vb[1])
            for vb in var_binds
        }
        self._log.debug("SNMP GET-MANY returned %d values", len(result))
        return result

    async def walk(self, base_oid: str) -> dict[str, Any]:
        """Perform an SNMP WALK under base_oid using GETBULK (bulk_cmd loop).

        Returns {full_oid_string: value} for every OID in the subtree.
        Uses GETBULK for efficiency; stops when OIDs leave the base subtree.
        """
        self._log.debug("SNMP WALK %s -> %s", self._host, base_oid)
        engine = await self._get_engine()
        result: dict[str, Any] = {}
        transport = await self._transport()

        # bulk_cmd is a single-shot coroutine; use a GETNEXT-style loop via
        # walk_cmd (GETNEXT-based async generator) with lexicographicMode=False
        # to stop at the subtree boundary.
        async for (error_indication, error_status, _, var_binds) in walk_cmd(
            engine,
            self._community_data,
            transport,
            ContextData(),
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        ):
            if error_indication:
                if result:
                    self._log.warning(
                        "SNMP WALK %s ended early: %s", base_oid, error_indication
                    )
                    break
                raise _classify_error(error_indication)
            if error_status:
                raise SnmpConnectionError(
                    f"SNMP WALK {base_oid} error: {error_status.prettyPrint()}"
                )
            for vb in var_binds:
                oid_str = str(vb[0])
                value = vb[1].prettyPrint() if hasattr(vb[1], "prettyPrint") else vb[1]
                result[oid_str] = value

        self._log.debug("SNMP WALK %s returned %d OIDs", base_oid, len(result))
        return result

    async def walk_typed(self, base_oid: str) -> dict[str, tuple[str, str]]:
        """Perform an SNMP WALK preserving the SNMP type name alongside the value.

        Returns {full_oid_string: (snmp_type_name, pretty_value)}.
        Used by discover_all.py for per-OID type annotation. Not needed by
        other probe scripts which use walk() for simplicity.
        """
        self._log.debug("SNMP WALK-TYPED %s -> %s", self._host, base_oid)
        engine = await self._get_engine()
        result: dict[str, tuple[str, str]] = {}
        transport = await self._transport()

        async for (error_indication, error_status, _, var_binds) in walk_cmd(
            engine,
            self._community_data,
            transport,
            ContextData(),
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        ):
            if error_indication:
                if result:
                    self._log.warning(
                        "SNMP WALK-TYPED %s ended early: %s", base_oid, error_indication
                    )
                    break
                raise _classify_error(error_indication)
            if error_status:
                raise SnmpConnectionError(
                    f"SNMP WALK-TYPED {base_oid} error: {error_status.prettyPrint()}"
                )
            for vb in var_binds:
                oid_str    = str(vb[0])
                raw_val    = vb[1]
                type_name  = type(raw_val).__name__
                pretty_val = raw_val.prettyPrint() if hasattr(raw_val, "prettyPrint") else str(raw_val)
                result[oid_str] = (type_name, pretty_val)

        self._log.debug("SNMP WALK-TYPED %s returned %d OIDs", base_oid, len(result))
        return result
