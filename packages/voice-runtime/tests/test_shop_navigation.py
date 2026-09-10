import pytest
from voice_runtime.focus import requests_shop_navigation


@pytest.mark.parametrize(
    "text",
    [
        "show me my basket",
        "open cart",
        "mera cart dikhao",
        "मेरा कार्ट दिखाओ",
        "cancel checkout",
        "cancel order review",
        "close review",
        "checkout band karo",
    ],
)
def test_surface_navigation(text):
    assert requests_shop_navigation(text)


@pytest.mark.parametrize(
    "text",
    [
        "cancel my order",
        "cancel payment",
        "do not close checkout",
        "how do I cancel checkout",
        "show namkeen",
    ],
)
def test_not_surface_navigation(text):
    assert not requests_shop_navigation(text)
