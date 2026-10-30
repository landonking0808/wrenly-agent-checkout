"""scripts/sandbox_e2e.py --preflight: checks credentials and the Payouts scope without moving money (no network here)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("sandbox_e2e", Path(__file__).resolve().parent.parent / "scripts" / "sandbox_e2e.py")
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Http:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.resp


CR = {"client_id": "id", "client_secret": "sec", "buyer_email": "buyer@personal.example.com", "buyer_password": "pw",
      "crew_email": "crew@personal.example.com"}


def test_preflight_ok_with_payouts_scope():
    h = _Http(_Resp(200, {"scope": "openid " + e2e.PAYOUTS_SCOPE, "app_id": "APP-1"}))
    assert e2e.preflight(CR, http=h)
    url, kw = h.calls[0]
    assert url.endswith("/v1/oauth2/token") and "sandbox" in url and kw["auth"] == ("id", "sec")


def test_preflight_fails_without_payouts_scope_or_bad_auth_or_same_receiver():
    assert not e2e.preflight(CR, http=_Http(_Resp(200, {"scope": "openid"})))
    assert not e2e.preflight(CR, http=_Http(_Resp(401, {})))
    same = dict(CR, crew_email="Buyer@personal.example.com")
    assert not e2e.preflight(same, http=_Http(_Resp(200, {"scope": e2e.PAYOUTS_SCOPE})))
