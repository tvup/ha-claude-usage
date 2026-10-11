"""DataUpdateCoordinator for Claude Usage."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BASE_URL,
    CONF_CF_CLEARANCE,
    CONF_ORG_ID,
    CONF_SESSION_KEY,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    OPTIONAL_ENDPOINTS,
    UPDATE_INTERVAL,
    USAGE_ENDPOINT,
)

_LOGGER = logging.getLogger(__name__)

# Timeouts détaillés : chaque phase a son propre budget
_TIMEOUT = aiohttp.ClientTimeout(
    total=30,        # budget global de la requête entière
    connect=8,       # acquisition d'une connexion depuis le pool (DNS + TCP + TLS)
    sock_connect=8,  # établissement TCP+TLS pour une nouvelle connexion
    sock_read=20,    # délai maximum entre deux lectures successives (TTFB inclus)
)

# Headers qui passent Cloudflare — copie de scripts/check_session_usage.py
_BROWSER_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9,fr;q=0.8",
    "accept-encoding": "gzip, deflate",
    "referer": "https://claude.ai/settings/usage",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


# ---------------------------------------------------------------------------
# Helpers de masquage
# ---------------------------------------------------------------------------

def _mask(value: str, keep: int = 8) -> str:
    """Retourne les `keep` premiers caractères suivis de '[masqué]'."""
    if not value:
        return "(vide)"
    if len(value) <= keep:
        return "[masqué]"
    return value[:keep] + "…[masqué]"


def _safe_headers(headers: dict) -> dict:
    """Copie des headers avec la valeur du cookie masquée (noms visibles)."""
    safe = dict(headers)
    if "cookie" in safe:
        masked = []
        for part in safe["cookie"].split("; "):
            if "=" in part:
                name, val = part.split("=", 1)
                masked.append(f"{name}={_mask(val)}")
            else:
                masked.append(part)
        safe["cookie"] = "; ".join(masked)
    return safe


# ---------------------------------------------------------------------------
# Trace aiohttp : mesure de chaque phase réseau
# ---------------------------------------------------------------------------

async def _trace_on_request_start(
    session: aiohttp.ClientSession,
    ctx: aiohttp.TraceRequestContext,
    params: aiohttp.TraceRequestStartParams,
) -> None:
    td = ctx.trace_request_ctx
    td["t_start"] = time.monotonic()
    _LOGGER.debug("[TRACE] Début requête → %s %s", params.method, params.url)


async def _trace_on_dns_start(
    session: aiohttp.ClientSession,
    ctx: aiohttp.TraceRequestContext,
    params: aiohttp.TraceDnsResolveHostStartParams,
) -> None:
    td = ctx.trace_request_ctx
    td["t_dns_start"] = time.monotonic()
    _LOGGER.debug("[TRACE][DNS] Résolution de '%s' en cours…", params.host)


async def _trace_on_dns_end(
    session: aiohttp.ClientSession,
    ctx: aiohttp.TraceRequestContext,
    params: aiohttp.TraceDnsResolveHostEndParams,
) -> None:
    td = ctx.trace_request_ctx
    td["t_dns_end"] = time.monotonic()
    dns_ms = int((td["t_dns_end"] - td.get("t_dns_start", td["t_dns_end"])) * 1000)
    _LOGGER.debug("[TRACE][DNS] '%s' résolu en %d ms", params.host, dns_ms)


async def _trace_on_connect_start(
    session: aiohttp.ClientSession,
    ctx: aiohttp.TraceRequestContext,
    params: aiohttp.TraceConnectionCreateStartParams,
) -> None:
    td = ctx.trace_request_ctx
    td["t_connect_start"] = time.monotonic()
    _LOGGER.debug("[TRACE][TCP/TLS] Établissement de la connexion…")


async def _trace_on_connect_end(
    session: aiohttp.ClientSession,
    ctx: aiohttp.TraceRequestContext,
    params: aiohttp.TraceConnectionCreateEndParams,
) -> None:
    td = ctx.trace_request_ctx
    td["t_connect_end"] = time.monotonic()
    connect_ms = int(
        (td["t_connect_end"] - td.get("t_connect_start", td["t_connect_end"])) * 1000
    )
    _LOGGER.debug("[TRACE][TCP/TLS] Connexion établie en %d ms", connect_ms)


async def _trace_on_connection_reuse(
    session: aiohttp.ClientSession,
    ctx: aiohttp.TraceRequestContext,
    params: aiohttp.TraceConnectionReuseconnParams,
) -> None:
    td = ctx.trace_request_ctx
    td["t_connect_end"] = time.monotonic()
    td["connection_reused"] = True
    _LOGGER.debug("[TRACE][TCP/TLS] Connexion réutilisée depuis le pool")


async def _trace_on_headers_sent(
    session: aiohttp.ClientSession,
    ctx: aiohttp.TraceRequestContext,
    params: aiohttp.TraceRequestHeadersSentParams,
) -> None:
    td = ctx.trace_request_ctx
    td["t_headers_sent"] = time.monotonic()
    since_start = int(
        (td["t_headers_sent"] - td.get("t_start", td["t_headers_sent"])) * 1000
    )
    _LOGGER.debug("[TRACE][HTTP] Headers envoyés (+%d ms depuis début)", since_start)


async def _trace_on_chunk_received(
    session: aiohttp.ClientSession,
    ctx: aiohttp.TraceRequestContext,
    params: aiohttp.TraceResponseChunkReceivedParams,
) -> None:
    td = ctx.trace_request_ctx
    if "t_first_byte" not in td:
        td["t_first_byte"] = time.monotonic()
        since_start = int(
            (td["t_first_byte"] - td.get("t_start", td["t_first_byte"])) * 1000
        )
        since_send = (
            int((td["t_first_byte"] - td["t_headers_sent"]) * 1000)
            if "t_headers_sent" in td
            else "N/A"
        )
        _LOGGER.debug(
            "[TRACE][HTTP] Premier octet reçu (TTFB) : +%d ms total, +%s ms depuis envoi",
            since_start,
            since_send,
        )


async def _trace_on_request_end(
    session: aiohttp.ClientSession,
    ctx: aiohttp.TraceRequestContext,
    params: aiohttp.TraceRequestEndParams,
) -> None:
    td = ctx.trace_request_ctx
    td["t_end"] = time.monotonic()
    total_ms = int((td["t_end"] - td.get("t_start", td["t_end"])) * 1000)
    _LOGGER.debug(
        "[TRACE][HTTP] Requête complète — status=%d, durée=%d ms",
        params.response.status,
        total_ms,
    )


async def _trace_on_request_exception(
    session: aiohttp.ClientSession,
    ctx: aiohttp.TraceRequestContext,
    params: aiohttp.TraceRequestExceptionParams,
) -> None:
    td = ctx.trace_request_ctx
    elapsed = int((time.monotonic() - td.get("t_start", time.monotonic())) * 1000)
    _LOGGER.warning(
        "[TRACE][HTTP] Exception après %d ms : %s — %s",
        elapsed,
        type(params.exception).__name__,
        params.exception,
    )


def _build_trace_config() -> aiohttp.TraceConfig:
    """Construit un TraceConfig qui mesure chaque phase réseau."""
    tc = aiohttp.TraceConfig()
    tc.on_request_start.append(_trace_on_request_start)
    tc.on_dns_resolvehost_start.append(_trace_on_dns_start)
    tc.on_dns_resolvehost_end.append(_trace_on_dns_end)
    tc.on_connection_create_start.append(_trace_on_connect_start)
    tc.on_connection_create_end.append(_trace_on_connect_end)
    tc.on_connection_reuseconn.append(_trace_on_connection_reuse)
    tc.on_request_headers_sent.append(_trace_on_headers_sent)
    tc.on_response_chunk_received.append(_trace_on_chunk_received)
    tc.on_request_end.append(_trace_on_request_end)
    tc.on_request_exception.append(_trace_on_request_exception)
    return tc


def _diagnose_timeout(td: dict) -> None:
    """Émet un WARNING identifiant la phase où le timeout s'est produit."""
    t_now = time.monotonic()
    t_start = td.get("t_start")
    elapsed_ms = int((t_now - t_start) * 1000) if t_start else "?"

    if "t_dns_start" not in td:
        phase = "avant la résolution DNS — la connexion est bloquée dès le départ (firewall, réseau injoignable ?)"
    elif "t_dns_end" not in td:
        phase = "pendant la résolution DNS — le serveur DNS ne répond pas"
    elif "t_connect_start" not in td:
        phase = "entre la résolution DNS et la tentative TCP — problème de routage ?"
    elif "t_connect_end" not in td:
        phase = (
            "pendant la connexion TCP/TLS — "
            "Cloudflare drop silencieux, port bloqué, ou TLS handshake suspendu"
        )
    elif "t_headers_sent" not in td:
        phase = "entre la connexion et l'envoi des headers HTTP"
    elif "t_first_byte" not in td:
        phase = (
            "en attente des headers HTTP de réponse (TTFB dépassé) — "
            "serveur lent, session expirée côté Cloudflare, ou challenge JS en cours"
        )
    else:
        phase = "pendant la lecture du corps de réponse — réponse partiellement reçue"

    _LOGGER.warning(
        "[TIMEOUT] Timeout après %s ms | Phase identifiée : %s",
        elapsed_ms,
        phase,
    )


