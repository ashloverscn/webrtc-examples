cmake_minimum_required(VERSION 3.15)
project(webrtc-file-upload-server CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(nlohmann_json REQUIRED)
find_package(LibDataChannel CONFIG REQUIRED)

# Fetch cpp-httplib automatically
include(FetchContent)
FetchContent_Declare(
    httplib
    GIT_REPOSITORY https://github.com/yhirose/cpp-httplib.git
    GIT_TAG        v0.15.3
)
FetchContent_MakeAvailable(httplib)

# Change target name to match your project
add_executable(webrtc-video0-mqtt-rtsp main.cpp)

target_link_libraries(webrtc-video0-mqtt-rtsp PRIVATE 
    nlohmann_json::nlohmann_json
    LibDataChannel::LibDataChannel
    httplib::httplib
)

if(WIN32)
    target_link_libraries(webrtc-video0-mqtt-rtsp PRIVATE ws2_32 crypt32)
endif()
