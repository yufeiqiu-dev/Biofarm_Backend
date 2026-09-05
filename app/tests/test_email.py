"""Transactional email.

The assertions worth having here are not "does it format nicely" but "can this
break an order". Two of the three sends sit on paths where an exception would do
real damage - the confirmation runs inside the Stripe webhook, where a non-200
makes Stripe retry and the retry creates a second order for one payment - so the
tests that matter are the ones that break SES on purpose.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.models.order import Order, OrderItem, OrderStatus
from app.services import email_service


def make_order(**overrides) -> Order:
    order = Order(
        order_number="1234567890",
        user_id="test-user-123",
        customer_email="buyer@example.com",
        status=OrderStatus.awaiting_fulfillment,
        shipping_name="Test Researcher",
        shipping_phone="5551234567",
        shipping_address1="123 Main St",
        shipping_address2=None,
        shipping_city="Springfield",
        shipping_state="IL",
        shipping_zip="62701",
        total_amount=Decimal("640.00"),
        tax_amount=Decimal("56.00"),
    )
    order.items = [
        OrderItem(
            product_name="Amyloid-beta 42 ELISA Kit",
            variant_label="96 tests",
            unit_price=Decimal("640.00"),
            quantity=1,
        )
    ]
    for field, value in overrides.items():
        setattr(order, field, value)
    return order


@pytest.fixture(autouse=True)
def _sending_enabled(monkeypatch):
    """This module is the one place that asserts on sending, so it is the one
    place that turns the suite-wide bypass off. Everything is patched, so no
    request leaves the process."""
    monkeypatch.setenv("EMAIL_BYPASS", "false")
    get_settings.cache_clear()
    email_service._ses_client.cache_clear()
    yield
    get_settings.cache_clear()
    email_service._ses_client.cache_clear()


@pytest.fixture
def ses():
    with patch.object(email_service, "_ses_client") as factory:
        client = MagicMock()
        factory.return_value = client
        yield client


class TestSending:
    def test_the_confirmation_goes_to_the_buyer(self, ses):
        email_service.send_order_confirmation(make_order())

        ses.send_email.assert_called_once()
        request = ses.send_email.call_args.kwargs
        assert request["Destination"]["ToAddresses"] == ["buyer@example.com"]
        assert "1234567890" in request["Message"]["Subject"]["Data"]

    def test_the_confirmation_states_the_total_actually_charged(self, ses):
        """Subtotal plus tax. Showing the subtotal as the total is the kind of
        mistake that produces a support email for every order."""
        email_service.send_order_confirmation(make_order())

        body = ses.send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "$696.00" in body

    def test_the_shipped_email_carries_the_tracking_number(self, ses):
        email_service.send_order_shipped(make_order(tracking_number="1Z999AA10123456784"))

        body = ses.send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "1Z999AA10123456784" in body

    def test_the_shipped_email_says_so_when_there_is_no_tracking_yet(self, ses):
        email_service.send_order_shipped(make_order(tracking_number=None))

        body = ses.send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "None" not in body
        assert "follow separately" in body

    def test_the_reply_to_is_set_when_configured(self, ses, monkeypatch):
        monkeypatch.setenv("EMAIL_REPLY_TO", "support@test.invalid")
        get_settings.cache_clear()

        email_service.send_order_confirmation(make_order())

        assert ses.send_email.call_args.kwargs["ReplyToAddresses"] == ["support@test.invalid"]


class TestNotSending:
    def test_an_order_with_no_address_is_skipped(self, ses):
        """Orders predating the customer_email column carry an empty string.
        Nobody to write to is not an error."""
        email_service.send_order_confirmation(make_order(customer_email=""))

        ses.send_email.assert_not_called()

    def test_bypass_sends_nothing(self, ses, monkeypatch):
        monkeypatch.setenv("EMAIL_BYPASS", "true")
        get_settings.cache_clear()

        email_service.send_order_confirmation(make_order())

        ses.send_email.assert_not_called()


class TestFailuresAreContained:
    """The reason this module exists in the shape it does."""

    def test_a_ses_failure_does_not_reach_the_caller(self, ses):
        # A non-200 out of the Stripe webhook makes Stripe retry, and the retry
        # creates a second order for a single payment. Mail is not worth that.
        ses.send_email.side_effect = ClientError(
            {"Error": {"Code": "MessageRejected", "Message": "Email address is not verified"}},
            "SendEmail",
        )

        email_service.send_order_confirmation(make_order())  # must not raise

    def test_an_unverified_sender_does_not_break_shipping(self, ses):
        # SES in sandbox rejects unverified recipients, which is the normal state
        # of a staging environment. Shipping must still work.
        ses.send_email.side_effect = ClientError(
            {"Error": {"Code": "MessageRejected", "Message": "not verified"}}, "SendEmail"
        )

        email_service.send_order_shipped(make_order())  # must not raise

    def test_the_failure_is_logged_with_the_order_number(self, ses, caplog):
        """So it can be found and resent by hand."""
        ses.send_email.side_effect = ClientError(
            {"Error": {"Code": "MessageRejected", "Message": "nope"}}, "SendEmail"
        )

        email_service.send_order_confirmation(make_order())

        assert "1234567890" in caplog.text


class TestWiring:
    """That the lifecycle actually calls these.

    Everything above tests email_service on its own, so without this a deleted
    call in order_service breaks no test and the customer simply stops hearing
    from us - the exact silent failure the module is meant to prevent.
    """

    def test_shipping_an_order_tells_the_customer(self, db_session):
        from app.tests.test_orders import make_order
        from app.services.order_service import confirm_order_admin, ship_order

        order, _ = make_order(db_session, stock=5)
        confirm_order_admin(db_session, order.id)

        with patch.object(email_service, "send_order_shipped") as send:
            ship_order(db_session, order.id, tracking_number="1Z999")

        send.assert_called_once()
        assert send.call_args.args[0].id == order.id

    def test_an_admin_cancellation_tells_the_customer(self, db_session):
        from app.tests.test_orders import make_order
        from app.services.order_service import cancel_order

        order, _ = make_order(db_session)

        with patch.object(email_service, "send_order_cancelled") as send:
            cancel_order(db_session, order.id)

        send.assert_called_once()

    def test_a_customer_cancellation_tells_them_too(self, db_session):
        from app.tests.test_orders import make_order
        from app.services.order_service import cancel_order_by_customer

        order, _ = make_order(db_session, user_id="test-user-123")

        with patch.object(email_service, "send_order_cancelled") as send:
            cancel_order_by_customer(db_session, order.id, "test-user-123")

        send.assert_called_once()

    def test_shipping_still_succeeds_when_the_send_blows_up(self, db_session):
        """The send is wrapped, but the wiring must not undo that by calling it
        somewhere the exception would escape - before the commit, say."""
        from app.tests.test_orders import make_order
        from app.services.order_service import confirm_order_admin, ship_order
        from app.models.order import OrderStatus as Status

        order, _ = make_order(db_session, stock=5)
        confirm_order_admin(db_session, order.id)

        with patch.object(email_service, "_send", side_effect=RuntimeError("SES down")):
            with pytest.raises(RuntimeError):
                ship_order(db_session, order.id)

        # The status change is committed before the send, so it survives.
        db_session.expire_all()
        assert db_session.get(type(order), order.id).status == Status.shipped
