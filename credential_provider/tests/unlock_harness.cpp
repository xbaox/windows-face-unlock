// Live unlock harness for the Face Unlock Credential Provider (dev / bring-up).
//
// Calls FaceUnlock::RequestUnlock against a RUNNING FaceService over
// \\.\pipe\FaceUnlock -- the same code path the credential provider DLL uses --
// and prints ONLY a safe, password-masked summary:
//   * server-SID identity-check outcome (checked / trusted / server SID string)
//   * ok / username / domain
//   * password LENGTH plus a masked preview (first+last char for len>=4)
//
// The plaintext password is NEVER printed or logged. This is a diagnostic tool;
// it does NOT register anything and does NOT touch the camera itself (the
// service does the capture).
//
// Intended run context (Bao's machine, as the logged-in user / SELF):
//   * FaceService running in the user session
//   * enrollment present, credentials stored (~/.face-unlock)
//   * config pipe_unlock_require_system = FALSE  (so a SELF caller is allowed;
//     with TRUE the service would answer this non-SYSTEM caller "not-authorized")
//
// Build (from a VS2022 x64 dev prompt) via tests/CMakeLists.txt:
//   cmake -B build-tests -S credential_provider/tests -A x64
//   cmake --build build-tests --config Release
//   build-tests\Release\unlock_harness.exe
//
// Exit code: 0 on a successful unlock, 1 otherwise.

#include "../PipeClient.h"

#include <windows.h>
#include <string>
#include <cstdio>

using namespace FaceUnlock;

static std::string ToUtf8(const std::wstring& w) {
    if (w.empty()) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string out(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), out.data(), n, nullptr, nullptr);
    return out;
}

// Password preview that never reveals the secret: always the length; for
// len>=4 also the first and last character (ASCII-printable, else '.'), with
// the middle collapsed to a fixed "***". Short passwords are fully masked.
static std::string MaskPassword(const std::wstring& pw) {
    const size_t n = pw.size();
    if (n == 0) return "len=0 (empty)";
    auto safe = [](wchar_t c) -> char { return (c >= 0x20 && c < 0x7F) ? (char)c : '.'; };
    std::string s = "len=" + std::to_string(n) + " ";
    if (n < 4) {
        s += std::string(n, '*');
    } else {
        s += safe(pw.front());
        s += "***";
        s += safe(pw.back());
    }
    return s;
}

int main() {
    SetConsoleOutputCP(CP_UTF8);

    std::printf("Face Unlock live harness\n");
    std::printf("========================\n");
    std::printf("(sends {\"cmd\":\"unlock\"} to \\\\.\\pipe\\FaceUnlock; masked summary only)\n\n");

    std::wstring user, pass, dom;
    std::string err;
    ServerTrust trust;

    const ULONGLONG t0 = GetTickCount64();
    const bool ok = RequestUnlock(user, pass, dom, err, &trust);
    const ULONGLONG dt = GetTickCount64() - t0;

    std::printf("server-SID check : checked=%s trusted=%s sid=%s\n",
                trust.checked ? "yes" : "no",
                trust.trusted ? "yes" : "no",
                trust.serverSid.empty() ? "(unresolved)" : ToUtf8(trust.serverSid).c_str());

    if (ok) {
        std::printf("unlock result    : ok=true\n");
        std::printf("  username       : %s\n", ToUtf8(user).c_str());
        std::printf("  domain         : %s\n", ToUtf8(dom).c_str());
        std::printf("  password       : %s   (masked; plaintext never printed)\n",
                    MaskPassword(pass).c_str());
    } else {
        std::printf("unlock result    : ok=false  reason=%s\n", err.c_str());
    }
    std::printf("round-trip       : %llu ms\n", (unsigned long long)dt);

    return ok ? 0 : 1;
}
