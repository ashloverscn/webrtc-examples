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
#include <fstream>

#include <nlohmann/json.hpp>
#include <rtc/rtc.hpp>
#include <httplib.h>

using json = nlohmann::json;

const uint16_t PORT = 8889;
const std::string PORTMAP_ENDPOINT = "ashloverscn-58056.portmap.host:58056";

// Synchronizing state transitions between signaling thread and delivery thread
std::mutex connection_mutex;
bool receiving_allowed = false;
std::shared_ptr<rtc::PeerConnection> pc;
std::shared_ptr<rtc::DataChannel> file_channel;
uint64_t current_session_id = 0;

// Signaling message queues for HTTP polling
std::mutex signaling_mutex;
std::queue<json> cpp_to_browser_queue;

// File reception tracking state
struct ReceivedFile {
    std::string name;
    size_t size = 0;
    size_t received_bytes = 0;
    std::vector<uint8_t> data;
    std::ofstream file_stream;
} active_file;

std::mutex file_io_mutex;

std::string generate_peer_id() {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> distr(1000, 9999);
    return "uploader_cpp_" + std::to_string(distr(gen));
}

std::string peer_id = generate_peer_id();

void clear_active_session_pointers() {
    receiving_allowed = false;
    if (file_channel) {
        try { file_channel->close(); } catch (...) {}
        file_channel.reset();
    }
    if (pc) {
        pc.reset();
    }
}

