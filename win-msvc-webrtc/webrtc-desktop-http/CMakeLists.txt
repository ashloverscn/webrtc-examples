cmake_minimum_required(VERSION 3.15)
project(remote-desktop-http CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Force everything to match vcpkg's static compilation layout
set(VCPKG_LIBRARY_LINKAGE static)
set(VCPKG_CRT_LINKAGE dynamic)

# Required dependencies for WebRTC Data Channel handling and JSON metadata parsing
find_package(nlohmann_json REQUIRED)
find_package(LibDataChannel CONFIG REQUIRED)
find_package(Threads REQUIRED)

# Linux-only dependencies
if(UNIX AND NOT APPLE)
    find_package(X11 REQUIRED)
    find_package(JPEG REQUIRED)
endif()

# Optional: Keep cpp-httplib if you still need a fallback HTTP server for initial web page delivery
include(FetchContent)
FetchContent_Declare(
    httplib
    GIT_REPOSITORY https://github.com/yhirose/cpp-httplib.git
    GIT_TAG        v0.15.3
)
FetchContent_MakeAvailable(httplib)

add_executable(remote-desktop-http main.cpp)

target_link_libraries(remote-desktop-http PRIVATE 
    nlohmann_json::nlohmann_json
    LibDataChannel::LibDataChannel
    httplib::httplib
    Threads::Threads
)

# Linux-specific libraries linking
if(UNIX AND NOT APPLE)
    target_link_libraries(remote-desktop-http PRIVATE 
        ${X11_LIBRARIES}
        Xtst
        JPEG::JPEG
    )
endif()

# Windows-specific system libraries linking
if(WIN32)
    target_link_libraries(remote-desktop-http PRIVATE 
        ws2_32 
        crypt32
    )
endif()
