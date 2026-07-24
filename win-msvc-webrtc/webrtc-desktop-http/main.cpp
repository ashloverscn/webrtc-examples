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
#include <exception>

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
std::shared_ptr<rtc::DataChannel> clipboard_channel;
uint64_t current_session_id = 0;

std::mutex signaling_mutex;
std::queue<json> cpp_to_browser_queue;

std::string generate_peer_id() {
    try {
        std::random_device rd;
        std::mt19937 gen(rd());
        std::uniform_int_distribution<> distr(1000, 9999);
        return "desktop_cpp_" + std::to_string(distr(gen));
    } catch (...) {
        return "desktop_cpp_fallback";
    }
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
    if (clipboard_channel) {
        try { clipboard_channel->close(); } catch (...) {}
        clipboard_channel.reset();
    }
    if (pc) {
        try { pc->close(); } catch (...) {}
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
std::atomic<bool> running_capture(false);

// Host clipboard state tracking to prevent feedback loops
std::string last_host_clipboard_text = "";
std::mutex host_clipboard_mutex;

std::string get_windows_clipboard_text() {
    std::string text = "";
    if (!OpenClipboard(NULL)) return text;
    HANDLE hData = GetClipboardData(CF_TEXT);
    if (hData) {
        char* pszText = static_cast<char*>(GlobalLock(hData));
        if (pszText) {
            text = std::string(pszText);
            GlobalUnlock(hData);
        }
    }
    CloseClipboard();
    return text;
}

void set_windows_clipboard_text(const std::string& text) {
    if (!OpenClipboard(NULL)) return;
    EmptyClipboard();
    HGLOBAL hMem = GlobalAlloc(GMEM_MOVEABLE, text.size() + 1);
    if (hMem) {
        memcpy(GlobalLock(hMem), text.c_str(), text.size() + 1);
        GlobalUnlock(hMem);
        SetClipboardData(CF_TEXT, hMem);
    }
    CloseClipboard();
}

void clipboard_monitor_loop() {
    while (running_capture) {
        std::this_thread::sleep_for(std::chrono::milliseconds(800));
        if (!running_capture) break;

        std::string current_text = get_windows_clipboard_text();
        if (!current_text.empty()) {
            std::lock_guard<std::mutex> lock(host_clipboard_mutex);
            if (current_text != last_host_clipboard_text) {
                last_host_clipboard_text = current_text;
                
                std::lock_guard<std::mutex> conn_lock(connection_mutex);
                if (clipboard_channel && clipboard_channel->isOpen()) {
                    try {
                        json msg = {{"type", "clipboard"}, {"text", current_text}};
                        clipboard_channel->send(msg.dump());
                    } catch (...) {}
                }
            }
        }
    }
}

int GetEncoderClsid(const WCHAR* format, CLSID* pClsid) {
    if (!format || !pClsid) return -1;
    try {
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
    } catch (...) {}
    return -1;
}

bool EncodeBitmapToJPEG(HBITMAP hBitmap, int quality, std::vector<uint8_t>& buffer) {
    if (!hBitmap) return false;
    try {
        Gdiplus::Bitmap bitmap(hBitmap, NULL);
        if (bitmap.GetLastStatus() != Gdiplus::Ok) return false;

        CLSID jpegClsid;
        if (GetEncoderClsid(L"image/jpeg", &jpegClsid) == -1) return false;

        Gdiplus::EncoderParameters encoderParams;
        encoderParams.Count = 1;
        encoderParams.Parameter[0].Guid = Gdiplus::EncoderQuality;
        encoderParams.Parameter[0].Type = Gdiplus::EncoderParameterValueTypeLong;
        encoderParams.Parameter[0].NumberOfValues = 1;
        encoderParams.Parameter[0].Value = &quality;

        IStream* pStream = NULL;
        if (CreateStreamOnHGlobal(NULL, TRUE, &pStream) != S_OK || !pStream) return false;

        if (bitmap.Save(pStream, &jpegClsid, &encoderParams) != Gdiplus::Ok) {
            pStream->Release();
            return false;
        }

        ULARGE_INTEGER liSize = {0};
        STATSTG stats;
        if (pStream->Stat(&stats, STATFLAG_NONAME) == S_OK) {
            liSize = stats.cbSize;
        }
        DWORD len = (DWORD)liSize.QuadPart;
        if (len == 0) {
            pStream->Release();
            return false;
        }

        buffer.resize(len);
        HGLOBAL hGlobal = NULL;
        if (GetHGlobalFromStream(pStream, &hGlobal) == S_OK && hGlobal) {
            void* pBuffer = GlobalLock(hGlobal);
            if (pBuffer) {
                memcpy(buffer.data(), pBuffer, len);
                GlobalUnlock(hGlobal);
            }
        }
        pStream->Release();
        return true;
    } catch (...) {
        return false;
    }
}

void screen_capture_loop() {
    Gdiplus::GdiplusStartupInput gdiplusStartupInput;
    ULONG_PTR gdiplusToken = 0;
    if (Gdiplus::GdiplusStartup(&gdiplusToken, &gdiplusStartupInput, NULL) != Gdiplus::Ok) {
        return;
    }

    auto frame_duration = std::chrono::milliseconds(50);

    while (running_capture) {
        auto start_time = std::chrono::steady_clock::now();

        int screen_w = GetSystemMetrics(SM_CXSCREEN);
        int screen_h = GetSystemMetrics(SM_CYSCREEN);
        if (screen_w <= 0 || screen_h <= 0) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            continue;
        }

        HDC hScreenDC = GetDC(NULL);
        if (!hScreenDC) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            continue;
        }
        HDC hMemoryDC = CreateCompatibleDC(hScreenDC);
        if (!hMemoryDC) {
            ReleaseDC(NULL, hScreenDC);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            continue;
        }

        HBITMAP hBitmap = CreateCompatibleBitmap(hScreenDC, screen_w, screen_h);
        if (!hBitmap) {
            DeleteDC(hMemoryDC);
            ReleaseDC(NULL, hScreenDC);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            continue;
        }

        HBITMAP hOldBitmap = (HBITMAP)SelectObject(hMemoryDC, hBitmap);
        BOOL blt_ok = BitBlt(hMemoryDC, 0, 0, screen_w, screen_h, hScreenDC, 0, 0, SRCCOPY | CAPTUREBLT);
        SelectObject(hMemoryDC, hOldBitmap);

        if (blt_ok) {
            std::vector<uint8_t> encoded;
            if (EncodeBitmapToJPEG(hBitmap, 60, encoded)) {
                std::lock_guard<std::mutex> lock(frame_mutex);
                latest_frame = {std::move(encoded), screen_w, screen_h};
                frame_ready = true;
                frame_cv.notify_one();
            }
        }

        DeleteObject(hBitmap);
        DeleteDC(hMemoryDC);
        ReleaseDC(NULL, hScreenDC);

        auto processing_time = std::chrono::steady_clock::now() - start_time;
        if (processing_time < frame_duration) {
            std::this_thread::sleep_for(frame_duration - processing_time);
        }
    }

    if (gdiplusToken != 0) {
        Gdiplus::GdiplusShutdown(gdiplusToken);
    }
}

