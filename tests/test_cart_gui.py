"""User-facing cart integration, using real widgets and offline settings."""

from copy import deepcopy

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from test_gui_app import main_window  # noqa: F401
from ticket_app.configuration import SEAT_SPECS
from ticket_app.gui.compat import load_gui_settings, save_gui_settings


def fill(window, *, trains="G79", seat="二等座", origin="北京西", destination="郑州东", scope="specific"):
    window.from_station.setText(origin)
    window.to_station.setText(destination)
    window.train_scope.setCurrentIndex(window.train_scope.findData(scope))
    window.preferred_trains.setText(trains)
    window.cart_seat.setCurrentIndex(window.cart_seat.findData(seat))


def answer_draft(text):
    def click():
        modal = QApplication.activeModalWidget()
        assert isinstance(modal, QMessageBox)
        next(button for button in modal.buttons() if button.text() == text).click()
    QTimer.singleShot(0, click)


def test_cart_is_empty_and_old_strategy_controls_are_absent(main_window):
    assert main_window.cart_items == []
    assert main_window.cart_seat.currentData() == ""
    assert not hasattr(main_window, "only_preferred")
    assert not hasattr(main_window, "priority_strategy")
    main_window._next_step()
    assert main_window.current_step == 0
    assert "购物车" in main_window.flow_error.text()


@pytest.mark.parametrize("separator", [",", "，", "、", ";", "；", "\n", "\t", "，;、"])
def test_add_multiple_trains_and_duplicates_preserves_order_and_form(main_window, qtbot, separator):
    fill(main_window, trains=separator.join(["g79", "G81", "G79"]))
    qtbot.mouseClick(main_window.add_cart_button, Qt.MouseButton.LeftButton)
    assert [row["train_code"] for row in main_window.cart_items] == ["G79", "G81"]
    original = deepcopy(main_window.cart_items)
    qtbot.mouseClick(main_window.add_cart_button, Qt.MouseButton.LeftButton)
    assert main_window.cart_items == original
    assert "已存在" in main_window.cart_draft_status.text()
    assert main_window.preferred_trains.text() == separator.join(["g79", "G81", "G79"])
    assert "（2）" in main_window.cart_button.text()


@pytest.mark.parametrize("seat", list(SEAT_SPECS))
def test_one_item_per_seat_and_preference_capability_tracks_cart(main_window, seat):
    fill(main_window, seat=seat)
    assert main_window._add_cart_items()
    main_window.passengers.setText("张三")
    config = main_window._build_current_config()
    assert config.seat_types == [seat]
    assert config.cart_items[0].seat_type == seat


def test_any_train_requires_explicit_selection_and_is_separate_from_same_train_other_seat(main_window):
    fill(main_window, trains="")
    assert not main_window._add_cart_items()
    assert main_window.cart_items == []
    main_window.train_scope.setCurrentIndex(main_window.train_scope.findData("all"))
    assert main_window._add_cart_items()
    fill(main_window, trains="G79", seat="一等座")
    assert main_window._add_cart_items()
    assert [(row["train_scope"], row["train_code"], row["seat_type"]) for row in main_window.cart_items] == [
        ("all", "", "二等座"), ("specific", "G79", "一等座")]


@pytest.mark.parametrize("choice,step,count", [("加入并继续", 1, 2), ("仅使用购物车", 1, 1), ("返回编辑", 0, 1)])
def test_next_resolves_only_unadded_changes(main_window, choice, step, count):
    fill(main_window)
    main_window._add_cart_items()
    main_window.preferred_trains.setText("G81")
    answer_draft(choice)
    main_window._next_step()
    assert main_window.current_step == step
    assert len(main_window.cart_items) == count
    assert main_window._active_operation is None


def test_invalid_unadded_draft_does_not_change_task_or_saved_settings(main_window, tmp_path):
    fill(main_window)
    main_window._add_cart_items()
    main_window.passengers.setText("张三")
    original = deepcopy(main_window.cart_items)
    fill(main_window, origin="不存在站点", trains="bad", seat="")
    assert main_window._validate_all() == {}
    path = tmp_path / "cart.json"
    save_gui_settings(path, main_window._collect_mapping())
    saved = load_gui_settings(path)
    assert saved["cart_items"] == original
    assert main_window._build_current_config().cart_items[0].train_code == "G79"


