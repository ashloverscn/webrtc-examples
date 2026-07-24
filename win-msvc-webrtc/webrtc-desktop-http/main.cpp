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

#include <nlohmann/json.hpp>
#include <rtc/rtc.hpp>
#include <httplib.h>
#include <windows.h>
#include <objidl.h>
#include <gdiplus.h>

#pragma comment (lib, "gdiplus.lib")

using json = nlohmann::json;

const uint16_t PORT = 8889;
const std::string PORTMAP_ENDPOINT = "ashloverscn-58056.portmap.host:58056";

std::mutex connection_mutex;
bool streaming_allowed = false;
std::shared_ptr<rtc::PeerConnection> pc;
std::shared_ptr<rtc::DataChannel> screen_channel;
std::shared_ptr<rtc::DataChannel> control_channel;
uint64_t current_session_id = 0;

std::mutex signaling_mutex;
std::queue<json> cpp_to_browser_queue;

std::string generate_peer_id() {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<> distr(1000, 9999);
    return "desktop_cpp_" + std::to_string(distr(gen));
}

std::string peer_id = generate_peer_id();

void clear_active_session_pointers() {
    streaming_allowed = false;
    if (screen_channel) {
        try { screen_channel->close(); } catch (...) {}
        screen_channel.reset();
    }
    if (control_channel) {
        try { control_channel->close(); } catch (...) {}
        control_channel.reset();
    }
    if (pc) {
        pc.reset();
    }
}

struct ScreenFrame {
    std::vector<uint8_t> data;
    int width;
    int height;
};

std::mutex frame_mutex;
std::condition_variable frame_cv;
ScreenFrame latest_frame;
bool frame_ready = false;
bool running_capture = false;

int GetEncoderClsid(const WCHAR* format, CLSID* pClsid) {
    UINT num = 0, size = 0;
    Gdiplus::GetImageEncodersSize(&num, &size);
    if (size == 0) return -1;
    std::vector<BYTE> codecInfo(size);
    Gdiplus::ImageCodecInfo* pImageCodecInfo = (Gdiplus::ImageCodecInfo*)(codecInfo.data());
    Gdiplus::GetImageEncoders(num, size, pImageCodecInfo);
    for (UINT j = 0; j < num; ++j) {
        if (wcscmp(pImageCodecInfo[j].MimeType, format) == 0) {
            *pClsid = pImageCodecInfo[j].Clsid;
            return j;
        }
    }
    return -1;
}

bool EncodeBitmapToJPEG(HBITMAP hBitmap, int quality, std::vector<uint8_t>& buffer) {
    Gdiplus::Bitmap bitmap(hBitmap, NULL);
    CLSID jpegClsid;
    if (GetEncoderClsid(L"image/jpeg", &jpegClsid) == -1) return false;

    Gdiplus::EncoderParameters encoderParams;
    encoderParams.Count = 1;
    encoderParams.Parameter[0].Guid = Gdiplus::EncoderQuality;
    encoderParams.Parameter[0].Type = Gdiplus::EncoderParameterValueTypeLong;
    encoderParams.Parameter[0].NumberOfValues = 1;
    encoderParams.Parameter[0].Value = &quality;

    IStream* pStream = NULL;
    if (CreateStreamOnHGlobal(NULL, TRUE, &pStream) != S_OK) return false;

    if (bitmap.Save(pStream, &jpegClsid, &encoderParams) != Gdiplus::Ok) {
        pStream->Release();
        return false;
    }

    ULARGE_INTEGER liSize;
    {
        STATSTG stats;
        if (pStream->Stat(&stats, STATFLAG_NONAME) == S_OK) {
            liSize = stats.cbSize;
        } else {
            liSize.QuadPart = 0;
        }
    }
    DWORD len = (DWORD)liSize.QuadPart;

    buffer.resize(len);
    HGLOBAL hGlobal = NULL;
    GetHGlobalFromStream(pStream, &hGlobal);
    void* pBuffer = GlobalLock(hGlobal);
    if (pBuffer) {
        memcpy(buffer.data(), pBuffer, len);
        GlobalUnlock(hGlobal);
    }
    pStream->Release();
    return true;
}

