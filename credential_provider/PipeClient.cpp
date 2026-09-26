#include "PipeClient.h"
#include <aclapi.h>     // GetSecurityInfo
#include <sddl.h>       // ConvertSidToStringSidW / ConvertStringSidToSidW
#include <cstring>
#include <map>
#include <vector>

namespace FaceUnlock {

// 8b F-25: reply buffers and parsed strings holding the password were freed unwiped ->
// plaintext residue in the LogonUI (SYSTEM) heap -> wipe them in place before release.
template <class S>
static void WipeContents(S& s) {
    if (!s.empty()) SecureZeroMemory(&s[0], s.size() * sizeof(s[0]));
}
template <class S>
static void WipeAndClear(S& s) {
    WipeContents(s);
    s.clear();
}

// Wipes a string/vector when the scope ends -- on every path, an exception included.
template <class S>
class WipeOnExit {
public:
    explicit WipeOnExit(S& s) : m_s(s) {}
    ~WipeOnExit() { WipeContents(m_s); }
    WipeOnExit(const WipeOnExit&) = delete;
    WipeOnExit& operator=(const WipeOnExit&) = delete;
private:
    S& m_s;
};

// Stage 9 (F-71): raw HANDLEs leaked when an allocation threw between open and close -> a leaked,
// connected, silent client handle wedged the sequential server -> RAII on every handle.
class UniqueHandle {
public:
    UniqueHandle() = default;
    explicit UniqueHandle(HANDLE h) : m_h(h) {}
    ~UniqueHandle() { reset(); }
    UniqueHandle(const UniqueHandle&) = delete;
    UniqueHandle& operator=(const UniqueHandle&) = delete;
    HANDLE get() const { return m_h; }
    bool valid() const { return m_h != nullptr && m_h != INVALID_HANDLE_VALUE; }
    void reset(HANDLE h = nullptr) {
        if (valid()) CloseHandle(m_h);
        m_h = h;
    }
private:
    HANDLE m_h = nullptr;
};

// Stage 9 (F-73): invalid UTF-8 was replaced with U+FFFD in silence, so a mangled password was
// packed and rejected by Windows -> decode strictly; a failure is "malformed-response".
// 8b F-25: decode a secret straight into its destination, sized exactly once.
static bool Utf8ToWideStrict(const std::string& s, std::wstring& out) {
    WipeAndClear(out);
    if (s.empty()) return true;
    int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.c_str(), (int)s.size(), nullptr, 0);
    if (n <= 0) return false;
    out.resize((size_t)n);
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.c_str(), (int)s.size(), &out[0], n) != n) {
        WipeAndClear(out);
        return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
// Minimal, dependency-free, STRICT JSON reader for the flat reply objects of this protocol.
//
// Stage 9 (D-55, D-58). The reader accepted non-JSON: any unknown value start was a "number"
// ({"ok":} parsed), '{' could be closed by ']', bytes after the closing brace were ignored and
// raw control bytes inside strings were taken. A hostile server could not get a credential
// through that (only typed STRING / BOOL fields are consumed), but the grammar is now enforced:
// strict numbers, matched brackets, nothing but whitespace after the object, no raw byte < 0x20
// inside a string. The service uses json.dumps (ensure_ascii), so every non-ASCII character
// arrives as \uXXXX (astral ones as a surrogate pair); both are decoded here.
// ---------------------------------------------------------------------------
namespace {

struct JsonValue {
    enum Type { STRING, BOOL, NUMBER, NUL, OBJECT, ARRAY } type = NUL;
    std::string str;        // decoded UTF-8 (STRING) or the literal text (NUMBER)
    bool boolean = false;   // valid when type == BOOL

    // 8b F-25: non-copyable (values are swapped into the map, never copied), wiped on destruction.
    JsonValue() = default;
    JsonValue(const JsonValue&) = delete;
    JsonValue& operator=(const JsonValue&) = delete;
    ~JsonValue() { WipeContents(str); }
};

inline void SkipWs(const char*& p, const char* end) {
    while (p < end && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) ++p;
}

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

// Parse a JSON string. p must point AT the opening quote; on success p is just past the closing
// quote and `out` holds the decoded UTF-8 bytes.
bool ParseString(const char*& p, const char* end, std::string& out) {
    if (p >= end || *p != '"') return false;
    ++p;
    // 8b F-25: reserve the raw span up front so no reallocation frees a partial password copy.
    // Decoding never lengthens a string, so no reallocation follows.
    {
        const char* q = p;
        while (q < end && *q != '"') {
            if (*q == '\\' && ++q >= end) break;
            ++q;
        }
        out.reserve(out.size() + (size_t)(q - p));
    }
    while (p < end) {
        char c = *p++;
        if (c == '"') return true;
        if ((unsigned char)c < 0x20) return false;          // D-58: raw control byte
        if (c != '\\') { out.push_back(c); continue; }
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
                    if (end - p >= 2 && p[0] == '\\' && p[1] == 'u') {
                        const char* save = p;
                        p += 2;
                        unsigned int lo;
                        if (!ParseHex4(p, end, lo)) return false;
                        if (lo >= 0xDC00 && lo <= 0xDFFF) {
                            cp = 0x10000u + ((cp - 0xD800u) << 10) + (lo - 0xDC00u);
                        } else {
                            AppendUtf8(out, 0xFFFD);   // unpaired high surrogate
                            p = save;                  // re-read `lo` as its own escape
                            break;
                        }
                    } else {
                        cp = 0xFFFD;
                    }
                } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                    cp = 0xFFFD;
                }
                AppendUtf8(out, cp);
                break;
            }
            default: return false;
        }
    }
    return false;  // unterminated
}

