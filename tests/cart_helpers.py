"""Setup helpers for existing offline GUI lifecycle tests."""

from ticket_app.train_policy import normalize_train_codes


def set_cart(window, seats, trains=None):
    trains = trains if trains is not None else normalize_train_codes(window.preferred_trains.text()) or ["G79"]
    window.cart_items = [
        {"from_station": window.from_station.text(), "to_station": window.to_station.text(),
         "train_scope": "specific" if train else "all", "train_code": train, "seat_type": seat}
        for train in trains for seat in seats
    ]
    window.cart_migration = {"notes": [], "issues": []}
    window._cart_draft_baseline = window._cart_draft_signature()
    window._cart_changed()