void screen_capture_loop() {
    Gdiplus::GdiplusStartupInput gdiplusStartupInput;
    ULONG_PTR gdiplusToken;
    Gdiplus::GdiplusStartup(&gdiplusToken, &gdiplusStartupInput, NULL);

    int screen_w = GetSystemMetrics(SM_CXSCREEN);
    int screen_h = GetSystemMetrics(SM_CYSCREEN);

    HDC hScreenDC = GetDC(NULL);
    HDC hMemoryDC = CreateCompatibleDC(hScreenDC);

    auto frame_duration = std::chrono::milliseconds(50);

    while (running_capture) {
        auto start_time = std::chrono::steady_clock::now();

        HBITMAP hBitmap = CreateCompatibleBitmap(hScreenDC, screen_w, screen_h);
        HBITMAP hOldBitmap = (HBITMAP)SelectObject(hMemoryDC, hBitmap);

        BitBlt(hMemoryDC, 0, 0, screen_w, screen_h, hScreenDC, 0, 0, SRCCOPY | CAPTUREBLT);
        SelectObject(hMemoryDC, hOldBitmap);

        std::vector<uint8_t> encoded;
        if (EncodeBitmapToJPEG(hBitmap, 60, encoded)) {
            std::lock_guard<std::mutex> lock(frame_mutex);
            latest_frame = {std::move(encoded), screen_w, screen_h};
            frame_ready = true;
            frame_cv.notify_one();
        }

        DeleteObject(hBitmap);

        auto processing_time = std::chrono::steady_clock::now() - start_time;
        if (processing_time < frame_duration) {
            std::this_thread::sleep_for(frame_duration - processing_time);
        }
    }

    DeleteDC(hMemoryDC);
    ReleaseDC(NULL, hScreenDC);
    Gdiplus::GdiplusShutdown(gdiplusToken);
}

void handle_remote_input(const std::string& msg_str) {
    try {
        auto j = json::parse(msg_str);
        std::string type = j.value("type", "");

        if (type == "mousemove") {
            int x = j.value("x", 0);
            int y = j.value("y", 0);
            int screen_w = GetSystemMetrics(SM_CXSCREEN);
            int screen_h = GetSystemMetrics(SM_CYSCREEN);
            
            double abs_x = (double)x / j.value("vw", screen_w) * 65535.0;
            double abs_y = (double)y / j.value("vh", screen_h) * 65535.0;

            INPUT input = {0};
            input.type = INPUT_MOUSE;
            input.mi.dx = (LONG)abs_x;
            input.mi.dy = (LONG)abs_y;
            input.mi.dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE;
            SendInput(1, &input, sizeof(INPUT));
        } 
        else if (type == "mousedown" || type == "mouseup") {
            int button = j.value("button", 0);
            DWORD flags = 0;
            if (button == 0) flags = (type == "mousedown") ? MOUSEEVENTF_LEFTDOWN : MOUSEEVENTF_LEFTUP;
            else if (button == 2) flags = (type == "mousedown") ? MOUSEEVENTF_RIGHTDOWN : MOUSEEVENTF_RIGHTUP;

            if (flags != 0) {
                INPUT input = {0};
                input.type = INPUT_MOUSE;
                input.mi.dwFlags = flags;
                SendInput(1, &input, sizeof(INPUT));
            }
        }
        else if (type == "keydown" || type == "keyup") {
            WORD vk = j.value("keyCode", 0);
            DWORD flags = (type == "keyup") ? KEYEVENTF_KEYUP : 0;

            INPUT input = {0};
            input.type = INPUT_KEYBOARD;
            input.ki.wVk = vk;
            input.ki.dwFlags = flags;
            SendInput(1, &input, sizeof(INPUT));
        }
    } catch (...) {}
}

