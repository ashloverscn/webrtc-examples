#define PAHO_MQTTPP_IMPORTS 1

#include <iostream>
#include <string>
#include <vector>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <chrono>
#include <memory>
#include <random>
#include <atomic>

#include <opencv2/opencv.hpp>
#include <opencv2/core/utils/logger.hpp>
#include <mqtt/async_client.h>
#include <nlohmann/json.hpp>
#include <rtc/rtc.hpp>

using json = nlohmann::json;

const int WIDTH = 640;
const int HEIGHT = 480;
const std::string MQTT_SERVER = "ssl://e5122a5328ea4986a0295fa6e037655a.s2.eu.hivemq.cloud:8883";
const std::string TOPIC = "webrtc/signaling";

std::mutex frame_mutex;
std::condition_variable frame_cv;
std::vector<uint8_t> latest_frame_bytes;
bool frame_ready = false;
bool running_capture = false;

// Synchronizing state transitions between MQTT thread and delivery thread
std::mutex connection_mutex;
bool streaming_allowed = false;
std::shared_ptr<rtc::PeerConnection> pc;
std::shared_ptr<rtc::DataChannel> video_channel;
std::string viewer_id = "";

// Monotonic generation counter to track structural validity
uint64_t current_session_id = 0;

std::string generate_peer_id() {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> distr(1000, 9999);
    return "video0_cpp_" + std::to_string(distr(gen));
}

std::string peer_id = generate_peer_id();

// Clean up current references safely (Expects connection_mutex lock to be managed safely)
void clear_active_session_pointers() {
    streaming_allowed = false;
    viewer_id = "";
    if (video_channel) {
        try { video_channel->close(); } catch (...) {}
        video_channel.reset();
    }
    if (pc) {
        pc.reset();
    }
}

// --- OpenCV Webcam Loop (Adaptive Platform Backend Selection) ---
void opencv_video_loop() {
    // Determine native capture API backend based on host platform
#if defined(_WIN32) || defined(_WIN64)
    int api_preference = cv::CAP_DSHOW;
    const char* os_name = "Windows (DirectShow)";
#elif defined(__linux__)
    int api_preference = cv::CAP_V4L2;
    const char* os_name = "Linux (V4L2)";
#else
    int api_preference = cv::CAP_ANY;
    const char* os_name = "Generic System Backend";
#endif

    cv::VideoCapture video_capture(0, api_preference);
    
    // Fallback attempt if explicit API backend fails
    if (!video_capture.isOpened()) {
        video_capture.open(0, cv::CAP_ANY);
    }

    if (!video_capture.isOpened()) {
        std::cerr << "❌ Error: Could not open webcam at index 0 on " << os_name << "!" << std::endl;
        running_capture = false;
        return;
    }

    video_capture.set(cv::CAP_PROP_FRAME_WIDTH, WIDTH);
    video_capture.set(cv::CAP_PROP_FRAME_HEIGHT, HEIGHT);
    
    auto frame_duration = std::chrono::milliseconds(33);

    cv::Mat frame;
    std::vector<uint8_t> jpeg_buffer;
    std::vector<int> params = {cv::IMWRITE_JPEG_QUALITY, 75};

    while (running_capture) {
        auto start_time = std::chrono::steady_clock::now();

        video_capture >> frame;
        
        if (frame.empty()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            continue;
        }

        if (frame.cols != WIDTH || frame.rows != HEIGHT) {
            cv::resize(frame, frame, cv::Size(WIDTH, HEIGHT));
        }

        cv::imencode(".jpg", frame, jpeg_buffer, params);

        {
            std::lock_guard<std::mutex> lock(frame_mutex);
            latest_frame_bytes = std::move(jpeg_buffer);
            frame_ready = true;
        }
        frame_cv.notify_one();
        
        auto processing_time = std::chrono::steady_clock::now() - start_time;
        if (processing_time < frame_duration) {
            std::this_thread::sleep_for(frame_duration - processing_time);
        }
    }
    video_capture.release();
}

