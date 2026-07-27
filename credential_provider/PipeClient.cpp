#include "PipeClient.h"
#include <vector>
#include <map>
#include <cstring>
#include <sddl.h>       // ConvertSidToStringSidW / ConvertStringSidToSidW

namespace FaceUnlock {

static std::wstring Utf8ToWide(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    std::wstring out(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), out.data(), n);
    return out;
}

// ---------------------------------------------------------------------------
// Minimal, dependency-free JSON reader.
//
// Replaces the previous naive substring scanner, which broke on: JSON escape
// sequences (a password containing a quote or backslash arrives escaped as \"
// or \\ and truncated the value), pretty-printed whitespace, empty values, and
// added/reordered fields. This reader parses the whole top-level object
// structurally -- respecting string escaping so a '"' inside a value is never
// mistaken for a delimiter -- and decodes every JSON escape the service can
// emit. The Python service uses json.dumps() with ensure_ascii=True (its
// default), so every non-ASCII character is sent as \uXXXX (astral chars as a
// UTF-16 surrogate pair); both are decoded here.
//
// Scope note: this is intentionally NOT a general-purpose JSON library (no
// vendored dependency -- zero-deps is a project principle). It parses exactly
// the flat request/response shapes this protocol uses.
// ---------------------------------------------------------------------------
namespace {

struct JsonValue {
    enum Type { STRING, BOOL, NUMBER, NUL, OBJECT, ARRAY } type = NUL;
    std::string str;        // decoded UTF-8, valid when type == STRING
    bool boolean = false;   // valid when type == BOOL
};

inline void SkipWs(const char*& p, const char* end) {
    while (p < end && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) ++p;
}

// Encode a Unicode code point as UTF-8.
void AppendUtf8(std::string& out, unsigned int cp) {
    if (cp <= 0x7F) {
        out.push_back((char)cp);
    } else if (cp <= 0x7FF) {
        out.push_back((char)(0xC0 | (cp >> 6)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    } else if (cp <= 0xFFFF) {
        out.push_back((char)(0xE0 | (cp >> 12)));
        out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    } else {
        out.push_back((char)(0xF0 | (cp >> 18)));
        out.push_back((char)(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    }
}

// Parse 4 hex digits at p (advancing p). Returns false on non-hex/short input.
bool ParseHex4(const char*& p, const char* end, unsigned int& out) {
    if (end - p < 4) return false;
    unsigned int v = 0;
    for (int i = 0; i < 4; ++i) {
        char c = p[i];
        v <<= 4;
        if      (c >= '0' && c <= '9') v |= (unsigned)(c - '0');
        else if (c >= 'a' && c <= 'f') v |= (unsigned)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F') v |= (unsigned)(c - 'A' + 10);
        else return false;
    }
    p += 4;
    out = v;
    return true;
}

// Parse a JSON string. p must point AT the opening quote; on success p is left
// just past the closing quote and `out` holds the decoded UTF-8 bytes.
bool ParseString(const char*& p, const char* end, std::string& out) {
    if (p >= end || *p != '"') return false;
    ++p;  // opening quote
    while (p < end) {
        char c = *p++;
        if (c == '"') return true;                 // closing quote
        if (c != '\\') { out.push_back(c); continue; }  // raw byte (incl UTF-8 tail)
        if (p >= end) return false;
        char e = *p++;
        switch (e) {
            case '"':  out.push_back('"');  break;
            case '\\': out.push_back('\\'); break;
            case '/':  out.push_back('/');  break;
            case 'b':  out.push_back('\b'); break;
            case 'f':  out.push_back('\f'); break;
            case 'n':  out.push_back('\n'); break;
            case 'r':  out.push_back('\r'); break;
            case 't':  out.push_back('\t'); break;
            case 'u': {
                unsigned int cp;
                if (!ParseHex4(p, end, cp)) return false;
                if (cp >= 0xD800 && cp <= 0xDBFF) {
                    // High surrogate -> expect a \uXXXX low surrogate next.
                    if (end - p >= 2 && p[0] == '\\' && p[1] == 'u') {
                        const char* save = p;
                        p += 2;
                        unsigned int lo;
                        if (!ParseHex4(p, end, lo)) return false;
                        if (lo >= 0xDC00 && lo <= 0xDFFF) {
                            cp = 0x10000u + ((cp - 0xD800u) << 10) + (lo - 0xDC00u);
                        } else {
                            // Not a low surrogate: emit U+FFFD for the unpaired
                            // high surrogate and rewind so `lo` is parsed normally.
                            AppendUtf8(out, 0xFFFD);
                            p = save;
                            break;
                        }
                    } else {
                        cp = 0xFFFD;  // lone high surrogate at end of string
                    }
                } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                    cp = 0xFFFD;      // unpaired low surrogate
                }
                AppendUtf8(out, cp);
                break;
            }
            default: return false;    // invalid escape
        }
    }
    return false;  // unterminated string
}

// Consume any JSON value at p (advancing p). Content is discarded; used to skip
// over fields we don't care about so parsing stays aligned. Respects string
// escaping and nested containers.
bool SkipValue(const char*& p, const char* end) {
    SkipWs(p, end);
    if (p >= end) return false;
    if (*p == '"') { std::string tmp; return ParseString(p, end, tmp); }
    if (*p == '{' || *p == '[') {
        int depth = 0;
        while (p < end) {
            char c = *p;
            if (c == '"') { std::string tmp; if (!ParseString(p, end, tmp)) return false; continue; }
            if (c == '{' || c == '[') { ++depth; ++p; continue; }
            if (c == '}' || c == ']') { --depth; ++p; if (depth == 0) return true; continue; }
            ++p;
        }
        return false;  // unbalanced
    }
    // number / true / false / null
    while (p < end && *p != ',' && *p != '}' && *p != ']') ++p;
    return true;
}

// Parse a flat top-level JSON object into a key->value map. Tolerant of extra
// fields, reordering, nested values (skipped), and any whitespace. Last value
// wins on duplicate keys.
bool ParseObject(const std::string& json, std::map<std::string, JsonValue>& out) {
    const char* p = json.data();
    const char* end = p + json.size();
    SkipWs(p, end);
    if (p >= end || *p != '{') return false;
    ++p;
    SkipWs(p, end);
    if (p < end && *p == '}') return true;  // empty object
    while (p < end) {
        SkipWs(p, end);
        std::string key;
        if (!ParseString(p, end, key)) return false;   // key
        SkipWs(p, end);
        if (p >= end || *p != ':') return false;
        ++p;
        SkipWs(p, end);
        if (p >= end) return false;
        JsonValue v;
        char c = *p;
        if (c == '"') {
            v.type = JsonValue::STRING;
            if (!ParseString(p, end, v.str)) return false;
        } else if (c == 't' || c == 'f') {
            v.type = JsonValue::BOOL;
            if (end - p >= 4 && std::strncmp(p, "true", 4) == 0)  { v.boolean = true;  p += 4; }
            else if (end - p >= 5 && std::strncmp(p, "false", 5) == 0) { v.boolean = false; p += 5; }
            else return false;
        } else if (c == 'n') {
            v.type = JsonValue::NUL;
            if (end - p >= 4 && std::strncmp(p, "null", 4) == 0) p += 4; else return false;
        } else if (c == '{' || c == '[') {
            v.type = (c == '{') ? JsonValue::OBJECT : JsonValue::ARRAY;
            if (!SkipValue(p, end)) return false;
        } else {
            v.type = JsonValue::NUMBER;
            const char* s = p;
            while (p < end && *p != ',' && *p != '}' &&
                   *p != ' ' && *p != '\t' && *p != '\n' && *p != '\r') ++p;
            v.str.assign(s, p);
        }
        out[key] = v;
        SkipWs(p, end);
        if (p < end && *p == ',') { ++p; continue; }
        if (p < end && *p == '}') { ++p; return true; }
        return false;  // malformed separator
    }
    return false;
}

// Copy a top-level STRING field into `out`. A field that is absent, or present with any
// other type, leaves `out` untouched -- callers rely on that to keep a default ("." for
// domain) or an empty string (the gesture fields) rather than inventing a value.
void TakeString(const std::map<std::string, JsonValue>& obj, const char* key, std::string& out) {
    auto it = obj.find(key);
    if (it != obj.end() && it->second.type == JsonValue::STRING) out = it->second.str;
}

}  // anonymous namespace

// Parse an unlock reply. Exposed (declared in the header) so the offline unit
// test can exercise it without a live pipe or service. Returns true only for a
// well-formed success reply with non-empty username AND password.
bool ParseUnlockReply(const std::string& response, UnlockReply& out) {
    std::map<std::string, JsonValue> obj;
    if (!ParseObject(response, obj)) {
        out.reason = "malformed-response";
        return false;
    }

    // Stage 7-i phase 1. Read the gesture section BEFORE branching on ok, and independently
    // of it: the fields belong to the reply, not to a particular outcome, and a caller that
    // sees reason "needs-gesture" must be able to tell "the service sent a challenge" from
    // "the service sent needs-gesture but no usable token" -- which it does by finding these
    // empty. `prompt` is display text and may be non-ASCII: the service emits it as \uXXXX
    // (json.dumps defaults to ensure_ascii), ParseString decodes that to UTF-8, and it is
    // widened here. `ttl_s` is deliberately NOT read -- the client owns its own phase-2
    // timeout and must not take a duration from the wire.
    TakeString(obj, "gesture", out.gesture);
    TakeString(obj, "token", out.token);
    std::string promptUtf8;
    TakeString(obj, "prompt", promptUtf8);
    out.prompt = Utf8ToWide(promptUtf8);

    auto itOk = obj.find("ok");
    bool ok = (itOk != obj.end() && itOk->second.type == JsonValue::BOOL && itOk->second.boolean);
    if (!ok) {
        std::string reason;
        auto itR = obj.find("reason");
        if (itR != obj.end() && itR->second.type == JsonValue::STRING) reason = itR->second.str;
        out.reason = reason.empty() ? "no-match" : reason;
        return false;
    }

    // On ok=true, username and password are mandatory and must be non-empty --
    // an empty credential must never be packed into a logon buffer.
    auto itU = obj.find("username");
    auto itP = obj.find("password");
    const bool haveU = (itU != obj.end() && itU->second.type == JsonValue::STRING && !itU->second.str.empty());
    const bool haveP = (itP != obj.end() && itP->second.type == JsonValue::STRING && !itP->second.str.empty());
    if (!haveU || !haveP) {
        out.reason = "malformed-response";
        return false;
    }

    std::string d;
    TakeString(obj, "domain", d);

    out.username = Utf8ToWide(itU->second.str);
    out.password = Utf8ToWide(itP->second.str);
    out.domain   = Utf8ToWide(d.empty() ? "." : d);
    return true;
}

// Back-compat wrapper: the pre-Stage-7-i signature, unchanged in behaviour.
bool ParseUnlockResponse(const std::string& response,
                         std::wstring& username,
                         std::wstring& password,
                         std::wstring& domain,
                         std::string& errorOut) {
    UnlockReply r;
    const bool ok = ParseUnlockReply(response, r);
    username = r.username;
    password = r.password;
    domain   = r.domain;
    errorOut = r.reason;
    return ok;
}

// ---------------------------------------------------------------------------
// Client-side pipe-server identity check (defense-in-depth against a squatter
// owning the \\.\pipe\FaceUnlock name). We read the SID of the process on the
// SERVER end of the connection and accept it only if it is a trusted owner.
// This is an OS-level control (SID comparison) -- no crypto is introduced.
//
// Accepted server owners:
//   * SYSTEM (S-1-5-18)                 -- a SYSTEM-hosted service
//   * this process's own token user     -- SELF harness: the service runs as the
//                                          same user as the client
//   * any real user account (S-1-5-21-) -- registered CP: the client is
//                                          LogonUI/SYSTEM and the service runs as
//                                          the interactive user, so we accept a
//                                          normal machine/domain user account
//
// What the server side does and does NOT buy us (be precise here -- an earlier
// version of this comment claimed "a foreign account cannot own this pipe", which
// is not true):
//   * The hardened DACL applies to an ALREADY-CREATED object. It constrains who may
//     open (and add instances to) OUR pipe once our server exists; it does NOT
//     reserve the NAME in advance.
//   * So there is a COLD WINDOW -- from boot/logon until FaceService binds the name --
//     in which \\.\pipe\FaceUnlock is simply free, and any S-1-5-21 user on the box
//     can take it with their own CreateNamedPipe and their own DACL.
//   * FIRST_PIPE_INSTANCE does not PREVENT that squat. It only guarantees we never
//     silently share a name someone else already owns: our server refuses to start
//     and logs loudly, which makes a persistent squatter visible instead of hidden.
//   * The client rule below accepts ANY real S-1-5-21 account, so in that cold window
//     a squatter running as another (or the same) local user is trusted -> interposition
//     is possible. This check rejects only well-known / service SIDs (SYSTEM aside,
//     LOCAL/NETWORK SERVICE, logon and capability SIDs).
//
// What a squatter still cannot do: obtain the password. The client sends only the
// literal {"cmd":"unlock"} -- no secret travels in the request. A valid password comes
// back only from the genuine service, which decrypts its own DPAPI-protected store; a
// squatter has no way to read it and can only return a failure. The realistic worst
// case is therefore DENIAL OF SERVICE: face unlock fails and the user falls back to PIN.
//
// This check also deliberately does NOT depend on WTSQueryUserToken/session resolution,
// which is unreliable in the LogonUI secure-desktop context (it was silently failing and
// closing the pipe before the request was sent).
// ---------------------------------------------------------------------------
namespace {

bool SidStringFromToken(HANDLE hToken, std::wstring& out) {
    DWORD len = 0;
    GetTokenInformation(hToken, TokenUser, nullptr, 0, &len);  // query required size
    if (len == 0) return false;
    std::vector<BYTE> buf(len);
    if (!GetTokenInformation(hToken, TokenUser, buf.data(), len, &len)) return false;
    const TOKEN_USER* tu = reinterpret_cast<const TOKEN_USER*>(buf.data());
    LPWSTR s = nullptr;
    if (!ConvertSidToStringSidW(tu->User.Sid, &s)) return false;
    out = s;
    LocalFree(s);
    return true;
}

bool SidStringFromProcess(HANDLE hProcess, std::wstring& out) {
    HANDLE htok = nullptr;
    if (!OpenProcessToken(hProcess, TOKEN_QUERY, &htok)) return false;
    bool r = SidStringFromToken(htok, out);
    CloseHandle(htok);
    return r;
}

// SID of the process on the SERVER end of an open pipe handle:
// GetNamedPipeServerProcessId -> OpenProcess(QUERY_LIMITED) -> token -> SID.
bool ServerSidString(HANDLE hPipe, std::wstring& out) {
    ULONG pid = 0;
    if (!GetNamedPipeServerProcessId(hPipe, &pid)) return false;
    HANDLE hp = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!hp) return false;
    bool r = SidStringFromProcess(hp, out);
    CloseHandle(hp);
    return r;
}

// True iff sidStr is a regular machine/domain user account SID: NT authority
// (S-1-5-) with first sub-authority 21 (SECURITY_NT_NON_UNIQUE), i.e. S-1-5-21-<...>.
// Deliberately EXCLUDES SYSTEM (S-1-5-18), LOCAL/NETWORK SERVICE (S-1-5-19/20),
// well-known groups, logon SIDs and capability SIDs -- so a service or pseudo
// account cannot masquerade as a legitimate user-owned FaceService.
bool IsRegularUserSid(const std::wstring& sidStr) {
    PSID sid = nullptr;
    if (!ConvertStringSidToSidW(sidStr.c_str(), &sid)) return false;
    bool ok = false;
    if (IsValidSid(sid)) {
        PSID_IDENTIFIER_AUTHORITY auth = GetSidIdentifierAuthority(sid);
        const bool ntAuthority =
            auth->Value[0] == 0 && auth->Value[1] == 0 && auth->Value[2] == 0 &&
            auth->Value[3] == 0 && auth->Value[4] == 0 && auth->Value[5] == 5;
        if (ntAuthority && *GetSidSubAuthorityCount(sid) >= 1 &&
            *GetSidSubAuthority(sid, 0) == 21) {   // SECURITY_NT_NON_UNIQUE -> S-1-5-21-...
            ok = true;
        }
    }
    LocalFree(sid);
    return ok;
}

bool IsTrustedServerSid(const std::wstring& serverSid) {
    if (serverSid == L"S-1-5-18") return true;   // SYSTEM: a SYSTEM-hosted service
    std::wstring own;
    if (SidStringFromProcess(GetCurrentProcess(), own) && !own.empty() && serverSid == own)
        return true;                             // same-user: the SELF harness (client == service)
    // Registered-CP case: the client is SYSTEM (LogonUI) and the service runs as the interactive
    // user, so neither rule above matches. Trust the server iff it is a real machine/domain user
    // account (S-1-5-21-...). See the header comment for why this does NOT resolve the session user
    // via WTSQueryUserToken (unreliable on the secure desktop), and for the limits of this rule --
    // it accepts ANY real user account, so it does not by itself exclude a cold-window squatter.
    if (IsRegularUserSid(serverSid)) return true;
    return false;
}

}  // anonymous namespace

static bool OverlappedWait(HANDLE pipe, OVERLAPPED& ov, DWORD timeoutMs, DWORD& transferred) {
    DWORD wr = WaitForSingleObject(ov.hEvent, timeoutMs);
    if (wr != WAIT_OBJECT_0) {
        CancelIoEx(pipe, &ov);
        // drain any pending completion so CloseHandle is safe
        GetOverlappedResult(pipe, &ov, &transferred, TRUE);
        return false;
    }
    return GetOverlappedResult(pipe, &ov, &transferred, FALSE) != 0;
}

bool PipeCall(const std::wstring& pipeName,
              const std::string& requestJson,
              std::string& response,
              DWORD timeoutMs,
              bool verifyServer,
              ServerTrust* trust) {
    // Total deadline for the whole transaction.
    const DWORD startTick = GetTickCount();
    auto remaining = [&]() -> DWORD {
        DWORD elapsed = GetTickCount() - startTick;
        return (elapsed >= timeoutMs) ? 0 : (timeoutMs - elapsed);
    };

    // Try to open the pipe within the total timeout.
    HANDLE h = INVALID_HANDLE_VALUE;
    while (true) {
        h = CreateFileW(pipeName.c_str(),
                        GENERIC_READ | GENERIC_WRITE,
                        0, nullptr, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, nullptr);
        if (h != INVALID_HANDLE_VALUE) break;
        DWORD err = GetLastError();
        if (remaining() == 0) return false;
        if (err == ERROR_PIPE_BUSY) {
            WaitNamedPipeW(pipeName.c_str(), (remaining() < 500) ? remaining() : 500);
        } else if (err == ERROR_FILE_NOT_FOUND) {
            // Service not running; short sleep and retry within budget.
            Sleep(200);
        } else {
            return false;
        }
    }

    // Anti-squatting: verify the SERVER end is a trusted owner BEFORE sending
    // anything on this exact handle (same-connection -> no TOCTOU window).
    if (verifyServer) {
        std::wstring serverSid;
        bool haveSid = ServerSidString(h, serverSid);
        bool trusted = haveSid && IsTrustedServerSid(serverSid);
        if (trust) {
            trust->checked = true;
            trust->trusted = trusted;
            trust->serverSid = haveSid ? serverSid : std::wstring();
        }
        if (!trusted) {
            CloseHandle(h);
            return false;  // refuse: do NOT send the request to an untrusted server
        }
    }

    DWORD mode = PIPE_READMODE_MESSAGE;
    SetNamedPipeHandleState(h, &mode, nullptr, nullptr);

    HANDLE ev = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!ev) { CloseHandle(h); return false; }

    bool success = false;
    OVERLAPPED ov{};
    ov.hEvent = ev;
    DWORD transferred = 0;
    std::vector<char> buf(65536);

    do {
        BOOL w = WriteFile(h, requestJson.data(), (DWORD)requestJson.size(), nullptr, &ov);
        if (!w && GetLastError() != ERROR_IO_PENDING) break;
        if (!OverlappedWait(h, ov, remaining(), transferred)) break;
        if (transferred != requestJson.size()) break;

        ResetEvent(ev);
        BOOL r = ReadFile(h, buf.data(), (DWORD)buf.size(), nullptr, &ov);
        DWORD rerr = r ? 0 : GetLastError();
        if (!r && rerr != ERROR_IO_PENDING && rerr != ERROR_MORE_DATA) break;
        if (!OverlappedWait(h, ov, remaining(), transferred)) break;
        if (transferred == 0) break;

        response.assign(buf.data(), transferred);
        success = true;
    } while (false);

    CloseHandle(ev);
    CloseHandle(h);
    return success;
}

bool RequestUnlock(UnlockReply& out, ServerTrust* trust) {
    const std::wstring pipe = L"\\\\.\\pipe\\FaceUnlock";
    // 12s total: if the service is dead or the user is not visible, fail fast
    // so the user can switch to the password/PIN tile without feeling stuck.
    const DWORD kUnlockTimeoutMs = 12000;

    std::string resp;
    ServerTrust localTrust;
    ServerTrust* t = trust ? trust : &localTrust;
    if (!PipeCall(pipe, "{\"cmd\":\"unlock\"}", resp, kUnlockTimeoutMs, /*verifyServer=*/true, t)) {
        // Distinguish an untrusted-server refusal from a plain transport failure.
        out.reason = (t->checked && !t->trusted) ? "server-untrusted" : "pipe-unavailable";
        return false;
    }
    return ParseUnlockReply(resp, out);
}

namespace {
// The phase-1 token is data we received over the pipe and are about to paste back into a
// request document. Constrain it to what the service actually issues (hex) instead of
// escaping: a token carrying a quote or backslash would otherwise build malformed -- or
// attacker-shaped -- request JSON. 64 is a generous ceiling over the 32 chars in use.
bool IsHexToken(const std::string& s) {
    if (s.empty() || s.size() > 64) return false;
    for (char c : s) {
        const bool hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
        if (!hex) return false;
    }
    return true;
}
}  // anonymous namespace

bool RequestUnlockGesture(const std::string& token, UnlockReply& out, ServerTrust* trust) {
    const std::wstring pipe = L"\\\\.\\pipe\\FaceUnlock";
    // Deliberately NOT kUnlockTimeoutMs: phase 2 waits for a human to blink or turn their
    // head, and the service's own round is capped well above the passive-unlock budget. A
    // separate constant so tightening one never silently tightens the other.
    const DWORD kGestureTimeoutMs = 15000;

    if (!IsHexToken(token)) {
        out.reason = "gesture-token-invalid";
        return false;
    }

    std::string resp;
    ServerTrust localTrust;
    ServerTrust* t = trust ? trust : &localTrust;
    const std::string req = "{\"cmd\":\"unlock_gesture\",\"token\":\"" + token + "\"}";
    if (!PipeCall(pipe, req, resp, kGestureTimeoutMs, /*verifyServer=*/true, t)) {
        out.reason = (t->checked && !t->trusted) ? "server-untrusted" : "pipe-unavailable";
        return false;
    }
    return ParseUnlockReply(resp, out);
}

// Back-compat overload: same call, gesture fields discarded.
bool RequestUnlock(std::wstring& username,
                   std::wstring& password,
                   std::wstring& domain,
                   std::string& errorOut,
                   ServerTrust* trust) {
    UnlockReply r;
    const bool ok = RequestUnlock(r, trust);
    username = r.username;
    password = r.password;
    domain   = r.domain;
    errorOut = r.reason;
    return ok;
}

}  // namespace FaceUnlock
