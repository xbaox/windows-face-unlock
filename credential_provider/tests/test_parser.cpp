// Offline unit test for the Credential Provider's pipe client (credential_provider/PipeClient).
//
// Camera-free and service-free. Most cases feed hand-crafted JSON replies straight into the
// parser; the Stage 9 transport cases run a private pipe server inside this process (test build
// only: FACEUNLOCK_TESTING swaps the owner and the pipe name).
//   baseline      the Stage-5 parser fixes (escapes, empty credentials, whitespace, extra fields)
//   Stage 7-i     the phase-1 gesture section
//   8b            hostile vectors, length caps, IsHexToken, sanitizer
//   Stage 9       protocol v2 (§2.1): version, grant_id, retry_after_s, lang, report_result,
//                 strict JSON (D-58), strict UTF-8 (F-73), R2 sanitizer (F-74), honest texts EN/RU
//                 (R3), owner-pinned server trust (R1), cancellable transport (R3), oversize (D-54)
//
// Every reply the service sends carries "v":2 since Stage 9, and every grant a "grant_id". The
// older suites keep their vectors: Reply() inserts those two fields in front of each object, so
// what each case proves is unchanged; the cases that test the version itself build raw replies.
//
// Test vectors are SYNTHETIC -- not real credentials -- so decoded values are printed on failure.
//
// Build via tests/CMakeLists.txt (it passes /utf-8, which the UTF-8 literals below need; D-61):
//   cmake -B build-tests -S credential_provider/tests -A x64
//   cmake --build build-tests --config Release
// or by hand from a VS2022 x64 prompt:
//   cl /EHsc /std:c++17 /utf-8 /DFACEUNLOCK_TESTING test_parser.cpp ..\PipeClient.cpp advapi32.lib user32.lib
// Exit code 0 = all pass, 1 = at least one failure.

#include "../PipeClient.h"
#include "../helpers.h"

#include <windows.h>
#include <sddl.h>
#include <cstdio>
#include <functional>
#include <string>
#include <thread>

using FaceUnlock::ParseUnlockReply;
using FaceUnlock::UnlockReply;
using namespace std::string_literals;   // "..."s keeps embedded NUL bytes (8b hostile vectors)

static int g_pass = 0;
static int g_fail = 0;

// D-56: the pre-7-i five-argument wrapper used to ship inside the LogonUI DLL for this test and
// the harness only; it lives here now.
static bool ParseUnlockResponse(const std::string& response, std::wstring& username,
                                std::wstring& password, std::wstring& domain, std::string& errorOut) {
    UnlockReply r;
    const bool ok = ParseUnlockReply(response, r);
    username = r.username;
    password = r.password;
    domain   = r.domain;
    errorOut = r.reason;
    return ok;
}

// Insert "v":2 and a grant id in front of the first member of an object reply.
static std::string Reply(const std::string& json) {
    size_t i = 0;
    while (i < json.size() && (json[i] == ' ' || json[i] == '\t' || json[i] == '\r' || json[i] == '\n')) ++i;
    if (i >= json.size() || json[i] != '{') return json;
    return json.substr(0, i + 1) + "\"v\":2,\"grant_id\":\"a1b2c3\"," + json.substr(i + 1);
}

static std::string ToUtf8(const std::wstring& w) {
    if (w.empty()) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string out(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), out.data(), n, nullptr, nullptr);
    return out;
}

// Expect a successful parse with the given username/password/domain.
static void CaseOk(const char* label, const std::string& resp,
                   const std::wstring& eu, const std::wstring& ep, const std::wstring& ed) {
    std::wstring u, p, d;
    std::string err;
    bool ok = ParseUnlockResponse(Reply(resp), u, p, d, err);
    bool good = ok && u == eu && p == ep && d == ed;
    if (good) {
        ++g_pass;
        std::printf("  PASS  %s\n", label);
    } else {
        ++g_fail;
        std::printf("  FAIL  %s\n", label);
        std::printf("        ok=%d err=%s\n", (int)ok, err.c_str());
        std::printf("        user  exp=[%s] got=[%s]\n", ToUtf8(eu).c_str(), ToUtf8(u).c_str());
        std::printf("        pass  exp=[%s](%d wch) got=[%s](%d wch)\n",
                    ToUtf8(ep).c_str(), (int)ep.size(), ToUtf8(p).c_str(), (int)p.size());
        std::printf("        dom   exp=[%s] got=[%s]\n", ToUtf8(ed).c_str(), ToUtf8(d).c_str());
    }
}

// Expect the parse to be rejected. If expectErr is non-null, the reason must match exactly.
// `raw` skips Reply() (the version cases).
static void CaseReject(const char* label, const std::string& resp, const char* expectErr,
                       bool raw = false) {
    std::wstring u, p, d;
    std::string err;
    bool ok = ParseUnlockResponse(raw ? resp : Reply(resp), u, p, d, err);
    bool good = !ok && (expectErr == nullptr || err == expectErr) && u.empty() && p.empty();
    if (good) {
        ++g_pass;
        std::printf("  PASS  %s  (rejected: %s)\n", label, err.c_str());
    } else {
        ++g_fail;
        std::printf("  FAIL  %s\n", label);
        std::printf("        expected reject%s%s, got ok=%d err=%s pass_len=%d\n",
                    expectErr ? " err=" : "", expectErr ? expectErr : "",
                    (int)ok, err.c_str(), (int)p.size());
    }
}

// Expect a REJECTED parse (no credentials) whose UnlockReply carries the given phase-1 section.
// nullptr = the field must come back EMPTY -- the parser must never invent a value.
static void CaseGesture(const char* label, const std::string& resp, const char* expectReason,
                        const char* expectGesture, const wchar_t* expectPrompt,
                        const char* expectToken) {
    const std::string wantG = expectGesture ? expectGesture : "";
    const std::wstring wantP = expectPrompt ? expectPrompt : L"";
    const std::string wantT = expectToken ? expectToken : "";
    UnlockReply r;
    bool ok = ParseUnlockReply(Reply(resp), r);
    bool good = !ok && r.reason == expectReason && r.gesture == wantG
                && r.prompt == wantP && r.token == wantT
                && r.username.empty() && r.password.empty();
    if (good) {
        ++g_pass;
        std::printf("  PASS  %s\n", label);
    } else {
        ++g_fail;
        std::printf("  FAIL  %s\n", label);
        std::printf("        ok=%d reason exp=[%s] got=[%s]\n",
                    (int)ok, expectReason, r.reason.c_str());
        std::printf("        gesture exp=[%s] got=[%s]\n", wantG.c_str(), r.gesture.c_str());
        std::printf("        prompt  exp=[%s] got=[%s]\n",
                    ToUtf8(wantP).c_str(), ToUtf8(r.prompt).c_str());
        std::printf("        token   exp=[%s] got=[%s]\n", wantT.c_str(), r.token.c_str());
    }
}

