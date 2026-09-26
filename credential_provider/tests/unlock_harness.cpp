// Live harness for the Face Unlock Credential Provider's pipe client (dev / bring-up).
//
// Sends a protocol-v2 {"cmd":"unlock"} through FaceUnlock::RequestUnlock -- the client the
// credential provider DLL uses -- to a RUNNING FaceService on \\.\pipe\FaceUnlock, and prints:
//   * the server identity check (checked / trusted / server SID) against the recorded owner
//   * the outcome and the service's reason
//
// Since Stage 9 no configuration lets a non-SYSTEM caller unlock: run as the logged-in user this
// harness is ANSWERED "not-authorized", which is the expected result -- it proves the pipe, the
// owner-pinned server check and the SYSTEM gate. Run as SYSTEM (psexec -s) it can also receive a
// grant; then it prints the password LENGTH only (Stage 9, F-95: the first and last characters
// used to be printed), wipes the copy and does NOT send report_result, so the grant is abandoned
// and nothing is committed.
//
// Build via tests/CMakeLists.txt:
//   cmake -B build-tests -S credential_provider/tests -A x64
//   cmake --build build-tests --config Release
//   build-tests\Release\unlock_harness.exe
//
// Exit code: 0 when the service answered at all (any reason), 1 on a transport / trust failure.

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

int main() {
    SetConsoleOutputCP(CP_UTF8);

    std::printf("Face Unlock live harness (protocol v%d)\n", kProtocolVersion);
    std::printf("======================================\n\n");

    std::wstring owner;
    std::printf("recorded owner   : %s\n",
                ReadOwnerSid(owner) ? ToUtf8(owner).c_str() : "(none -- HKLM value missing)");

    UnlockReply r;
    ServerTrust trust;
    const ULONGLONG t0 = GetTickCount64();
    const bool ok = RequestUnlock(r, nullptr, &trust);
    const ULONGLONG dt = GetTickCount64() - t0;

    std::printf("server check     : checked=%s trusted=%s sid=%s\n",
                trust.checked ? "yes" : "no",
                trust.trusted ? "yes" : "no",
                trust.serverSid.empty() ? "(unresolved)" : ToUtf8(trust.serverSid).c_str());
    if (ok) {
        std::printf("unlock result    : ok=true user=%s domain=%s password_len=%u grant=%s\n",
                    ToUtf8(r.username).c_str(), ToUtf8(r.domain).c_str(),
                    (unsigned)r.password.size(), r.grantId.c_str());
        std::printf("                   (not reported: the service abandons the grant in 30 s)\n");
    } else {
        std::printf("unlock result    : ok=false reason=%s lang=%s v=%d\n",
                    r.reason.c_str(), r.lang.c_str(), r.version);
    }
    std::printf("round-trip       : %llu ms\n", (unsigned long long)dt);

    const bool answered = ok || (r.reason != "pipe-unavailable" && r.reason != "server-untrusted" &&
                                 r.reason != "no-owner" && r.reason != "cancelled");
    return answered ? 0 : 1;   // UnlockReply's destructor wipes the password copy
}
