"""A correct config with someone else's token still writes into someone else's plane.

The namespace fence in ``AgentConfig`` closed the config road to misattribution. This is
the other road, and it was completely unguarded: the only test on the provisioning
template checked that four environment variable NAMES existed. It passed with all four
pointing at the same 1Password item — which is precisely the fix an operator reaches for
when an agent is crash-looping on a missing bearer at deploy time.

We know that reach is real, because Sumi's token was hand-patched into the live env file
and never added to the template. Re-render, she crash-loops, and the fastest thing that
makes the red go away is pasting a sister's bearer.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest
from sdk.bearer_identity import (
    AGENTS,
    BearerIdentityError,
    _op_references,
    _scope_allows,
    check_bearer,
    decode_claims,
    runtime_write_targets,
    verify_env,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "config" / "livekit.env.tpl"

# The 1Password item each agent's bearer MUST come from. Pinned by name, because the whole
# failure mode is a plausible-looking reference to the wrong item — and `musubi-v2-sumi` is
# a REAL item that is the WRONG one. It is her Hermes/fleet presence; her voice line is
# `musubi-v2-sumi-voice`. Guessing that from the pattern gives you the wrong credential and
# no error. (I hashed the live token in her running container against both items rather
# than guess. The obvious guess would have been wrong.)
EXPECTED_ITEM = {
    "nyla": "musubi-v2-nyla",
    "aoi": "musubi-v2-aoi",
    "yua": "musubi-v2-yua",
    "sumi": "musubi-v2-sumi-voice",
}


NEVER = 2_000_000_000  # far-future exp


def _token(sub: str, scope: str, exp: int = NEVER) -> str:
    """A JWT-shaped token. The signature is deliberately garbage: this module's whole point
    is that a well-formed payload proves nothing, and only the server can say otherwise."""
    payload = {
        "iss": "test",
        "aud": "musubi",
        "sub": sub,
        "presence": sub,
        "scope": scope,
        "exp": exp,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{body}.signature"


def _healthy_env() -> dict[str, str]:
    """What the four bearers actually look like in production (verified on mizuki)."""
    env = {
        f"MUSUBI_V2_TOKEN_{a.upper()}": _token(f"{a}/voice", f"{a}/voice:r {a}/voice/*:rw **:r")
        for a in AGENTS
    }
    env["MUSUBI_V2_BASE_URL"] = "http://musubi.invalid:8100/v1"
    return env


def accepts_everything(_base_url: str, _token: str) -> tuple[int, str]:
    """A Musubi that accepts every bearer."""
    return 200, "aoi/voice"


def rejects_everything(_base_url: str, _token: str) -> tuple[int, str]:
    """A Musubi that refuses the token — forged, corrupt, revoked, or wrongly signed."""
    return 401, "Unauthorized"


def unreachable(_base_url: str, _token: str) -> tuple[int, str]:
    return 0, "unreachable: connection refused"


# --- the template: WHERE each bearer comes from -----------------------------------


def test_the_template_provisions_the_right_item_for_each_agent() -> None:
    """The old test asserted four names existed. It never looked at what they pointed AT."""
    refs = _op_references(TEMPLATE.read_text())

    for agent in AGENTS:
        assert agent in refs, f"config/livekit.env.tpl has no bearer line for {agent}"
        item = EXPECTED_ITEM[agent]
        assert f"/{item}/" in refs[agent], (
            f"{agent}'s bearer resolves from {refs[agent]!r}, but her voice token is the "
            f"1Password item {item!r}. A plausible-looking reference to the wrong item is "
            f"silent: she boots, she authorizes, and her memories land in the wrong plane."
        )


def test_no_two_agents_resolve_the_same_bearer() -> None:
    """THE BUG THE OLD TEST LET THROUGH. Nyla and Aoi both pointing at Aoi's item passed."""
    refs = _op_references(TEMPLATE.read_text())

    seen: dict[str, list[str]] = {}
    for agent, ref in refs.items():
        seen.setdefault(ref, []).append(agent)

    shared = {ref: who for ref, who in seen.items() if len(who) > 1}
    assert not shared, (
        f"agents share a bearer: {shared}. Two sisters holding one token are ONE identity at "
        f"the Musubi API — their memories are not merely mixed, they are indistinguishable."
    )