// JSON number grammar: -?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?
bool ParseNumber(const char*& p, const char* end, std::string& text) {
    const char* s = p;
    if (p < end && *p == '-') ++p;
    if (p >= end) return false;
    if (*p == '0') {
        ++p;
    } else if (*p >= '1' && *p <= '9') {
        while (p < end && *p >= '0' && *p <= '9') ++p;
    } else {
        return false;
    }
    if (p < end && *p == '.') {
        ++p;
        if (p >= end || !(*p >= '0' && *p <= '9')) return false;
        while (p < end && *p >= '0' && *p <= '9') ++p;
    }
    if (p < end && (*p == 'e' || *p == 'E')) {
        ++p;
        if (p < end && (*p == '+' || *p == '-')) ++p;
        if (p >= end || !(*p >= '0' && *p <= '9')) return false;
        while (p < end && *p >= '0' && *p <= '9') ++p;
    }
    text.assign(s, p);
    return true;
}

// Skip a nested object or array starting AT '{' or '['. Iterative (no recursion, so no stack
// exhaustion), brackets must match by type, strings are parsed so a quoted bracket is inert.
bool SkipContainer(const char*& p, const char* end) {
    std::string closers;
    while (p < end) {
        char c = *p;
        if (c == '"') {
            std::string tmp;
            if (!ParseString(p, end, tmp)) return false;
            continue;
        }
        if (c == '{') { closers.push_back('}'); ++p; continue; }
        if (c == '[') { closers.push_back(']'); ++p; continue; }
        if (c == '}' || c == ']') {
            if (closers.empty() || closers.back() != c) return false;
            closers.pop_back();
            ++p;
            if (closers.empty()) return true;
            continue;
        }
        if ((unsigned char)c < 0x20 && c != ' ' && c != '\t' && c != '\n' && c != '\r') return false;
        ++p;
    }
    return false;
}

// Parse a flat top-level JSON object into a key->value map. Nested values are skipped; last
// value wins on duplicate keys; only whitespace may follow the closing brace.
bool ParseObject(const std::string& json, std::map<std::string, JsonValue>& out) {
    const char* p = json.data();
    const char* end = p + json.size();
    SkipWs(p, end);
    if (p >= end || *p != '{') return false;
    ++p;
    SkipWs(p, end);
    if (p < end && *p == '}') {
        ++p;
        SkipWs(p, end);
        return p == end;
    }
    while (p < end) {
        SkipWs(p, end);
        std::string key;
        if (!ParseString(p, end, key)) return false;
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
            if (end - p >= 4 && std::strncmp(p, "true", 4) == 0)       { v.boolean = true;  p += 4; }
            else if (end - p >= 5 && std::strncmp(p, "false", 5) == 0) { v.boolean = false; p += 5; }
            else return false;
        } else if (c == 'n') {
            v.type = JsonValue::NUL;
            if (end - p >= 4 && std::strncmp(p, "null", 4) == 0) p += 4; else return false;
        } else if (c == '{' || c == '[') {
            v.type = (c == '{') ? JsonValue::OBJECT : JsonValue::ARRAY;
            if (!SkipContainer(p, end)) return false;
        } else {
            v.type = JsonValue::NUMBER;
            if (!ParseNumber(p, end, v.str)) return false;
        }
        // 8b F-25: wipe the slot, then swap the value in, so each decoded byte exists once.
        JsonValue& slot = out[key];
        WipeAndClear(slot.str);
        slot.type = v.type;
        slot.boolean = v.boolean;
        slot.str.swap(v.str);
        SkipWs(p, end);
        if (p < end && *p == ',') { ++p; continue; }
        if (p < end && *p == '}') {
            ++p;
            SkipWs(p, end);
            return p == end;                       // D-58: nothing may trail the object
        }
        return false;
    }
    return false;
}

// Copy a top-level STRING field. Absent or another type leaves `out` untouched.
void TakeString(const std::map<std::string, JsonValue>& obj, const char* key, std::string& out) {
    auto it = obj.find(key);
    if (it != obj.end() && it->second.type == JsonValue::STRING) out = it->second.str;
}