int main() {
    cv::utils::logging::setLogLevel(cv::utils::logging::LOG_LEVEL_SILENT);
    rtc::InitLogger(rtc::LogLevel::Info);

    running_capture = true;
    std::thread capture_thread(opencv_video_loop);

    rtc::Configuration config;
    config.iceServers.emplace_back("stun:stun.l.google.com:19302");

    mqtt::async_client mqtt_client(MQTT_SERVER, "cam_" + peer_id);
    
    auto ssl_opts = mqtt::ssl_options_builder().trust_store("cacert.pem").finalize();
    auto connOptions = mqtt::connect_options_builder()
        .user_name("admin").password("admin1234S")
        .ssl(ssl_opts).clean_session(true).finalize();

    auto on_message_callback = [&](mqtt::const_message_ptr msg) {
        try {
            auto payload = json::parse(msg->get_payload_str());
            if (payload.value("to", "") != peer_id) return;

            std::string type = payload.value("type", "");

            if (type == "offer") {
                std::shared_ptr<rtc::PeerConnection> old_pc_to_destroy = nullptr;
                uint64_t this_session = 0;

                {
                    std::lock_guard<std::mutex> lock(connection_mutex);
                    
                    current_session_id++;
                    this_session = current_session_id;

                    std::cout << "📥 Offer Received. Staging Session #" << this_session << "..." << std::endl;
                    
                    if (pc) {
                        old_pc_to_destroy = pc;
                    }
                    clear_active_session_pointers();

                    viewer_id = payload.value("from", "");
                    pc = std::make_shared<rtc::PeerConnection>(config);
                }

                if (old_pc_to_destroy) {
                    std::cout << "🔄 Clearing historical native STUN agent context safely..." << std::endl;
                    try { old_pc_to_destroy->close(); } catch(...) {}
                    old_pc_to_destroy.reset();
                }

                std::shared_ptr<rtc::PeerConnection> local_pc = pc;

                local_pc->onLocalDescription([&, client_ptr = &mqtt_client, this_session](rtc::Description description) {
                    {
                        std::lock_guard<std::mutex> lock(connection_mutex);
                        if (this_session != current_session_id) return;
                    }
                    json answer = {
                        {"type", description.typeString()},
                        {"from", peer_id},
                        {"to", viewer_id},
                        {"data", {{"sdp", std::string(description)}, {"type", description.typeString()}}}
                    };
                    try { client_ptr->publish(TOPIC, answer.dump()); } catch (...) {}
                });

                local_pc->onLocalCandidate([&, client_ptr = &mqtt_client, this_session](rtc::Candidate candidate) {
                    {
                        std::lock_guard<std::mutex> lock(connection_mutex);
                        if (this_session != current_session_id) return;
                    }
                    json ice = {
                        {"type", "ice"}, {"from", peer_id}, {"to", viewer_id},
                        {"data", {{"candidate", std::string(candidate)}, {"sdpMid", candidate.mid()}, {"sdpMLineIndex", 0}}}
                    };
                    try { client_ptr->publish(TOPIC, ice.dump()); } catch (...) {}
                });

                local_pc->onDataChannel([&, this_session](std::shared_ptr<rtc::DataChannel> dc) {
                    if (dc->label() == "video-stream") {
                        std::lock_guard<std::mutex> dc_lock(connection_mutex);
                        if (this_session != current_session_id) return;
                        
                        video_channel = dc;
                        
                        video_channel->onOpen([&, this_session]() {
                            std::lock_guard<std::mutex> stream_lock(connection_mutex);
                            if (this_session != current_session_id) return;
                            std::cout << "🚀 Video Data Channel Connected. Streaming Webcam [Session #" << this_session << "]." << std::endl;
                            streaming_allowed = true;
                        });
                        
                        video_channel->onClosed([&, this_session]() { 
                            std::lock_guard<std::mutex> stream_lock(connection_mutex);
                            if (this_session != current_session_id) return;
                            std::cout << "🛑 Video Data Channel Closed [Session #" << this_session << "]." << std::endl;
                            streaming_allowed = false; 
                        });
                    }
                });

                local_pc->onStateChange([&, this_session, local_pc](rtc::PeerConnection::State state) {
                    std::cout << "[*] WebRTC State Change: " << state << " [Session #" << this_session << "]" << std::endl;
                    if (state == rtc::PeerConnection::State::Failed || 
                        state == rtc::PeerConnection::State::Disconnected || 
                        state == rtc::PeerConnection::State::Closed) {
                        
                        std::thread([&, this_session, local_pc]() {
                            std::this_thread::sleep_for(std::chrono::milliseconds(30));
                            std::lock_guard<std::mutex> state_lock(connection_mutex);
                            
                            if (this_session == current_session_id) {
                                std::cout << "⚠️ Processing structural drop for active Session #" << this_session << "..." << std::endl;
                                clear_active_session_pointers();
                                try { local_pc->close(); } catch(...) {}
                                std::cout << "✅ Resources cleared. Ready for incoming loops." << std::endl;
                            } else {
                                std::cout << "ℹ️ Suppressed legacy session teardown request for generation ID #" << this_session << std::endl;
                            }
                        }).detach();
                    }
                });

                try {
                    std::string sdp = payload["data"]["sdp"];
                    local_pc->setRemoteDescription(rtc::Description(sdp, "offer"));
                } catch (const std::exception& e) {
                    std::cerr << "❌ Failed to parse Remote Offer Description: " << e.what() << std::endl;
                    std::lock_guard<std::mutex> err_lock(connection_mutex);
                    if (this_session == current_session_id) {
                        clear_active_session_pointers();
                        try { local_pc->close(); } catch(...) {}
                    }
                }

            } else if (type == "ice") {
                std::lock_guard<std::mutex> lock(connection_mutex);
                if (!pc) return;
                if (payload.value("from", "") != viewer_id) return;
                
                try {
                    std::string candidate_str = payload["data"]["candidate"];
                    std::string mid = payload["data"]["sdpMid"];
                    if (!candidate_str.empty()) {
                        pc->addRemoteCandidate(rtc::Candidate(candidate_str, mid));
                    }
                } catch (const std::exception& e) {
                    std::cerr << "⚠️ Dropped incompatible or mismatched ICE candidate: " << e.what() << std::endl;
                }
                
            } else if (type == "disconnect") {
                std::lock_guard<std::mutex> lock(connection_mutex);
                std::cout << "📥 Explicit tear disconnect requested from viewer node." << std::endl;
                if (pc) {
                    try { pc->close(); } catch (...) {}
                }
                clear_active_session_pointers();
            }

        } catch (const std::exception& e) {
            std::cerr << "Signaling Parse Error: " << e.what() << std::endl;
        }
    };

    try {
        mqtt_client.connect(connOptions)->wait();
        mqtt_client.subscribe(TOPIC, 1)->wait();
        mqtt_client.set_message_callback(on_message_callback);
    } catch (const std::exception& e) {
        std::cerr << "❌ MQTT Failure: " << e.what() << std::endl;
        running_capture = false;
        if (capture_thread.joinable()) capture_thread.join();
        return 1;
    }

    std::thread presence_thread([&]() {
        while (running_capture) {
            json msg = {{"type", "presence"}, {"from", peer_id}};
            try { mqtt_client.publish(TOPIC, msg.dump()); } catch (...) {}
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    });

#if defined(_WIN32) || defined(_WIN64)
    std::string platform_label = "WINDOWS";
#elif defined(__linux__)
    std::string platform_label = "LINUX";
#else
    std::string platform_label = "CROSS-PLATFORM";
#endif

    std::cout << "============================================" << std::endl;
    std::cout << "🎥 " << platform_label << " WEBCAM 0 TRANSCEIVER ONLINE" << std::endl;
    std::cout << "DEVICE ID: " << peer_id << std::endl;
    std::cout << "============================================" << std::endl;

    std::vector<uint8_t> frame_buffer;
    while (running_capture) {
        {
            std::unique_lock<std::mutex> lock(frame_mutex);
            frame_cv.wait(lock, [] { return frame_ready || !running_capture; });
            if (!running_capture) break;
            frame_buffer = std::move(latest_frame_bytes);
            frame_ready = false;
        }

        std::lock_guard<std::mutex> lock(connection_mutex);
        if (streaming_allowed && video_channel && video_channel->isOpen() && !frame_buffer.empty()) {
            try {
                if (video_channel->bufferedAmount() > 2 * 1024 * 1024) { 
                    continue; 
                }

                if (streaming_allowed) {
                    video_channel->send(reinterpret_cast<const std::byte*>(frame_buffer.data()), frame_buffer.size());
                }
            } catch (...) {
                // Absorbed to handle runtime connection drop transitions quietly
            }
        }
    }

    running_capture = false;
    frame_cv.notify_all();
    
    {
        std::lock_guard<std::mutex> lock(connection_mutex);
        if (pc) { try { pc->close(); } catch(...) {} }
        clear_active_session_pointers();
    }
    
    if (presence_thread.joinable()) presence_thread.join();
    if (capture_thread.joinable()) capture_thread.join();
    return 0;
}
