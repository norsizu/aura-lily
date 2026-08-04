from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WIFI_SOURCE = ROOT / "firmware" / "esp32" / "main" / "network" / "wifi_manager.c"
MAIN_SOURCE = ROOT / "firmware" / "esp32" / "main" / "main.c"


def test_wifi_manager_keeps_two_credential_slots_and_switches_between_them():
    source = WIFI_SOURCE.read_text(encoding="utf-8")
    assert 'return slot == 0 ? "ssid" : "ssid2"' in source
    assert 'return slot == 0 ? "pass" : "pass2"' in source
    assert "Switching to saved WiFi slot" in source
    assert 'nvs_set_u8(nvs, "preferred"' in source
    assert '"saved_networks"' in source
    assert '"/select"' in source
    assert "wifi_manager_connect_saved_network" in source


def test_starting_provisioning_does_not_clear_saved_networks():
    source = MAIN_SOURCE.read_text(encoding="utf-8")
    start = source.index("static void wifi_menu_start_provisioning(void)")
    end = source.index("static void ota_status_changed", start)
    assert "wifi_manager_clear_credentials" not in source[start:end]


def test_device_wifi_menu_exposes_both_saved_slots():
    source = MAIN_SOURCE.read_text(encoding="utf-8")
    assert "WIFI_MENU_SLOT_1" in source
    assert "WIFI_MENU_SLOT_2" in source
    assert "wifi_manager_get_saved_ssid" in source
