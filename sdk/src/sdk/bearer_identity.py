"""Prove each agent's Musubi bearer IS that agent — before she ever writes a memory.

``AgentConfig.__post_init__`` now fences the namespace: Aoi cannot be built pointing at
``nyla/voice``. That closes the CONFIG road to misattribution. It does not close the
CREDENTIAL road.

The bearer is the other half of the identity. If ``MUSUBI_V2_TOKEN_NYLA`` resolves to
Aoi's token, Nyla's config is correct, her namespace is correct, every test passes — and
her writes are authorized as, and land under, Aoi. The one test that guarded this checked
that four environment variable NAMES existed in the template. It passed with all four
pointing at the same 1Password item.

That is not hypothetical wiring: Sumi's token was hand-patched into the live env file and
missing from the template, and the tempting fix under deploy pressure — paste a sibling's
bearer, get the agent booting again — is exactly this bug, entered deliberately.

So: read what the token CLAIMS, and check it against who she is meant to be.

A Musubi bearer's payload carries::

    {"sub": "aoi/voice", "presence": "aoi/voice", "scope": "aoi/voice:r aoi/voice/*:rw **:r"}

and the write scopes in there are what actually decide where her memories may land.

**Two things this deliberately does.** It never prints, logs, or returns the token — only
the claims, which are not secret; the signature is. And it reads the ``scope`` claim
directly rather than asking ``GET /v1/namespaces``, because that endpoint splits on ``:``
and DISCARDS the access level — it reports ``**`` identically for a harmless grandfathered
``**:r`` and for a catastrophic ``**:rw``. An instrument that cannot distinguish the safe
case from the dangerous one cannot be the gate.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

AGENTS: tuple[str, ...] = ("nyla", "aoi", "yua", "sumi")

# The voice presence. Each agent's bearer is scoped to her own voice channel — NOT to her
# fleet presence. Sumi is the reason this is spelled out: `musubi-v2-sumi` is her Hermes/
# fleet token and `musubi-v2-sumi-voice` is this one. They are different credentials for
# different presences of the same person, and guessing wrong is silent.
CHANNEL = "voice"

# Grandfathered fleet-wide READ. It lets a sister recall across the fleet; it grants no
# write anywhere. Allowed, and named here so it is a decision rather than an oversight.
GRANDFATHERED_READ = "**"

WRITE_ACCESS = {"w", "rw"}


class BearerIdentityError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        body = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"BEARER IDENTITY FAILED ({len(problems)} problem(s)):\n{body}")


@dataclass(frozen=True)
class BearerClaims:
    """What a token says about itself. Never carries the token."""

    subject: str
    presence: str
    scopes: tuple[str, ...]  # raw "<namespace>:<access>" strings
    expires_at: int = 0  # unix seconds; 0 = no exp claim


def decode_claims(token: str) -> BearerClaims:
    """Read a JWT's payload. NOT a signature check — Musubi does that, with the secret.

    We are not authenticating here; we are asking the token who it thinks it is, which is
    the question the deploy gate needs answered.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise BearerIdentityError(["token is not a JWT (expected three dot-separated parts)"])

    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # base64url, unpadded
    try:
        body = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise BearerIdentityError([f"token payload is not decodable JSON ({exc})"]) from None

    try:
        expires_at = int(body.get("exp", 0) or 0)
    except (TypeError, ValueError):
        expires_at = 0

    return BearerClaims(
        subject=str(body.get("sub", "")),
        presence=str(body.get("presence", "")),
        scopes=tuple(str(body.get("scope", "")).split()),
        expires_at=expires_at,
    )


