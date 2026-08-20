idf_component_register(
    SRCS
        "main.c"
        "display/rlcd_driver.c"
        "display/dither.c"
        "display/renderer.c"
        "display/atlas.c"
        "audio/es8311.c"
        "audio/es7210.c"
        "audio/audio_frontend.c"
        "audio/audio_pipeline.c"
        "audio/opus_bridge.cc"
        "audio/opus_decode_bridge.cc"
        "audio/sfx.c"
        "audio/music_player.c"
        "audio/music_library.c"
        "network/wifi_manager.c"
        "network/net_discovery.c"
        "network/ws_client.c"
        "network/ota_update.c"
        "sensors/shtc3.c"
        "sensors/pcf85063.c"
        "sensors/battery.c"
        "storage/sd_card.c"
        "ui/layout.c"
        "ui/status_bar.c"
        "ui/panels.c"
        "ui/panels_info_board.c"
        "ui/font.c"
        "ui/state_helpers.c"
        "input/buttons.c"
        "protocol/messages.c"
        "core/state_machine.c"
        "audio/wake_word.c"
        "minigame/minigame_state.c"
        "minigame/minigame_data.c"
        "minigame/minigame_battle.c"
        "minigame/minigame_render.c"
        "minigame/minigame_protocol.c"
    INCLUDE_DIRS
        "."
        "display"
        "audio"
        "network"
        "sensors"
        "storage"
        "ui"
        "input"
        "protocol"
        "core"
        "minigame"
    REQUIRES
        driver esp_driver_spi esp_driver_i2c esp_driver_i2s
        esp_driver_sdmmc sdmmc
        esp_adc
        esp_wifi esp_timer esp_http_server
        esp_http_client app_update
        nvs_flash fatfs
        spi_flash esp_psram mbedtls
        esp_websocket_client json spiffs
        esp_app_format
        esp-sr esp_audio_simple_player
        78__esp-opus 78__esp-opus-encoder
)
