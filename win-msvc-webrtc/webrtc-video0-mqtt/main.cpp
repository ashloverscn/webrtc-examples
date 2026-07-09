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
const int CAMERA_INDEX = 0; // 👈 0 is usually the default built-in or USB webcam

std::mutex frame_mutex;
std::condition_variable frame_cv;
std::vector<uint8_t> latest_frame_bytes;
bool frame_ready = false;
bool running_capture = false;
bool streaming_allowed = false;

std::string generate_peer_id() {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> distr(1000, 9999);
    return "webcam_cpp_" + std::to_string(distr(gen));
}

std::string peer_id = generate_peer_id();
std::string viewer_id = "";

// --- OpenCV Web Camera Loop (Threaded) ---
void opencv_video_loop() {
    cv::VideoCapture video_capture(CAMERA_INDEX);
    if (!video_capture.isOpened()) {
        std::cerr << "❌ Could not open web camera at index: " << CAMERA_INDEX << std::endl;
        running_capture = false;
        return;
    }

    // Try to set hardware capture dimensions for efficiency
    video_capture.set(cv::CAP_PROP_FRAME_WIDTH, WIDTH);
    video_capture.set(cv::CAP_PROP_FRAME_HEIGHT, HEIGHT);

    cv::Mat frame;
    std::vector<uint8_t> jpeg_buffer;
    std::vector<int> params = {cv::IMWRITE_JPEG_QUALITY, 75};

    while (running_capture) {
        video_capture >> frame;
        
        if (frame.empty()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            continue;
        }

        if (frame.cols != WIDTH || frame.rows != HEIGHT) {
            cv::resize(frame, frame, cv::Size(WIDTH, HEIGHT));
        }

        // Compress frame to JPEG for WebRTC DataChannel delivery
        cv::imencode(".jpg", frame, jpeg_buffer, params);

        {
            std::lock_guard<std::mutex> lock(frame_mutex);
            latest_frame_bytes = std::move(jpeg_buffer);
            frame_ready = true;
        }
        frame_cv.notify_one();
        
        // ~30FPS pacing delay for the camera thread
        std::this_thread::sleep_for(std::chrono::milliseconds(33));
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

    std::shared_ptr<rtc::PeerConnection> pc;
    std::shared_ptr<rtc::DataChannel> video_channel;

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
                viewer_id = payload.value("from", "");
                std::cout << "📥 Received Offer from " << viewer_id << " (Webcam Stream Core)" << std::endl;

                pc = std::make_shared<rtc::PeerConnection>(config);

                pc->onLocalDescription([&](rtc::Description description) {
                    json answer = {
                        {"type", description.typeString()},
                        {"from", peer_id},
                        {"to", viewer_id},
                        {"data", {{"sdp", std::string(description)}, {"type", description.typeString()}}}
                    };
                    mqtt_client.publish(TOPIC, answer.dump());
                });

                pc->onLocalCandidate([&](rtc::Candidate candidate) {
                    json ice = {
                        {"type", "ice"}, {"from", peer_id}, {"to", viewer_id},
                        {"data", {{"candidate", std::string(candidate)}, {"sdpMid", candidate.mid()}, {"sdpMLineIndex", 0}}}
                    };
                    mqtt_client.publish(TOPIC, ice.dump());
                });

                pc->onDataChannel([&](std::shared_ptr<rtc::DataChannel> dc) {
                    if (dc->label() == "video-stream") {
                        video_channel = dc;
                        video_channel->onOpen([&]() {
                            std::cout << "🚀 Video Data Channel Connected. Streaming Webcam." << std::endl;
                            streaming_allowed = true;
                        });
                        video_channel->onClosed([&]() { streaming_allowed = false; });
                    }
                });

                pc->onStateChange([&](rtc::PeerConnection::State state) {
                    std::cout << "[*] WebRTC State: " << state << std::endl;
                    if (state == rtc::PeerConnection::State::Failed || state == rtc::PeerConnection::State::Closed) {
                        streaming_allowed = false;
                    }
                });

                std::string sdp = payload["data"]["sdp"];
                pc->setRemoteDescription(rtc::Description(sdp, "offer"));

            } else if (type == "ice" && pc) {
                std::string candidate_str = payload["data"]["candidate"];
                std::string mid = payload["data"]["sdpMid"];
                pc->addRemoteCandidate(rtc::Candidate(candidate_str, mid));
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
    std::cout << "📷 WEBCAM LIVE STREAM TRANSCEIVER ONLINE" << std::endl;
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

        if (streaming_allowed && video_channel && video_channel->isOpen() && !frame_buffer.empty()) {
            try {
                video_channel->send(reinterpret_cast<const std::byte*>(frame_buffer.data()), frame_buffer.size());
            } catch (...) {}
        }
    }

    running_capture = false;
    frame_cv.notify_all();
    if (presence_thread.joinable()) presence_thread.join();
    if (capture_thread.joinable()) capture_thread.join();
    return 0;
}