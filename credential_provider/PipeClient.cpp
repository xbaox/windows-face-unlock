#include "PipeClient.h"
#include <vector>
#include <map>
#include <cstring>

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

}  // anonymous namespace

// Parse an unlock reply. Exposed (declared in the header) so the offline unit
// test can exercise it without a live pipe or service. Returns true only for a
// well-formed success reply with non-empty username AND password.
bool ParseUnlockResponse(const std::string& response,
                         std::wstring& username,
                         std::wstring& password,
                         std::wstring& domain,
                         std::string& errorOut) {
    std::map<std::string, JsonValue> obj;
    if (!ParseObject(response, obj)) {
        errorOut = "malformed-response";
        return false;
    }

    auto itOk = obj.find("ok");
    bool ok = (itOk != obj.end() && itOk->second.type == JsonValue::BOOL && itOk->second.boolean);
    if (!ok) {
        std::string reason;
        auto itR = obj.find("reason");
        if (itR != obj.end() && itR->second.type == JsonValue::STRING) reason = itR->second.str;
        errorOut = reason.empty() ? "no-match" : reason;
        return false;
    }

    // On ok=true, username and password are mandatory and must be non-empty --
    // an empty credential must never be packed into a logon buffer.
    auto itU = obj.find("username");
    auto itP = obj.find("password");
    const bool haveU = (itU != obj.end() && itU->second.type == JsonValue::STRING && !itU->second.str.empty());
    const bool haveP = (itP != obj.end() && itP->second.type == JsonValue::STRING && !itP->second.str.empty());
    if (!haveU || !haveP) {
        errorOut = "malformed-response";
        return false;
    }

    std::string d;
    auto itD = obj.find("domain");
    if (itD != obj.end() && itD->second.type == JsonValue::STRING) d = itD->second.str;

    username = Utf8ToWide(itU->second.str);
    password = Utf8ToWide(itP->second.str);
    domain   = Utf8ToWide(d.empty() ? "." : d);
    return true;
}

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
              DWORD timeoutMs) {
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

bool RequestUnlock(std::wstring& username,
                   std::wstring& password,
                   std::wstring& domain,
                   std::string& errorOut) {
    const std::wstring pipe = L"\\\\.\\pipe\\FaceUnlock";
    // 12s total: if the service is dead or the user is not visible, fail fast
    // so the user can switch to the password/PIN tile without feeling stuck.
    const DWORD kUnlockTimeoutMs = 12000;

    std::string resp;
    if (!PipeCall(pipe, "{\"cmd\":\"unlock\"}", resp, kUnlockTimeoutMs)) {
        errorOut = "pipe-unavailable";
        return false;
    }
    return ParseUnlockResponse(resp, username, password, domain, errorOut);
}

}  // namespace FaceUnlock
