#include <opencv2/opencv.hpp>
#include <iostream>
#include <string>
#include <vector>
#include <thread>
#include <chrono>

// Windows Networking & System Headers
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <iphlpapi.h>

// Tell MSVC to link against the required Windows network libraries
#pragma comment(lib, "Ws2_32.lib")
#pragma comment(lib, "IPHLPAPI.lib")

#define BIND_PORT 8080
#define BUFFER_SIZE 2048

/**
 * @brief Handles individual client connections in an isolated thread context using Winsock2.
 * @param client_socket Open SOCKET handle for the connected browser peer.
 */
void execute_client_pipeline(SOCKET client_socket) {
    std::string html_index_payload =
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/html\r\n"
        "Connection: close\r\n"
        "\r\n"
        "<!DOCTYPE html>\n<html>\n<head>\n"
        "<title>Pi Low-Latency MJPEG Engine (Windows)</title>\n"
        "<style>\n"
        "  body { background-color: #0f1115; color: #e2e8f0; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; text-align: center; margin: 0; padding: 20px; }\n"
        "  .container { max-width: 960px; margin: 0 auto; background: #1a202c; padding: 25px; border-radius: 12px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.5); }\n"
        "  h1 { color: #38bdf8; margin-top: 0; font-weight: 600; }\n"
        "  .stream-viewport { max-width: 100%; height: auto; border: 4px solid #334155; border-radius: 8px; background: #000; }\n"
        "  .status-tag { display: inline-block; background: #16a34a; color: white; padding: 4px 12px; border-radius: 9999px; font-size: 0.85rem; margin-top: 10px; font-weight: bold; }\n"
        "</style>\n</head>\n<body>\n"
        "<div class='container'>\n"
        "  <h1>Windows High-Density Media Server</h1>\n"
        "  <img src='/stream' class='stream-viewport' alt='Live Video Feed Node Loading...' />\n"
        "  <br/><span class='status-tag'>ENGINE ACTIVE (WIN_WINSOCK)</span>\n"
        "</div>\n</body>\n</html>\n";

    std::vector<char> request_buffer(BUFFER_SIZE, 0);
    int read_bytes = recv(client_socket, request_buffer.data(), static_cast<int>(request_buffer.size() - 1), 0);

    if (read_bytes <= 0) {
        closesocket(client_socket);
        return;
    }

    std::string http_request_string(request_buffer.data());

    // Evaluate Routing Path
    if (http_request_string.find("GET /stream") != std::string::npos) {

        std::string mjpeg_init_header =
            "HTTP/1.1 200 OK\r\n"
            "Connection: close\r\n"
            "Cache-Control: no-cache, private\r\n"
            "Pragma: no-cache\r\n"
            "Content-Type: multipart/x-mixed-replace; boundary=frameboundary\r\n\r\n";

        // Windows doesn't use MSG_NOSIGNAL; drop flag to 0
        if (send(client_socket, mjpeg_init_header.c_str(), static_cast<int>(mjpeg_init_header.length()), 0) == SOCKET_ERROR) {
            closesocket(client_socket);
            return;
        }

        cv::VideoCapture video_capture_engine(0, cv::CAP_DSHOW);
        if (!video_capture_engine.isOpened()) {
            std::cerr << "[CRITICAL] Worker Thread Failure: Unable to open webcam device 0." << std::endl;
            closesocket(client_socket);
            return;
        }

        // Configure webcam
        video_capture_engine.set(cv::CAP_PROP_FRAME_WIDTH, 1280);
        video_capture_engine.set(cv::CAP_PROP_FRAME_HEIGHT, 720);
        video_capture_engine.set(cv::CAP_PROP_FPS, 30);

        cv::Mat continuous_frame_matrix;
        std::vector<uchar> compressed_jpeg_bitstream;
        std::vector<int> compression_parameters = { cv::IMWRITE_JPEG_QUALITY, 75 };

        std::cout << "[INFO] Stream initialization verified. Inbound pipe bound to client handle." << std::endl;

        while (true) {
            video_capture_engine >> continuous_frame_matrix;

            if (continuous_frame_matrix.empty()) {
                video_capture_engine.set(cv::CAP_PROP_POS_FRAMES, 0);
                continue;
            }

            cv::imencode(".jpg", continuous_frame_matrix, compressed_jpeg_bitstream, compression_parameters);

            std::string individual_frame_boundary_header =
                "--frameboundary\r\n"
                "Content-Type: image/jpeg\r\n"
                "Content-Length: " + std::to_string(compressed_jpeg_bitstream.size()) + "\r\n\r\n";

            // Sequential transport writes. If client disconnects, send will return SOCKET_ERROR.
            if (send(client_socket, individual_frame_boundary_header.c_str(), static_cast<int>(individual_frame_boundary_header.length()), 0) == SOCKET_ERROR) break;
            if (send(client_socket, reinterpret_cast<const char*>(compressed_jpeg_bitstream.data()), static_cast<int>(compressed_jpeg_bitstream.size()), 0) == SOCKET_ERROR) break;
            if (send(client_socket, "\r\n", 2, 0) == SOCKET_ERROR) break;

            std::this_thread::sleep_for(std::chrono::milliseconds(33));
        }

        video_capture_engine.release();
        std::cout << "[INFO] Pipeline terminated. Connection cleanly closed." << std::endl;
    }
    else {
        send(client_socket, html_index_payload.c_str(), static_cast<int>(html_index_payload.length()), 0);
    }

    closesocket(client_socket);
}

