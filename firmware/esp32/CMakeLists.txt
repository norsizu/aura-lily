cmake_minimum_required(VERSION 3.16)

set(IDF_TARGET "esp32s3")
set(EXTRA_COMPONENT_DIRS "")
if(NOT DEFINED PROJECT_VER)
    set(PROJECT_VER "0.17.16")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(aura_doudou)

# Some upstream audio components currently emit format warnings on ESP-IDF
# 5.3/xtensa; keep those warnings visible without failing the whole build.
idf_build_set_property(COMPILE_OPTIONS "-Wno-error=format" APPEND)

# ── 把 assets/ 目录打包成 SPIFFS 镜像，刷写到 "assets" 分区 ──
spiffs_create_partition_image(assets assets FLASH_IN_PROJECT)