static void Check(const char* label, bool cond) {
    if (cond) {
        ++g_pass;
        std::printf("  PASS  %s\n", label);
    } else {
        ++g_fail;
        std::printf("  FAIL  %s\n", label);
    }
}

// 8b: build {"ok":true,"username":U,"password":P[,"domain":D]} from raw JSON string bodies.
static std::string Grant(const std::string& u, const std::string& p, const char* d = nullptr) {
    std::string s = "{\"ok\":true,\"username\":\"" + u + "\",\"password\":\"" + p + "\"";
    if (d) s += ",\"domain\":\"" + std::string(d) + "\"";
    return s + "}";
}

static std::string Repeat(const std::string& unit, size_t n) {
    std::string s;
    for (size_t i = 0; i < n; ++i) s += unit;
    return s;
}

static std::wstring RepeatW(const std::wstring& unit, size_t n) {
    std::wstring s;
    for (size_t i = 0; i < n; ++i) s += unit;
    return s;
}

// Parse a needs-gesture reply carrying `promptJson` (a raw JSON string body) and compare the
// decoded + sanitized prompt.
static void CasePrompt(const char* label, const std::string& promptJson, const std::wstring& expect) {
    UnlockReply r;
    const std::string resp = "{\"ok\":false,\"reason\":\"needs-gesture\",\"gesture\":\"nod\","
                             "\"token\":\"ab\",\"prompt\":\"" + promptJson + "\"}";
    const bool ok = ParseUnlockReply(Reply(resp), r);
    const bool good = !ok && r.reason == "needs-gesture" && r.prompt == expect;
    if (good) {
        ++g_pass;
        std::printf("  PASS  %s\n", label);
    } else {
        ++g_fail;
        std::printf("  FAIL  %s\n", label);
        std::printf("        ok=%d reason=[%s] prompt exp=[%s](%d) got=[%s](%d)\n", (int)ok,
                    r.reason.c_str(), ToUtf8(expect).c_str(), (int)expect.size(),
                    ToUtf8(r.prompt).c_str(), (int)r.prompt.size());
    }
}

// ---------------------------------------------------------------------------------------------
// Stage 9 transport cases: a private pipe server in this process stands in for the service.
// ---------------------------------------------------------------------------------------------
struct FakeServer {
    std::wstring name;
    std::string reply;
    bool silent = false;       // read the request, then never answer
    bool oversize = false;     // answer with a 70000-byte message
    bool gotRequest = false;
    std::string received;
    HANDLE ready = nullptr;
};

static void ServeOnce(FakeServer* s) {
    HANDLE h = CreateNamedPipeW(s->name.c_str(), PIPE_ACCESS_DUPLEX,
                                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT, 1,
                                131072, 131072, 0, nullptr);
    SetEvent(s->ready);
    if (h == INVALID_HANDLE_VALUE) return;
    if (ConnectNamedPipe(h, nullptr) || GetLastError() == ERROR_PIPE_CONNECTED) {
        std::string buf(65536, '\0');
        DWORD n = 0;
        if (ReadFile(h, &buf[0], (DWORD)buf.size(), &n, nullptr) && n) {
            s->gotRequest = true;
            s->received.assign(buf.data(), n);
            if (s->silent) {
                Sleep(2500);
            } else {
                const std::string out = s->oversize ? std::string(70000, ' ') : s->reply;
                DWORD w = 0;
                WriteFile(h, out.data(), (DWORD)out.size(), &w, nullptr);
                // wait (bounded) for the client to read and close, like the service's drain
                char one;
                DWORD r1 = 0;
                ReadFile(h, &one, 1, &r1, nullptr);
            }
        }
    }
    DisconnectNamedPipe(h);
    CloseHandle(h);
}

static std::wstring SelfSid() {
    HANDLE tok = nullptr;
    OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &tok);
    DWORD len = 0;
    GetTokenInformation(tok, TokenUser, nullptr, 0, &len);
    std::string buf(len, '\0');
    GetTokenInformation(tok, TokenUser, &buf[0], len, &len);
    CloseHandle(tok);
    LPWSTR s = nullptr;
    ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER*>(&buf[0])->User.Sid, &s);
    std::wstring out = s ? s : L"";
    LocalFree(s);
    return out;
}

static void RunWithServer(FakeServer& srv, const std::function<void()>& client) {
    srv.ready = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    std::thread t(ServeOnce, &srv);
    WaitForSingleObject(srv.ready, 5000);
    client();
    // Unblock a server still waiting in ConnectNamedPipe (a client that never connected).
    HANDLE poke = CreateFileW(srv.name.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                              OPEN_EXISTING, 0, nullptr);
    if (poke != INVALID_HANDLE_VALUE) CloseHandle(poke);
    t.join();
    CloseHandle(srv.ready);
}

