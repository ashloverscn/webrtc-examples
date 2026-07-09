cmake_minimum_required(VERSION 3.15)
project(hello_world CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Force everything to match vcpkg's static compilation layout
set(VCPKG_LIBRARY_LINKAGE static)
set(VCPKG_CRT_LINKAGE dynamic)

find_package(OpenCV REQUIRED COMPONENTS core videoio imgproc)
find_package(nlohmann_json REQUIRED)
find_package(PahoMqttCpp REQUIRED)
find_package(LibDataChannel CONFIG REQUIRED)

add_executable(webrtc-video0-mqtt main.cpp)

target_compile_definitions(webrtc-video0-mqtt PRIVATE PAHO_MQTTPP_IMPORTS)

target_link_libraries(webrtc-video0-mqtt PRIVATE 
    opencv_core 
    opencv_videoio 
    opencv_imgproc
    nlohmann_json::nlohmann_json
    PahoMqttCpp::paho-mqttpp3
    LibDataChannel::LibDataChannel
)
