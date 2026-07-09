// Offline unit test for FaceUnlock::ParseUnlockResponse (credential_provider).
//
// Camera-free, service-free, pipe-free: feeds hand-crafted JSON replies straight
// into the parser and asserts decode/accept/reject. Directly proves the four
// Stage-5 parser fixes:
//   #1 JSON escape decoding (\" \\ \/ \b \f \n \r \t \uXXXX + surrogate pairs)
//   #2 empty username/password on ok=true are rejected
//   #3 robust whitespace skip (incl. \r\n) around tokens
//   #4 tolerance of added / reordered / nested fields and delimiter-bearing values
//
// Test vectors below are SYNTHETIC -- not real credentials -- so decoded values
// are printed on failure for debugging.
//
// Build (from a VS2022 x64 dev prompt), or via tests/CMakeLists.txt:
//   cl /EHsc /std:c++17 test_parser.cpp ..\PipeClient.cpp
// Exit code 0 = all pass, 1 = at least one failure.

#include "../PipeClient.h"

#include <windows.h>
#include <string>
#include <cstdio>

using FaceUnlock::ParseUnlockResponse;

static int g_pass = 0;
static int g_fail = 0;

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
    bool ok = ParseUnlockResponse(resp, u, p, d, err);
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

// Expect the parse to be rejected (return false). If expectErr is non-null, the
// errorOut must match it exactly.
static void CaseReject(const char* label, const std::string& resp, const char* expectErr) {
    std::wstring u, p, d;
    std::string err;
    bool ok = ParseUnlockResponse(resp, u, p, d, err);
    bool good = !ok && (expectErr == nullptr || err == expectErr);
    if (good) {
        ++g_pass;
        std::printf("  PASS  %s  (rejected: %s)\n", label, err.c_str());
    } else {
        ++g_fail;
        std::printf("  FAIL  %s\n", label);
        std::printf("        expected reject%s%s, got ok=%d err=%s\n",
                    expectErr ? " err=" : "", expectErr ? expectErr : "",
                    (int)ok, err.c_str());
    }
}

int main() {
    std::printf("ParseUnlockResponse unit test\n");
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

    // ok=false without reason -> generic "no-match"
    CaseReject("ok=false without reason -> no-match",
               R"({"ok":false})", "no-match");

    // Malformed JSON rejected
    CaseReject("unterminated object rejected",
               R"({"ok":true,"username":"u","password":"x")", "malformed-response");

    // "ok" as a string (wrong type) must NOT count as success
    CaseReject("ok as string is not truthy",
               R"({"ok":"true","username":"u","password":"x"})", nullptr);

    std::printf("-----------------------------\n");
    std::printf("PASS=%d  FAIL=%d\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