def test_sumi_is_not_provisioned_from_her_fleet_token() -> None:
    """`musubi-v2-sumi` exists. It is the wrong one. That is what makes it dangerous."""
    refs = _op_references(TEMPLATE.read_text())
    assert "/musubi-v2-sumi/" not in refs["sumi"], (
        "sumi's voice bearer is provisioned from `musubi-v2-sumi` — that is her Hermes/fleet "
        "presence, not her voice line (`musubi-v2-sumi-voice`). Both items exist; only one is "
        "hers to speak with."
    )


# --- the resolved bearers: WHO each one is ----------------------------------------


def test_the_healthy_fleet_verifies() -> None:
    verify_env(_healthy_env())


@pytest.mark.parametrize("victim", AGENTS)
def test_a_sisters_token_is_refused(victim: str) -> None:
    """The copy-paste fix. Her config is right, her namespace is right, the token is not."""
    env = _healthy_env()
    thief = "aoi" if victim != "aoi" else "nyla"
    env[f"MUSUBI_V2_TOKEN_{victim.upper()}"] = env[f"MUSUBI_V2_TOKEN_{thief.upper()}"]

    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env)

    assert any("SOMEONE ELSE'S TOKEN" in p for p in exc.value.problems), exc.value.problems
    assert any("share ONE bearer" in p for p in exc.value.problems), exc.value.problems


# NOTE: an earlier test here asserted that `**:rw` would let an agent "write into ANY
# sister's plane". THAT WAS WRONG — I had not read Musubi's matcher, I had inferred it.
# `_namespace_scope_allows` refuses `**` for write outright, so `**:rw` grants no write at
# all. The real danger is the opposite of the one I alarmed about: a scope that LOOKS like
# fleet-write and silently authorizes nothing, so every write fails at runtime. That is
# `test_a_fleet_write_wildcard_grants_nothing_and_is_refused`, below.


def test_the_grandfathered_fleet_read_is_allowed() -> None:
    """It is a decision, not an oversight — so it is asserted, not merely tolerated."""
    env = _healthy_env()
    env["MUSUBI_V2_TOKEN_YUA"] = _token("yua/voice", "yua/voice/*:rw **:r")
    verify_env(env)


def test_a_write_scope_into_a_sisters_tenant_is_refused() -> None:
    """Right subject, wrong write scope — she authorizes as herself and writes into Nyla."""
    env = _healthy_env()
    env["MUSUBI_V2_TOKEN_AOI"] = _token("aoi/voice", "aoi/voice/*:rw nyla/voice/*:rw")

    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env, probe=accepts_everything)

    assert any("nyla" in p for p in exc.value.problems), exc.value.problems


def test_an_unrendered_template_is_refused() -> None:
    """`op://…` still in the file means nothing here is a bearer. Do not boot on it."""
    env = _healthy_env()
    env["MUSUBI_V2_TOKEN_SUMI"] = "op://Harem World/musubi-v2-sumi-voice/credential"

    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env)

    assert any("never rendered" in p for p in exc.value.problems), exc.value.problems


def test_a_missing_bearer_is_refused() -> None:
    env = _healthy_env()
    del env["MUSUBI_V2_TOKEN_NYLA"]

    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env)

    assert any("missing" in p for p in exc.value.problems), exc.value.problems


def test_the_verifier_never_returns_the_token() -> None:
    """The gate runs in CI and on a deploy host. A leaked bearer in a log is a real bearer.

    (I have already redacted a secret with a sed that did not redact. Do not trust that the
    output is clean because it was written to be clean — assert it.)
    """
    secret = _token("aoi/voice", "aoi/voice/*:rw")
    claims = decode_claims(secret)

    rendered = repr(claims)
    assert secret not in rendered
    assert "signature" not in rendered

    env = _healthy_env()
    env["MUSUBI_V2_TOKEN_AOI"] = _token("nyla/voice", "nyla/voice/*:rw")
    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env)

    message = str(exc.value)
    for value in env.values():
        assert value not in message, "the failure message leaked a bearer token"
    assert not re.search(r"\.[A-Za-z0-9_-]{20,}\.", message), "a JWT-shaped string leaked"


