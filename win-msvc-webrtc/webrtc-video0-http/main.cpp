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
#include <unordered_set>

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
std::string viewer_id = "";

// Active WebSocket client sessions
std::mutex clients_mutex;
std::unordered_set<std::shared_ptr<rtc::WebSocket>> active_websockets;
std::shared_ptr<rtc::WebSocket> active_viewer_ws = nullptr;

uint64_t current_session_id = 0;

std::string generate_peer_id() {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> distr(1000, 9999);
    return "video0_cpp_" + std::to_string(distr(gen));
}

std::string peer_id = generate_peer_id();

void clear_active_session_pointers() {
    streaming_allowed = false;
    viewer_id = "";
    active_viewer_ws = nullptr;
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
    rtc::InitLogger(rtc::LogLevel::Warning);

    running_capture = true;
    std::thread capture_thread(opencv_video_loop);

    rtc::Configuration config;
    config.iceServers.emplace_back("stun:stun.l.google.com:19302");

    // Initialize libdatachannel WebSocketServer for Signaling on port 8890 (internal ws pipe)
    // To achieve single-port external tunneling via portmap, ws routes separately
    rtc::WebSocketServer::Configuration ws_config;
    ws_config.port = PORT + 1; // 8890 internally for WebRTC signaling websocket
    auto ws_server = std::make_shared<rtc::WebSocketServer>(ws_config);

    ws_server->onClient([config](std::shared_ptr<rtc::WebSocket> ws) {
        {
            std::lock_guard<std::mutex> lock(clients_mutex);
            active_websockets.insert(ws);
        }

        ws->onOpen([ws]() {
            std::string remote_addr = ws->remoteAddress().value_or("unknown");
            std::cout << "✅ Viewer WebSocket Connected: " << remote_addr << std::endl;
        });

        ws->onMessage([ws, config](std::variant<rtc::binary, rtc::string> data) {
            if (!std::holds_alternative<rtc::string>(data)) return;
            std::string msg_str = std::get<rtc::string>(data);

            try {
                auto payload = json::parse(msg_str);
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

                        viewer_id = payload.value("from", "viewer");
                        active_viewer_ws = ws;
                        pc = std::make_shared<rtc::PeerConnection>(config);
                    }

                    if (old_pc_to_destroy) {
                        try { old_pc_to_destroy->close(); } catch(...) {}
                        old_pc_to_destroy.reset();
                    }

                    std::shared_ptr<rtc::PeerConnection> local_pc = pc;

                    local_pc->onLocalDescription([ws, this_session](rtc::Description description) {
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
                        try { ws->send(answer.dump()); } catch (...) {}
                    });

                    local_pc->onLocalCandidate([ws, this_session](rtc::Candidate candidate) {
                        {
                            std::lock_guard<std::mutex> lock(connection_mutex);
                            if (this_session != current_session_id) return;
                        }
                        json ice = {
                            {"type", "ice"}, {"from", peer_id}, {"to", viewer_id},
                            {"data", {{"candidate", std::string(candidate)}, {"sdpMid", candidate.mid()}, {"sdpMLineIndex", 0}}}
                        };
                        try { ws->send(ice.dump()); } catch (...) {}
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
                        std::cout << "[*] WebRTC State Change: " << static_cast<int>(state) << " [Session #" << this_session << "]" << std::endl;
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
                                }
                            }).detach();
                        }
                    });

                    try {
                        std::string sdp = payload["data"]["sdp"];
                        local_pc->setRemoteDescription(rtc::Description(sdp, "offer"));
                    } catch (const std::exception& e) {
                        std::cerr << "❌ Failed to parse Remote Offer: " << e.what() << std::endl;
                        std::lock_guard<std::mutex> err_lock(connection_mutex);
                        if (this_session == current_session_id) {
                            clear_active_session_pointers();
                            try { local_pc->close(); } catch(...) {}
                        }
                    }

                } else if (type == "ice") {
                    std::lock_guard<std::mutex> lock(connection_mutex);
                    if (!pc) return;
                    
                    try {
                        std::string candidate_str = payload["data"]["candidate"];
                        std::string mid = payload["data"]["sdpMid"];
                        if (!candidate_str.empty()) {
                            pc->addRemoteCandidate(rtc::Candidate(candidate_str, mid));
                        }
                    } catch (const std::exception& e) {
                        std::cerr << "⚠️ Dropped ICE candidate: " << e.what() << std::endl;
                    }
                }
            } catch (const std::exception& e) {
                std::cerr << "Signaling Parse Error: " << e.what() << std::endl;
            }
        });

        ws->onClosed([ws]() {
            std::cout << "🔌 Viewer WebSocket Disconnected." << std::endl;
            std::lock_guard<std::mutex> lock(clients_mutex);
            active_websockets.erase(ws);

            std::lock_guard<std::mutex> conn_lock(connection_mutex);
            if (active_viewer_ws == ws) {
                if (pc) { try { pc->close(); } catch(...) {} }
                clear_active_session_pointers();
            }
        });
    });

    // Initialize cpp-httplib Server for HTTP web pages on port 8889
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
    <div id="status">Connecting to signaling server...</div>
    <img id="remoteVideo" alt="Awaiting video stream..." />
    <script>
        const statusDiv = document.getElementById('status');
        const imgElement = document.getElementById('remoteVideo');
        
        // Automatically maps WebSocket port to HTTP port + 1 (8890) or relative matching
        const wsPort = 8890;
        const wsProtocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
        const ws = new WebSocket(wsProtocol + window.location.hostname + ':' + wsPort + '/');
        let pc = null;
        const peer_id = "viewer_" + Math.floor(Math.random() * 9000 + 1000);

        ws.onopen = async () => {
            statusDiv.innerText = "Connected to Signaling. Initializing WebRTC PeerConnection...";
            pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });

            const dataChannel = pc.createDataChannel('video-stream', { ordered: false, maxRetransmits: 0 });
            dataChannel.binaryType = 'arraybuffer';

            dataChannel.onopen = () => {
                statusDiv.innerText = "Data Channel Open. Receiving Video Stream...";
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
                    ws.send(JSON.stringify({
                        type: 'ice',
                        from: peer_id,
                        data: { candidate: event.candidate.candidate, sdpMid: event.candidate.sdpMid }
                    }));
                }
            };

            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);

            ws.send(JSON.stringify({
                type: 'offer',
                from: peer_id,
                data: { sdp: offer.sdp, type: offer.type }
            }));
        };

        ws.onmessage = async (event) => {
            const msg = JSON.parse(event.data);
            if (msg.type === 'answer') {
                await pc.setRemoteDescription(new RTCSessionDescription({ type: msg.data.type, sdp: msg.data.sdp }));
                statusDiv.innerText = "Streaming Active!";
            } else if (msg.type === 'ice') {
                try {
                    await pc.addIceCandidate(new RTCIceCandidate({ candidate: msg.data.candidate, sdpMid: msg.data.sdpMid }));
                } catch (e) {
                    console.error('Error adding received ICE candidate', e);
                }
            }
        };

        ws.onclose = () => { statusDiv.innerText = "Disconnected from signaling."; statusDiv.style.color = "#f44336"; };
    </script>
</body>
</html>)";

    http_svr.Get("/", [html_content](const httplib::Request&, httplib::Response& res) {
        res.set_content(html_content, "text/html");
    });

    std::thread http_thread([&http_svr]() {
        http_svr.listen("0.0.0.0", PORT);
    });

    std::cout << "============================================" << std::endl;
    std::cout << "🎥 WEBCAM TRANSCEIVER ONLINE" << std::endl;
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
    ws_server.reset();
    if (capture_thread.joinable()) capture_thread.join();
    if (http_thread.joinable()) http_thread.join();
    return 0;
}
