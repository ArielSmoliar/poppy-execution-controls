"""Poppy delegated Session JWT verification against operator-pinned public metadata.

No request-controlled URL is fetched. Provisioning/refreshing the trust manifest is
an operator responsibility in this reference implementation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import jwt

ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class ProtocolError(Exception):
    def __init__(self, code, status=400, headers=None):
        self.code, self.status, self.headers = code, status, headers or {}
        super().__init__(code)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def thumbprint(key):
    canonical = {k: key[k] for k in ("crv", "kty", "x", "y")}
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()


def https_url(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
        or url.query
    ):
        raise ValueError("Expected an absolute HTTPS URL without credentials, query or fragment")
    return url


@dataclass(frozen=True)
class Identity:
    client: str
    user: str
    session: str
    agent: str | None
    version: str
    extensions: str

    @property
    def owner(self):
        return digest([self.client, self.user])

    @property
    def provider(self):
        return digest([self.client])


class SessionVerifier:
    def __init__(self, company, clients):
        https_url(company)
        self.company = company
        self.clients = clients
        for client, entry in clients.items():
            host = https_url(client).netloc
            if entry["metadata"]["client_id"] != client:
                raise ValueError("Pinned metadata client_id must match its source URL")
            if https_url(entry["metadata"]["jwks_uri"]).netloc != host:
                raise ValueError("Pinned jwks_uri must share the client_id host")
            keys = entry["jwks"]["keys"]
            if not keys or len({k.get("kid") for k in keys}) != len(keys):
                raise ValueError("Trust keys need distinct kid values")
            for key in keys:
                if any(k in key for k in ("d", "p", "q", "dp", "dq", "qi", "oth", "k")):
                    raise ValueError("Trust manifest must contain public keys only")

    def verify(self, token, method, url, version, extensions="", authorization=None):
        if not isinstance(version, str) or not re.fullmatch(r"1\.[0-9]+", version):
            raise ProtocolError("version_unsupported")
        if authorization is not None:
            # This API offers no account access; never silently ignore supplied credentials.
            raise ProtocolError(
                "invalid_token", 401, {"WWW-Authenticate": 'Bearer error="invalid_token"'}
            )
        try:
            if not isinstance(token, str) or len(token) > 16384:
                raise ValueError("Invalid token size")
            header = jwt.get_unverified_header(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
            client = unverified["iss"]
            entry = self.clients[client]
            if header.get("typ") != "poppy-session+jwt" or header.get("alg") != "ES256":
                raise ValueError("Wrong session token type or algorithm")
            if header.get("crit"):
                raise ValueError("Unsupported critical header")
            delegation = header["poppy_dlg"]
            dh = jwt.get_unverified_header(delegation)
            if dh.get("typ") != "poppy-delegation+jwt" or dh.get("alg") not in ("ES256", "PS384"):
                raise ValueError("Wrong delegation type or algorithm")
            if dh.get("crit"):
                raise ValueError("Unsupported critical header")
            root = next(k for k in entry["jwks"]["keys"] if k["kid"] == dh["kid"])
            algorithm = dh["alg"]
            if root.get("alg", algorithm) != algorithm or root.get("use", "sig") != "sig":
                raise ValueError("Root key is not authorized for this signature")
            if "key_ops" in root and "verify" not in root["key_ops"]:
                raise ValueError("Root key cannot verify")
            proof = jwt.decode(
                delegation,
                jwt.PyJWK.from_dict(root, algorithm=algorithm).key,
                algorithms=[algorithm],
                issuer=client,
                options={"require": ["iss", "iat", "exp", "jti", "cnf"]},
            )
            if (
                type(proof["iat"]) is not int
                or type(proof["exp"]) is not int
                or not 0 < proof["exp"] - proof["iat"] <= 86400
                or not isinstance(proof["jti"], str)
                or not proof["jti"]
            ):
                raise ValueError("Invalid delegation lifetime or identifier")
            key = proof["cnf"]["jwk"]
            if (
                key.get("kty") != "EC"
                or key.get("crv") != "P-256"
                or "d" in key
                or key.get("alg", "ES256") != "ES256"
                or key.get("use", "sig") != "sig"
                or ("key_ops" in key and "verify" not in key["key_ops"])
            ):
                raise ValueError("Invalid delegated public key")
            if header["kid"] != thumbprint(key):
                raise ValueError("Delegated thumbprint mismatch")
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_dict(key, algorithm="ES256").key,
                algorithms=["ES256"],
                audience=self.company,
                issuer=client,
                options={
                    "require": ["iss", "aud", "sid", "sub", "htm", "htu", "iat", "jti"],
                    "strict_aud": True,
                },
            )
            if (
                type(claims["iat"]) is not int
                or not 0 <= time.time() - claims["iat"] <= 60
                or claims["iat"] > proof["exp"]
                or "ath" in claims
                or claims["htm"] != method
                or claims["htu"] != url
            ):
                raise ValueError("Stale or incorrectly bound session")
            for name in ("sid", "sub", "jti"):
                if not isinstance(claims[name], str) or not ID.fullmatch(claims[name]):
                    raise ValueError("Invalid session identifier")
            agent = claims.get("agent_id")
            if agent is not None and (not isinstance(agent, str) or not ID.fullmatch(agent)):
                raise ValueError("Invalid agent identifier")
            return Identity(
                client, claims["sub"], claims["sid"], agent, version, extensions
            ), claims["jti"]
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, StopIteration, AttributeError):
            raise ProtocolError("invalid_session") from None
