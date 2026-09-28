import pytest

from ticket_app.preferences import BerthPreference, OrderCapabilities, OrderPreferencePayload, SeatRelationPreference, build_order_preference_payload


def preference(seat_type="O", requested=True, available=True):
    return build_order_preference_payload(SeatRelationPreference(), BerthPreference(), OrderCapabilities(), seat_type, 1, quiet_carriage_preference=requested, quiet_carriage_available=available)


def test_quiet_carriage_requires_opt_in_actual_second_class_and_explicit_capability():
    assert preference().to_form_fields()["is_jy"] == "Y"
    assert preference(requested=False).is_jy == "N"
    assert preference(requested=False).warnings == ()


@pytest.mark.parametrize("seat_type", ["WZ", "M", "9", "1", "I", "J", "3", "4"])
def test_other_seats_and_standing_never_request_quiet_carriage(seat_type):
    payload = preference(seat_type)
    assert payload.is_jy == "N"
    assert len(payload.warnings) == 1


@pytest.mark.parametrize("available", [False, None, "Y", "N", 1])
def test_missing_or_untrusted_capability_downgrades_to_normal_carriage(available):
    payload = preference(available=available)
    assert payload.is_jy == "N"
    assert "将由12306分配车厢" in payload.warnings[0]


def test_quiet_and_position_preferences_can_both_be_submitted():
    payload = build_order_preference_payload(SeatRelationPreference.from_value(["1A"]), BerthPreference(), OrderCapabilities.from_mapping({"canChooseSeats": "Y", "choose_Seats": "O"}), "O", 1, quiet_carriage_preference=True, quiet_carriage_available=True)
    assert payload.to_form_fields() == {"choose_seats": "1A", "seatDetailType": "000", "is_jy": "Y"}


def test_existing_positional_payload_constructor_still_works_and_rejects_bad_quiet_code():
    assert OrderPreferencePayload("1A", "000", ("warning",)).is_jy == "N"
    with pytest.raises(ValueError, match="is_jy"):
        OrderPreferencePayload(is_jy="true")
