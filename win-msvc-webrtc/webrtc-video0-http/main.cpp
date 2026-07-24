#define PAHO_MQTTPP_IMPORTS 0

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
#include <queue>

#include <opencv2/opencv.hpp>
#include <opencv2/core/utils/logger.hpp>
#include <nlohmann/json.hpp>
#include <rtc/rtc.hpp>
#include <httplib.h>

using json = nlohmann::json;

const int WIDTH = 640;
const int HEIGHT = 480;
const uint16_t PORT = 8889;
const std::string PORTMAP_ENDPOINT = "ashloverscn-58056.portmap.host:58056";

std::mutex frame_mutex;
std::condition_variable frame_cv;
std::vector<uint8_t> latest_frame_bytes;
bool frame_ready = false;
bool running_capture = false;

// Synchronizing state transitions between signaling thread and delivery thread
std::mutex connection_mutex;
bool streaming_allowed = false;
std::shared_ptr<rtc::PeerConnection> pc;
std::shared_ptr<rtc::DataChannel> video_channel;
uint64_t current_session_id = 0;

// Signaling message queues for HTTP polling
std::mutex signaling_mutex;
std::queue<json> cpp_to_browser_queue;

std::string generate_peer_id() {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> distr(1000, 9999);
    return "video0_cpp_" + std::to_string(distr(gen));
}

std::string peer_id = generate_peer_id();

void clear_active_session_pointers() {
    streaming_allowed = false;
    if (video_channel) {
        try { video_channel->close(); } catch (...) {}
        video_channel.reset();
    }
    if (pc) {
        pc.reset();
    }
}

// --- OpenCV Webcam Loop ---
void opencv_video_loop() {
    cv::VideoCapture video_capture(0, cv::CAP_ANY);

    if (!video_capture.isOpened()) {
        std::cerr << "❌ Error: Could not open webcam at index 0!" << std::endl;
        running_capture = false;
        return;
    }

    video_capture.set(cv::CAP_PROP_FRAME_WIDTH, WIDTH);
    video_capture.set(cv::CAP_PROP_FRAME_HEIGHT, HEIGHT);
    
    auto frame_duration = std::chrono::milliseconds(33);
    cv::Mat frame;

    std::vector<uint8_t> encoded_buffer;
    std::vector<int> compression_params = {cv::IMWRITE_JPEG_QUALITY, 75};

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

        cv::imencode(".jpg", frame, encoded_buffer, compression_params);
        
        {
            std::lock_guard<std::mutex> lock(frame_mutex);
            latest_frame_bytes = std::move(encoded_buffer);
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

    httplib::Server http_svr;
    std::string html_content = R"(<!DOCTYPE html>
<html>
<head>
    <title>C++ WebRTC Webcam Viewer</title>
    <style>
        body { font-family: Arial, sans-serif; text-align: center; background: #121212; color: #fff; margin-top: 50px; }
        #remoteVideo { width: 640px; height: 480px; background: #000; border: 2px solid #444; border-radius: 8px; object-fit: contain; }
        #status { margin: 15px; font-weight: bold; color: #ff9800; }
    </style>
</head>
<body>
    <h2>C++ OpenCV WebRTC Transceiver</h2>
    <div id="status">Initializing WebRTC PeerConnection...</div>
    <img id="remoteVideo" alt="Awaiting video stream..." />
    <script>
        const statusDiv = document.getElementById('status');
        const imgElement = document.getElementById('remoteVideo');
        
        let pc = null;
        const peer_id = "viewer_" + Math.floor(Math.random() * 9000 + 1000);

        async function sendSignaling(msg) {
            await fetch('/signal', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(msg)
            });
        }

        async function pollSignaling() {
            try {
                let res = await fetch('/poll');
                if (res.ok) {
                    let messages = await res.json();
                    for (let msg of messages) {
                        handleSignalingMessage(msg);
                    }
                }
            } catch (e) {
                console.error("Polling error:", e);
            }
            setTimeout(pollSignaling, 500);
        }

        async function initWebRTC() {
            statusDiv.innerText = "Setting up PeerConnection...";
            pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });

            const dataChannel = pc.createDataChannel('video-stream', { ordered: false, maxRetransmits: 0 });
            dataChannel.binaryType = 'arraybuffer';

            dataChannel.onopen = () => {
                statusDiv.innerText = "Data Channel Open. Receiving Real IP Video Stream...";
                statusDiv.style.color = "#4caf50";
            };

            dataChannel.onmessage = (event) => {
                const blob = new Blob([event.data], { type: 'image/jpeg' });
                const url = URL.createObjectURL(blob);
                imgElement.src = url;
                imgElement.onload = () => URL.revokeObjectURL(url);
            };

            pc.onicecandidate = (event) => {
                if (event.candidate) {
                    console.log("[Client] Local ICE Candidate:", event.candidate.candidate);
                    sendSignaling({
                        type: 'ice',
                        from: peer_id,
                        data: { candidate: event.candidate.candidate, sdpMid: event.candidate.sdpMid }
                    });
                }
            };

            const offer = await pc.createOffer();
            console.log("[Client] Created Local Offer SDP:", offer.sdp);
            await pc.setLocalDescription(offer);

            await sendSignaling({
                type: 'offer',
                from: peer_id,
                data: { sdp: offer.sdp, type: offer.type }
            });

            statusDiv.innerText = "Offer sent. Awaiting Answer...";
            pollSignaling();
        }

        async function handleSignalingMessage(msg) {
            console.log("[Client] Signaling Message Received:", msg.type);
            if (msg.type === 'answer') {
                if (!pc.remoteDescription || pc.remoteDescription.type === "") {
                    console.log("[Client] Setting Remote Answer SDP:", msg.data.sdp);
                    await pc.setRemoteDescription(new RTCSessionDescription({ type: msg.data.type, sdp: msg.data.sdp }));
                    statusDiv.innerText = "Streaming Active using Real IP!";
                }
            } else if (msg.type === 'ice') {
                try {
                    console.log("[Client] Adding Remote ICE Candidate:", msg.data.candidate);
                    await pc.addIceCandidate(new RTCIceCandidate({ candidate: msg.data.candidate, sdpMid: msg.data.sdpMid }));
                } catch (e) {
                    console.error('Error adding received ICE candidate', e);
                }
            }
        }

        initWebRTC();
    </script>