int main() {
    rtc::InitLogger(rtc::LogLevel::Info);

    running_capture = true;
    std::thread capture_thread(screen_capture_loop);

    rtc::Configuration config;
    config.iceServers.emplace_back("stun:stun.l.google.com:19302");

    httplib::Server http_svr;
    std::string html_content = R"(<!DOCTYPE html>
<html>
<head>
    <title>C++ Remote Desktop Client</title>
    <style>
        body { font-family: Arial, sans-serif; text-align: center; background: #121212; color: #fff; margin: 0; padding-top: 10px; }
        #status { margin: 5px; font-weight: bold; color: #ff9800; font-size: 14px; }
        #desktopView { background: #000; border: 2px solid #444; border-radius: 4px; cursor: default; max-width: 100%; height: auto; }
    </style>
</head>
<body>
    <h3>C++ WebRTC Remote Desktop Engine</h3>
    <div id="status">Initializing PeerConnection...</div>
    <img id="desktopView" alt="Awaiting screen stream..." />
    <script>
        const statusDiv = document.getElementById('status');
        const imgElement = document.getElementById('desktopView');
        
        let pc = null;
        let screenChannel = null;
        let controlChannel = null;
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
            } catch (e) {}
            setTimeout(pollSignaling, 500);
        }

        async function initWebRTC() {
            statusDiv.innerText = "Setting up PeerConnection...";
            pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });

            screenChannel = pc.createDataChannel('screen-stream', { ordered: false, maxRetransmits: 0 });
            screenChannel.binaryType = 'arraybuffer';

            screenChannel.onmessage = (event) => {
                const blob = new Blob([event.data], { type: 'image/jpeg' });
                const url = URL.createObjectURL(blob);
                imgElement.src = url;
                imgElement.onload = () => URL.revokeObjectURL(url);
            };

            controlChannel = pc.createDataChannel('control-stream', { ordered: true });
            controlChannel.onopen = () => {
                statusDiv.innerText = "Connected! Control interface active.";
                statusDiv.style.color = "#4caf50";
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

            statusDiv.innerText = "Offer sent. Awaiting Host Connection...";
            pollSignaling();
        }

        async function handleSignalingMessage(msg) {
            if (msg.type === 'answer') {
                if (!pc.remoteDescription || pc.remoteDescription.type === "") {
                    await pc.setRemoteDescription(new RTCSessionDescription({ type: msg.data.type, sdp: msg.data.sdp }));
                }
            } else if (msg.type === 'ice') {
                try {
                    await pc.addIceCandidate(new RTCIceCandidate({ candidate: msg.data.candidate, sdpMid: msg.data.sdpMid }));
                } catch (e) {}
            }
        }

        function sendControl(data) {
            if (controlChannel && controlChannel.readyState === 'open') {
                controlChannel.send(JSON.stringify(data));
            }
        }

        imgElement.addEventListener('mousemove', (e) => {
            const rect = imgElement.getBoundingClientRect();
            sendControl({
                type: 'mousemove',
                x: e.clientX - rect.left,
                y: e.clientY - rect.top,
                vw: imgElement.clientWidth,
                vh: imgElement.clientHeight
            });
        });

        imgElement.addEventListener('mousedown', (e) => {
            e.preventDefault();
            sendControl({ type: 'mousedown', button: e.button });
        });

        imgElement.addEventListener('mouseup', (e) => {
            e.preventDefault();
            sendControl({ type: 'mouseup', button: e.button });
        });

        window.addEventListener('keydown', (e) => {
            sendControl({ type: 'keydown', keyCode: e.keyCode });
        });

        window.addEventListener('keyup', (e) => {
            sendControl({ type: 'keyup', keyCode: e.keyCode });
        });

        initWebRTC();
    </script>
