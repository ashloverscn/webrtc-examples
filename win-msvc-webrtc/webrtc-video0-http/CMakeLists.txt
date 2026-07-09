cmake_minimum_required(VERSION 3.16)

project(webrtc-video0-http LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(OpenCV REQUIRED)

add_executable(webrtc-video0-http
    main.cpp
)

target_link_libraries(webrtc-video0-http PRIVATE
    ${OpenCV_LIBS}
    Ws2_32
    Iphlpapi
)

target_include_directories(webrtc-video0-http PRIVATE
    ${OpenCV_INCLUDE_DIRS}
)