# --- the claims are UNSIGNED SELF-REPORT ------------------------------------------
#
# decode_claims() does not verify the signature — it cannot; the secret is Musubi's. So
# every check above answers "what does this token SAY", and none of them answers "does
# Musubi ACCEPT it". A forged, corrupt, expired or revoked token carrying the right-looking
# claims passed the gate, the containers booted, health went green, and the failure surfaced
# on the first memory write of a live call. (Yua, round 2.)


def test_a_server_rejected_bearer_is_refused() -> None:
    """PERFECT CLAIMS, REFUSED BY MUSUBI. This is the whole point of probing."""
    with pytest.raises(BearerIdentityError) as exc:
        verify_env(_healthy_env(), probe=rejects_everything)

    assert any("MUSUBI REJECTED" in p for p in exc.value.problems), exc.value.problems


def test_a_corrupt_but_well_shaped_token_is_refused_by_the_server() -> None:
    """The signature is garbage; the payload is immaculate. Only the server can tell."""
    env = _healthy_env()
    good = env["MUSUBI_V2_TOKEN_AOI"]
    env["MUSUBI_V2_TOKEN_AOI"] = good.rsplit(".", 1)[0] + ".not-a-real-signature"

    def only_aoi_is_rejected(_base: str, token: str) -> tuple[int, str]:
        return (401, "bad signature") if token.endswith("not-a-real-signature") else (200, "")

    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env, probe=only_aoi_is_rejected)

    assert any("aoi: MUSUBI REJECTED" in p for p in exc.value.problems), exc.value.problems


def test_an_expired_bearer_is_refused() -> None:
    """Caught locally from `exp`, before the server has to say it — and long before a call."""
    env = _healthy_env()
    env["MUSUBI_V2_TOKEN_YUA"] = _token("yua/voice", "yua/voice/*:rw", exp=1_000)

    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env, probe=accepts_everything, now=2_000)

    assert any("EXPIRED" in p for p in exc.value.problems), exc.value.problems


def test_an_unreachable_musubi_does_not_pass_the_gate() -> None:
    """UNVERIFIABLE IS NOT VERIFIED — the gate does not pass a check it could not run."""
    with pytest.raises(BearerIdentityError) as exc:
        verify_env(_healthy_env(), probe=unreachable)

    assert any("UNVERIFIABLE IS NOT VERIFIED" in p for p in exc.value.problems), exc.value.problems


def test_a_missing_base_url_does_not_pass_the_gate() -> None:
    env = _healthy_env()
    del env["MUSUBI_V2_BASE_URL"]

    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env, probe=accepts_everything)

    assert any("MUSUBI_V2_BASE_URL is missing" in p for p in exc.value.problems), exc.value.problems


def test_an_accepted_fleet_verifies() -> None:
    """The control: right claims AND the server agrees."""
    verify_env(_healthy_env(), probe=accepts_everything)


def test_the_probe_never_leaks_the_token(monkeypatch) -> None:
    """The probe is the one place the raw bearer touches the network. Watch what comes back."""
    seen: list[str] = []

    def capture(base_url: str, token: str) -> tuple[int, str]:
        seen.append(token)
        return 401, "Unauthorized"

    env = _healthy_env()
    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env, probe=capture)

    assert seen, "the probe was never called — the server check did not run"
    message = str(exc.value)
    for token in seen:
        assert token not in message, "the failure message leaked the bearer it probed with"


# --- she must be able to write WHERE THE TOOLS WRITE ------------------------------
#
# The gate only ever asked "can she write somewhere she must not?". It never asked "can she
# write where she must?" — so a server-accepted bearer with the right tenant and no usable
# write grant passed, and the CLI printed "she may write only to her own plane" about an
# agent who could not write at all. A negative-only check certifies the absence of one bug,
# not the presence of the capability. (Yua, round 3.)