static void TransportTests() {
    using namespace FaceUnlock;
    const std::wstring self = SelfSid();
    const std::wstring pipe = L"\\\\.\\pipe\\FaceUnlockCpTest-" + std::to_wstring(GetCurrentProcessId());
    TestSetPipeName(pipe);
    TestSetOwnerOverride(self);

    {   // A: a v2 grant through the real transport; the request is v2 and carries its budget
        FakeServer srv;
        srv.name = pipe;
        srv.reply = R"({"v":2,"lang":"en","ok":true,"username":"u","password":"p","grant_id":"abcd"})";
        UnlockReply r;
        ServerTrust tr;
        bool ok = false;
        RunWithServer(srv, [&] { ok = RequestUnlock(r, nullptr, &tr); });
        const size_t b = srv.received.find("\"budget_ms\":");
        const long budget = (b == std::string::npos) ? -1 : std::stol(srv.received.substr(b + 12));
        Check("transport: owner-run server -> grant, trusted", ok && tr.trusted && r.grantId == "abcd");
        Check("transport: request is {cmd:unlock, v:2, budget_ms}",
              srv.received.rfind(R"({"cmd":"unlock","v":2,"budget_ms":)", 0) == 0);
        Check("transport: budget_ms is what is left of 12 s after the connect",
              budget > 10000 && budget <= (long)kUnlockTimeoutMs);
    }
    {   // B: R1 -- a server that is not the owner never receives the request
        TestSetOwnerOverride(L"S-1-5-21-1111111111-2222222222-3333333333-1001");
        FakeServer srv;
        srv.name = pipe;
        srv.reply = R"({"v":2,"ok":false,"reason":"no-match"})";
        UnlockReply r;
        ServerTrust tr;
        bool ok = true;
        RunWithServer(srv, [&] { ok = RequestUnlock(r, nullptr, &tr); });
        Check("transport: non-owner server -> server-untrusted",
              !ok && r.reason == "server-untrusted" && tr.checked && !tr.trusted);
        Check("transport: nothing was written to the non-owner server", !srv.gotRequest);
        TestSetOwnerOverride(self);
    }
    {   // C: R3 -- a silent server; the cancel event ends the call at once
        FakeServer srv;
        srv.name = pipe;
        srv.silent = true;
        UnlockReply r;
        bool ok = true;
        DWORD took = 0;
        HANDLE cancel = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        RunWithServer(srv, [&] {
            std::thread canceller([&] { Sleep(200); SetEvent(cancel); });
            const DWORD t0 = GetTickCount();
            ok = RequestUnlock(r, cancel);
            took = GetTickCount() - t0;
            canceller.join();
        });
        CloseHandle(cancel);
        Check("transport: cancel during the reply wait -> 'cancelled'", !ok && r.reason == "cancelled");
        Check("transport: ... within 300 ms of the cancel (N-04), not the 12 s budget", took < 500);
    }
    {   // D: D-54 -- a reply over 64 KiB is malformed, not "unavailable"
        FakeServer srv;
        srv.name = pipe;
        srv.oversize = true;
        UnlockReply r;
        bool ok = true;
        RunWithServer(srv, [&] { ok = RequestUnlock(r, nullptr); });
        Check("transport: 70000-byte reply -> malformed-response", !ok && r.reason == "malformed-response");
    }
    {   // E: report_result
        FakeServer srv;
        srv.name = pipe;
        srv.reply = R"({"v":2,"lang":"en","ok":true})";
        bool ok = false;
        RunWithServer(srv, [&] { ok = SendReportResult("abcd", false, nullptr); });
        Check("transport: report_result sent and acknowledged",
              ok && srv.received == R"({"cmd":"report_result","v":2,"grant_id":"abcd","ok":false})");
        Check("transport: report_result refuses a non-hex grant id locally",
              !SendReportResult("ab\"cd", true, nullptr));
    }
    {   // F: no service at all; the connect loop watches the cancel event too
        HANDLE cancel = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        std::thread canceller([&] { Sleep(300); SetEvent(cancel); });
        UnlockReply r;
        const DWORD t0 = GetTickCount();
        const bool ok = RequestUnlock(r, cancel);
        const DWORD took = GetTickCount() - t0;
        canceller.join();
        CloseHandle(cancel);
        Check("transport: no pipe + cancel -> 'cancelled' within 1 s", !ok && r.reason == "cancelled" && took < 1000);
        Check("transport: no pipe -> ServicePipeExists() is false", !ServicePipeExists());
    }
    {   // G: a malformed phase-1 token never reaches the pipe
        UnlockReply r;
        Check("transport: non-hex token refused locally",
              !RequestUnlockGesture("ab\",\"cmd\":\"x", r, nullptr) && r.reason == "gesture-token-invalid");
    }
    {   // H: no owner recorded -> nothing is attempted
        TestSetOwnerOverride(L"S-1-5-18");      // not a person -> treated as "no owner"
        UnlockReply r;
        Check("transport: no (valid) owner -> 'no-owner'", !RequestUnlock(r, nullptr) && r.reason == "no-owner");
        TestSetOwnerOverride(self);
    }
    TestSetPipeName(L"");
    TestSetOwnerOverride(L"");
}

// ---------------------------------------------------------------------------------------------
// Stage 9: more transport shapes (B14 N-03) and the logon buffer (B14 N-06).
// ---------------------------------------------------------------------------------------------
static void TransportShapes() {
    using namespace FaceUnlock;
    const std::wstring self = SelfSid();
    const std::wstring pipe = L"\\\\.\\pipe\\FaceUnlockCpTest2-" + std::to_wstring(GetCurrentProcessId());
    TestSetPipeName(pipe);
    TestSetOwnerOverride(self);

    {   // NOT_FOUND for a while, then the server appears: retried inside the budget
        UnlockReply r;
        bool ok = false;
        std::thread late([&] {
            Sleep(700);
            FakeServer srv;
            srv.name = pipe;
            srv.reply = R"({"v":2,"ok":false,"reason":"no-match"})";
            srv.ready = CreateEventW(nullptr, TRUE, FALSE, nullptr);
            ServeOnce(&srv);
            CloseHandle(srv.ready);
        });
        ok = RequestUnlock(r, nullptr);
        late.join();
        Check("transport: FILE_NOT_FOUND is retried until the service appears",
              !ok && r.reason == "no-match");
    }
    {   // zero-byte reply -> unavailable, never a parse of nothing
        FakeServer srv;
        srv.name = pipe;
        srv.reply = "";
        UnlockReply r;
        bool ok = true;
        RunWithServer(srv, [&] { ok = RequestUnlock(r, nullptr); });
        Check("transport: zero-byte reply -> pipe-unavailable", !ok && r.reason == "pipe-unavailable");
    }
    {   // a byte-mode server: the client still reads one whole reply or fails safe
        HANDLE ready = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        std::thread srv([&] {
            HANDLE h = CreateNamedPipeW(pipe.c_str(), PIPE_ACCESS_DUPLEX, PIPE_TYPE_BYTE | PIPE_WAIT, 1,
                                        4096, 4096, 0, nullptr);
            SetEvent(ready);
            if (ConnectNamedPipe(h, nullptr) || GetLastError() == ERROR_PIPE_CONNECTED) {
                char buf[4096];
                DWORD n = 0;
                ReadFile(h, buf, sizeof(buf), &n, nullptr);
                const char part[] = "{\"v\":2,\"ok\":fa";      // half a reply, then close
                DWORD w = 0;
                WriteFile(h, part, (DWORD)strlen(part), &w, nullptr);
                FlushFileBuffers(h);
            }
            DisconnectNamedPipe(h);
            CloseHandle(h);
        });
        WaitForSingleObject(ready, 5000);
        UnlockReply r;
        const bool ok = RequestUnlock(r, nullptr);
        srv.join();
        CloseHandle(ready);
        Check("transport: byte-mode server with a torn reply fails safe (no credential)",
              !ok && r.password.empty() &&
              (r.reason == "malformed-response" || r.reason == "pipe-unavailable"));
    }
    TestSetPipeName(L"");
    TestSetOwnerOverride(L"");
}