int main() {
    // 1. Initialize Winsock Runtime Environment
    WSADATA wsa_data;
    int wsa_startup_result = WSAStartup(MAKEWORD(2, 2), &wsa_data);
    if (wsa_startup_result != 0) {
        std::cerr << "[FATAL] Winsock initialization failed with error code: " << wsa_startup_result << std::endl;
        return EXIT_FAILURE;
    }

    // 2. Instantiate TCP Socket Endpoint Handler
    SOCKET master_socket_descriptor = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (master_socket_descriptor == INVALID_SOCKET) {
        std::cerr << "[FATAL] Failed to create socket descriptor context: " << WSAGetLastError() << std::endl;
        WSACleanup();
        return EXIT_FAILURE;
    }

    // 3. Configure Socket Re-use Directives
    char optimization_flag_value = 1;
    if (setsockopt(master_socket_descriptor, SOL_SOCKET, SO_REUSEADDR, &optimization_flag_value, sizeof(optimization_flag_value)) == SOCKET_ERROR) {
        std::cerr << "[FATAL] Socket configuration optimization routine failed." << std::endl;
        closesocket(master_socket_descriptor);
        WSACleanup();
        return EXIT_FAILURE;
    }

    // 4. Map Memory Allocation Parameters to Address Structures
    sockaddr_in network_endpoint_address{};
    network_endpoint_address.sin_family = AF_INET;
    network_endpoint_address.sin_addr.s_addr = INADDR_ANY; 
    network_endpoint_address.sin_port = htons(BIND_PORT);

    // 5. Bind Virtual Socket Descriptor to Hardware Port Space
    if (bind(master_socket_descriptor, (struct sockaddr*)&network_endpoint_address, sizeof(network_endpoint_address)) == SOCKET_ERROR) {
        std::cerr << "[FATAL] Network Interface Binding Failure on port: " << BIND_PORT << " Error: " << WSAGetLastError() << std::endl;
        closesocket(master_socket_descriptor);
        WSACleanup();
        return EXIT_FAILURE;
    }

    // 6. Enter Passive Listening State
    if (listen(master_socket_descriptor, 32) == SOCKET_ERROR) {
        std::cerr << "[FATAL] Unable to transit system into listening state mode." << std::endl;
        closesocket(master_socket_descriptor);
        WSACleanup();
        return EXIT_FAILURE;
    }

    // Dynamic extraction logic loop using Windows API to find external IP
    std::string display_ip = "127.0.0.1";
    ULONG out_buf_len = 15000;
    PIP_ADAPTER_ADDRESSES addresses = (IP_ADAPTER_ADDRESSES*)malloc(out_buf_len);

    if (addresses != nullptr) {
        ULONG flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER;
        DWORD result = GetAdaptersAddresses(AF_INET, flags, nullptr, addresses, &out_buf_len);
        
        if (result == ERROR_BUFFER_OVERFLOW) {
            free(addresses);
            addresses = (IP_ADAPTER_ADDRESSES*)malloc(out_buf_len);
            result = GetAdaptersAddresses(AF_INET, flags, nullptr, addresses, &out_buf_len);
        }

        if (result == NO_ERROR) {
            PIP_ADAPTER_ADDRESSES curr_adapter = addresses;
            while (curr_adapter != nullptr) {
                // Ignore loopback adapters and check for valid unicast IPv4 addresses
                if (curr_adapter->IfType != IF_TYPE_SOFTWARE_LOOPBACK && curr_adapter->FirstUnicastAddress != nullptr) {
                    sockaddr_in* ipv4 = (sockaddr_in*)curr_adapter->FirstUnicastAddress->Address.lpSockaddr;
                    char ip_address_buffer[INET_ADDRSTRLEN];
                    inet_ntop(AF_INET, &(ipv4->sin_addr), ip_address_buffer, INET_ADDRSTRLEN);
                    display_ip = ip_address_buffer;
                    break; 
                }
                curr_adapter = curr_adapter->Next;
            }
        }
        free(addresses);
    }

    std::cout << "============================================================" << std::endl;
    std::cout << " ENGINE OPERATIONAL (MSVC Windows Native)" << std::endl;
    std::cout << " Server live at http://" << display_ip << ":" << BIND_PORT << std::endl;
    std::cout << "============================================================" << std::endl;

    // 7. System Orchestration Infinite Loop
    while (true) {
        SOCKET inbound_client_socket = accept(master_socket_descriptor, nullptr, nullptr);

        if (inbound_client_socket != INVALID_SOCKET) {
            try {
                std::thread(execute_client_pipeline, inbound_client_socket).detach();
            } catch (const std::exception& processing_exception) {
                std::cerr << "[WARNING] Unable to delegate client handling task thread: " << processing_exception.what() << std::endl;
                closesocket(inbound_client_socket);
            }
        }
    }

    closesocket(master_socket_descriptor);
    WSACleanup();
    return EXIT_SUCCESS;
}