def test_a_read_only_bearer_is_refused() -> None:
    """Server accepts her. Tenant is hers. She cannot save a single memory."""
    env = _healthy_env()
    env["MUSUBI_V2_TOKEN_AOI"] = _token("aoi/voice", "aoi/voice:r **:r")

    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env, probe=accepts_everything)

    assert any("CANNOT WRITE" in p for p in exc.value.problems), exc.value.problems


def test_a_wrong_channel_grant_in_the_right_tenant_is_refused() -> None:
    """`aoi/discord/*:rw` — her tenant, her token, and none of it reaches voice."""
    env = _healthy_env()
    env["MUSUBI_V2_TOKEN_AOI"] = _token("aoi/voice", "aoi/discord/*:rw **:r")

    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env, probe=accepts_everything)

    assert any("CANNOT WRITE" in p for p in exc.value.problems), exc.value.problems


def test_an_exact_prefix_grant_with_too_few_segments_is_refused() -> None:
    """THE SUBTLE ONE. `aoi/voice:rw` looks exactly right and authorizes nothing.

    Musubi requires the segment count to MATCH: a two-segment pattern cannot match the
    three-segment `aoi/voice/episodic` the tools actually write. A bearer scoped to precisely
    the namespace in her config still cannot save a memory.
    """
    env = _healthy_env()
    env["MUSUBI_V2_TOKEN_AOI"] = _token("aoi/voice", "aoi/voice:rw")

    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env, probe=accepts_everything)

    assert any("CANNOT WRITE" in p for p in exc.value.problems), exc.value.problems


def test_a_fleet_write_wildcard_grants_nothing_and_is_refused() -> None:
    """`**:rw` is refused by the allowlist — and separately, it authorizes nothing.

    Musubi REFUSES `**` for writes, so a bearer relying on this grant fails every write. It
    reads as fleet-wide write and behaves as nothing. Both are reasons to reject it; the
    allowlist does not need to know which.
    """
    env = _healthy_env()
    env["MUSUBI_V2_TOKEN_AOI"] = _token("aoi/voice", "**:rw")

    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env, probe=accepts_everything)

    assert any("not permitted" in p for p in exc.value.problems), exc.value.problems
    assert any("CANNOT WRITE" in p for p in exc.value.problems), exc.value.problems


def test_the_wildcard_plane_grant_is_accepted() -> None:
    """POSITIVE COVERAGE: `<agent>/voice/*:rw` — what production actually carries."""
    verify_env(_healthy_env(), probe=accepts_everything)


def test_explicit_per_plane_grants_are_accepted() -> None:
    """POSITIVE COVERAGE: naming each plane instead of wildcarding is equally valid."""
    env = _healthy_env()
    env["MUSUBI_V2_TOKEN_SUMI"] = _token(
        "sumi/voice", "sumi/voice/episodic:rw sumi/voice/thought:rw **:r"
    )
    verify_env(env, probe=accepts_everything)


def test_a_grant_covering_only_episodic_is_refused() -> None:
    """`think` writes to the thought plane. Half a grant is a runtime failure, not a warning."""
    env = _healthy_env()
    env["MUSUBI_V2_TOKEN_YUA"] = _token("yua/voice", "yua/voice/episodic:rw **:r")

    with pytest.raises(BearerIdentityError) as exc:
        verify_env(env, probe=accepts_everything)

    assert any("thought" in p for p in exc.value.problems), exc.value.problems


def test_the_matcher_mirrors_musubi() -> None:
    """These are Musubi's rules, not mine. If they drift, this gate certifies a fiction.

    Re-derived from musubi.auth.scopes._namespace_scope_allows / _namespace_matches.
    """
    assert _scope_allows("aoi/voice/*:rw", "aoi/voice/episodic", "w")
    assert _scope_allows("aoi/voice/episodic:rw", "aoi/voice/episodic", "w")
    assert _scope_allows("aoi/voice/episodic:w", "aoi/voice/episodic", "w")

    # segment count must match exactly
    assert not _scope_allows("aoi/voice:rw", "aoi/voice/episodic", "w")
    assert not _scope_allows("aoi/*:rw", "aoi/voice/episodic", "w")

    # ** reads everything, writes nothing
    assert _scope_allows("**:r", "aoi/voice/episodic", "r")
    assert not _scope_allows("**:rw", "aoi/voice/episodic", "w")

    # read grant is not a write grant
    assert not _scope_allows("aoi/voice/*:r", "aoi/voice/episodic", "w")