static void KerbPackTests() {
    using namespace FaceUnlock;
    CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION cs{};
    HRESULT hr = KerbPackInteractiveUnlock(L"PC", L"user", L"pa55word", CPUS_UNLOCK_WORKSTATION, &cs);
    bool inside = false, unlockType = false;
    if (SUCCEEDED(hr) && cs.rgbSerialization) {
        auto* k = reinterpret_cast<KERB_INTERACTIVE_UNLOCK_LOGON*>(cs.rgbSerialization);
        const KERB_INTERACTIVE_LOGON& l = k->Logon;
        unlockType = l.MessageType == KerbWorkstationUnlockLogon;
        auto in = [&](const UNICODE_STRING& u) {
            const ULONG_PTR off = (ULONG_PTR)u.Buffer;
            return u.Length == 0 || (off >= sizeof(KERB_INTERACTIVE_UNLOCK_LOGON) &&
                                     off + u.Length <= cs.cbSerialization);
        };
        inside = in(l.LogonDomainName) && in(l.UserName) && in(l.Password) &&
                 l.UserName.Length == 4 * sizeof(wchar_t) && l.LogonDomainName.Length == 2 * sizeof(wchar_t);
        // F-94: the password field no longer holds the plain text
        const wchar_t* pw = reinterpret_cast<const wchar_t*>(cs.rgbSerialization + (ULONG_PTR)l.Password.Buffer);
        const std::wstring packed(pw, l.Password.Length / sizeof(wchar_t));
        Check("kerbpack: the password is CredProtect-ed, not plain", packed != L"pa55word" && !packed.empty());
    }
    Check("kerbpack: unlock scenario -> KerbWorkstationUnlockLogon", SUCCEEDED(hr) && unlockType);
    Check("kerbpack: every string lies inside the buffer, as an offset", inside);
    KerbUnpackFree(&cs);
    Check("kerbpack: KerbUnpackFree releases and clears", cs.rgbSerialization == nullptr && cs.cbSerialization == 0);

    CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION c2{};
    hr = KerbPackInteractiveUnlock(L".", L"user", L"pw", CPUS_LOGON, &c2);
    Check("kerbpack: logon scenario -> KerbInteractiveLogon",
          SUCCEEDED(hr) && reinterpret_cast<KERB_INTERACTIVE_UNLOCK_LOGON*>(c2.rgbSerialization)->Logon.MessageType ==
                               KerbInteractiveLogon);
    KerbUnpackFree(&c2);
    CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION c3{};
    Check("kerbpack: an embedded NUL is refused",
          KerbPackInteractiveUnlock(L".", L"us\0er"s, L"pw", CPUS_LOGON, &c3) == E_INVALIDARG);
    Check("kerbpack: a field over USHORT bytes is refused",
          KerbPackInteractiveUnlock(L".", L"u", std::wstring(40000, L'p'), CPUS_LOGON, &c3) == E_INVALIDARG);
}

