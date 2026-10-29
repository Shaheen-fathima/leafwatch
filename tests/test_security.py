"""Admin access: no default password, constant-time check, no crash on odd input, brute-force lock-out, nothing admin-only is public."""
import asyncio, pathlib, re
import pytest
from helpers import *


def _status(fn, *a, **k):
    try: fn(*a, **k); return 200
    except Exception as e: return getattr(e, "status_code", type(e).__name__)


def test_admin_is_disabled_until_a_key_is_configured():
    A = fresh_app(admin_key=None)
    assert _status(A.need_admin, "admin123") == 503 and _status(A.need_admin, "") == 503 and _status(A.admin_overview, x_admin_key="admin123") == 503


def test_wrong_empty_and_unicode_keys_are_401_never_a_crash():
    A = fresh_app()
    for k in ("", "wrong", "admin123", "pässwörd-é-日本語", "x" * 5000): assert _status(A.need_admin, k) == 401, k
    assert _status(A.need_admin, ADMIN) == 200


def test_endpoints_need_the_key():
    A = fresh_app(); three_sick_neighbours(A); oid = one(A, "SELECT id FROM outbreaks")["id"]
    assert _status(A.admin_overview, x_admin_key="") == 401 and _status(A.admin_overview, x_admin_key="nope") == 401
    v = A.VerifyIn(confirmed=True)
    assert _status(lambda: asyncio.run(A.admin_verify(oid, v, x_admin_key="nope"))) == 401
    assert one(A, "SELECT status FROM outbreaks")["status"] == "pending" and count(A, "alerts") == 0
    assert _status(lambda: asyncio.run(A.admin_verify(oid, v, x_admin_key=ADMIN))) == 200 and count(A, "alerts") == 3


def test_brute_force_lockout():
    A = fresh_app()
    for _ in range(A.MAX_FAILS): assert _status(A.need_admin, "guess") == 401
    assert _status(A.need_admin, "guess") == 429 and _status(A.need_admin, ADMIN) == 429, "lock-out must also stop the right key for a while"
    A.FAILS[:] = [t - A.FAIL_WINDOW_S - 1 for t in A.FAILS]; assert _status(A.need_admin, ADMIN) == 200


def test_public_endpoints_do_not_leak_admin_data():
    A = fresh_app(); three_sick_neighbours(A)
    assert A.outbreaks() == [] and "evidence" not in str(A.stats()) and all("probs" not in r and "farmer_id" not in r for r in A.recent())


def test_no_hardcoded_default_admin_password_in_shipped_code():
    root = pathlib.Path(__file__).resolve().parent.parent
    for f in list(root.glob("*.py")) + [root / "static" / "index.html", root / "run.bat"]:
        assert not re.search(r"admin123", f.read_text(encoding="utf-8", errors="ignore")) or f.name.startswith("test_"), f.name