def test_legacy_scope_stays_blocked_until_explicit_resolution(main_window, monkeypatch):
    main_window._apply_mapping({"from_station": "北京西", "to_station": "郑州东", "seat_types": ["二等座"],
                                "only_preferred_trains": False, "empty_train_scope": "high_speed"})
    assert "cart_items" in main_window._validate_all()
    assert not main_window.resolve_cart_button.isHidden()
    fill(main_window, trains="G79")
    main_window._add_cart_items()
    assert main_window.cart_migration["issues"]  # Adding/reordering does not acknowledge broadening.
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Cancel)
    main_window._resolve_cart_migration()
    assert main_window.cart_migration["issues"]
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    main_window._resolve_cart_migration()
    assert not main_window.cart_migration["issues"]


def test_import_order_and_migration_note_visible_on_confirmation(main_window, tmp_path):
    main_window._apply_mapping({"from_station": "北京西", "to_station": "郑州东", "seat_types": ["二等座", "一等座"],
                                "preferred_trains": ["G81", "G79"], "only_preferred_trains": False})
    path = tmp_path / "roundtrip.json"
    original = deepcopy(main_window.cart_items)
    save_gui_settings(path, main_window._collect_mapping())
    main_window._apply_mapping(load_gui_settings(path))
    main_window._go_to_step(2)
    assert main_window.cart_items == original
    text = main_window.confirm_summary.text()
    assert "顺序已变化" in text
    assert text.index("G81 · 二等座") < text.index("G81 · 一等座") < text.index("G79 · 二等座")


def test_runtime_distinguishes_query_failure_empty_and_exact_success_route(main_window, monkeypatch):
    main_window._go_to_step(3)
    context = {"cart_index": 2, "cart_total": 3, "from_station": "北京南", "to_station": "上海虹桥",
               "train_code": "G103", "seat_label": "二等座"}
    main_window._on_runtime_event("query_failed", {**context, "message": "查询失败，下轮重试"})
    assert "查询失败" in main_window.cart_query_status.text()
    main_window._on_runtime_event("query_empty", {**context, "message": "查询成功，当前无票"})
    assert "查询成功" in main_window.cart_query_status.text()
    dialogs = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: dialogs.append(args[2]))
    main_window._on_runtime_event("order_success", {**context, "order_id": "demo"})
    assert "北京南 → 上海虹桥" in main_window.current_cart_item.text()
    assert "北京南 → 上海虹桥" in dialogs[0]
    assert "备选 2 / 3" in main_window.current_cart_item.text()


def test_advanced_reset_retains_unadded_draft_and_warning(main_window):
    fill(main_window)
    main_window._add_cart_items()
    fill(main_window, trains="G81", seat="一等座")
    draft = main_window._cart_draft_signature()
    main_window._reset_advanced()
    assert main_window._cart_draft_signature() == draft
    assert draft != main_window._cart_draft_baseline


def test_idle_clock_operation_shows_current_cart_not_previous_task(main_window, monkeypatch):
    from types import SimpleNamespace
    from ticket_app.gui import cart_flow
    from PySide6.QtWidgets import QDialog

    fill(main_window)
    main_window._add_cart_items()
    main_window.passengers.setText("张三")
    main_window._last_run_config = main_window._build_current_config()
    main_window.cart_items[0]["train_code"] = "G81"
    main_window._active_operation = SimpleNamespace(mode="sync_clock")
    main_window._operation_mode = "sync_clock"
    seen = []

    class Dialog:
        def __init__(self, items, *args, **kwargs):
            seen.append((deepcopy(items), kwargs["read_only"]))

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(cart_flow, "CartDialog", Dialog)
    main_window._open_cart()
    assert seen[0][0][0]["train_code"] == "G81"
    assert seen[0][1] is True
    main_window._active_operation = None
    main_window._operation_mode = None