def probe_bearer(base_url: str, token: str, *, timeout: float = 5.0) -> tuple[int, str]:
    """Ask MUSUBI whether it accepts this bearer. Non-mutating (GET /namespaces).

    THE CLAIMS ARE UNSIGNED SELF-REPORT. ``decode_claims`` reads the payload without
    verifying the signature — it cannot; the secret is Musubi's. So a forged, corrupt,
    expired or revoked token carrying exactly the right-looking claims sails through every
    check above, the gate prints "bearer identity OK", the containers boot, health goes
    green — and her first memory write fails authorization, at 3am, on a call.

    "This token says it is Aoi" and "Musubi accepts this token as Aoi" are different
    sentences, and only the second one is the deploy gate's business. (Yua, round 2.)

    Returns (status_code, detail). Never returns or logs the token; ``base_url`` and the
    status are the only things that come back out.
    """
    request = urllib.request.Request(  # noqa: S310 — fixed https/http scheme from our own config
        f"{base_url.rstrip('/')}/namespaces",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = json.loads(response.read() or b"{}")
            return response.status, " ".join(body.get("items") or [])
    except urllib.error.HTTPError as exc:
        # 401/403 here is the whole point: the server has REFUSED this bearer.
        return exc.code, exc.reason or "rejected"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        # Unreachable is NOT acceptable. A gate that cannot check must not report ok —
        # that is how "advisory" becomes "never".
        return 0, f"unreachable: {exc}"


def runtime_write_targets(agent: str) -> tuple[str, ...]:
    """The namespaces the voice tools ACTUALLY write to at runtime.

    ``MusubiToolsMixin`` appends the plane to ``musubi_v2_namespace``: remember/recent write
    ``<prefix>/episodic``, think writes ``<prefix>/thought``. These are the paths — not the
    two-segment prefix — that the bearer must be able to write.
    """
    return (f"{agent}/{CHANNEL}/episodic", f"{agent}/{CHANNEL}/thought")


def _scope_allows(scope: str, namespace: str, access: str) -> bool:
    """Musubi's OWN matching rules, mirrored exactly (``musubi.auth.scopes``).

    Re-deriving these by intuition is how you build a gate that disagrees with the server it
    is gating. The rules that actually matter here and are easy to get wrong:

    - ``**`` matches anything for READ and is **refused outright for write**. So ``**:rw``
      grants no write at all — a bearer relying on it fails every write, and my earlier
      "fleet-wide write" alarm overstated the danger while missing the real one.
    - Segment counts must match EXACTLY; ``*`` matches exactly one segment. So
      ``aoi/voice:rw`` (two segments) does NOT authorize ``aoi/voice/episodic`` (three).
      A bearer can therefore be scoped to precisely the namespace in the config and still be
      unable to write a single memory.
    """
    pattern, sep, granted = scope.rpartition(":")
    if not sep or granted not in {"r", "w", "rw"}:
        return False

    if pattern == "**":
        if access == "w":
            return False  # musubi refuses ** for writes
        return granted in {"r", "rw"}

    pattern_parts = pattern.split("/")
    ns_parts = namespace.split("/")
    if len(pattern_parts) != len(ns_parts):
        return False
    if not all(p == "*" or p == n for p, n in zip(pattern_parts, ns_parts, strict=True)):
        return False

    return granted == "rw" or granted == access


def check_bearer(agent: str, claims: BearerClaims) -> list[str]:
    """Every way this bearer could belong to someone other than ``agent`` — or be useless.

    Two directions, and I had only built one.

    **Can she write where she must not?** (foreign tenant) — that is misattribution.

    **Can she write where she must?** — I never asked. So a server-accepted bearer carrying
    only ``**:r``, or one writable only to ``aoi/discord/*``, passed every check and the gate
    printed *"she may write only to her own plane"* about an agent who cannot write at all.
    The tenant was hers; the grant was useless. Every memory write fails at runtime, on a
    call, and the deploy gate said OK. (Yua, round 3.)

    A negative-only check certifies the absence of one bug, not the presence of the capability.
    """
    problems: list[str] = []
    expected = f"{agent}/{CHANNEL}"

    if claims.subject != expected:
        problems.append(
            f"{agent}: bearer subject is {claims.subject or '(none)'!r}, expected {expected!r}. "
            f"THIS IS SOMEONE ELSE'S TOKEN. {agent} would authorize as "
            f"{claims.subject.split('/')[0] or 'nobody'} and her memories would land there — "
            f"her config, her namespace and her tests would all still be correct."
        )

    if claims.presence and claims.presence != expected:
        problems.append(f"{agent}: bearer presence is {claims.presence!r}, expected {expected!r}")

    if not claims.scopes:
        problems.append(f"{agent}: bearer carries no scope claim — it can do nothing")

    # --- direction 1: she must not be able to write anywhere else -------
    for scope in claims.scopes:
        namespace, sep, access = scope.rpartition(":")
        if not sep or access not in WRITE_ACCESS:
            continue  # a read scope cannot misattribute a memory

        if namespace == GRANDFATHERED_READ:
            # Musubi refuses ** for writes, so this grants nothing — but a scope that LOOKS
            # like fleet-wide write and silently isn't is its own trap: it invites someone to
            # rely on it, and every write fails at runtime.
            problems.append(
                f"{agent}: bearer carries {scope!r}. Musubi REFUSES '**' for writes, so this "
                f"grants no write at all — it reads as fleet-write and behaves as nothing. "
                f"({GRANDFATHERED_READ}:r is the grandfathered read and is fine.)"
            )
            continue

        tenant = namespace.split("/")[0]
        if tenant not in (agent, "*"):
            problems.append(
                f"{agent}: bearer may WRITE into {namespace!r} — that is {tenant}'s tenant. "
                f"A voice agent writes to her own plane and nowhere else."
            )

    # --- direction 2: she MUST be able to write where the tools write ---
    #
    # This is the half that was missing entirely. Note the segment arithmetic, which is where
    # a plausible-looking grant dies: `aoi/voice:rw` is TWO segments and the tools write THREE
    # (`aoi/voice/episodic`), so it authorizes nothing they do.
    for target in runtime_write_targets(agent):
        if not any(_scope_allows(scope, target, "w") for scope in claims.scopes):
            problems.append(
                f"{agent}: bearer CANNOT WRITE {target!r} — no scope in "
                f"{list(claims.scopes)!r} grants write there. She boots, she is healthy, "
                f"Musubi accepts her token, and every memory she tries to save fails "
                f"authorization mid-call. A read-only bearer, a wrong-channel grant "
                f"({agent}/discord/*:rw), and an exact-prefix grant ({agent}/{CHANNEL}:rw — "
                f"too few segments) all land here."
            )

    return problems


def parse_env_file(path: Path) -> dict[str, str]:
    """Read ``KEY=value`` lines from the rendered secrets env. Values stay in memory."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def verify_env(
    env: dict[str, str],
    agents: tuple[str, ...] = AGENTS,
    *,
    probe: Callable[[str, str], tuple[int, str]] | None = None,
    now: int | None = None,
) -> None:
    """Verify every agent's resolved bearer. Raises with EVERY problem found.

    ``probe`` — if given, called as ``probe(base_url, token)`` per agent and required to
    return 200. This is the half that claim-reading CANNOT do: prove the SERVER accepts the
    token. Injectable so tests can drive a rejecting server without one.
    """
    problems: list[str] = []
    subjects: dict[str, list[str]] = {}
    now = int(time.time()) if now is None else now

    base_url = env.get("MUSUBI_V2_BASE_URL", "")
    if probe is not None and not base_url:
        problems.append(
            "MUSUBI_V2_BASE_URL is missing — the bearers cannot be checked against the server, "
            "and an unverifiable bearer must not be reported as verified"
        )

    for agent in agents:
        var = f"MUSUBI_V2_TOKEN_{agent.upper()}"
        token = env.get(var, "")

        if not token:
            problems.append(f"{agent}: {var} is missing — she crash-loops at start (exit 78)")
            continue

        if token.startswith("op://"):
            problems.append(
                f"{agent}: {var} is still an unresolved 1Password reference — this env file "
                f"was never rendered, so nothing here is a real bearer"
            )
            continue

        try:
            claims = decode_claims(token)
        except BearerIdentityError as exc:
            problems.extend(f"{agent}: {p}" for p in exc.problems)
            continue

        problems.extend(check_bearer(agent, claims))
        subjects.setdefault(claims.subject, []).append(agent)

        # Expiry is in the claims, so we can catch it before the server does — and before
        # the agent does, at 3am, mid-call.
        if claims.expires_at and claims.expires_at <= now:
            problems.append(
                f"{agent}: bearer EXPIRED at {claims.expires_at} (now {now}) — she boots, she "
                f"is healthy, and every memory write fails authorization"
            )

        # And the claims are unsigned self-report, so ask the server.
        if probe is not None and base_url:
            status, detail = probe(base_url, token)
            if status == 200:
                continue
            if status == 0:
                problems.append(
                    f"{agent}: could not reach Musubi to verify the bearer ({detail}). "
                    f"UNVERIFIABLE IS NOT VERIFIED — this gate does not pass on a check it "
                    f"could not run."
                )
            else:
                problems.append(
                    f"{agent}: MUSUBI REJECTED this bearer (HTTP {status} {detail}). The claims "
                    f"look right and the server does not accept it — forged, corrupt, revoked, "
                    f"or signed with a key Musubi no longer trusts. She would boot healthy and "
                    f"fail every memory call."
                )

    # Two agents holding the SAME token. The check above catches it for at least one of
    # them, but say it plainly — this is the shape a copy-paste fix actually takes.
    for subject, holders in sorted(subjects.items()):
        if len(holders) > 1:
            problems.append(
                f"{', '.join(sorted(holders))} share ONE bearer (subject {subject!r}). "
                f"Their memories are indistinguishable at the API — same identity, one plane."
            )

    if problems:
        raise BearerIdentityError(problems)


def _op_references(template_body: str) -> dict[str, str]:
    """agent -> the ``op://`` reference the template provisions for her.

    Captures to end of line, NOT ``\\S+`` — the vault is ``op://Harem World/...`` and a
    non-greedy whitespace match silently truncates every reference to ``op://Harem``, at
    which point all four agents look like they share one item. (They do not. My first
    version of this regex said they did.)
    """
    found = {}
    for var, ref in re.findall(r"^MUSUBI_V2_TOKEN_([A-Z]+)=(.+)$", template_body, re.MULTILINE):
        found[var.lower()] = ref.strip()
    return found


def main(argv: list[str]) -> int:
    """``python -m sdk.bearer_identity <rendered-env-file>``. Prints claims, never tokens."""
    if len(argv) < 2:
        print("usage: python -m sdk.bearer_identity <secrets/livekit-agents.env>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    if not path.is_file():
        print(f"no such env file: {path}", file=sys.stderr)
        return 2

    env = parse_env_file(path)

    # Probe by default. --no-probe is for an offline claims-only run and SAYS SO in the
    # output, because "I checked what the token claims" and "Musubi accepts this token" are
    # different sentences and only one of them is a deploy gate.
    probe = None if "--no-probe" in argv else probe_bearer

    try:
        verify_env(env, probe=probe)
    except BearerIdentityError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for agent in AGENTS:
        claims = decode_claims(env[f"MUSUBI_V2_TOKEN_{agent.upper()}"])
        writes = [s for s in claims.scopes if s.partition(":")[2] in WRITE_ACCESS]
        print(f"  ok  {agent:5} sub={claims.subject:12} writes={' '.join(writes) or '(none)'}")

    if probe is None:
        print("claims OK — NOT verified against Musubi (--no-probe); this is not a deploy gate")
    else:
        print(
            "bearer identity OK — each agent authorizes as herself, Musubi accepts her token, "
            "and she may write only to her own plane"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
