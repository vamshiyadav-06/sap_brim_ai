"""Free token wallet: 100 tokens per account, each answer costs 0 to 1 token, 0 blocks new questions."""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from backend.app.database import SessionLocal
from backend.app.main import app
from backend.app.models.user import User
from backend.app.services import token_wallet
from backend.tests.org_helpers import OrgFixtures, headers_for

client = TestClient(app)


def test_cost_runs_from_zero_to_one_token_by_how_hard_the_question_was():
    assert token_wallet.cost_milli(0) == 0  # memory answers and refusals call no model
    assert token_wallet.cost_milli(1) == token_wallet.MIN_COST_MILLI
    assert token_wallet.cost_tokens(3000) == 0.5  # exactly what is subtracted
    easy, medium = token_wallet.cost_milli(600), token_wallet.cost_milli(3000)
    assert 0 < easy < medium < token_wallet.MILLI
    assert token_wallet.cost_milli(token_wallet.FULL_COST_LLM_TOKENS) == token_wallet.MILLI
    assert token_wallet.cost_milli(10 ** 7) == token_wallet.MILLI  # a very hard question costs exactly 1


def test_new_accounts_start_with_100_tokens_and_the_balance_is_exposed():
    fx = OrgFixtures()
    try:
        user = fx.user()
        me = client.get("/api/auth/me", headers=headers_for(user.id))
        assert me.status_code == 200
        assert me.json()["token_balance"] == 100.0
    finally:
        fx.cleanup()


def test_charging_subtracts_fractions_and_never_goes_below_zero():
    fx = OrgFixtures()
    try:
        user = fx.user()
        with SessionLocal() as db:
            assert token_wallet.charge(db, user.id, 250) == 100_000 - 250
            db.query(User).filter(User.id == user.id).update({"token_balance_milli": 100})
            db.commit()
            assert token_wallet.charge(db, user.id, 1000) == 0
    finally:
        fx.cleanup()


def test_a_user_with_no_tokens_cannot_ask_but_admins_and_org_members_are_not_metered():
    fx = OrgFixtures()
    try:
        user = fx.user()
        admin = fx.user(is_admin=True)
        org = fx.organisation()
        member = fx.user(org=org, role="member")
        with SessionLocal() as db:
            db.query(User).filter(User.id.in_([user.id, admin.id, member.id])).update(
                {"token_balance_milli": 0}, synchronize_session=False)
            db.commit()
        with SessionLocal() as db:
            assert not token_wallet.has_balance(db.get(User, user.id))
            assert token_wallet.has_balance(db.get(User, admin.id))
            assert token_wallet.has_balance(db.get(User, member.id))

        chat = client.post("/api/chats", headers=headers_for(user.id), json={"title": "t"}).json()
        blocked = client.post(f"/api/chats/{chat['id']}/messages/stream", headers=headers_for(user.id),
                              json={"content": "What is FI-CA?", "mode": "sprint"})
        assert blocked.status_code == 402
        assert blocked.json()["detail"] == "You have used all your tokens."
    finally:
        fx.cleanup()


def test_the_wallet_refills_to_the_allowance_every_three_days():
    fx = OrgFixtures()
    try:
        user = fx.user()
        with SessionLocal() as db:
            row = db.get(User, user.id)
            token_wallet.refill_if_due(db, row)  # first touch starts the 3-day window, balance untouched
            first_window = row.token_refill_at
            assert first_window is not None
            assert timedelta(days=2, hours=23) < first_window - datetime.utcnow() <= timedelta(days=3)

            token_wallet.charge(db, user.id, 40_000)
            db.refresh(row)
            token_wallet.refill_if_due(db, row)  # window still open: nothing changes
            assert row.token_balance_milli == 60_000 and row.token_refill_at == first_window

            row.token_refill_at = datetime.utcnow() - timedelta(minutes=1)  # three days pass
            db.commit()
            token_wallet.refill_if_due(db, row)
            assert row.token_balance_milli == 100_000  # back to 100, not 160: unused tokens do not pile up
            assert row.token_refill_at > datetime.utcnow() + timedelta(days=2)
    finally:
        fx.cleanup()


def test_an_empty_wallet_is_blocked_until_the_refill_date_then_answers_again():
    fx = OrgFixtures()
    try:
        user = fx.user()
        with SessionLocal() as db:
            db.query(User).filter(User.id == user.id).update(
                {"token_balance_milli": 0, "token_refill_at": datetime.utcnow() + timedelta(days=1)})
            db.commit()
        chat = client.post("/api/chats", headers=headers_for(user.id), json={"title": "t"}).json()
        url = f"/api/chats/{chat['id']}/messages/stream"
        body = {"content": "What is FI-CA?", "mode": "sprint"}
        assert client.post(url, headers=headers_for(user.id), json=body).status_code == 402

        with SessionLocal() as db:
            db.query(User).filter(User.id == user.id).update({"token_refill_at": datetime.utcnow() - timedelta(minutes=1)})
            db.commit()
        me = client.get("/api/auth/me", headers=headers_for(user.id)).json()
        assert me["token_balance"] == 100.0 and me["token_refill_at"]
    finally:
        fx.cleanup()