</body>
</html>)";

    http_svr.Get("/", [html_content](const httplib::Request&, httplib::Response& res) {
        res.set_content(html_content, "text/html");
    });

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

                    if (pc) old_pc_to_destroy = pc;
                    clear_active_session_pointers();
                    pc = std::make_shared<rtc::PeerConnection>(config);
                }

                if (old_pc_to_destroy) {
                    try { old_pc_to_destroy->close(); } catch(...) {}
                }

                std::shared_ptr<rtc::PeerConnection> local_pc = pc;

                local_pc->onLocalDescription([this_session](rtc::Description description) {
                    {
                        std::lock_guard<std::mutex> lock(connection_mutex);
                        if (this_session != current_session_id) return;
                    }
                    
                    std::string sdp_str = std::string(description);
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
                    json ice = {
                        {"type", "ice"}, {"from", peer_id},
                        {"data", {{"candidate", cand_str}, {"sdpMid", candidate.mid()}, {"sdpMLineIndex", 0}}}
                    };
                    
                    std::lock_guard<std::mutex> lock(signaling_mutex);
                    cpp_to_browser_queue.push(ice);
                });

                local_pc->onDataChannel([local_pc, this_session](std::shared_ptr<rtc::DataChannel> dc) {
                    std::lock_guard<std::mutex> dc_lock(connection_mutex);
                    if (this_session != current_session_id) return;

                    if (dc->label() == "screen-stream") {
                        screen_channel = dc;
                        screen_channel->onOpen([this_session]() {
                            std::lock_guard<std::mutex> stream_lock(connection_mutex);
                            if (this_session != current_session_id) return;
                            streaming_allowed = true;
                        });
                        screen_channel->onClosed([this_session]() { 
                            std::lock_guard<std::mutex> stream_lock(connection_mutex);
                            if (this_session != current_session_id) return;
                            streaming_allowed = false; 
                        });
                    } else if (dc->label() == "control-stream") {
                        control_channel = dc;
                        control_channel->onMessage([](rtc::message_variant message) {
                            if (std::holds_alternative<std::string>(message)) {
                                handle_remote_input(std::get<std::string>(message));
                            }
                        });
                    }
                });

                std::string sdp = payload["data"]["sdp"];
                local_pc->setRemoteDescription(rtc::Description(sdp, "offer"));

            } else if (type == "ice") {
                std::lock_guard<std::mutex> lock(connection_mutex);
                if (!pc) return;
                
                try {
                    std::string candidate_str = payload["data"]["candidate"];
                    std::string mid = payload["data"]["sdpMid"];
                    if (!candidate_str.empty()) {
                        pc->addRemoteCandidate(rtc::Candidate(candidate_str, mid));
                    }
                } catch (...) {}
            }
        } catch (...) {
            res.status = 400;
            return;
        }
        res.set_content("{\"status\":\"ok\"}", "application/json");
    });

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
    std::cout << "💻 C++ REMOTE DESKTOP ENGINE ONLINE" << std::endl;
    std::cout << "LOCAL URL  : http://localhost:" << PORT << "/" << std::endl;
    std::cout << "PORTMAP URL: http://" << PORTMAP_ENDPOINT << "/" << std::endl;
    std::cout << "============================================" << std::endl;

    ScreenFrame current_frame_buffer;
    while (running_capture) {
        {
            std::unique_lock<std::mutex> lock(frame_mutex);
            frame_cv.wait(lock, [] { return frame_ready || !running_capture; });
            if (!running_capture) break;
            current_frame_buffer = std::move(latest_frame);
            frame_ready = false;
        }

        std::lock_guard<std::mutex> lock(connection_mutex);
        if (streaming_allowed && screen_channel && screen_channel->isOpen() && !current_frame_buffer.data.empty()) {
            try {
                if (screen_channel->bufferedAmount() > 2 * 1024 * 1024) continue;
                screen_channel->send(reinterpret_cast<const std::byte*>(current_frame_buffer.data.data()), current_frame_buffer.data.size());
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