// Value of a validated JSON number literal, without locale-dependent strtod.
double NumberValue(const std::string& t) {
    const char* p = t.c_str();
    bool neg = false;
    if (*p == '-') { neg = true; ++p; }
    double v = 0.0;
    while (*p >= '0' && *p <= '9') { v = v * 10.0 + (*p - '0'); ++p; }
    if (*p == '.') {
        ++p;
        double scale = 0.1;
        while (*p >= '0' && *p <= '9') { v += (*p - '0') * scale; scale /= 10.0; ++p; }
    }
    if (*p == 'e' || *p == 'E') {
        ++p;
        bool eneg = false;
        if (*p == '+' || *p == '-') { eneg = (*p == '-'); ++p; }
        int e = 0;
        while (*p >= '0' && *p <= '9' && e < 400) { e = e * 10 + (*p - '0'); ++p; }
        for (int i = 0; i < e; ++i) v = eneg ? v / 10.0 : v * 10.0;
    }
    return neg ? -v : v;
}

bool TakeNumber(const std::map<std::string, JsonValue>& obj, const char* key, double& out) {
    auto it = obj.find(key);
    if (it == obj.end() || it->second.type != JsonValue::NUMBER) return false;
    out = NumberValue(it->second.str);
    return true;
}

}  // anonymous namespace

bool IsHexToken(const std::string& s) {
    if (s.empty() || s.size() > 64) return false;
    for (char c : s) {
        const bool hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
        if (!hex) return false;
    }
    return true;
}

std::wstring SanitizePromptText(const std::wstring& text) {
    std::wstring out;
    out.reserve(text.size() < kMaxPromptChars ? text.size() : kMaxPromptChars);
    for (wchar_t c : text) {
        if (c < 0x20 || c == 0x7F) continue;                     // C0 controls and DEL
        if (c >= 0x80 && c <= 0x9F) continue;                    // C1 controls (U+0085 NEL too)
        if (c == 0x200E || c == 0x200F) continue;                // LRM / RLM
        if (c >= 0x202A && c <= 0x202E) continue;                // LRE RLE PDF LRO RLO
        if (c >= 0x2066 && c <= 0x2069) continue;                // LRI RLI FSI PDI
        if (c == 0x2028 || c == 0x2029) continue;                // line / paragraph separator
        if (out.size() >= kMaxPromptChars) break;
        out.push_back(c);
    }
    // Never leave half a surrogate pair at the cut.
    if (!out.empty() && out.back() >= 0xD800 && out.back() <= 0xDBFF) out.pop_back();
    return out;
}

// Parse an unlock reply. Exposed so the offline test can exercise it without a pipe.
bool ParseUnlockReply(const std::string& response, UnlockReply& out) {
    std::map<std::string, JsonValue> obj;
    if (!ParseObject(response, obj)) {
        out.reason = "malformed-response";
        return false;
    }

    // Optional fields, read whatever the outcome (the parser never invents a value).
    TakeString(obj, "gesture", out.gesture);
    TakeString(obj, "token", out.token);
    TakeString(obj, "grant_id", out.grantId);
    TakeString(obj, "lang", out.lang);
    double num = 0.0;
    if (TakeNumber(obj, "retry_after_s", num) && num >= 0.0 && num < 1e7) out.retryAfterS = num;
    if (TakeNumber(obj, "v", num) && num >= 0.0 && num < 1e6) out.version = (int)num;
    std::string promptUtf8;
    TakeString(obj, "prompt", promptUtf8);
    std::wstring promptWide;
    if (!Utf8ToWideStrict(promptUtf8, promptWide)) {
        out.reason = "malformed-response";
        return false;
    }
    // 8b F-48 + Stage 9 R2: server text reaches the secure desktop only sanitized and capped.
    out.prompt = SanitizePromptText(promptWide);

    // §2.1: a reply that is not protocol v2 is refused as a whole -- nothing is kept.
    if (out.version != kProtocolVersion) {
        out.reason = "version-mismatch";
        return false;
    }

    auto itOk = obj.find("ok");
    const bool ok = (itOk != obj.end() && itOk->second.type == JsonValue::BOOL && itOk->second.boolean);
    if (!ok) {
        std::string reason;
        TakeString(obj, "reason", reason);
        // Stage 9 (F-79): ok:false without a reason is a protocol defect, not a face mismatch.
        out.reason = reason.empty() ? "malformed-response" : reason;
        return false;
    }

    // ok=true: username, password and a hex grant id are mandatory; empty credentials are never
    // packed.
    auto itU = obj.find("username");
    auto itP = obj.find("password");
    const bool haveU = (itU != obj.end() && itU->second.type == JsonValue::STRING && !itU->second.str.empty());
    const bool haveP = (itP != obj.end() && itP->second.type == JsonValue::STRING && !itP->second.str.empty());
    if (!haveU || !haveP || !IsHexToken(out.grantId)) {
        out.reason = "malformed-response";
        return false;
    }

    std::string d;
    TakeString(obj, "domain", d);
    const bool decoded = Utf8ToWideStrict(itU->second.str, out.username) &&
                         Utf8ToWideStrict(itP->second.str, out.password) &&
                         Utf8ToWideStrict(d.empty() ? std::string(".") : d, out.domain);

    // 8b F-48: caps and no embedded NUL, checked before anything is stored or packed.
    auto badField = [](const std::wstring& w, size_t cap) {
        return w.empty() || w.size() > cap || w.find(L'\0') != std::wstring::npos;
    };
    if (!decoded ||
        badField(out.username, kMaxUsernameChars) ||
        badField(out.password, kMaxPasswordChars) ||
        badField(out.domain,   kMaxDomainChars)) {
        WipeAndClear(out.username);
        WipeAndClear(out.password);
        WipeAndClear(out.domain);
        out.reason = "malformed-response";
        return false;
    }
    return true;
}

