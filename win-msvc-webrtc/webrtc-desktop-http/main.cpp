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

#ifdef _WIN32
#include <windows.h>
#include <objidl.h>
#include <gdiplus.h>
#pragma comment (lib, "gdiplus.lib")
#else
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/extensions/XTest.h>
#include <jpeglib.h>
#include <cstring>
#endif

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

std::string last_host_clipboard_text = "";
std::mutex host_clipboard_mutex;

#ifdef _WIN32
std::string get_system_clipboard_text() {
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

void set_system_clipboard_text(const std::string& text) {
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
#else
std::string get_system_clipboard_text() {
    return "";
}

void set_system_clipboard_text(const std::string& text) {
}

bool EncodeImageToJPEG(const uint8_t* rgb_data, int width, int height, int quality, std::vector<uint8_t>& buffer) {
    struct jpeg_compress_struct cinfo;
    struct jpeg_error_mgr jerr;
    cinfo.err = jpeg_std_error(&jerr);
    jpeg_create_compress(&cinfo);

    unsigned char* outbuffer = nullptr;
    unsigned long outsize = 0;
    jpeg_mem_dest(&cinfo, &outbuffer, &outsize);

    cinfo.image_width = width;
    cinfo.image_height = height;
    cinfo.input_components = 3;
    cinfo.in_color_space = JCS_RGB;

    jpeg_set_defaults(&cinfo);
    jpeg_set_quality(&cinfo, quality, TRUE);
    jpeg_start_compress(&cinfo, TRUE);

    std::vector<uint8_t> row(width * 3);
    while (cinfo.next_scanline < cinfo.image_height) {
        memcpy(row.data(), rgb_data + cinfo.next_scanline * width * 3, width * 3);
        JSAMPROW row_pointer = row.data();
        jpeg_write_scanlines(&cinfo, &row_pointer, 1);
    }

    jpeg_finish_compress(&cinfo);
    buffer.assign(outbuffer, outbuffer + outsize);
    jpeg_destroy_compress(&cinfo);
    if (outbuffer) free(outbuffer);
    return true;
}
#endif

void clipboard_monitor_loop() {
    while (running_capture) {
        std::this_thread::sleep_for(std::chrono::milliseconds(800));
        if (!running_capture) break;

        std::string current_text = get_system_clipboard_text();
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

#ifdef _WIN32
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
#endif

void screen_capture_loop() {
#ifdef _WIN32
    Gdiplus::GdiplusStartupInput gdiplusStartupInput;
    ULONG_PTR gdiplusToken = 0;
    if (Gdiplus::GdiplusStartup(&gdiplusToken, &gdiplusStartupInput, NULL) != Gdiplus::Ok) {
        return;
    }
#else
    Display* display = XOpenDisplay(NULL);
    if (!display) return;
    int screen = DefaultScreen(display);
    Window root = RootWindow(display, screen);
#endif

    auto frame_duration = std::chrono::milliseconds(50);

    while (running_capture) {
        auto start_time = std::chrono::steady_clock::now();

#ifdef _WIN32
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
        HBITMAP hBitmap = CreateCompatibleBitmap(hScreenDC, screen_w, screen_h);
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
#else
        XWindowAttributes attr;
        XGetWindowAttributes(display, root, &attr);
        int screen_w = attr.width;
        int screen_h = attr.height;

        XImage* img = XGetImage(display, root, 0, 0, screen_w, screen_h, AllPlanes, ZPixmap);
        if (img) {
            std::vector<uint8_t> rgb(screen_w * screen_h * 3);
            for (int y = 0; y < screen_h; ++y) {
                for (int x = 0; x < screen_w; ++x) {
                    unsigned long pixel = XGetPixel(img, x, y);
                    int idx = (y * screen_w + x) * 3;
                    rgb[idx + 0] = (pixel & img->red_mask) >> 16;
                    rgb[idx + 1] = (pixel & img->green_mask) >> 8;
                    rgb[idx + 2] = (pixel & img->blue_mask);
                }
            }
            XDestroyImage(img);

            std::vector<uint8_t> encoded;
            if (EncodeImageToJPEG(rgb.data(), screen_w, screen_h, 60, encoded)) {
                std::lock_guard<std::mutex> lock(frame_mutex);
                latest_frame = {std::move(encoded), screen_w, screen_h};
                frame_ready = true;
                frame_cv.notify_one();
            }
        }
#endif

        auto processing_time = std::chrono::steady_clock::now() - start_time;
        if (processing_time < frame_duration) {
            std::this_thread::sleep_for(frame_duration - processing_time);
        }
    }

#ifdef _WIN32
    if (gdiplusToken != 0) {
        Gdiplus::GdiplusShutdown(gdiplusToken);
    }
#else
    XCloseDisplay(display);
#endif
}

void handle_remote_input(const std::string& msg_str) {
    try {
        auto j = json::parse(msg_str);
        std::string type = j.value("type", "");

#ifdef _WIN32
        int screen_w = GetSystemMetrics(SM_CXSCREEN);
        int screen_h = GetSystemMetrics(SM_CYSCREEN);
#else
        Display* display = XOpenDisplay(NULL);
        if (!display) return;
        int screen = DefaultScreen(display);
        int screen_w = DisplayWidth(display, screen);
        int screen_h = DisplayHeight(display, screen);
#endif

        if (type == "mousemove") {
            int x = j.value("x", 0);
            int y = j.value("y", 0);
            int vw = j.value("vw", 1);
            int vh = j.value("vh", 1);
            
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

            int target_x = (int)(norm_x * screen_w);
            int target_y = (int)(norm_y * screen_h);

#ifdef _WIN32
            double abs_x = ((double)target_x / screen_w) * 65535.0;
            double abs_y = ((double)target_y / screen_h) * 65535.0;
            INPUT input = {0};
            input.type = INPUT_MOUSE;
            input.mi.dx = (LONG)abs_x;
            input.mi.dy = (LONG)abs_y;
            input.mi.dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE;
            SendInput(1, &input, sizeof(INPUT));
#else
            XTestFakeMotionEvent(display, DefaultScreen(display), target_x, target_y, CurrentTime);
            XFlush(display);
            XCloseDisplay(display);
#endif
        } 
        else if (type == "mousedown" || type == "mouseup") {
            int button = j.value("button", 0);
#ifdef _WIN32
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
#else
            int x11_button = 1;
            if (button == 1) x11_button = 2;
            else if (button == 2) x11_button = 3;

            XTestFakeButtonEvent(display, x11_button, (type == "mousedown"), CurrentTime);
            XFlush(display);
            XCloseDisplay(display);
#endif
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
            margin: 0; padding: 0; width: 100%; height: 100%;
            background: #000; overflow: hidden; display: flex;
            justify-content: center; align-items: center;
        }
        .viewer-wrapper { width: 100%; height: 100%; display: flex; justify-content: center; align-items: center; overflow: hidden; }
        #desktopView { display: block; max-width: 100%; max-height: 100%; width: auto; height: auto; object-fit: contain; cursor: default; user-select: none; }
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
                    const blob = new Blob([event.data], { type: 'image/jpeg' });
                    const url = URL.createObjectURL(blob);
                    imgElement.src = url;
                    imgElement.onload = () => URL.revokeObjectURL(url);
                };

                controlChannel = pc.createDataChannel('control-stream', { ordered: true });
                clipboardChannel = pc.createDataChannel('clipboard-stream', { ordered: true });

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
                await sendSignaling({ type: 'offer', from: peer_id, data: { sdp: offer.sdp, type: offer.type } });
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

        wrapper.addEventListener('mousemove', (e) => {
            const rect = imgElement.getBoundingClientRect();
            sendControl({ type: 'mousemove', x: e.clientX - rect.left, y: e.clientY - rect.top, vw: rect.width, vh: rect.height });
        });
        wrapper.addEventListener('mousedown', (e) => { e.preventDefault(); sendControl({ type: 'mousedown', button: e.button }); });
        wrapper.addEventListener('mouseup', (e) => { e.preventDefault(); sendControl({ type: 'mouseup', button: e.button }); });

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
                std::lock_guard<std::mutex> lock(connection_mutex);
                current_session_id++;
                uint64_t this_session = current_session_id;

                clear_active_session_pointers();
                pc = std::make_shared<rtc::PeerConnection>(config);

                std::shared_ptr<rtc::PeerConnection> local_pc = pc;
                local_pc->onLocalDescription([this_session](rtc::Description description) {
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
                    std::string cand_str = std::string(candidate);
                    json ice = {
                        {"type", "ice"}, {"from", peer_id},
                        {"data", {{"candidate", cand_str}, {"sdpMid", candidate.mid()}, {"sdpMLineIndex", 0}}}
                    };
                    std::lock_guard<std::mutex> lock(signaling_mutex);
                    cpp_to_browser_queue.push(ice);
                });

                local_pc->onDataChannel([this_session](std::shared_ptr<rtc::DataChannel> dc) {
                    std::lock_guard<std::mutex> dc_lock(connection_mutex);
                    if (this_session != current_session_id || !dc) return;

                    if (dc->label() == "screen-stream") {
                        screen_channel = dc;
                        screen_channel->onOpen([this_session]() { streaming_allowed = true; });
                        screen_channel->onClosed([this_session]() { streaming_allowed = false; });
                    } else if (dc->label() == "control-stream") {
                        control_channel = dc;
                        control_channel->onMessage([](rtc::message_variant message) {
                            if (std::holds_alternative<std::string>(message)) {
                                handle_remote_input(std::get<std::string>(message));
                            }
                        });
                    }
                });

                if (payload.contains("data") && payload["data"].contains("sdp")) {
                    std::string sdp = payload["data"]["sdp"];
                    local_pc->setRemoteDescription(rtc::Description(sdp, "offer"));
                }
            } else if (type == "ice") {
                std::lock_guard<std::mutex> lock(connection_mutex);
                if (pc && payload.contains("data")) {
                    std::string candidate_str = payload["data"]["candidate"];
                    std::string mid = payload["data"]["sdpMid"];
                    if (!candidate_str.empty()) {
                        pc->addRemoteCandidate(rtc::Candidate(candidate_str, mid));
                    }
                }
            }
        } catch (...) {
            res.status = 400;
            res.set_content("{\"status\":\"error\"}", "application/json");
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
    std::cout << "💻 C++ REMOTE DESKTOP ENGINE ONLINE (LINUX)" << std::endl;
    std::cout << "LOCAL URL  : http://localhost:" << PORT << "/" << std::endl;
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
            if (screen_channel->bufferedAmount() <= 2 * 1024 * 1024) {
                screen_channel->send(reinterpret_cast<const std::byte*>(current_frame_buffer.data.data()), current_frame_buffer.data.size());
            }
        }
    }

    running_capture = false;
    frame_cv.notify_all();
    http_svr.stop();

    if (capture_thread.joinable()) capture_thread.join();
    if (clipboard_thread.joinable()) clipboard_thread.join();
    if (http_thread.joinable()) http_thread.join();
    return 0;
}
