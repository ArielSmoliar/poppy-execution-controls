"""Local demo personal-agent identity; keys are generated, never bundled."""

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from poppy_controls.protocol.auth import thumbprint

CLIENT = "https://agent.example/client.json"
COMPANY = "https://airline.example"


def public_jwk(key):
    return json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))


def initialize(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    root = ec.generate_private_key(ec.SECP256R1())
    public = public_jwk(root)
    public.update(kid=thumbprint(public), alg="ES256", use="sig")
    manifest = {
        "company": COMPANY,
        "clients": {
            CLIENT: {
                "metadata": {
                    "client_id": CLIENT,
                    "client_name": "Local test agent",
                    "jwks_uri": "https://agent.example/jwks.json",
                },
                "jwks": {"keys": [public]},
            }
        },
    }
    (directory / "trust.json").write_text(json.dumps(manifest, indent=2))
    fd = os.open(directory / "agent-private.pem", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(
            root.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )


class Signer:
    def __init__(
        self,
        root,
        client=CLIENT,
        company=COMPANY,
        user="demo-user",
        session=None,
        root_algorithm="ES256",
        root_kid=None,
    ):
        self.root, self.client, self.company, self.user = root, client, company, user
        self.session = session or uuid4().hex
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = public_jwk(self.key)
        self.root_algorithm = root_algorithm
        self.root_kid = root_kid or thumbprint(public_jwk(root))

    def headers(self, method, path):
        now = int(time.time())
        delegation = jwt.encode(
            {
                "iss": self.client,
                "cnf": {"jwk": self.public},
                "iat": now,
                "exp": now + 300,
                "jti": uuid4().hex,
            },
            self.root,
            algorithm=self.root_algorithm,
            headers={"typ": "poppy-delegation+jwt", "kid": self.root_kid},
        )
        token = jwt.encode(
            {
                "iss": self.client,
                "aud": self.company,
                "sub": self.user,
                "sid": self.session,
                "iat": now,
                "jti": uuid4().hex,
                "htm": method,
                "htu": self.company + path.split("?", 1)[0],
            },
            self.key,
            algorithm="ES256",
            headers={
                "typ": "poppy-session+jwt",
                "kid": thumbprint(self.public),
                "poppy_dlg": delegation,
            },
        )
        return {"Poppy-Session": token, "Poppy-Version": "1.0"}

    @classmethod
    def load(cls, directory, **kwargs):
        root = serialization.load_pem_private_key(
            (Path(directory) / "agent-private.pem").read_bytes(), password=None
        )
        manifest = json.loads((Path(directory) / "trust.json").read_text())
        return cls(
            root, client=next(iter(manifest["clients"])), company=manifest["company"], **kwargs
        )