bool ParseReportReply(const std::string& response) {
    std::map<std::string, JsonValue> obj;
    if (!ParseObject(response, obj)) return false;
    double v = 0.0;
    if (!TakeNumber(obj, "v", v) || (int)v != kProtocolVersion) return false;
    auto itOk = obj.find("ok");
    return itOk != obj.end() && itOk->second.type == JsonValue::BOOL && itOk->second.boolean;
}

// ---------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------
std::string BuildUnlockRequest(DWORD budgetMs) {
    return "{\"cmd\":\"unlock\",\"v\":2,\"budget_ms\":" + std::to_string(budgetMs) + "}";
}

std::string BuildGestureRequest(const std::string& token, DWORD budgetMs) {
    // The token came over the pipe and is pasted back into JSON: only hex is allowed, so a quote
    // or backslash can never shape the request (callers check IsHexToken first).
    return "{\"cmd\":\"unlock_gesture\",\"v\":2,\"token\":\"" + token + "\",\"budget_ms\":" +
           std::to_string(budgetMs) + "}";
}

std::string BuildReportRequest(const std::string& grantId, bool ok) {
    return "{\"cmd\":\"report_result\",\"v\":2,\"grant_id\":\"" + grantId + "\",\"ok\":" +
           (ok ? "true" : "false") + "}";
}

// ---------------------------------------------------------------------------
// Tile texts (R3). English and Russian; the service tells us which one the user chose.
// ---------------------------------------------------------------------------
namespace {

struct TextPair { const wchar_t* en; const wchar_t* ru; };

const TextPair& TextFor(Text id) {
    static const TextPair kLabel        { L"Face Unlock", L"Face Unlock" };
    static const TextPair kPressArrow   { L"Press the arrow to scan your face",
                                          L"Нажмите стрелку, чтобы распознать лицо" };
    static const TextPair kScanning     { L"Scanning face...", L"Распознаю лицо..." };
    static const TextPair kVerified     { L"Face verified — press the arrow",
                                          L"Лицо распознано — нажмите стрелку" };
    static const TextPair kPacking      { L"Could not prepare the sign-in. Use PIN or password.",
                                          L"Не удалось подготовить вход. Войдите по PIN-коду или паролю." };
    static const TextPair kNotRecog     { L"Face not recognised. Try again or use PIN or password.",
                                          L"Лицо не распознано. Попробуйте ещё раз или войдите по PIN-коду или паролю." };
    static const TextPair kLocked       { L"Face sign-in is temporarily locked. Use PIN or password.",
                                          L"Вход по лицу временно заблокирован. Войдите по PIN-коду или паролю." };
    static const TextPair kLockedSecs   { L"Face sign-in is locked for %u s. Use PIN or password.",
                                          L"Вход по лицу заблокирован на %u с. Войдите по PIN-коду или паролю." };
    static const TextPair kUnavailable  { L"Face Unlock service is not running. Use PIN or password.",
                                          L"Служба Face Unlock не запущена. Войдите по PIN-коду или паролю." };
    static const TextPair kNoPassword   { L"No Windows password is saved in Face Unlock. Sign in with PIN and save it in Face Unlock.",
                                          L"В Face Unlock не сохранён пароль Windows. Войдите по PIN-коду и сохраните его в Face Unlock." };
    static const TextPair kNoEnrollment { L"No face is set up yet. Sign in with PIN and set it up in Face Unlock.",
                                          L"Лицо ещё не настроено. Войдите по PIN-коду и настройте его в Face Unlock." };
    static const TextPair kCameraBusy   { L"The camera is busy or not responding. Use PIN or password.",
                                          L"Камера занята или не отвечает. Войдите по PIN-коду или паролю." };
    static const TextPair kTooDark      { L"Too dark to recognise your face. Add light or use PIN or password.",
                                          L"Слишком темно для распознавания. Добавьте света или войдите по PIN-коду или паролю." };
    static const TextPair kPwRejected   { L"Windows rejected the saved password. Sign in with PIN and update it in Face Unlock.",
                                          L"Windows отклонила сохранённый пароль. Войдите по PIN-коду и обновите его в Face Unlock." };
    static const TextPair kUpdate       { L"Face Unlock components do not match. Update Face Unlock.",
                                          L"Компоненты Face Unlock разных версий. Обновите Face Unlock." };
    static const TextPair kAttention    { L"Face Unlock needs attention. Sign in with PIN and open Face Unlock.",
                                          L"Face Unlock требует внимания. Войдите по PIN-коду и откройте Face Unlock." };
    static const TextPair kFailed       { L"Face sign-in did not complete. Try again or use PIN or password.",
                                          L"Вход по лицу не завершён. Попробуйте ещё раз или войдите по PIN-коду или паролю." };
    static const TextPair kGesture      { L"Move your head as asked, then hold still.",
                                          L"Двигайте головой, как просят, затем замрите." };
    switch (id) {
        case Text::Label:            return kLabel;
        case Text::PressArrow:       return kPressArrow;
        case Text::Scanning:         return kScanning;
        case Text::Verified:         return kVerified;
        case Text::PackingFailed:    return kPacking;
        case Text::NotRecognised:    return kNotRecog;
        case Text::LockedOut:        return kLocked;
        case Text::LockedOutSecs:    return kLockedSecs;
        case Text::Unavailable:      return kUnavailable;
        case Text::NoPassword:       return kNoPassword;
        case Text::NoEnrollment:     return kNoEnrollment;
        case Text::CameraBusy:       return kCameraBusy;
        case Text::TooDark:          return kTooDark;
        case Text::PasswordRejected: return kPwRejected;
        case Text::UpdateNeeded:     return kUpdate;
        case Text::NeedsAttention:   return kAttention;
        case Text::GestureFallback:  return kGesture;
        case Text::Failed:
        default:                     return kFailed;
    }
}

}  // anonymous namespace

