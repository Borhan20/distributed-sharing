"""License check for Python dependencies (plan §12, §14). Fails on copyleft or source-available licenses.

    uv run python scripts/check_licenses.py

Reads the license metadata of every installed distribution in the environment.
Metadata can be missing or vague; unknown licenses are reported for a human to
check, and only known-bad ones fail the build. This is not legal advice.
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import distributions

ALLOWED = re.compile(r"\b(MIT|BSD|Apache|ISC|PSF|Python Software Foundation|MPL|Mozilla Public License|"
                     r"Unlicense|0BSD|Zlib|PostgreSQL|HPND)\b", re.I)
DENIED = re.compile(r"\b(AGPL|GPL(?!.*(?:exception|linking))|SSPL|BUSL|Business Source|Commons Clause|"
                    r"Elastic License|proprietary)\b", re.I)
# Reviewed exceptions: name -> reason. Keep this list short and explained.
EXCEPTIONS: dict[str, str] = {}
OURS = {"pool-protocol", "pool-cp", "pool-cli", "pool-dev"}


def license_of(dist) -> str:
    md = dist.metadata
    expr = md.get("License-Expression") or ""
    classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    text = (md.get("License") or "").strip().splitlines()[0][:80] if md.get("License") else ""
    return "; ".join(x for x in [expr, *classifiers, text] if x) or "UNKNOWN"


def main() -> int:
    bad, unknown = [], []
    for dist in sorted(distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        if name.lower() in OURS:
            continue
        lic = license_of(dist)
        if name.lower() in EXCEPTIONS:
            continue
        if DENIED.search(lic) and not ALLOWED.search(lic.replace("LGPL", "")):
            bad.append((name, lic))
        elif lic == "UNKNOWN" or not ALLOWED.search(lic):
            unknown.append((name, lic))
    for name, lic in unknown:
        print(f"CHECK   {name}: {lic}")
    for name, lic in bad:
        print(f"DENIED  {name}: {lic}")
    print(f"{len(bad)} denied, {len(unknown)} to check by hand")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