def test_the_runtime_targets_are_the_planes_the_tools_write() -> None:
    """If MusubiToolsMixin grows a plane, this gate must grow with it or it stops proving
    write coverage while still claiming to."""
    assert runtime_write_targets("aoi") == ("aoi/voice/episodic", "aoi/voice/thought")


# --- THE TENANT SEGMENT ADMITS NO WILDCARD ----------------------------------------
#
# I wrote `tenant not in (agent, "*")` — hand-excusing `*` in the tenant position, inside the
# function whose entire job is keeping one sister out of another's plane. `*/voice/*:rw`
# covers aoi/voice/episodic, so the write-coverage requirement I had JUST added was satisfied,
# and the same grant authorizes every other sister's voice plane. I punched the hole while
# building the fence. (Yua, round 4 — reproduced at the reviewed head.)


@pytest.mark.parametrize(
    "scope",
    [
        "*/voice/*:rw",  # Yua's exact repro
        "*/*/*:rw",
        "*/voice/episodic:rw",
        "*/voice/*:w",
    ],
)
def test_a_wildcard_tenant_write_is_refused(scope: str) -> None:
    """It satisfies her own coverage AND opens every sister's plane. That is the whole trap:
    a scope that passes the "can she write?" test by being far too permissive."""
    claims = decode_claims(_token("aoi/voice", f"{scope} **:r"))

    problems = check_bearer("aoi", claims)

    assert problems, f"{scope!r} authorizes cross-sister writes and the gate accepted it"
    assert any("every sister" in p or "AUTHORIZES WRITING" in p for p in problems), problems


def test_the_wildcard_tenant_really_does_reach_a_sister() -> None:
    """Yua's second line. The matcher agrees it is a cross-sister write — so this is not a
    style objection about wildcards, it is a live capability."""
    assert _scope_allows("*/voice/*:rw", "nyla/voice/episodic", "w")
    assert _scope_allows("*/voice/*:rw", "yua/voice/thought", "w")


@pytest.mark.parametrize(
    "scope",
    [
        "aoi/voice/*:rw",  # what production actually carries
        "aoi/voice/episodic:rw aoi/voice/thought:rw",  # explicit per-plane
    ],
)
def test_the_allowed_write_grants_pass(scope: str) -> None:
    """The allowlist must still admit the real fleet. A gate that cannot pass production is
    not a gate, it is an outage."""
    claims = decode_claims(_token("aoi/voice", f"{scope} **:r"))

    assert check_bearer("aoi", claims) == []


def test_a_channel_wildcard_in_her_own_tenant_is_refused() -> None:
    """`aoi/*/*:rw` — the tenant is HERS, and it is still refused.

    It grants her discord and hermes planes as well, which makes the sentence this gate
    PRINTS — "she may write only to her own voice plane" — false. The verdict is the contract;
    a grant that outruns the verdict is a lie the gate tells on our behalf. (Yua, round 4
    addendum.)
    """
    claims = decode_claims(_token("aoi/voice", "aoi/*/*:rw **:r"))

    problems = check_bearer("aoi", claims)

    assert problems, "aoi/*/*:rw reaches beyond her voice plane and the gate accepted it"
    assert any("beyond her voice channel" in p for p in problems), problems


def test_the_property_check_catches_what_the_syntax_check_might_not() -> None:
    """The tenant-string rule is only as good as my imagination — which is exactly what failed.

    So the cross-sister fence is ALSO stated as a property, asked through Musubi's own matcher:
    does this scope authorize writing any other agent's runtime target? That question does not
    depend on me predicting the spelling.
    """
    claims = decode_claims(_token("aoi/voice", "nyla/voice/episodic:rw"))
    problems = check_bearer("aoi", claims)

    assert any("AUTHORIZES WRITING" in p and "nyla/voice/episodic" in p for p in problems), problems