std::string ResolveLang(const std::string& replyLang) {
    if (replyLang == "ru") return "ru";
    if (replyLang == "en") return "en";
    return PRIMARYLANGID(GetSystemDefaultUILanguage()) == LANG_RUSSIAN ? "ru" : "en";
}

std::wstring TileText(Text id, const std::string& lang) {
    const TextPair& t = TextFor(id);
    return (lang == "ru") ? t.ru : t.en;
}

Text FailureClass(const std::string& r) {
    if (r == "no-match" || r == "no-face" || r == "gesture-failed" ||
        r == "motion-before-prompt" || r == "screen-suspected")
        return Text::NotRecognised;
    if (r == "locked-out") return Text::LockedOut;
    if (r == "pipe-unavailable" || r == "server-untrusted") return Text::Unavailable;
    if (r == "no-credentials") return Text::NoPassword;
    if (r == "no-enrollment") return Text::NoEnrollment;
    if (r == "camera-busy" || r == "camera-error" || r == "no-frames") return Text::CameraBusy;
    if (r == "too-dark") return Text::TooDark;
    if (r == "password-rejected") return Text::PasswordRejected;
    if (r == "version-mismatch") return Text::UpdateNeeded;
    if (r == "not-owner" || r == "custody" || r == "insecure-data-dir" || r == "no-models" ||
        r == "lockout-store-error" || r == "no-owner")
        return Text::NeedsAttention;
    // deadline-exceeded, engine-error, internal-error, malformed-response, bad-request,
    // unknown-command, not-authorized, gesture-token-invalid, cancelled, anything unknown.
    return Text::Failed;
}

std::wstring FailureText(const std::string& reason, double retryAfterS, const std::string& lang) {
    const Text cls = FailureClass(reason);
    if (cls == Text::LockedOut && retryAfterS >= 0.5 && retryAfterS < 86400.0) {
        wchar_t buf[256];
        const unsigned secs = (unsigned)(retryAfterS + 0.5);
        swprintf_s(buf, TileText(Text::LockedOutSecs, lang).c_str(), secs);
        return buf;
    }
    return TileText(cls, lang);
}

std::wstring GesturePromptFromKinds(const std::string& kinds, const std::string& lang) {
    const bool ru = (lang == "ru");
    std::wstring out;
    size_t start = 0;
    int steps = 0;
    while (start <= kinds.size()) {
        size_t comma = kinds.find(',', start);
        if (comma == std::string::npos) comma = kinds.size();
        const std::string k = kinds.substr(start, comma - start);
        const wchar_t* phrase = nullptr;
        if (k == "turn_left")  phrase = ru ? L"поверните голову влево" : L"turn your head left";
        if (k == "turn_right") phrase = ru ? L"поверните голову вправо" : L"turn your head right";
        if (k == "nod")        phrase = ru ? L"кивните" : L"nod";
        if (!phrase || steps >= 4) return std::wstring();
        if (steps > 0) out += ru ? L", затем " : L", then ";
        out += phrase;
        ++steps;
        start = comma + 1;
        if (comma == kinds.size()) break;
    }
    if (out.empty()) return out;
    out[0] = (wchar_t)(ULONG_PTR)CharUpperW((LPWSTR)(ULONG_PTR)out[0]);
    return out;
}

