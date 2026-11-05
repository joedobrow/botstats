"""Checks of the site's defenses: python3 tests/test_security.py

Each is a hole that was found and closed before launch: an off-site
redirect, a Steam login that could be replayed under someone else's id, and
spreadsheet formulas smuggled into the signups CSV.
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="security-test-"), "test.db")
os.environ.setdefault("DISCORD_TOKEN", "dev")

from web import routes, steam    # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    for bad in ("//evil.example", "/\\evil.example", "/\\/evil.example", "https://evil.example",
                "evil.example", "/x\r\nSet-Cookie: a=b", "", None):
        check(f"sends {bad!r} home", routes._safe_next(bad) == "/", routes._safe_next(bad))
    check("keeps a path on this site", routes._safe_next("/season/1/na?tab=teams") == "/season/1/na?tab=teams")

    claimed = "https://steamcommunity.com/openid/id/76561198047746855"
    good = {"openid.mode": "id_res", "openid.op_endpoint": steam.OPENID_URL,
            "openid.return_to": "https://adcl.gg/auth/steam/callback?next=/",
            "openid.claimed_id": claimed, "openid.identity": claimed,
            "openid.signed": "signed,op_endpoint,claimed_id,identity,return_to,response_nonce,assoc_handle"}
    for label, params in (
            ("a login whose signature skips the claimed id",
             {**good, "openid.signed": "signed,op_endpoint,return_to,response_nonce,assoc_handle"}),
            ("a login whose identity and claimed id differ",
             {**good, "openid.identity": claimed[:-1] + "0"}),
            ("a login meant for another site",
             {**good, "openid.return_to": "https://evil.example/auth/steam/callback"})):
        # Refused before Steam is asked, so no network is needed here.
        result = asyncio.run(steam.verify(params, "https://adcl.gg/auth/steam/callback"))
        check(f"refuses {label}", result is None, str(result))

    for formula in ("=HYPERLINK(\"https://evil.example\")", "+1", "-1+2", "@SUM(A1)"):
        check(f"writes {formula!r} as text", routes._csv_cell(formula) == "'" + formula)
    check("leaves ordinary text and numbers alone",
          routes._csv_cell("pos 4/5, 4k") == "pos 4/5, 4k" and routes._csv_cell(-3) == -3)

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
