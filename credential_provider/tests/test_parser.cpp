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

using FaceUnlock::ParseUnlockReply;
using FaceUnlock::ParseUnlockResponse;
using FaceUnlock::UnlockReply;
using namespace std::string_literals;   // "..."s keeps embedded NUL bytes (8b hostile vectors)

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

// Expect a REJECTED parse (no credentials) whose UnlockReply carries the given Stage-7-i
// phase-1 section. Pass nullptr for a field that must come back EMPTY -- the parser must
// never invent a value. Also asserts no credential leaked out of a non-ok reply.
static void CaseGesture(const char* label, const std::string& resp, const char* expectReason,
                        const char* expectGesture, const wchar_t* expectPrompt,
                        const char* expectToken) {
    const std::string wantG = expectGesture ? expectGesture : "";
    const std::wstring wantP = expectPrompt ? expectPrompt : L"";
    const std::string wantT = expectToken ? expectToken : "";
    UnlockReply r;
    bool ok = ParseUnlockReply(resp, r);
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
        std::printf("        creds leaked: user=[%s] pass_len=%d\n",
                    ToUtf8(r.username).c_str(), (int)r.password.size());
    }
}

// 8b: a plain boolean assertion, for the helpers that are not parse cases (IsHexToken,
// FailureTextForReason, SanitizePromptText).
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

// 8b: parse a needs-gesture reply carrying `promptJson` (a raw JSON string body) and compare
// the decoded + sanitized prompt.
static void CasePrompt(const char* label, const std::string& promptJson, const std::wstring& expect) {
    UnlockReply r;
    const std::string resp = "{\"ok\":false,\"reason\":\"needs-gesture\",\"gesture\":\"blink\","
                             "\"token\":\"ab\",\"prompt\":\"" + promptJson + "\"}";
    const bool ok = ParseUnlockReply(resp, r);
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
               R"({"ok":true,"username":"u","password":"x","ok":false})", "no-match");
    CaseReject("duplicate: a later non-string password wins and is rejected",
               R"({"ok":true,"username":"u","password":"x","password":7})", "malformed-response");

    // --- Huge numbers and non-string types (never coerced) ---
    CaseOk("number: huge exponent and 400-digit number are skipped",
           "{\"ok\":true,\"retry_after_s\":1e999999,\"n\":" + std::string(400, '9') +
               ",\"username\":\"u\",\"password\":\"x\"}",
           L"u", L"x", L".");
    CaseReject("type: ok as number 1 is not truthy",
               R"({"ok":1,"username":"u","password":"x"})", "no-match");
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
    CaseReject("type: non-string reason -> no-match",
               R"({"ok":false,"reason":42})", "no-match");
    CaseOk("type: non-string domain is ignored -> \".\"",
           R"({"ok":true,"username":"u","password":"x","domain":7})", L"u", L"x", L".");

    // --- Gesture prompt: C0/DEL stripped, capped at 120 UTF-16 units ---
    CasePrompt("prompt: escaped C0 controls and DEL stripped",
               "Bl\\n\\tink\\u0007 \\u001bnow\\u007f!", L"Blink now!");
    CasePrompt("prompt: raw control bytes stripped",
               "Bl\x01ink\x1f now", L"Blink now");
    CasePrompt("prompt: 300 chars capped to 120", std::string(300, 'a'), std::wstring(120, L'a'));
    CasePrompt("prompt: cap never splits a surrogate pair",
               std::string(119, 'a') + "\\uD83D\\uDE00", std::wstring(119, L'a'));
    CasePrompt("prompt: C1/non-ASCII text is kept",
               "Turn \\u00e9 \\u0085", L"Turn \u00e9 \u0085");
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

    // --- Fixed failure texts by reason ---
    using FaceUnlock::FailureTextForReason;
    Check("text: no-match -> not recognised",
          std::wstring(FailureTextForReason("no-match")) == FaceUnlock::kTextNotRecognised);
    Check("text: gesture-failed -> not recognised",
          std::wstring(FailureTextForReason("gesture-failed")) == FaceUnlock::kTextNotRecognised);
    Check("text: too-dark -> not recognised",
          std::wstring(FailureTextForReason("too-dark")) == FaceUnlock::kTextNotRecognised);
    Check("text: locked-out -> temporarily locked",
          std::wstring(FailureTextForReason("locked-out")) == FaceUnlock::kTextLockedOut);
    Check("text: pipe-unavailable -> unavailable",
          std::wstring(FailureTextForReason("pipe-unavailable")) == FaceUnlock::kTextUnavailable);
    Check("text: server-untrusted -> unavailable",
          std::wstring(FailureTextForReason("server-untrusted")) == FaceUnlock::kTextUnavailable);
    Check("text: insecure-data-dir -> unavailable (ordinary failure)",
          std::wstring(FailureTextForReason("insecure-data-dir")) == FaceUnlock::kTextUnavailable);
    Check("text: unknown / empty / exception reasons -> generic unavailable",
          std::wstring(FailureTextForReason("")) == FaceUnlock::kTextUnavailable &&
          std::wstring(FailureTextForReason("exception: boom")) == FaceUnlock::kTextUnavailable &&
          std::wstring(FailureTextForReason("needs-gesture")) == FaceUnlock::kTextUnavailable);
    Check("text: stored-password-rejected text is exact, with U+2014",
          std::wstring(FaceUnlock::kTextPasswordRejected) ==
              L"Stored password was rejected \u2014 sign in with PIN and update it in Face Unlock");

    const int total8b = g_pass + g_fail - pre8bCount;

    std::printf("-----------------------------\n");
    std::printf("PASS=%d  FAIL=%d  (baseline 16 + Stage 7-i %d + 8b %d)\n",
                g_pass, g_fail, stage7iTotal, total8b);
    return g_fail == 0 ? 0 : 1;
}