# ---------------------------------------------------------------------------
# Transformations de la réponse API
# ---------------------------------------------------------------------------

def _minutes_until(iso: str) -> int | None:
    """Retourne les minutes restantes jusqu'à un timestamp ISO, ou None."""
    try:
        dt = datetime.fromisoformat(iso)
        now = datetime.now(timezone.utc)
        return max(0, int((dt - now).total_seconds() / 60))
    except Exception:
        return None


def _parse_slot(raw: dict, key: str) -> dict | None:
    entry = raw.get(key)
    if not entry:
        return None
    resets_at = entry.get("resets_at")
    return {
        "utilization": entry.get("utilization") or 0.0,
        "resets_at": resets_at,
        "resets_in_minutes": _minutes_until(resets_at) if resets_at else None,
    }


def _first(*values):
    """Retourne la première valeur non-None."""
    for value in values:
        if value is not None:
            return value
    return None


def _deep_find(payload, keys: tuple[str, ...]):
    """Cherche récursivement la première clé (insensible à la casse) parmi `keys`.

    Les noms de champs des nouvelles sections de claude.ai/settings/usage ne sont
    pas documentés : on accepte plusieurs variantes plutôt que d'échouer.
    """
    wanted = {k.lower() for k in keys}
    stack = [payload]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in wanted and value is not None:
                    return value
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _round2(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _subscription_type(raw: dict, organization: dict) -> str | None:
    """Déduit le plan (Free / Pro / Max 5x / Max 20x / Team / Enterprise)."""
    explicit = _deep_find(raw, ("subscription_type", "plan_type", "plan"))
    if isinstance(explicit, str) and explicit:
        return explicit

    tier = str(organization.get("rate_limit_tier") or "").lower()
    capabilities = [str(c).lower() for c in organization.get("capabilities") or []]
    billing = str(organization.get("billing_type") or "").lower()

    if "max_20x" in tier:
        return "Max 20x"
    if "max_5x" in tier:
        return "Max 5x"
    if "claude_max" in capabilities or "max" in tier:
        return "Max"
    if "claude_pro" in capabilities or "pro" in tier:
        return "Pro"
    if "enterprise" in tier or "enterprise" in billing:
        return "Enterprise"
    if "raven" in capabilities or "team" in tier:
        return "Team"
    if organization:
        return "Free"
    return None


def _limit_resets(payloads: dict) -> dict:
    """Section « Limit resets » : liste de resets disponibles avec date d'expiration."""
    # /usage expose les resets sous le nom de code `cedar_ember.grants`.
    grants = ((payloads.get("usage") or {}).get("cedar_ember") or {}).get("grants")
    if isinstance(grants, list):
        left = [g for g in grants if isinstance(g, dict)]
        expiries = sorted(
            str(g["ends_at"]) for g in left if g.get("ends_at") and (g.get("resets_left") or 0) > 0
        )
        return {
            "count": int(sum(_number(g.get("resets_left")) or 0 for g in left)),
            "next_expires_at": expiries[0] if expiries else None,
        }
    found = _deep_find(
        payloads,
        ("limit_resets", "rate_limit_resets", "available_resets", "reset_grants"),
    )
    if isinstance(found, list):
        expiries = sorted(
            str(e) for item in found if isinstance(item, dict)
            for e in [_first(item.get("expires_at"), item.get("expiration"), item.get("expiry"))]
            if e
        )
        return {"count": len(found), "next_expires_at": expiries[0] if expiries else None}
    count = _number(found)
    if count is not None:
        return {"count": int(count), "next_expires_at": None}
    return {"count": None, "next_expires_at": None}


def _cloud_credits(payloads: dict) -> dict:
    """Section « Cloud session credits » (montants en cents si le champ l'indique)."""
    # /usage expose le crédit sous le nom de code `iguana_necktie` (montants en dollars).
    slot = (payloads.get("usage") or {}).get("iguana_necktie")
    if isinstance(slot, dict) and slot.get("limit_dollars") is not None:
        return {
            "remaining": _round2(_number(slot.get("remaining_dollars"))),
            "total": _round2(_number(slot.get("limit_dollars"))),
            "expires_at": slot.get("resets_at"),
            "currency": "USD",
        }
    section = _deep_find(
        payloads,
        ("cloud_session_credits", "cloud_session_credit", "cloud_credits", "remote_session_credits"),
    )
    if not isinstance(section, dict):
        return {"remaining": None, "total": None, "expires_at": None, "currency": None}

    def amount(*keys: str) -> float | None:
        for key in keys:
            value = _number(section.get(key))
            if value is not None:
                return value / 100 if key.endswith(("_cents", "_minor_units")) else value
        return None

    return {
        "remaining": amount(
            "remaining", "remaining_amount", "balance", "left", "amount_remaining",
            "remaining_cents", "balance_cents", "remaining_minor_units",
        ),
        "total": amount(
            "total", "total_amount", "granted", "initial_amount", "amount",
            "total_cents", "granted_cents", "amount_cents", "amount_minor_units",
        ),
        "expires_at": _first(section.get("expires_at"), section.get("expiration"), section.get("expiry")),
        "currency": section.get("currency"),
    }


def _usage_credits(payloads: dict) -> dict:
    """Section « Usage credits » : solde prépayé (API en unités mineures = cents)."""
    prepaid = payloads.get("prepaid_credits") or {}
    cents = _number(_first(prepaid.get("amount"), prepaid.get("balance"), prepaid.get("amount_cents")))
    return {
        "amount": round(cents / 100, 2) if cents is not None else None,
        "amount_cents": int(cents) if cents is not None else None,
        "currency": prepaid.get("currency"),
    }


def _transform(raw: dict, extras: dict | None = None) -> dict:
    """Transforme la réponse brute de l'API en structure attendue par les sensors.

    `extras` contient les réponses des endpoints optionnels (clé = OPTIONAL_ENDPOINTS).
    """
    extras = extras or {}
    payloads = {"usage": raw, **extras}
    # extra_usage vaut null quand l'usage supplémentaire est désactivé :
    # on se rabat alors sur overage_spend_limit (mêmes montants, en cents).
    extra = raw.get("extra_usage") or {}
    overage = extras.get("overage_spend_limit") or {}
    if extra:
        used = extra.get("used_credits")
        limit = extra.get("monthly_limit")
        enabled = extra.get("is_enabled", False)
        currency = extra.get("currency")
    else:
        used = overage.get("used_credits")
        limit = overage.get("monthly_credit_limit")
        enabled = overage.get("is_enabled", False)
        currency = overage.get("currency")
    return {
        "session_5h": _parse_slot(raw, "five_hour"),
        "weekly": _parse_slot(raw, "seven_day"),
        "weekly_sonnet": _parse_slot(raw, "seven_day_sonnet"),
        "weekly_opus": _parse_slot(raw, "seven_day_opus"),
        "extra_usage": {
            "enabled": enabled,
            "used": used,
            "limit": limit,
            "utilization": round((used or 0) / limit * 100, 1) if limit else None,
            "currency": currency,
        },
        "subscription_type": _subscription_type(raw, extras.get("organization") or {}),
        "limit_resets": _limit_resets(payloads),
        "cloud_credits": _cloud_credits(payloads),
        "usage_credits": _usage_credits(payloads),
        "raw": payloads,
    }


async def _fetch_optional(
    session: aiohttp.ClientSession, headers: dict, org_id: str
) -> dict:
    """Récupère les endpoints optionnels ; les erreurs sont journalisées puis ignorées."""
    results: dict = {}
    for name, endpoint in OPTIONAL_ENDPOINTS.items():
        url = BASE_URL + endpoint.format(org_id=org_id)
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=_TIMEOUT,
                allow_redirects=False,
                trace_request_ctx={},
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug("[OPTIONNEL] %s → status %d, ignoré", name, resp.status)
                    continue
                payload = json.loads(await resp.text())
                if isinstance(payload, dict):
                    results[name] = payload
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.debug("[OPTIONNEL] %s → %s: %s, ignoré", name, type(err).__name__, err)
    return results


# ---------------------------------------------------------------------------
# Requête HTTP partagée (validate_credentials + coordinator)
# ---------------------------------------------------------------------------

async def _fetch_usage(
    session_key: str,
    cf_clearance: str,
    org_id: str,
) -> tuple[dict, object]:
    """Effectue la requête API avec traçage complet de chaque phase réseau.

    Retourne (données_transformées, cookies_réponse).
    Lève ConfigEntryAuthFailed ou UpdateFailed selon le cas d'erreur.
    """
    url = BASE_URL + USAGE_ENDPOINT.format(org_id=org_id)
    headers = {
        **_BROWSER_HEADERS,
        "cookie": (
            f"sessionKey={quote(session_key, safe='')}; "
            f"cf_clearance={quote(cf_clearance, safe='')}"
        ),
    }

    _LOGGER.debug(
        "[FETCH] URL: %s | Timeout: total=%ds connect=%ds sock_connect=%ds sock_read=%ds",
        url,
        _TIMEOUT.total,
        _TIMEOUT.connect,
        _TIMEOUT.sock_connect,
        _TIMEOUT.sock_read,
    )
    _LOGGER.debug("[FETCH] Headers envoyés (secrets masqués): %s", _safe_headers(headers))

    trace_data: dict = {}
    trace_config = _build_trace_config()

    try:
        async with aiohttp.ClientSession(trace_configs=[trace_config]) as session:
            async with session.get(
                url,
                headers=headers,
                timeout=_TIMEOUT,
                allow_redirects=False,
                trace_request_ctx=trace_data,
            ) as resp:
                _LOGGER.debug(
                    "[HTTP] Réponse reçue : status=%d, content-type=%s, "
                    "cookies présents=%s, headers=%s",
                    resp.status,
                    resp.content_type,
                    list(resp.cookies.keys()),
                    dict(resp.headers),
                )

                # Redirection → session expirée
                if resp.status == 302:
                    location = resp.headers.get("Location", "(inconnu)")
                    _LOGGER.warning(
                        "[AUTH] Redirection 302 vers '%s' — session expirée ou cookie invalide",
                        location,
                    )
                    raise ConfigEntryAuthFailed("session_expired")

                # Accès refusé — distinguer Cloudflare d'une expiration de session
                if resp.status in (401, 403):
                    body = await resp.text()
                    cf_blocked = any(
                        x in body.lower()
                        for x in ("cloudflare", "just a moment", "cf-ray", "turnstile")
                    )
                    _LOGGER.warning(
                        "[AUTH] Status %d — cloudflare_block=%s | Extrait corps: %.400s",
                        resp.status,
                        cf_blocked,
                        body,
                    )
                    raise ConfigEntryAuthFailed(
                        "cf_clearance_expired" if cf_blocked else "session_expired"
                    )

                if resp.status != 200:
                    body_preview = (await resp.text())[:400]
                    _LOGGER.warning(
                        "[HTTP] Status inattendu %d | Extrait: %s",
                        resp.status,
                        body_preview,
                    )
                    raise UpdateFailed(f"HTTP {resp.status} inattendu")

                body = await resp.text()
                try:
                    raw = json.loads(body)
                except ValueError:
                    _LOGGER.warning(
                        "[JSON] Réponse non-JSON (content-type=%s): %.300s",
                        resp.content_type,
                        body,
                    )
                    raise UpdateFailed("Réponse non-JSON de l'API")

                _LOGGER.debug("[JSON] Réponse brute: %s", raw)
                cookies = resp.cookies

            extras = await _fetch_optional(session, headers, org_id)
            return _transform(raw, extras), cookies

    except ConfigEntryAuthFailed:
        raise

    except asyncio.TimeoutError as err:
        _diagnose_timeout(trace_data)
        raise UpdateFailed(
            "Timeout réseau — consultez les logs DEBUG pour la phase exacte"
        ) from err

    except aiohttp.ServerTimeoutError as err:
        _diagnose_timeout(trace_data)
        raise UpdateFailed(f"ServerTimeoutError: {err}") from err

    except aiohttp.ClientConnectorError as err:
        elapsed = int((time.monotonic() - trace_data.get("t_start", time.monotonic())) * 1000)
        _LOGGER.warning(
            "[RÉSEAU] Impossible de se connecter après %d ms : %s (os_error=%s)",
            elapsed,
            err,
            getattr(err, "os_error", None),
        )
        raise UpdateFailed(f"Connexion impossible: {err}") from err

    except aiohttp.ClientConnectionError as err:
        elapsed = int((time.monotonic() - trace_data.get("t_start", time.monotonic())) * 1000)
        _LOGGER.warning(
            "[RÉSEAU] Erreur de connexion après %d ms : %s: %s",
            elapsed,
            type(err).__name__,
            err,
        )
        raise UpdateFailed(f"Erreur de connexion: {err}") from err

    except aiohttp.ClientResponseError as err:
        _LOGGER.warning(
            "[HTTP] ClientResponseError : status=%d, message=%s",
            err.status,
            err.message,
        )
        raise UpdateFailed(f"Erreur réponse HTTP {err.status}: {err.message}") from err

    except aiohttp.ClientError as err:
        elapsed = int((time.monotonic() - trace_data.get("t_start", time.monotonic())) * 1000)
        _LOGGER.warning(
            "[RÉSEAU] Erreur aiohttp inattendue après %d ms : %s — %s",
            elapsed,
            type(err).__name__,
            err,
        )
        raise UpdateFailed(f"Erreur réseau ({type(err).__name__}): {err}") from err


# ---------------------------------------------------------------------------
# Validation des credentials (config_flow)
# ---------------------------------------------------------------------------

async def validate_credentials(
    session_key: str, cf_clearance: str, org_id: str
) -> dict:
    """Valide les credentials via un vrai appel API.

    Retourne (données_parsées, cookies).
    Lève ConfigEntryAuthFailed ou aiohttp.ClientError en cas d'échec.
    """
    return await _fetch_usage(session_key, cf_clearance, org_id)


# ---------------------------------------------------------------------------
# Métriques
# ---------------------------------------------------------------------------

@dataclass
class UsageMetrics:
    """Accumule les métriques du plugin sur les cycles de polling."""
    total_requests: int = 0
    failed_requests: int = 0
    cookie_renewals: int = 0
    last_success_at: str | None = None   # timestamp ISO
    last_response_ms: int | None = None  # durée du dernier appel réussi


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class ClaudeUsageCoordinator(DataUpdateCoordinator[dict]):
    """Récupère les données d'usage Claude toutes les N secondes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        interval = int(entry.options.get(CONF_UPDATE_INTERVAL, UPDATE_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.entry = entry
        self.metrics = UsageMetrics()

    async def _async_update_data(self) -> dict:
        session_key = self.entry.data[CONF_SESSION_KEY]
        cf_clearance = self.entry.data[CONF_CF_CLEARANCE]
        org_id = self.entry.data[CONF_ORG_ID]

        self.metrics.total_requests += 1
        t_start = time.monotonic()

        try:
            data, cookies = await _fetch_usage(session_key, cf_clearance, org_id)
        except (ConfigEntryAuthFailed, UpdateFailed):
            self.metrics.failed_requests += 1
            raise

        # Succès — enregistrer les métriques de timing
        self.metrics.last_response_ms = int((time.monotonic() - t_start) * 1000)
        self.metrics.last_success_at = datetime.now(timezone.utc).isoformat()

        # Renouvellement automatique des cookies si le serveur en émet de nouveaux
        updates: dict = {}
        if "cf_clearance" in cookies:
            new_val = cookies["cf_clearance"].value
            if new_val and new_val != cf_clearance:
                updates[CONF_CF_CLEARANCE] = new_val
                self.metrics.cookie_renewals += 1
                _LOGGER.debug("[COOKIES] cf_clearance renouvelé automatiquement")
        if "sessionKey" in cookies:
            new_val = cookies["sessionKey"].value
            if new_val and new_val != session_key:
                updates[CONF_SESSION_KEY] = new_val
                _LOGGER.debug("[COOKIES] sessionKey renouvelé automatiquement")

        if updates:
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, **updates},
            )

        return data
