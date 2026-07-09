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

std::string generate_peer_id() {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> distr(1000, 9999);
    return "video0_cpp_" + std::to_string(distr(gen));
}

std::string peer_id = generate_peer_id();

// Helper to tear down current connection cleanly to allow reconnects
void reset_webrtc_connection() {
    std::lock_guard<std::mutex> lock(connection_mutex);
    if (!pc && !video_channel && !streaming_allowed) return; // Already reset

    std::cout << "🔄 Resetting WebRTC resources & clearing old session contexts..." << std::endl;
    streaming_allowed = false;
    viewer_id = "";

    if (video_channel) {
        try { video_channel->close(); } catch (...) {}
        video_channel.reset();
    }
    if (pc) {
        try { pc->close(); } catch (...) {}
        pc.reset();
    }
    std::cout << "✅ Resources cleared. Instantly ready for new connection offers." << std::endl;
}

// --- OpenCV Webcam Loop (Threaded for Windows DirectShow Device 0) ---
void opencv_video_loop() {
    cv::VideoCapture video_capture(0, cv::CAP_DSHOW);
    if (!video_capture.isOpened()) {
        std::cerr << "❌ Windows Error: Could not open webcam at index 0 using DirectShow!" << std::endl;
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
                // Instantly wipe previous session contexts on new inbound offer
                reset_webrtc_connection();

                std::shared_ptr<rtc::PeerConnection> local_pc;
                {
                    std::lock_guard<std::mutex> lock(connection_mutex);
                    viewer_id = payload.value("from", "");
                    std::cout << "📥 Received Offer from " << viewer_id << " (Live Webcam Core)" << std::endl;

                    pc = std::make_shared<rtc::PeerConnection>(config);
                    local_pc = pc; 
                }

                local_pc->onLocalDescription([&, client_ptr = &mqtt_client](rtc::Description description) {
                    json answer = {
                        {"type", description.typeString()},
                        {"from", peer_id},
                        {"to", viewer_id},
                        {"data", {{"sdp", std::string(description)}, {"type", description.typeString()}}}
                    };
                    try { client_ptr->publish(TOPIC, answer.dump()); } catch (...) {}
                });

                local_pc->onLocalCandidate([&, client_ptr = &mqtt_client](rtc::Candidate candidate) {
                    json ice = {
                        {"type", "ice"}, {"from", peer_id}, {"to", viewer_id},
                        {"data", {{"candidate", std::string(candidate)}, {"sdpMid", candidate.mid()}, {"sdpMLineIndex", 0}}}
                    };
                    try { client_ptr->publish(TOPIC, ice.dump()); } catch (...) {}
                });

                local_pc->onDataChannel([&](std::shared_ptr<rtc::DataChannel> dc) {
                    if (dc->label() == "video-stream") {
                        std::lock_guard<std::mutex> dc_lock(connection_mutex);
                        video_channel = dc;
                        
                        video_channel->onOpen([&]() {
                            std::cout << "🚀 Video Data Channel Connected. Streaming Webcam." << std::endl;
                            std::lock_guard<std::mutex> stream_lock(connection_mutex);
                            streaming_allowed = true;
                        });
                        
                        video_channel->onClosed([&]() { 
                            std::cout << "🛑 Video Data Channel Closed." << std::endl;
                            std::lock_guard<std::mutex> stream_lock(connection_mutex);
                            streaming_allowed = false; 
                        });
                    }
                });

                local_pc->onStateChange([&](rtc::PeerConnection::State state) {
                    std::cout << "[*] WebRTC State Change: " << state << std::endl;
                    // 🔌 Crucial: Treat Disconnected and Failed instantly as a tear-down state
                    if (state == rtc::PeerConnection::State::Failed || 
                        state == rtc::PeerConnection::State::Disconnected || 
                        state == rtc::PeerConnection::State::Closed) {
                        std::cout << "⚠️ Connection link failure detected. Actively purging old context..." << std::endl;
                        std::thread([]() { reset_webrtc_connection(); }).detach();
                    }
                });

                try {
                    std::string sdp = payload["data"]["sdp"];
                    local_pc->setRemoteDescription(rtc::Description(sdp, "offer"));
                } catch (const std::exception& e) {
                    std::cerr << "❌ Failed to set Remote Description: " << e.what() << std::endl;
                    std::thread([]() { reset_webrtc_connection(); }).detach();
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
                    std::cerr << "⚠️ Dropped incompatible or out-of-order ICE candidate: " << e.what() << std::endl;
                }
                
            } else if (type == "disconnect") {
                std::cout << "📥 Explicit disconnect payload received from viewer." << std::endl;
                std::thread([]() { reset_webrtc_connection(); }).detach();
            }

        } catch (const std::exception& e) {
            std::cerr << "Signaling Error: " << e.what() << std::endl;
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

    std::cout << "============================================" << std::endl;
    std::cout << "🎥 WINDOWS WEBCAM 0 TRANSCEIVER ONLINE" << std::endl;
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
                // Absorb quietly to ensure loop consistency during drops
            }
        }
    }

    running_capture = false;
    frame_cv.notify_all();
    reset_webrtc_connection();
    
    if (presence_thread.joinable()) presence_thread.join();
    if (capture_thread.joinable()) capture_thread.join();
    return 0;
}
