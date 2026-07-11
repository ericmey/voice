"""The tool catalog must match the tools. AGENTS.md calls it authoritative — so it must be.

`tools/README.md` is named by `AGENTS.md` as "the tool catalog (what each tool does, args,
owners)". On 2026-07-11 it documented **eight** tools. Five existed.

The phantoms:

- ``musubi_think``      — un-registered 2026-07-10 (it contradicted every persona)
- ``musubi_get``        — removed 2026-07-09
- ``household_status``  — from ``tools/src/tools/household.py``, a module that **does not
  exist**. Neither does ``HouseholdToolsMixin``. The README's worked example
  (``class NylaAgent(HouseholdToolsMixin, BaseRealtimeAgent)``) would not even import.

A catalog that lists tools an agent does not have is worse than no catalog. It sends the
next reader — future me, a teammate, Eric at 2am — hunting a capability that was deleted, or
worse, *planning around* one. A false map costs more than a blank one, because a blank spot
makes you look and a lie makes you confident.

So the fix is not "edit the README". Editing it fixes today and rots by Friday. The fix is
to make it **structurally unable to lie**: the catalog is checked against the actual
``@function_tool`` methods on the actual mixins, both directions.

If you add a tool, this fails until you document it.
If you delete one, this fails until you un-document it.
"""

from __future__ import annotations

import re
from pathlib import Path

from tools.core import CoreToolsMixin
from tools.memory import MusubiToolsMixin

REPO = Path(__file__).resolve().parents[2]
README = REPO / "tools" / "README.md"

MIXINS = (CoreToolsMixin, MusubiToolsMixin)


def _live_tools() -> dict[str, str]:
    """Every method LiveKit actually exposes as a tool -> the mixin that owns it.

    Read off the classes, not off a list someone maintains. A hand-kept list is just the next
    thing to fall out of sync — which is the entire bug this file exists to prevent.
    """
    found: dict[str, str] = {}
    for mixin in MIXINS:
        for name, fn in vars(mixin).items():
            if not callable(fn):
                continue
            # livekit's @function_tool decorator stamps the callable with its tool info.
            info = getattr(fn, "__livekit_tool_info", None) or getattr(fn, "_tool_info", None)
            if info is not None:
                found[name] = mixin.__name__
    return found


def _live_catalog_section() -> str:
    """Only the LIVE catalog — everything before `## Retired`.

    The Retired table deliberately names `musubi_think`, `musubi_get` and `household_status`,
    and that is CORRECT: a deleted tool that vanishes without trace gets re-proposed six weeks
    later by someone who never knew it existed. What must not happen is a retired tool sitting
    in the LIVE catalog as though an agent still has it.

    (The first cut of this test banned those names anywhere in the file, which would have
    outlawed the retirement notes themselves — the test being over-broad, not the doc wrong.)
    """
    body = README.read_text()
    cut = body.find("## Retired")
    return body[:cut] if cut != -1 else body


def _documented_tools() -> set[str]:
    """Tool names claimed by the LIVE catalog table (the `| \\`name\\` |` column)."""
    return set(
        re.findall(r"^\|\s*`([a-z_][a-z0-9_]*)`\s*\|", _live_catalog_section(), re.MULTILINE)
    )


def test_there_is_at_least_one_real_tool() -> None:
    """Guard the guard: if the introspection silently found nothing, every other assertion
    below would pass vacuously — a green test proving nothing, which is the failure mode this
    whole codebase has been bitten by all day."""
    live = _live_tools()
    assert live, (
        "no @function_tool methods discovered on the mixins. The introspection is broken, "
        "not the catalog — every other test in this file would now pass for the wrong reason."
    )


def test_the_catalog_documents_no_tool_that_does_not_exist() -> None:
    """Phantoms. `musubi_think`, `musubi_get`, `household_status` all sat here after deletion."""
    phantoms = _documented_tools() - set(_live_tools())
    assert not phantoms, (
        f"tools/README.md documents tools that DO NOT EXIST: {sorted(phantoms)}. "
        f"AGENTS.md names this file as the authoritative tool catalog. A catalog that lists a "
        f"capability an agent does not have sends the next reader hunting something that was "
        f"deleted — or planning around it. Delete the row, or restore the tool."
    )


def test_every_real_tool_is_in_the_catalog() -> None:
    """The other direction: a tool nobody documented is a tool nobody knows she has."""
    undocumented = set(_live_tools()) - _documented_tools()
    assert not undocumented, (
        f"these tools exist but are NOT in tools/README.md: {sorted(undocumented)}. "
        f"An undocumented capability is one the next person will re-implement."
    )


def test_the_catalog_names_the_right_owning_mixin() -> None:
    """`musubi_recent` must be attributed to MusubiToolsMixin, not to a class that is gone.

    The README attributed `household_status` to `HouseholdToolsMixin` — which does not exist.
    Getting the OWNER wrong is how you end up composing an agent out of a class that will not
    import.
    """
    body = _live_catalog_section()
    for tool, owner in _live_tools().items():
        row = re.search(rf"^\|\s*`{tool}`\s*\|(.*)$", body, re.MULTILINE)
        assert row, f"{tool} has no catalog row"
        assert owner in row.group(1), (
            f"the catalog row for `{tool}` does not name its real owner ({owner}). "
            f"Composing an agent from the wrong mixin name gives you an ImportError at best "
            f"and the wrong tool set at worst."
        )


def test_the_live_sections_reference_no_class_that_does_not_exist() -> None:
    """The worked example must actually import.

    The README's composition example was
    `class NylaAgent(HouseholdToolsMixin, BaseRealtimeAgent)` — a class that does not exist,
    in the file AGENTS.md calls authoritative. Anyone following it got an ImportError.

    Scoped to the LIVE sections: the Retired table names these on purpose.
    """
    ghosts = ("HouseholdToolsMixin", "MemoryToolsMixin", "household.py")

    # Scoped to CODE BLOCKS and TABLE ROWS — the things a reader copies or composes from.
    # Prose that EXPLAINS why a class was deleted must be allowed to name it; that is the
    # point of a retirement note. What must not survive is a ghost inside a ```python block
    # someone will paste, or in a catalog row they will compose an agent out of.
    live = _live_catalog_section()
    code_blocks = "\n".join(re.findall(r"```(?:python)?\n(.*?)```", live, re.DOTALL))
    table_rows = "\n".join(re.findall(r"^\|.*$", live, re.MULTILINE))
    checkable = code_blocks + "\n" + table_rows

    present = [g for g in ghosts if g in checkable]
    assert not present, (
        f"a code example or catalog row in tools/README.md references {present}, which do not "
        f"exist in tools/src/tools/ (the package contains: base_agent.py, core.py, memory.py). "
        f"The old worked example was `class NylaAgent(HouseholdToolsMixin, BaseRealtimeAgent)` "
        f"— anyone copying it got an ImportError, out of the file AGENTS.md calls "
        f"authoritative."
    )
