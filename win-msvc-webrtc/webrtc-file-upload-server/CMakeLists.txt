cmake_minimum_required(VERSION 3.15)
project(webrtc-file-upload-server CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Force everything to match vcpkg's static compilation layout
set(VCPKG_LIBRARY_LINKAGE static)
set(VCPKG_CRT_LINKAGE dynamic)

find_package(OpenCV REQUIRED COMPONENTS core videoio imgproc)
find_package(nlohmann_json REQUIRED)
find_package(LibDataChannel CONFIG REQUIRED)

# cpp-httplib is no longer required since we replaced MQTT with LibDataChannel's 
# built-in rtc::WebSocketServer, but you can keep it if you need it for other endpoints.
include(FetchContent)
FetchContent_Declare(
    httplib
    GIT_REPOSITORY https://github.com/yhirose/cpp-httplib.git
    GIT_TAG        v0.15.3
)
FetchContent_MakeAvailable(httplib)

# FFmpeg discovery via vcpkg
find_path(FFMPEG_INCLUDE_DIR libavformat/avformat.h)
find_library(AVFORMAT_LIB avformat)
find_library(AVCODEC_LIB avcodec)
find_library(AVUTIL_LIB avutil)

add_executable(webrtc-file-upload-server main.cpp)

target_include_directories(webrtc-file-upload-server PRIVATE ${FFMPEG_INCLUDE_DIR})

target_link_libraries(webrtc-file-upload-server PRIVATE 
    opencv_core 
    opencv_videoio 
    opencv_imgproc
    nlohmann_json::nlohmann_json
    LibDataChannel::LibDataChannel
    httplib::httplib
    ${AVFORMAT_LIB}
    ${AVCODEC_LIB}
    ${AVUTIL_LIB}
    ws2_32
    crypt32
)