// ---------------------------------------------------------------------------
// Owner (R1) and server identity.
//
// Stage 9 (R1, F-54 / F-83 / F-86 / F-103). The CP used to trust ANY regular S-1-5-21 account as
// the pipe server, so another local user holding the name in the cold window could put text on
// the lock screen or answer for the owner's service. Now the server is trusted only when both the
// server PROCESS and the pipe OBJECT belong to the owner the installer recorded (the pipe owner is
// fixed at creation, so a reused PID cannot pass, F-64). The rule no longer depends on the account
// kind: local, domain and Entra ID (S-1-12-1-*) owners are all accepted.
// ---------------------------------------------------------------------------
namespace {

#ifdef FACEUNLOCK_TESTING
std::wstring g_testOwner;
std::wstring g_testPipe;
#endif

bool SidString(PSID sid, std::wstring& out) {
    LPWSTR s = nullptr;
    if (!ConvertSidToStringSidW(sid, &s)) return false;
    bool ok = true;
    try { out = s; } catch (...) { ok = false; }
    LocalFree(s);
    return ok;
}

bool ServerProcessSid(HANDLE hPipe, std::wstring& out) {
    ULONG pid = 0;
    if (!GetNamedPipeServerProcessId(hPipe, &pid)) return false;
    UniqueHandle proc(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid));
    if (!proc.valid()) return false;
    HANDLE tokRaw = nullptr;
    if (!OpenProcessToken(proc.get(), TOKEN_QUERY, &tokRaw)) return false;
    UniqueHandle tok(tokRaw);
    DWORD len = 0;
    GetTokenInformation(tok.get(), TokenUser, nullptr, 0, &len);
    if (len == 0) return false;
    std::vector<BYTE> buf(len);
    if (!GetTokenInformation(tok.get(), TokenUser, buf.data(), len, &len)) return false;
    return SidString(reinterpret_cast<const TOKEN_USER*>(buf.data())->User.Sid, out);
}

bool PipeObjectOwnerSid(HANDLE hPipe, std::wstring& out) {
    PSID owner = nullptr;
    PSECURITY_DESCRIPTOR sd = nullptr;
    if (GetSecurityInfo(hPipe, SE_KERNEL_OBJECT, OWNER_SECURITY_INFORMATION, &owner, nullptr,
                        nullptr, nullptr, &sd) != ERROR_SUCCESS)
        return false;
    const bool ok = owner != nullptr && SidString(owner, out);
    LocalFree(sd);
    return ok;
}

const wchar_t* PipeNameInUse() {
#ifdef FACEUNLOCK_TESTING
    if (!g_testPipe.empty()) return g_testPipe.c_str();
#endif
    return kPipeName;
}

// Wait on an overlapped operation AND the cancel event, bounded by timeoutMs. On a timeout or a
// cancel the operation is cancelled and DRAINED before the caller may free the buffer or handle.
enum class WaitResult { Done, Failed, Timeout, Cancelled };
WaitResult OverlappedWait(HANDLE pipe, OVERLAPPED& ov, DWORD timeoutMs, HANDLE cancel,
                          DWORD& transferred, DWORD& err) {
    HANDLE hs[2] = { ov.hEvent, cancel };
    const DWORD n = cancel ? 2 : 1;
    const DWORD wr = WaitForMultipleObjects(n, hs, FALSE, timeoutMs);
    if (wr != WAIT_OBJECT_0) {
        CancelIoEx(pipe, &ov);
        GetOverlappedResult(pipe, &ov, &transferred, TRUE);   // drain: the kernel owns the buffer
        return (wr == WAIT_OBJECT_0 + 1) ? WaitResult::Cancelled : WaitResult::Timeout;
    }
    if (!GetOverlappedResult(pipe, &ov, &transferred, FALSE)) {
        err = GetLastError();
        return WaitResult::Failed;
    }
    err = 0;
    return WaitResult::Done;
}

bool Cancelled(HANDLE cancel) {
    return cancel && WaitForSingleObject(cancel, 0) == WAIT_OBJECT_0;
}

// Sleep up to `ms`, returning early (true) when the cancel event fires.
bool SleepOrCancel(HANDLE cancel, DWORD ms) {
    if (!cancel) { Sleep(ms); return false; }
    return WaitForSingleObject(cancel, ms) == WAIT_OBJECT_0;
}

}  // anonymous namespace