int main() {
    rtc::InitLogger(rtc::LogLevel::Info);

    rtc::Configuration config;
    config.iceServers.emplace_back("stun:stun.l.google.com:19302");

    httplib::Server http_svr;
    std::string html_content = R"(<!DOCTYPE html>
<html>
<head>
    <title>C++ WebRTC File Uploader</title>
    <style>
        body { font-family: Arial, sans-serif; text-align: center; background: #121212; color: #fff; margin-top: 50px; }
        #status { margin: 15px; font-weight: bold; color: #ff9800; }
        .container { margin: 30px auto; width: 400px; padding: 20px; background: #1e1e1e; border: 2px solid #444; border-radius: 8px; }
        input[type="file"] { margin: 15px 0; color: #fff; }
        button { background: #4caf50; color: white; border: none; padding: 10px 20px; font-size: 16px; border-radius: 4px; cursor: pointer; }
        button:disabled { background: #555; cursor: not-allowed; }
        #progressBar { width: 100%; background: #333; border-radius: 4px; margin-top: 15px; overflow: hidden; display: none; }
        #progressFill { height: 20px; width: 0%; background: #4caf50; text-align: center; line-height: 20px; font-size: 12px; }
    </style>
</head>
<body>
    <h2>C++ WebRTC File Uploader</h2>
    <div id="status">Initializing WebRTC PeerConnection...</div>
    
    <div class="container">
        <input type="file" id="fileInput" /><br>
        <button id="uploadBtn" disabled>Send File</button>
        <div id="progressBar">
            <div id="progressFill">0%</div>
        </div>
    </div>

    <script>
        const statusDiv = document.getElementById('status');
        const fileInput = document.getElementById('fileInput');
        const uploadBtn = document.getElementById('uploadBtn');
        const progressBar = document.getElementById('progressBar');
        const progressFill = document.getElementById('progressFill');

        let pc = null;
        let dataChannel = null;
        const peer_id = "uploader_" + Math.floor(Math.random() * 9000 + 1000);
        const CHUNK_SIZE = 16384;

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

            dataChannel = pc.createDataChannel('file-transfer', { ordered: true });
            dataChannel.binaryType = 'arraybuffer';

            dataChannel.onopen = () => {
                statusDiv.innerText = "Data Channel Open. Ready to upload files.";
                statusDiv.style.color = "#4caf50";
                uploadBtn.disabled = false;
            };

            dataChannel.onclose = () => {
                statusDiv.innerText = "Data Channel Closed.";
                statusDiv.style.color = "#ff5722";
                uploadBtn.disabled = true;
            };

            pc.onicecandidate = (event) => {
                if (event.candidate) {
                    sendSignaling({
                        type: 'ice',
                        from: peer_id,
                        data: { candidate: event.candidate.candidate, sdpMid: event.candidate.sdpMid }
                    });
                }
            };

            const offer = await pc.createOffer();
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
            if (msg.type === 'answer') {
                if (!pc.remoteDescription || pc.remoteDescription.type === "") {
                    await pc.setRemoteDescription(new RTCSessionDescription({ type: msg.data.type, sdp: msg.data.sdp }));
                    statusDiv.innerText = "Connected! Ready to transfer files.";
                }
            } else if (msg.type === 'ice') {
                try {
                    await pc.addIceCandidate(new RTCIceCandidate({ candidate: msg.data.candidate, sdpMid: msg.data.sdpMid }));
                } catch (e) {
                    console.error('Error adding received ICE candidate', e);
                }
            }
        }

        uploadBtn.onclick = async () => {
            const file = fileInput.files[0];
            if (!file) return;

            uploadBtn.disabled = true;
            fileInput.disabled = true;
            progressBar.style.display = 'block';

            const metadata = JSON.stringify({ name: file.name, size: file.size, type: file.type });
            dataChannel.send(JSON.stringify({ type: 'metadata', payload: metadata }));

            const reader = new FileReader();
            let offset = 0;

            reader.onload = (e) => {
                if (dataChannel.bufferedAmount > 65536) {
                    setTimeout(() => reader.readAsArrayBuffer(file.slice(offset, offset + CHUNK_SIZE)), 50);
                    return;
                }

                dataChannel.send(e.target.result);
                offset += e.target.result.byteLength;

                const percent = Math.round((offset / file.size) * 100);
                progressFill.style.width = percent + '%';
                progressFill.innerText = percent + '%';

                if (offset < file.size) {
                    readNextChunk();
                } else {
                    statusDiv.innerText = "File transfer complete!";
                    uploadBtn.disabled = false;
                    fileInput.disabled = false;
                }
            };

            function readNextChunk() {
                const slice = file.slice(offset, offset + CHUNK_SIZE);
                reader.readAsArrayBuffer(slice);
            }

            readNextChunk();
        };

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
                    if (dc->label() == "file-transfer") {
                        std::lock_guard<std::mutex> dc_lock(connection_mutex);
                        if (this_session != current_session_id) return;
                        
                        file_channel = dc;
                        
                        file_channel->onOpen([this_session]() {
                            std::lock_guard<std::mutex> stream_lock(connection_mutex);
                            if (this_session != current_session_id) return;
                            std::cout << "🚀 File Data Channel Connected [Session #" << this_session << "]." << std::endl;
                            receiving_allowed = true;
                        });
                        
                        file_channel->onClosed([this_session]() { 
                            std::lock_guard<std::mutex> stream_lock(connection_mutex);
                            if (this_session != current_session_id) return;
                            std::cout << "🛑 File Data Channel Closed [Session #" << this_session << "]." << std::endl;
                            receiving_allowed = false; 
                        });

                        file_channel->onMessage([this_session](rtc::message_variant data) {
                            std::lock_guard<std::mutex> io_lock(file_io_mutex);
                            if (std::holds_alternative<std::string>(data)) {
                                std::string text = std::get<std::string>(data);
                                try {
                                    auto msg = json::parse(text);
                                    if (msg.value("type", "") == "metadata") {
                                        auto meta = json::parse(msg.value("payload", "{}"));
                                        active_file.name = meta.value("name", "uploaded_file");
                                        active_file.size = meta.value("size", 0);
                                        active_file.received_bytes = 0;
                                        active_file.file_stream.open(active_file.name, std::ios::binary);
                                        std::cout << "📄 Receiving file: " << active_file.name << " (" << active_file.size << " bytes)" << std::endl;
                                    }
                                } catch (...) {}
                            } else if (std::holds_alternative<rtc::binary>(data)) {
                                auto bin = std::get<rtc::binary>(data);
                                if (active_file.file_stream.is_open()) {
                                    active_file.file_stream.write(reinterpret_cast<const char*>(bin.data()), bin.size());
                                    active_file.received_bytes += bin.size();
                                    if (active_file.received_bytes >= active_file.size) {
                                        active_file.file_stream.close();
                                        std::cout << "✅ File successfully received and saved as: " << active_file.name << std::endl;
                                    }
                                }
                            }
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
    std::cout << "📁 FILE UPLOADER SERVER ONLINE (SINGLE-PORT)" << std::endl;
    std::cout << "LOCAL URL  : http://localhost:" << PORT << "/" << std::endl;
    std::cout << "PORTMAP URL: http://" << PORTMAP_ENDPOINT << "/" << std::endl;
    std::cout << "DEVICE ID  : " << peer_id << std::endl;
    std::cout << "============================================" << std::endl;

    // Keep the main thread alive while HTTP server runs
    while (true) {
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }

    http_svr.stop();
    if (http_thread.joinable()) http_thread.join();
    return 0;
}