int main() {
    std::printf("CP pipe-client unit test\n");
    std::printf("-----------------------------\n");

    // --- Happy path ---
    CaseOk("normal reply",
           R"({"ok":true,"username":"alice","password":"s3cret","domain":"CORP"})",
           L"alice", L"s3cret", L"CORP");

    // #1 escapes: embedded double-quote (old parser truncated at the first ")
    CaseOk("#1 password with escaped quote",
           R"({"ok":true,"username":"alice","password":"a\"b\"c","domain":"."})",
           L"alice", L"a\"b\"c", L".");

    // #1 escapes: embedded backslash
    CaseOk("#1 password with backslash",
           R"({"ok":true,"username":"alice","password":"do\\main\\x"})",
           L"alice", L"do\\main\\x", L".");   // domain omitted -> "."

    // #1 escapes: control + solidus escapes
    CaseOk("#1 password with \\t and \\/",
           R"({"ok":true,"username":"u","password":"a\/b\tc\nd"})",
           L"u", L"a/b\tc\nd", L".");

    // #1 escapes: string containing JSON delimiters must not break structure
    CaseOk("#1 password with braces/colon/comma",
           R"({"ok":true,"username":"u","password":"}{:,\"end"})",
           L"u", L"}{:,\"end", L".");

    // #1 \uXXXX BMP (service json.dumps emits non-ASCII as \uXXXX by default).
    // Input is the exact ASCII wire form: backslash-u-0-0-e-9 -> decodes to U+00E9.
    CaseOk("#1 password with \\u00e9 (cafe-acute)",
           "{\"ok\":true,\"username\":\"u\",\"password\":\"caf\\u00e9\"}",
           L"u", L"café", L".");

    // #1 \uXXXX surrogate pair -> astral code point U+1F600 (json.dumps emits
    // astral chars as a UTF-16 surrogate pair).
    CaseOk("#1 password with surrogate pair U+1F600",
           "{\"ok\":true,\"username\":\"u\",\"password\":\"grin\\uD83D\\uDE00\"}",
           L"u", L"grin\U0001F600", L".");

    // #2 empty password rejected
    CaseReject("#2 empty password rejected",
               R"({"ok":true,"username":"alice","password":""})", "malformed-response");

    // #2 empty username rejected
    CaseReject("#2 empty username rejected",
               R"({"ok":true,"username":"","password":"x"})", "malformed-response");

    // #2 missing password field rejected
    CaseReject("#2 missing password field",
               R"({"ok":true,"username":"alice"})", "malformed-response");

    // #3 whitespace: CRLF/tabs/spaces around tokens
    CaseOk("#3 pretty-printed whitespace (CRLF/tabs)",
           "{\r\n\t\"ok\" : true ,\r\n\t\"username\" : \"carol\" ,\r\n\t\"password\" : \"pw\"\r\n}",
           L"carol", L"pw", L".");

    // #4 extra + reordered + nested fields tolerated
    CaseOk("#4 reordered + extra + nested fields",
           R"({"reason":"","distance":0.21,"ok":true,"password":"p@ss","meta":{"a":[1,2],"b":"x"},"username":"bob"})",
           L"bob", L"p@ss", L".");

    // ok=false path surfaces the service reason verbatim
    CaseReject("ok=false surfaces reason 'locked-out'",
               R"({"ok":false,"reason":"locked-out","retry_after_s":42.5})", "locked-out");

    // ok=false without reason: Stage 9 (F-79) -- a protocol defect, not a face mismatch
    CaseReject("ok=false without reason -> malformed-response",
               R"({"ok":false})", "malformed-response");

    // Malformed JSON rejected
    CaseReject("unterminated object rejected",
               R"({"ok":true,"username":"u","password":"x")", "malformed-response");

    // "ok" as a string (wrong type) must NOT count as success
    CaseReject("ok as string is not truthy",
               R"({"ok":"true","username":"u","password":"x"})", nullptr);

    // Baseline gate: everything above is the pre-Stage-7-i suite, unchanged. It must still be
    // exactly 16 cases, all green, before any of the new ones run.
    const int baseTotal = g_pass + g_fail;
    std::printf("  ---- baseline suite: %d/%d green (expected 16/16) ----\n", g_pass, baseTotal);
    if (baseTotal != 16 || g_fail != 0) {
        ++g_fail;
        std::printf("  FAIL  baseline suite is not 16/16 green\n");
    }

    std::printf("\nStage 7-i: phase-1 gesture section\n");
    std::printf("-----------------------------\n");

    // The real phase-1 reply. The service json.dumps()es with ensure_ascii, so a localized
    // prompt arrives as \uXXXX -- here the RU "Моргни" (blink). Input below is the exact ASCII
    // wire form; it must decode to the Cyrillic string.
    CaseGesture("needs-gesture: RU prompt via \\u escapes + token + gesture",
                "{\"ok\":false,\"reason\":\"needs-gesture\",\"gesture\":\"blink\","
                "\"prompt\":\"\\u041c\\u043e\\u0440\\u0433\\u043d\\u0438\","
                "\"token\":\"0123456789abcdef0123456789abcdef\",\"ttl_s\":15.0,"
                "\"distance\":0.086,\"real\":true}",
                "needs-gesture", "blink", L"Моргни", "0123456789abcdef0123456789abcdef");

    // An EN prompt and a different kind, with the numeric fields in front of the strings.
    CaseGesture("needs-gesture: EN prompt, turn_left, reordered fields",
                R"({"distance":0.25,"real":true,"ttl_s":15.0,"ok":false,)"
                R"("token":"aaaaaaaabbbbbbbbccccccccdddddddd","reason":"needs-gesture",)"
                R"("prompt":"Turn your head left","gesture":"turn_left"})",
                "needs-gesture", "turn_left", L"Turn your head left",
                "aaaaaaaabbbbbbbbccccccccdddddddd");

    // needs-gesture with the phase-1 fields present but EMPTY -> the caller must be able to see
    // that there is nothing to act on (it treats this as a plain failure).
    CaseGesture("needs-gesture: empty gesture/prompt/token stay empty",
                R"({"ok":false,"reason":"needs-gesture","gesture":"","prompt":"","token":""})",
                "needs-gesture", nullptr, nullptr, nullptr);

    // Same, with the fields absent entirely rather than empty.
    CaseGesture("needs-gesture: absent gesture/prompt/token stay empty",
                R"({"ok":false,"reason":"needs-gesture","distance":0.1,"real":true})",
                "needs-gesture", nullptr, nullptr, nullptr);

    // Wrong-typed fields must NOT be coerced -- a numeric token comes back empty, not "12345".
    CaseGesture("needs-gesture: wrong-typed token is ignored, not coerced",
                R"({"ok":false,"reason":"needs-gesture","gesture":"nod","token":12345,)"
                R"("prompt":"Nod your head"})",
                "needs-gesture", "nod", L"Nod your head", nullptr);

    // Phase-2 failure. "challenge" is a DIFFERENT key from "gesture": it must not bleed into
    // the gesture field, or a failed round would look like a fresh challenge.
    CaseGesture("gesture-failed: 'challenge' does not populate 'gesture'",
                R"({"ok":false,"reason":"gesture-failed","challenge":"nod","state":"failed",)"
                R"("identity_frames":1})",
                "gesture-failed", nullptr, nullptr, nullptr);

    CaseGesture("gesture-token-invalid surfaces verbatim",
                R"({"ok":false,"reason":"gesture-token-invalid"})",
                "gesture-token-invalid", nullptr, nullptr, nullptr);

    // A prompt carrying JSON delimiters must not break the surrounding structure.
    CaseGesture("needs-gesture: prompt with quotes/braces survives",
                R"({"ok":false,"reason":"needs-gesture","gesture":"blink",)"
                R"("prompt":"Blink }{:,\"now","token":"ffffffffffffffffffffffffffffffff"})",
                "needs-gesture", "blink", L"Blink }{:,\"now",
                "ffffffffffffffffffffffffffffffff");

    // The ok:true contract is untouched by the new fields: a grant still grants...
    CaseOk("ok:true still grants when phase-1 fields ride along",
           R"({"ok":true,"username":"alice","password":"s3cret","gesture":"blink","token":"ab"})",
           L"alice", L"s3cret", L".");

    // ...and still refuses an empty password, gesture fields or not.
    CaseReject("phase-1 fields do not relax the ok:true contract",
               R"({"ok":true,"username":"alice","password":"","gesture":"blink","token":"ab"})",
               "malformed-response");

    // The back-compat 5-arg wrapper must pass the new reasons through unchanged.
    CaseReject("legacy wrapper surfaces reason 'needs-gesture'",
               R"({"ok":false,"reason":"needs-gesture","gesture":"nod","token":"ab"})",
               "needs-gesture");
    CaseReject("legacy wrapper surfaces reason 'gesture-failed'",
               R"({"ok":false,"reason":"gesture-failed","challenge":"blink"})",
               "gesture-failed");

    // Gate: the whole pre-8b suite (16 baseline + 12 Stage 7-i) must still be 28/28 green
    // before any 8b vector runs.
    const int pre8bTotal = g_pass + g_fail;
    std::printf("  ---- pre-8b suite: %d/%d green (expected 28/28) ----\n", g_pass, pre8bTotal);
    if (pre8bTotal != 28 || g_fail != 0) {
        ++g_fail;
        std::printf("  FAIL  pre-8b suite is not 28/28 green\n");
    }
    const int stage7iTotal = pre8bTotal - baseTotal;
    const int pre8bCount = g_pass + g_fail;
    int total8b = 0;

    std::printf("\n8b: hostile vectors, length caps, IsHexToken, failure texts\n");
    std::printf("-----------------------------\n");

    // --- Length caps (UTF-16 code units): username/domain <= 256, password <= 1024 ---
    CaseOk("cap: username of exactly 256 chars accepted",
           Grant(std::string(256, 'u'), "pw"),
           std::wstring(256, L'u'), L"pw", L".");
    CaseReject("cap: username of 257 chars rejected",
               Grant(std::string(257, 'u'), "pw"), "malformed-response");
    CaseOk("cap: domain of exactly 256 chars accepted",
           Grant("u", "pw", std::string(256, 'd').c_str()),
           L"u", L"pw", std::wstring(256, L'd'));
    CaseReject("cap: domain of 257 chars rejected",
               Grant("u", "pw", std::string(257, 'd').c_str()), "malformed-response");
    CaseOk("cap: password of exactly 1024 chars accepted",
           Grant("u", std::string(1024, 'p')),
           L"u", std::wstring(1024, L'p'), L".");
    CaseReject("cap: password of 1025 chars rejected",
               Grant("u", std::string(1025, 'p')), "malformed-response");
    CaseReject("cap: 70000-char password (beyond 64 KiB) rejected",
               Grant("u", std::string(70000, 'p')), "malformed-response");
    CaseOk("cap: counted in UTF-16 units -- 256 x \\u00e9 username accepted",
           Grant(Repeat("\\u00e9", 256), "pw"),
           std::wstring(256, L'\u00e9'), L"pw", L".");
    CaseOk("cap: 512 astral chars (1024 units) password accepted",
           Grant("u", Repeat("\\uD83D\\uDE00", 512)),
           L"u", RepeatW(L"\U0001F600", 512), L".");
    CaseReject("cap: 513 astral chars (1026 units) password rejected",
               Grant("u", Repeat("\\uD83D\\uDE00", 513)), "malformed-response");

    // --- Embedded NUL: never allowed in a credential field ---
    CaseReject("NUL: escaped \\u0000 inside password rejected",
               Grant("u", "ab\\u0000cd"), "malformed-response");
    CaseReject("NUL: escaped \\u0000 inside username rejected",
               Grant("al\\u0000ice", "pw"), "malformed-response");
    CaseReject("NUL: escaped \\u0000 inside domain rejected",
               Grant("u", "pw", "CO\\u0000RP"), "malformed-response");
    CaseReject("NUL: raw 0x00 byte inside password rejected",
               "{\"ok\":true,\"username\":\"u\",\"password\":\"ab\0cd\"}"s, "malformed-response");
    CaseReject("NUL: password that is only \\u0000 rejected",
               Grant("u", "\\u0000"), "malformed-response");

    // --- Bad / truncated escapes ---
    CaseReject("escape: invalid \\x rejected",
               Grant("u", "a\\xb"), "malformed-response");
    CaseReject("escape: truncated \\u12 rejected",
               R"({"ok":true,"username":"u","password":"a\u12"})", "malformed-response");
    CaseReject("escape: non-hex \\uZZZZ rejected",
               Grant("u", "a\\uZZZZ"), "malformed-response");
    CaseReject("escape: backslash at end of input rejected",
               R"({"ok":true,"username":"u","password":"a\)", "malformed-response");
    CaseReject("escape: high surrogate + truncated low \\uDC0 rejected",
               Grant("u", "\\uD83D\\uDC0"), "malformed-response");
    CaseOk("escape: lone low surrogate decodes to U+FFFD",
           Grant("u", "a\\uDC00b"), L"u", L"a\uFFFDb", L".");
    CaseOk("escape: high surrogate + non-low decodes to U+FFFD + char",
           Grant("u", "\\uD83D\\u0041"), L"u", L"\uFFFDA", L".");

    // --- Truncated JSON ---
    CaseReject("truncated: empty input", "", "malformed-response");
    CaseReject("truncated: lone '{'", "{", "malformed-response");
    CaseReject("truncated: inside a literal", R"({"ok":tr)", "malformed-response");
    CaseReject("truncated: unterminated password string",
               R"({"ok":true,"username":"u","password":"x)", "malformed-response");
    CaseReject("truncated: after a trailing comma",
               R"({"ok":true,"username":"u","password":"x",)", "malformed-response");
    CaseReject("truncated: inside a nested container",
               R"({"ok":true,"username":"u","password":"x","meta":{"a":[1,2)", "malformed-response");
    CaseReject("truncated: key without value",
               R"({"ok":true,"username":"u","password")", "malformed-response");
    CaseReject("not an object: top-level array",
               R"([{"ok":true,"username":"u","password":"x"}])", "malformed-response");

    // --- Nested and duplicated keys ---
    CaseOk("nested: a nested \"password\" does not override the top-level one",
           R"({"ok":true,"username":"u","password":"x","n":{"password":"evil","a":[{"b":[1,{"c":"}"}]}]}})",
           L"u", L"x", L".");
    CaseOk("nested: 5000-deep balanced nesting is skipped (iterative, no recursion)",
           "{\"ok\":true,\"username\":\"u\",\"password\":\"x\",\"deep\":" +
               std::string(5000, '[') + std::string(5000, ']') + "}",
           L"u", L"x", L".");
    CaseReject("nested: 5000-deep unbalanced nesting rejected",
               "{\"ok\":true,\"username\":\"u\",\"password\":\"x\",\"deep\":" +
                   std::string(5000, '[') + std::string(4999, ']') + "}",
               "malformed-response");
    CaseOk("duplicate: last \"password\" wins",
           R"({"ok":true,"username":"u","password":"first","password":"second"})",
           L"u", L"second", L".");
    CaseReject("duplicate: a later \"ok\":false wins over an earlier true",
               R"({"ok":true,"username":"u","password":"x","ok":false})", "malformed-response");
    CaseReject("duplicate: a later non-string password wins and is rejected",
               R"({"ok":true,"username":"u","password":"x","password":7})", "malformed-response");

    // --- Huge numbers and non-string types (never coerced) ---
    CaseOk("number: huge exponent and 400-digit number are skipped",
           "{\"ok\":true,\"retry_after_s\":1e999999,\"n\":" + std::string(400, '9') +
               ",\"username\":\"u\",\"password\":\"x\"}",
           L"u", L"x", L".");
    CaseReject("type: ok as number 1 is not truthy",
               R"({"ok":1,"username":"u","password":"x"})", "malformed-response");
    CaseReject("type: username as number rejected",
               R"({"ok":true,"username":12345,"password":"x"})", "malformed-response");
    CaseReject("type: password as array rejected",
               R"({"ok":true,"username":"u","password":["x"]})", "malformed-response");
    CaseReject("type: password as object rejected",
               R"({"ok":true,"username":"u","password":{"v":"x"}})", "malformed-response");
    CaseReject("type: password null rejected",
               R"({"ok":true,"username":"u","password":null})", "malformed-response");
    CaseReject("type: password true rejected",
               R"({"ok":true,"username":"u","password":true})", "malformed-response");
    CaseReject("type: non-string reason -> malformed-response",
               R"({"ok":false,"reason":42})", "malformed-response");
    CaseOk("type: non-string domain is ignored -> \".\"",
           R"({"ok":true,"username":"u","password":"x","domain":7})", L"u", L"x", L".");

    // --- Gesture prompt: C0/DEL stripped, capped at 120 UTF-16 units ---
    CasePrompt("prompt: escaped C0 controls and DEL stripped",
               "Bl\\n\\tink\\u0007 \\u001bnow\\u007f!", L"Blink now!");
    CaseReject("prompt: raw control bytes rejected (Stage 9 D-58: strict strings)",
               "{\"ok\":false,\"reason\":\"needs-gesture\",\"prompt\":\"Bl\x01ink\x1f now\"}",
               "malformed-response");
    CasePrompt("prompt: 300 chars capped to 120", std::string(300, 'a'), std::wstring(120, L'a'));
    CasePrompt("prompt: cap never splits a surrogate pair",
               std::string(119, 'a') + "\\uD83D\\uDE00", std::wstring(119, L'a'));
    CasePrompt("prompt: non-ASCII kept, C1 stripped (Stage 9 R2)",
               "Turn \\u00e9 \\u0085", L"Turn \u00e9 ");
    CasePrompt("prompt: only controls -> empty", "\\n\\r\\t", L"");

    // --- IsHexToken (the only token shape pasted back into the phase-2 request) ---
    using FaceUnlock::IsHexToken;
    Check("IsHexToken: empty -> false", !IsHexToken(""));
    Check("IsHexToken: 32 lowercase hex -> true", IsHexToken("0123456789abcdef0123456789abcdef"));
    Check("IsHexToken: uppercase hex -> true", IsHexToken("ABCDEF0123"));
    Check("IsHexToken: single digit -> true", IsHexToken("a"));
    Check("IsHexToken: 64 chars -> true", IsHexToken(std::string(64, 'f')));
    Check("IsHexToken: 65 chars -> false", !IsHexToken(std::string(65, 'f')));
    Check("IsHexToken: quote -> false", !IsHexToken("abc\""));
    Check("IsHexToken: backslash -> false", !IsHexToken("abc\\"));
    Check("IsHexToken: 0x prefix -> false", !IsHexToken("0x12"));
    Check("IsHexToken: non-hex letter -> false", !IsHexToken("abcg"));
    Check("IsHexToken: space -> false", !IsHexToken("ab cd"));
    Check("IsHexToken: embedded NUL -> false", !IsHexToken("ab\0cd"s));
    Check("IsHexToken: JSON-breaking payload -> false", !IsHexToken("ab\",\"cmd\":\"x"));
    total8b = g_pass + g_fail - pre8bCount;

    // ------------------------------------------------------------------------------------------
    std::printf("\nStage 9: protocol v2, strict JSON / UTF-8, sanitizer, texts, transport\n");
    std::printf("-----------------------------\n");
    const int pre9Count = g_pass + g_fail;
    using namespace FaceUnlock;

    // --- §2.1 version: a reply that is not v2 is refused whole, credentials included ---
    CaseReject("v2: grant without \"v\" -> version-mismatch, nothing kept",
               R"({"ok":true,"username":"u","password":"x","grant_id":"ab"})", "version-mismatch", true);
    CaseReject("v2: grant with \"v\":1 -> version-mismatch",
               R"({"v":1,"ok":true,"username":"u","password":"x","grant_id":"ab"})", "version-mismatch", true);
    CaseReject("v2: failure without \"v\" (a 0.1.x service) -> version-mismatch",
               R"({"ok":false,"reason":"no-match"})", "version-mismatch", true);
    CaseReject("v2: \"v\" as a string is not a version",
               R"({"v":"2","ok":true,"username":"u","password":"x","grant_id":"ab"})", "version-mismatch", true);
    CaseReject("v2: the service's own version-mismatch surfaces verbatim",
               R"({"v":2,"ok":false,"reason":"version-mismatch"})", "version-mismatch", true);
    // --- grant_id is mandatory on a grant, and hex ---
    CaseReject("v2: grant without grant_id -> malformed",
               R"({"v":2,"ok":true,"username":"u","password":"x"})", "malformed-response", true);
    CaseReject("v2: non-hex grant_id -> malformed",
               R"({"v":2,"ok":true,"username":"u","password":"x","grant_id":"zz\"}"})", "malformed-response", true);
    CaseReject("v2: 65-char grant_id -> malformed",
               "{\"v\":2,\"ok\":true,\"username\":\"u\",\"password\":\"x\",\"grant_id\":\"" +
                   std::string(65, 'a') + "\"}", "malformed-response", true);
    {
        UnlockReply r;
        const bool ok = ParseUnlockReply(
            R"({"v":2,"ok":true,"username":"u","password":"x","grant_id":"0123abcd","lang":"ru"})", r);
        Check("v2: grant carries grant_id and lang", ok && r.grantId == "0123abcd" && r.lang == "ru" &&
                                                     r.version == 2);
    }
    {
        UnlockReply r;
        const bool ok = ParseUnlockReply(
            R"({"v":2,"lang":"en","ok":false,"reason":"locked-out","retry_after_s":42.5})", r);
        Check("v2: locked-out carries retry_after_s = 42.5",
              !ok && r.reason == "locked-out" && r.retryAfterS > 42.49 && r.retryAfterS < 42.51);
        UnlockReply r2;
        ParseUnlockReply(R"({"v":2,"ok":false,"reason":"locked-out","retry_after_s":-3})", r2);
        Check("v2: a negative retry_after_s is ignored", r2.retryAfterS < 0.0);
        UnlockReply r3;
        ParseUnlockReply(R"({"v":2,"ok":false,"reason":"locked-out","retry_after_s":"42"})", r3);
        Check("v2: a string retry_after_s is ignored", r3.retryAfterS < 0.0);
    }

    // --- D-58 strict JSON ---
    CaseReject("strict: {\"ok\":} (no value) rejected", R"({"ok":})", "malformed-response");
    CaseReject("strict: bare word value rejected", R"({"ok":false,"reason":"x","n":abc})", "malformed-response");
    CaseReject("strict: '{' closed by ']' rejected", R"({"ok":false,"reason":"x","m":{"a":1]})", "malformed-response");
    CaseReject("strict: trailing garbage after the object rejected",
               R"({"ok":false,"reason":"no-match"}xyz)", "malformed-response");
    CaseReject("strict: a second object after the first rejected",
               R"({"ok":false,"reason":"no-match"}{"ok":true})", "malformed-response");
    CaseOk("strict: trailing whitespace after the object is fine",
           "{\"ok\":true,\"username\":\"u\",\"password\":\"x\"}\r\n  ", L"u", L"x", L".");
    CaseReject("strict: leading zero number rejected", R"({"ok":false,"reason":"x","n":012})", "malformed-response");
    CaseReject("strict: lone minus rejected", R"({"ok":false,"reason":"x","n":-})", "malformed-response");
    CaseReject("strict: '1.' rejected", R"({"ok":false,"reason":"x","n":1.})", "malformed-response");
    CaseReject("strict: '1e' rejected", R"({"ok":false,"reason":"x","n":1e})", "malformed-response");
    CaseOk("strict: -0.5e+3 is a valid number",
           R"({"n":-0.5e+3,"ok":true,"username":"u","password":"x"})", L"u", L"x", L".");
    CaseReject("strict: raw TAB byte inside a string rejected",
               "{\"ok\":true,\"username\":\"u\",\"password\":\"a\tb\"}", "malformed-response");
    CaseReject("strict: raw control byte inside a prompt rejected",
               "{\"ok\":false,\"reason\":\"needs-gesture\",\"gesture\":\"nod\",\"token\":\"ab\","
               "\"prompt\":\"Bl\x01ink\"}", "malformed-response");
    CaseOk("strict: empty nested object and array are skipped",
           R"({"a":{},"b":[],"ok":true,"username":"u","password":"x"})", L"u", L"x", L".");

    // --- F-73 strict UTF-8 ---
    CaseReject("utf8: invalid raw bytes in the password rejected (no silent U+FFFD)",
               "{\"ok\":true,\"username\":\"u\",\"password\":\"a\xC3\x28z\"}", "malformed-response");
    CaseReject("utf8: truncated raw sequence in the username rejected",
               "{\"ok\":true,\"username\":\"u\xE2\x82\",\"password\":\"x\"}", "malformed-response");
    CaseOk("utf8: valid raw UTF-8 (U+00E9) accepted",
           "{\"ok\":true,\"username\":\"u\",\"password\":\"caf\xC3\xA9\"}", L"u", L"café", L".");

    // --- F-74 / R2 sanitizer: C1, bidi controls, line / paragraph separators ---
    CasePrompt("sanitize: RLO/PDF bidi override stripped", "a\\u202Eevil\\u202Cz", L"aevilz");
    CasePrompt("sanitize: LRI..PDI isolates stripped", "\\u2066a\\u2067b\\u2068c\\u2069", L"abc");
    CasePrompt("sanitize: LRM/RLM stripped", "a\\u200Eb\\u200Fc", L"abc");
    CasePrompt("sanitize: U+2028 / U+2029 stripped", "line1\\u2028line2\\u2029end", L"line1line2end");
    CasePrompt("sanitize: C1 U+0080..U+009F stripped", "a\\u0080b\\u009Fc", L"abc");
    Check("sanitize: direct call keeps ordinary Cyrillic",
          SanitizePromptText(L"Поверните голову") == L"Поверните голову");

    // --- requests ---
    Check("request: unlock v2 with budget",
          BuildUnlockRequest(11500) == R"({"cmd":"unlock","v":2,"budget_ms":11500})");
    Check("request: unlock_gesture v2 with token and budget",
          BuildGestureRequest("ab12", 14000) ==
              R"({"cmd":"unlock_gesture","v":2,"token":"ab12","budget_ms":14000})");
    Check("request: report_result ok=true",
          BuildReportRequest("0a0b", true) == R"({"cmd":"report_result","v":2,"grant_id":"0a0b","ok":true})");
    Check("request: report_result ok=false",
          BuildReportRequest("0a0b", false) == R"({"cmd":"report_result","v":2,"grant_id":"0a0b","ok":false})");
    Check("report reply: {\"ok\":true,\"v\":2} accepted", ParseReportReply(R"({"ok":true,"v":2,"lang":"en"})"));
    Check("report reply: grant-unknown refused",
          !ParseReportReply(R"({"ok":false,"reason":"grant-unknown","v":2})"));
    Check("report reply: without v refused", !ParseReportReply(R"({"ok":true})"));

    // --- R3 honest texts ---
    Check("class: no-match / gesture-failed / motion-before-prompt / screen-suspected -> not recognised",
          FailureClass("no-match") == Text::NotRecognised && FailureClass("gesture-failed") == Text::NotRecognised &&
          FailureClass("motion-before-prompt") == Text::NotRecognised &&
          FailureClass("screen-suspected") == Text::NotRecognised);
    Check("class: too-dark has its own text (no longer 'not recognised')", FailureClass("too-dark") == Text::TooDark);
    Check("class: no-credentials -> no password", FailureClass("no-credentials") == Text::NoPassword);
    Check("class: no-enrollment -> no enrollment", FailureClass("no-enrollment") == Text::NoEnrollment);
    Check("class: camera-busy / camera-error / no-frames -> camera",
          FailureClass("camera-busy") == Text::CameraBusy && FailureClass("camera-error") == Text::CameraBusy &&
          FailureClass("no-frames") == Text::CameraBusy);
    Check("class: password-rejected", FailureClass("password-rejected") == Text::PasswordRejected);
    Check("class: version-mismatch -> update", FailureClass("version-mismatch") == Text::UpdateNeeded);
    Check("class: refusals -> needs attention",
          FailureClass("not-owner") == Text::NeedsAttention && FailureClass("custody") == Text::NeedsAttention &&
          FailureClass("insecure-data-dir") == Text::NeedsAttention &&
          FailureClass("no-models") == Text::NeedsAttention &&
          FailureClass("lockout-store-error") == Text::NeedsAttention);
    Check("class: 'unavailable' only for an unreachable / untrusted service",
          FailureClass("pipe-unavailable") == Text::Unavailable &&
          FailureClass("server-untrusted") == Text::Unavailable &&
          FailureClass("deadline-exceeded") == Text::Failed && FailureClass("engine-error") == Text::Failed &&
          FailureClass("malformed-response") == Text::Failed && FailureClass("") == Text::Failed &&
          FailureClass("internal-error") == Text::Failed && FailureClass("gesture-token-invalid") == Text::Failed);
    Check("text: locked-out with seconds (EN)",
          FailureText("locked-out", 42.4, "en") == L"Face sign-in is locked for 42 s. Use PIN or password.");
    Check("text: locked-out with seconds (RU)",
          FailureText("locked-out", 299.6, "ru") ==
              L"Вход по лицу заблокирован на 300 с. Войдите по PIN-коду или паролю.");
    Check("text: locked-out without seconds",
          FailureText("locked-out", -1.0, "en") == TileText(Text::LockedOut, "en"));
    {
        bool allDiffer = true, allRu = true;
        for (int i = (int)Text::Label + 1; i <= (int)Text::GestureFallback; ++i) {
            const Text t = (Text)i;
            if (TileText(t, "en").empty() || TileText(t, "ru").empty()) allRu = false;
            if (TileText(t, "en") == TileText(t, "ru")) allDiffer = false;
        }
        Check("texts: every text exists in EN and RU", allRu);
        Check("texts: every RU text differs from the EN one (the label aside)", allDiffer);
    }
    Check("lang: reply lang wins", ResolveLang("ru") == "ru" && ResolveLang("en") == "en");
    Check("lang: unknown reply lang -> system language, en or ru",
          ResolveLang("ja") == "en" || ResolveLang("ja") == "ru");
    Check("gesture text: turn_left,nod (EN)", GesturePromptFromKinds("turn_left,nod", "en") ==
                                                L"Turn your head left, then nod");
    Check("gesture text: nod,turn_right (RU)", GesturePromptFromKinds("nod,turn_right", "ru") ==
                                                 L"Кивните, затем поверните голову вправо");
    Check("gesture text: an unknown kind gives no text",
          GesturePromptFromKinds("blink", "en").empty() && GesturePromptFromKinds("", "en").empty() &&
          GesturePromptFromKinds("nod,", "en").empty());

    // --- R1 person SIDs ---
    Check("sid: local account S-1-5-21-a-b-c-rid", IsPersonSid(L"S-1-5-21-1-2-3-1001"));
    Check("sid: Entra ID S-1-12-1-a-b-c-d", IsPersonSid(L"S-1-12-1-111-222-333-444"));
    Check("sid: SYSTEM / LOCAL SERVICE / NETWORK SERVICE refused",
          !IsPersonSid(L"S-1-5-18") && !IsPersonSid(L"S-1-5-19") && !IsPersonSid(L"S-1-5-20"));
    Check("sid: truncated S-1-5-21-1 refused", !IsPersonSid(L"S-1-5-21-1"));
    Check("sid: garbage refused", !IsPersonSid(L"not a sid") && !IsPersonSid(L""));

    // --- transport against a private pipe server in this process (FACEUNLOCK_TESTING) ---
    TransportTests();
    TransportShapes();
    KerbPackTests();

    const int total9 = g_pass + g_fail - pre9Count;
    std::printf("-----------------------------\n");
    std::printf("PASS=%d  FAIL=%d  (baseline 16 + Stage 7-i %d + 8b %d + Stage 9 %d)\n",
                g_pass, g_fail, stage7iTotal, total8b, total9);
    return g_fail == 0 ? 0 : 1;
}