bool IsPersonSid(const std::wstring& sidStr) {
    PSID sid = nullptr;
    if (!ConvertStringSidToSidW(sidStr.c_str(), &sid)) return false;
    bool ok = false;
    if (IsValidSid(sid)) {
        PSID_IDENTIFIER_AUTHORITY auth = GetSidIdentifierAuthority(sid);
        const bool a012 = auth->Value[0] == 0 && auth->Value[1] == 0 && auth->Value[2] == 0 &&
                          auth->Value[3] == 0 && auth->Value[4] == 0;
        const UCHAR count = *GetSidSubAuthorityCount(sid);
        if (a012 && auth->Value[5] == 5 && count >= 5 && *GetSidSubAuthority(sid, 0) == 21)
            ok = true;                                   // S-1-5-21-a-b-c-RID (local / domain)
        if (a012 && auth->Value[5] == 12 && count >= 5 && *GetSidSubAuthority(sid, 0) == 1)
            ok = true;                                   // S-1-12-1-a-b-c-d (Entra ID)
    }
    LocalFree(sid);
    return ok;
}

bool ReadOwnerSid(std::wstring& out) {
#ifdef FACEUNLOCK_TESTING
    if (!g_testOwner.empty()) { out = g_testOwner; return IsPersonSid(out); }
#endif
    wchar_t buf[256] = {};
    DWORD cb = sizeof(buf) - sizeof(wchar_t);
    DWORD type = 0;
    const LSTATUS st = RegGetValueW(HKEY_LOCAL_MACHINE, L"Software\\WindowsFaceUnlock",
                                    L"OriginalUserSid", RRF_RT_REG_SZ | RRF_SUBKEY_WOW6464KEY,
                                    &type, buf, &cb);
    if (st != ERROR_SUCCESS) return false;
    out = buf;
    return IsPersonSid(out);
}

bool ServicePipeExists() {
    // WaitNamedPipe never connects: it returns at once with ERROR_FILE_NOT_FOUND when no
    // instance exists, TRUE when one is listening, or times out (1 ms) when all are busy.
    if (WaitNamedPipeW(PipeNameInUse(), 1)) return true;
    return GetLastError() == ERROR_SEM_TIMEOUT;
}

#ifdef FACEUNLOCK_TESTING
void TestSetOwnerOverride(const std::wstring& sid) { g_testOwner = sid; }
void TestSetPipeName(const std::wstring& name) { g_testPipe = name; }
#endif

CallStatus PipeCall(const wchar_t* pipeName, const std::wstring& ownerSid,
                    RequestBuilder buildRequest, const void* ctx,
                    std::string& response, DWORD timeoutMs, HANDLE cancelEvent,
                    ServerTrust* trust) {
    const DWORD startTick = GetTickCount();
    auto remaining = [&]() -> DWORD {
        const DWORD elapsed = GetTickCount() - startTick;     // wrap-safe unsigned difference
        return (elapsed >= timeoutMs) ? 0 : (timeoutMs - elapsed);
    };
    // Allocate before connecting (F-71): an allocation failure never strands a connected handle.
    std::vector<char> buf(65536);
    WipeOnExit<std::vector<char>> wipeBuf(buf);    // 8b F-25: the raw reply may carry the password

    UniqueHandle h;
    for (;;) {
        if (Cancelled(cancelEvent)) return CallStatus::Cancelled;
        // 8b F-26: identification only -- the service reads the caller SID, never impersonates.
        h.reset(CreateFileW(pipeName, GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING,
                            FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                            nullptr));
        DWORD err = 0;
        if (h.valid()) {
            DWORD mode = PIPE_READMODE_MESSAGE;
            if (SetNamedPipeHandleState(h.get(), &mode, nullptr, nullptr)) break;
            // Stage 9 (F-72): the instance was torn down (or is byte-mode) -> never read a reply
            // in byte mode; retry within the budget like the Python client does.
            h.reset();
            err = ERROR_PIPE_BUSY;
        } else {
            err = GetLastError();
        }
        // 8b F-03: read the budget once; every wait below is capped by it and never 0.
        const DWORD rem = remaining();
        if (rem == 0) return CallStatus::Unavailable;
        if (err == ERROR_PIPE_BUSY) {
            // WaitNamedPipe cannot watch the cancel event: keep each wait short.
            WaitNamedPipeW(pipeName, (rem < 200) ? rem : 200);
        } else if (err == ERROR_FILE_NOT_FOUND) {
            if (SleepOrCancel(cancelEvent, (rem < 200) ? rem : 200)) return CallStatus::Cancelled;
        } else {
            return CallStatus::Unavailable;
        }
    }

    // Anti-squatting (R1): check the SERVER on this exact handle BEFORE anything is sent.
    std::wstring serverSid, pipeOwner;
    const bool haveSid = ServerProcessSid(h.get(), serverSid);
    const bool haveOwner = PipeObjectOwnerSid(h.get(), pipeOwner);
    const bool trusted = haveSid && haveOwner && !ownerSid.empty() &&
                         serverSid == ownerSid && pipeOwner == ownerSid;
    if (trust) {
        trust->checked = true;
        trust->trusted = trusted;
        trust->serverSid = haveSid ? serverSid : std::wstring();
    }
    if (!trusted) return CallStatus::Untrusted;

    UniqueHandle ev(CreateEventW(nullptr, TRUE, FALSE, nullptr));
    if (!ev.valid()) return CallStatus::Unavailable;
    OVERLAPPED ov{};
    ov.hEvent = ev.get();
    DWORD transferred = 0, err = 0;

    // The request carries the budget left NOW, after the connect wait (F-61).
    const std::string request = buildRequest(remaining(), ctx);
    if (!WriteFile(h.get(), request.data(), (DWORD)request.size(), nullptr, &ov) &&
        GetLastError() != ERROR_IO_PENDING)
        return CallStatus::Unavailable;
    WaitResult w = OverlappedWait(h.get(), ov, remaining(), cancelEvent, transferred, err);
    if (w == WaitResult::Cancelled) return CallStatus::Cancelled;
    if (w != WaitResult::Done || transferred != request.size()) return CallStatus::Unavailable;

    ResetEvent(ev.get());
    ov = OVERLAPPED{};
    ov.hEvent = ev.get();
    if (!ReadFile(h.get(), buf.data(), (DWORD)buf.size(), nullptr, &ov)) {
        const DWORD rerr = GetLastError();
        // D-54: ERROR_MORE_DATA was "accepted" here and then failed as "unavailable" -> a reply
        // over 64 KiB is its own outcome: the caller maps it to malformed-response.
        if (rerr == ERROR_MORE_DATA) {
            GetOverlappedResult(h.get(), &ov, &transferred, TRUE);
            return CallStatus::Oversize;
        }
        if (rerr != ERROR_IO_PENDING) return CallStatus::Unavailable;
    }
    w = OverlappedWait(h.get(), ov, remaining(), cancelEvent, transferred, err);
    if (w == WaitResult::Cancelled) return CallStatus::Cancelled;
    if (w == WaitResult::Failed && err == ERROR_MORE_DATA) return CallStatus::Oversize;
    if (w != WaitResult::Done || transferred == 0) return CallStatus::Unavailable;
    response.assign(buf.data(), transferred);
    return CallStatus::Ok;
}