void handle_remote_input(const std::string& msg_str) {
    try {
        auto j = json::parse(msg_str);
        std::string type = j.value("type", "");

        if (type == "mousemove") {
            int x = j.value("x", 0);
            int y = j.value("y", 0);
            int vw = j.value("vw", 1);
            int vh = j.value("vh", 1);
            
            int screen_w = GetSystemMetrics(SM_CXSCREEN);
            int screen_h = GetSystemMetrics(SM_CYSCREEN);
            if (screen_w <= 0 || screen_h <= 0) return;

            double img_aspect = (double)screen_w / screen_h;
            double container_aspect = (double)vw / vh;

            double render_w, render_h, offset_x = 0, offset_y = 0;
            if (container_aspect > img_aspect) {
                render_h = vh;
                render_w = vh * img_aspect;
                offset_x = (vw - render_w) / 2.0;
            } else {
                render_w = vw;
                render_h = vw / img_aspect;
                offset_y = (vh - render_h) / 2.0;
            }

            double local_x = x - offset_x;
            double local_y = y - offset_y;

            if (local_x < 0) local_x = 0;
            if (local_x > render_w) local_x = render_w;
            if (local_y < 0) local_y = 0;
            if (local_y > render_h) local_y = render_h;

            double norm_x = (render_w > 0) ? (local_x / render_w) : 0;
            double norm_y = (render_h > 0) ? (local_y / render_h) : 0;

            double target_x = norm_x * screen_w;
            double target_y = norm_y * screen_h;

            double abs_x = (target_x / screen_w) * 65535.0;
            double abs_y = (target_y / screen_h) * 65535.0;

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
            else if (button == 1) flags = (type == "mousedown") ? MOUSEEVENTF_MIDDLEDOWN : MOUSEEVENTF_MIDDLEUP;

            if (flags != 0) {
                INPUT input = {0};
                input.type = INPUT_MOUSE;
                input.mi.dwFlags = flags;
                SendInput(1, &input, sizeof(INPUT));
            }
        }
        else if (type == "wheel") {
            int delta = j.value("delta", 0);
            if (delta != 0) {
                INPUT input = {0};
                input.type = INPUT_MOUSE;
                input.mi.dwFlags = INPUT_MOUSE; // fixed flag setting below
                input.mi.dwFlags = MOUSEEVENTF_WHEEL;
                input.mi.mouseData = (DWORD)(-delta);
                SendInput(1, &input, sizeof(INPUT));
            }
        }
        else if (type == "keydown" || type == "keyup") {
            WORD vk = j.value("keyCode", 0);
            if (vk == 122) return; // Block F11

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
    try {
        rtc::InitLogger(rtc::LogLevel::Info);
    } catch (...) {}

    running_capture = true;
    std::thread capture_thread;
    std::thread clipboard_thread;
    try {
        capture_thread = std::thread(screen_capture_loop);
        clipboard_thread = std::thread(clipboard_monitor_loop);
    } catch (...) {
        running_capture = false;
    }

    rtc::Configuration config;
    try {
        config.iceServers.emplace_back("stun:stun.l.google.com:19302");
    } catch (...) {}

    httplib::Server http_svr;
    std::string html_content = R"(<!DOCTYPE html>
<html>
<head>
    <title>Remote Desktop Viewer</title>
    <style>
        html, body {
            margin: 0;
            padding: 0;
            width: 100%;
            height: 100%;
            background: #000;
            overflow: hidden;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .viewer-wrapper {
            width: 100%;
            height: 100%;
            display: flex;
            justify-content: center;
            align-items: center;
            overflow: hidden;
        }
        #desktopView {
            display: block;
            max-width: 100%;
            max-height: 100%;
            width: auto;
            height: auto;
            object-fit: contain;
            cursor: default;
            user-select: none;
            -webkit-user-select: none;
        }
    </style>
</head>
<body>
    <div class="viewer-wrapper" id="viewerWrapper">
        <img id="desktopView" alt="" />
    </div>
    <script>
        const wrapper = document.getElementById('viewerWrapper');
        const imgElement = document.getElementById('desktopView');
        let pc = null;
        let screenChannel = null;
        let controlChannel = null;
        let clipboardChannel = null;
        let lastViewerClipboard = "";
        const peer_id = "viewer_" + Math.floor(Math.random() * 9000 + 1000);

        async function sendSignaling(msg) {
            try {
                await fetch('/signal', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(msg)
                });
            } catch (e) {}
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
            try {
                pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });

                screenChannel = pc.createDataChannel('screen-stream', { ordered: false, maxRetransmits: 0 });
                screenChannel.binaryType = 'arraybuffer';

                screenChannel.onmessage = (event) => {
                    try {
                        const blob = new Blob([event.data], { type: 'image/jpeg' });
                        const url = URL.createObjectURL(blob);
                        imgElement.src = url;
                        imgElement.onload = () => URL.revokeObjectURL(url);
                    } catch (err) {}
                };

                controlChannel = pc.createDataChannel('control-stream', { ordered: true });
                
                clipboardChannel = pc.createDataChannel('clipboard-stream', { ordered: true });
                clipboardChannel.onmessage = async (event) => {
                    try {
                        const data = JSON.parse(event.data);
                        if (data.type === 'clipboard' && data.text) {
                            lastViewerClipboard = data.text;
                            await navigator.clipboard.writeText(data.text);
                        }
                    } catch (e) {}
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

                pollSignaling();
            } catch (e) {
                setTimeout(initWebRTC, 2000);
            }
        }

        async function handleSignalingMessage(msg) {
            try {
                if (msg.type === 'answer') {
                    if (pc && (!pc.remoteDescription || pc.remoteDescription.type === "")) {
                        await pc.setRemoteDescription(new RTCSessionDescription({ type: msg.data.type, sdp: msg.data.sdp }));
                    }
                } else if (msg.type === 'ice') {
                    if (pc) {
                        await pc.addIceCandidate(new RTCIceCandidate({ candidate: msg.data.candidate, sdpMid: msg.data.sdpMid }));
                    }
                }
            } catch (e) {}
        }

        function sendControl(data) {
            try {
                if (controlChannel && controlChannel.readyState === 'open') {
                    controlChannel.send(JSON.stringify(data));
                }
            } catch (e) {}
        }

        function sendClipboard(text) {
            try {
                if (clipboardChannel && clipboardChannel.readyState === 'open') {
                    clipboardChannel.send(JSON.stringify({ type: 'clipboard', text: text }));
                }
            } catch (e) {}
        }

        // Periodic check for browser-side clipboard updates to send to host
        setInterval(async () => {
            try {
                if (document.hasFocus() && navigator.clipboard && navigator.clipboard.readText) {
                    const text = await navigator.clipboard.readText();
                    if (text && text !== lastViewerClipboard) {
                        lastViewerClipboard = text;
                        sendClipboard(text);
                    }
                }
            } catch (e) {}
        }, 1000);

        wrapper.addEventListener('mousemove', (e) => {
            const rect = imgElement.getBoundingClientRect();
            sendControl({
                type: 'mousemove',
                x: e.clientX - rect.left,
                y: e.clientY - rect.top,
                vw: rect.width,
                vh: rect.height
            });
        });

        wrapper.addEventListener('mousedown', (e) => {
            e.preventDefault();
            sendControl({ type: 'mousedown', button: e.button });
        });

        wrapper.addEventListener('mouseup', (e) => {
            e.preventDefault();
            sendControl({ type: 'mouseup', button: e.button });
        });

        wrapper.addEventListener('contextmenu', (e) => {
            e.preventDefault();
        });

        wrapper.addEventListener('wheel', (e) => {
            e.preventDefault();
            sendControl({ type: 'wheel', delta: e.deltaY });
        }, { passive: false });

        window.addEventListener('keydown', (e) => {
            if (e.keyCode === 122) return; // F11 toggle fullscreen
            sendControl({ type: 'keydown', keyCode: e.keyCode });
        });

        window.addEventListener('keyup', (e) => {
            if (e.keyCode === 122) return;
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
                if (!local_pc) {
                    res.status = 500;
                    res.set_content("{\"status\":\"error\"}", "application/json");
                    return;
                }

                local_pc->onLocalDescription([this_session](rtc::Description description) {
                    try {
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
                    } catch (...) {}
                });

                local_pc->onLocalCandidate([this_session](rtc::Candidate candidate) {
                    try {
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
                    } catch (...) {}
                });

                local_pc->onDataChannel([this_session](std::shared_ptr<rtc::DataChannel> dc) {
                    try {
                        std::lock_guard<std::mutex> dc_lock(connection_mutex);
                        if (this_session != current_session_id || !dc) return;

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
                                try {
                                    if (std::holds_alternative<std::string>(message)) {
                                        handle_remote_input(std::get<std::string>(message));
                                    }
                                } catch (...) {}
                            });
                        } else if (dc->label() == "clipboard-stream") {
                            clipboard_channel = dc;
                            clipboard_channel->onMessage([](rtc::message_variant message) {
                                try {
                                    if (std::holds_alternative<std::string>(message)) {
                                        auto j = json::parse(std::get<std::string>(message));
                                        if (j.value("type", "") == "clipboard") {
                                            std::string text = j.value("text", "");
                                            if (!text.empty()) {
                                                {
                                                    std::lock_guard<std::mutex> lock(host_clipboard_mutex);
                                                    last_host_clipboard_text = text;
                                                }
                                                set_windows_clipboard_text(text);
                                            }
                                        }
                                    }
                                } catch (...) {}
                            });
                        }
                    } catch (...) {}
                });

                if (payload.contains("data") && payload["data"].contains("sdp")) {
                    std::string sdp = payload["data"]["sdp"];
                    local_pc->setRemoteDescription(rtc::Description(sdp, "offer"));
                }

            } else if (type == "ice") {
                std::lock_guard<std::mutex> lock(connection_mutex);
                if (!pc) return;
                
                try {
                    if (payload.contains("data") && payload["data"].contains("candidate") && payload["data"].contains("sdpMid")) {
                        std::string candidate_str = payload["data"]["candidate"];
                        std::string mid = payload["data"]["sdpMid"];
                        if (!candidate_str.empty()) {
                            pc->addRemoteCandidate(rtc::Candidate(candidate_str, mid));
                        }
                    }
                } catch (...) {}
            }
        } catch (...) {
            res.status = 400;
            res.set_content("{\"status\":\"error\"}", "application/json");
            return;
        }
        res.set_content("{\"status\":\"ok\"}", "application/json");
    });

    http_svr.Get("/poll", [](const httplib::Request&, httplib::Response& res) {
        try {
            json batch = json::array();
            {
                std::lock_guard<std::mutex> lock(signaling_mutex);
                while (!cpp_to_browser_queue.empty()) {
                    batch.push_back(cpp_to_browser_queue.front());
                    cpp_to_browser_queue.pop();
                }
            }
            res.set_content(batch.dump(), "application/json");
        } catch (...) {
            res.status = 500;
            res.set_content("[]", "application/json");
        }
    });

    std::thread http_thread;
    try {
        http_thread = std::thread([&http_svr]() {
            http_svr.listen("0.0.0.0", PORT);
        });
    } catch (...) {
        running_capture = false;
    }

    std::cout << "============================================" << std::endl;
    std::cout << "💻 C++ REMOTE DESKTOP ENGINE ONLINE" << std::endl;
    std::cout << "LOCAL URL  : http://localhost:" << PORT << "/" << std::endl;
    std::cout << "PORTMAP URL: http://" << PORTMAP_ENDPOINT << "/" << std::endl;
    std::cout << "============================================" << std::endl;

    ScreenFrame current_frame_buffer;
    while (running_capture) {
        try {
            {
                std::unique_lock<std::mutex> lock(frame_mutex);
                frame_cv.wait(lock, [] { return frame_ready || !running_capture; });
                if (!running_capture) break;
                current_frame_buffer = std::move(latest_frame);
                frame_ready = false;
            }

            std::lock_guard<std::mutex> lock(connection_mutex);
            if (streaming_allowed && screen_channel && screen_channel->isOpen() && !current_frame_buffer.data.empty()) {
                if (screen_channel->bufferedAmount() <= 2 * 1024 * 1024) {
                    screen_channel->send(reinterpret_cast<const std::byte*>(current_frame_buffer.data.data()), current_frame_buffer.data.size());
                }
            }
        } catch (...) {}
    }

    running_capture = false;
    frame_cv.notify_all();
    
    {
        std::lock_guard<std::mutex> lock(connection_mutex);
        if (pc) { try { pc->close(); } catch(...) {} }
        clear_active_session_pointers();
    }
    
    try {
        http_svr.stop();
    } catch (...) {}

    if (capture_thread.joinable()) {
        try { capture_thread.join(); } catch (...) {}
    }
    if (clipboard_thread.joinable()) {
        try { clipboard_thread.join(); } catch (...) {}
    }
    if (http_thread.joinable()) {
        try { http_thread.join(); } catch (...) {}
    }
    return 0;
}