</body>
</html>)";

    // Serve HTML Web UI
    http_svr.Get("/", [html_content](const httplib::Request&, httplib::Response& res) {
        res.set_content(html_content, "text/html");
    });

    // Handle Incoming Signaling Messages via HTTP POST
    http_svr.Post("/signal", [config](const httplib::Request& req, httplib::Response& res) {
        try {
            auto payload = json::parse(req.body);
            std::string type = payload.value("type", "");

            if (type == "offer") {
                std::shared_ptr<rtc::PeerConnection> old_pc_to_destroy = nullptr;
                uint64_t this_session = 0;

                {
                    std::lock_guard<std::mutex> lock(connection_mutex);
                    current_session_id++;
                    this_session = current_session_id;

                    std::cout << "📥 Offer Received. Staging Session #" << this_session << "..." << std::endl;
                    if (pc) old_pc_to_destroy = pc;
                    clear_active_session_pointers();

                    pc = std::make_shared<rtc::PeerConnection>(config);
                }

                if (old_pc_to_destroy) {
                    try { old_pc_to_destroy->close(); } catch(...) {}
                    old_pc_to_destroy.reset();
                }

                std::shared_ptr<rtc::PeerConnection> local_pc = pc;

                local_pc->onLocalDescription([this_session](rtc::Description description) {
                    {
                        std::lock_guard<std::mutex> lock(connection_mutex);
                        if (this_session != current_session_id) return;
                    }
                    
                    std::string sdp_str = std::string(description);
                    std::cout << "\n[WEBRTC HANDSHAKE] Generated Local Description (SDP):\n" << sdp_str << "\n" << std::endl;

                    json answer = {
                        {"type", description.typeString()},
                        {"from", peer_id},
                        {"data", {{"sdp", sdp_str}, {"type", description.typeString()}}}
                    };
                    
                    std::lock_guard<std::mutex> lock(signaling_mutex);
                    cpp_to_browser_queue.push(answer);
                });

                local_pc->onLocalCandidate([this_session](rtc::Candidate candidate) {
                    {
                        std::lock_guard<std::mutex> lock(connection_mutex);
                        if (this_session != current_session_id) return;
                    }
                    
                    std::string cand_str = std::string(candidate);
                    std::cout << "[WEBRTC HANDSHAKE] Generated Local ICE Candidate: " << cand_str << std::endl;

                    json ice = {
                        {"type", "ice"}, {"from", peer_id},
                        {"data", {{"candidate", cand_str}, {"sdpMid", candidate.mid()}, {"sdpMLineIndex", 0}}}
                    };
                    
                    std::lock_guard<std::mutex> lock(signaling_mutex);
                    cpp_to_browser_queue.push(ice);
                });

                local_pc->onDataChannel([local_pc, this_session](std::shared_ptr<rtc::DataChannel> dc) {
                    if (dc->label() == "video-stream") {
                        std::lock_guard<std::mutex> dc_lock(connection_mutex);
                        if (this_session != current_session_id) return;
                        
                        video_channel = dc;
                        
                        video_channel->onOpen([this_session]() {
                            std::lock_guard<std::mutex> stream_lock(connection_mutex);
                            if (this_session != current_session_id) return;
                            std::cout << "🚀 Video Data Channel Connected. Streaming Webcam [Session #" << this_session << "]." << std::endl;
                            streaming_allowed = true;
                        });
                        
                        video_channel->onClosed([this_session]() { 
                            std::lock_guard<std::mutex> stream_lock(connection_mutex);
                            if (this_session != current_session_id) return;
                            std::cout << "🛑 Video Data Channel Closed [Session #" << this_session << "]." << std::endl;
                            streaming_allowed = false; 
                        });
                    }
                });

                local_pc->onStateChange([this_session, local_pc](rtc::PeerConnection::State state) {
                    std::cout << "[*] WebRTC State Change: " << static_cast<int>(state) << " [Session #" << this_session << "]" << std::endl;
                    if (state == rtc::PeerConnection::State::Failed || 
                        state == rtc::PeerConnection::State::Disconnected || 
                        state == rtc::PeerConnection::State::Closed) {
                        
                        std::thread([this_session, local_pc]() {
                            std::this_thread::sleep_for(std::chrono::milliseconds(30));
                            std::lock_guard<std::mutex> state_lock(connection_mutex);
                            
                            if (this_session == current_session_id) {
                                std::cout << "⚠️ Processing structural drop for active Session #" << this_session << "..." << std::endl;
                                clear_active_session_pointers();
                                try { local_pc->close(); } catch(...) {}
                                std::cout << "✅ Resources cleared. Ready for incoming loops." << std::endl;
                            }
                        }).detach();
                    }
                });

                std::string sdp = payload["data"]["sdp"];
                std::cout << "\n[WEBRTC HANDSHAKE] Applying Remote Offer SDP:\n" << sdp << "\n" << std::endl;
                local_pc->setRemoteDescription(rtc::Description(sdp, "offer"));

            } else if (type == "ice") {
                std::lock_guard<std::mutex> lock(connection_mutex);
                if (!pc) return;
                
                try {
                    std::string candidate_str = payload["data"]["candidate"];
                    std::string mid = payload["data"]["sdpMid"];
                    std::cout << "[WEBRTC HANDSHAKE] Adding Remote ICE Candidate: " << candidate_str << std::endl;
                    if (!candidate_str.empty()) {
                        pc->addRemoteCandidate(rtc::Candidate(candidate_str, mid));
                    }
                } catch (const std::exception& e) {
                    std::cerr << "⚠️ Dropped ICE candidate: " << e.what() << std::endl;
                }
            }
        } catch (const std::exception& e) {
            std::cerr << "Signaling Parse Error: " << e.what() << std::endl;
            res.status = 400;
            return;
        }
        res.set_content("{\"status\":\"ok\"}", "application/json");
    });

    // Provide Polling Endpoint for Browser Signaling Updates
    http_svr.Get("/poll", [](const httplib::Request&, httplib::Response& res) {
        json batch = json::array();
        {
            std::lock_guard<std::mutex> lock(signaling_mutex);
            while (!cpp_to_browser_queue.empty()) {
                batch.push_back(cpp_to_browser_queue.front());
                cpp_to_browser_queue.pop();
            }
        }
        res.set_content(batch.dump(), "application/json");
    });

    std::thread http_thread([&http_svr]() {
        http_svr.listen("0.0.0.0", PORT);
    });

    std::cout << "============================================" << std::endl;
    std::cout << "🎥 WEBCAM TRANSCEIVER ONLINE (SINGLE-PORT)" << std::endl;
    std::cout << "LOCAL URL  : http://localhost:" << PORT << "/" << std::endl;
    std::cout << "PORTMAP URL: http://" << PORTMAP_ENDPOINT << "/" << std::endl;
    std::cout << "DEVICE ID  : " << peer_id << std::endl;
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
                video_channel->send(reinterpret_cast<const std::byte*>(frame_buffer.data()), frame_buffer.size());
            } catch (...) {}
        }
    }

    running_capture = false;
    frame_cv.notify_all();
    
    {
        std::lock_guard<std::mutex> lock(connection_mutex);
        if (pc) { try { pc->close(); } catch(...) {} }
        clear_active_session_pointers();
    }
    
    http_svr.stop();
    if (capture_thread.joinable()) capture_thread.join();
    if (http_thread.joinable()) http_thread.join();
    return 0;
}