namespace {

struct GestureCtx { const std::string* token; };

std::string BuildUnlockCb(DWORD remainingMs, const void*) {
    return BuildUnlockRequest(remainingMs);
}
std::string BuildGestureCb(DWORD remainingMs, const void* ctx) {
    return BuildGestureRequest(*static_cast<const GestureCtx*>(ctx)->token, remainingMs);
}
struct ReportCtx { const std::string* grantId; bool ok; };
std::string BuildReportCb(DWORD, const void* ctx) {
    const auto* c = static_cast<const ReportCtx*>(ctx);
    return BuildReportRequest(*c->grantId, c->ok);
}

const char* ReasonFor(CallStatus st) {
    switch (st) {
        case CallStatus::Untrusted: return "server-untrusted";
        case CallStatus::Oversize:  return "malformed-response";
        case CallStatus::Cancelled: return "cancelled";
        default:                    return "pipe-unavailable";
    }
}

bool Exchange(RequestBuilder build, const void* ctx, DWORD budget, HANDLE cancel,
              ServerTrust* trust, UnlockReply& out) {
    std::wstring owner;
    if (!ReadOwnerSid(owner)) {
        out.reason = "no-owner";
        return false;
    }
    std::string resp;
    WipeOnExit<std::string> wipeResp(resp);   // 8b F-25: raw reply JSON may carry the password
    ServerTrust localTrust;
    const CallStatus st = PipeCall(PipeNameInUse(), owner, build, ctx, resp, budget, cancel,
                                   trust ? trust : &localTrust);
    if (st != CallStatus::Ok) {
        out.reason = ReasonFor(st);
        return false;
    }
    return ParseUnlockReply(resp, out);
}

}  // anonymous namespace

bool RequestUnlock(UnlockReply& out, HANDLE cancelEvent, ServerTrust* trust) {
    return Exchange(&BuildUnlockCb, nullptr, kUnlockTimeoutMs, cancelEvent, trust, out);
}

bool RequestUnlockGesture(const std::string& token, UnlockReply& out, HANDLE cancelEvent,
                          ServerTrust* trust) {
    if (!IsHexToken(token)) {
        out.reason = "gesture-token-invalid";
        return false;
    }
    GestureCtx ctx{ &token };
    return Exchange(&BuildGestureCb, &ctx, kGestureTimeoutMs, cancelEvent, trust, out);
}

bool SendReportResult(const std::string& grantId, bool ok, HANDLE cancelEvent) {
    if (!IsHexToken(grantId)) return false;
    std::wstring owner;
    if (!ReadOwnerSid(owner)) return false;
    std::string resp;
    ReportCtx ctx{ &grantId, ok };
    if (PipeCall(PipeNameInUse(), owner, &BuildReportCb, &ctx, resp, kReportTimeoutMs,
                 cancelEvent, nullptr) != CallStatus::Ok)
        return false;
    return ParseReportReply(resp);
}

}  // namespace FaceUnlock
