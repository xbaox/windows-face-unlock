#pragma once
#include <windows.h>
#include <string>

// Named-pipe client for the FaceService protocol, version 2 (Stage 9, act 9b §2.1).
// One JSON request, one JSON reply. Every wait is bounded by the call's budget AND watches a
// cancel event, so a caller on the LogonUI thread can always get out at once (R3).
namespace FaceUnlock {

constexpr int kProtocolVersion = 2;
inline constexpr wchar_t kPipeName[] = L"\\\\.\\pipe\\FaceUnlock";

// Budgets for the whole call (connect included). The request carries what is left of it
// ("budget_ms", measured after the connect), and the service stops 500 ms short of that.
constexpr DWORD kUnlockTimeoutMs  = 12000;   // phase 1: passive burst
constexpr DWORD kGestureTimeoutMs = 18000;   // phase 2: two head movements (R4: 12.4 s round)
// 9d (A-2): report_result is sent SYNCHRONOUSLY inside ReportResult, on the LogonUI thread --
// connect, identity check and write all inside this cap; the acknowledgement is awaited only in
// what is left of it. A detached reporter thread could be killed with LogonUI.exe right after a
// successful sign-in, before the report left.
constexpr DWORD kReportTimeoutMs  = 750;
// 9d (A-2): with no pipe at all the report stops retrying after this long (the service is not
// running; there is nobody to tell), instead of spending the whole cap on the LogonUI thread.
constexpr DWORD kReportNotFoundMs = 250;

// Outcome of the pipe-server identity check, for callers that want to observe it.
struct ServerTrust {
    bool checked = false;      // was the identity check performed
    bool trusted = false;      // server process AND pipe object owned by the product's owner
    std::wstring serverSid;    // server process SID (string form), if resolved
};

// Everything an unlock reply can carry.
//
// `prompt` is wide display text (the service localizes it and json.dumps sends non-ASCII as
// \uXXXX). `gesture`, `token`, `grantId`, `lang` are ASCII by construction. Every optional field is
// EMPTY (or -1 / 0) unless the reply actually carried it -- the parser never invents a value.
struct UnlockReply {
    std::wstring username;   // valid only when ParseUnlockReply returned true
    std::wstring password;   // valid only when ParseUnlockReply returned true
    std::wstring domain;     // "." when the reply omitted it
    std::string  reason;     // failure reason token, or a parse error; empty on success
    std::string  gesture;    // phase-1 challenge (e.g. "turn_left,nod"), else empty
    std::wstring prompt;     // instruction to show on the tile (sanitized), else empty
    std::string  token;      // phase-1 token (hex), replayed in the phase-2 request, else empty
    std::string  grantId;    // v2: one-shot id of a grant, echoed in report_result, else empty
    std::string  lang;       // v2: "en" / "ru" as sent by the service, else empty
    double       retryAfterS = -1.0;   // locked-out: seconds left; -1 when absent
    int          version = 0;          // "v" of the reply; 0 when absent

    // 8b F-25: every copy of the password is wiped on destruction, on every path.
    ~UnlockReply() {
        if (!password.empty()) SecureZeroMemory(&password[0], password.size() * sizeof(wchar_t));
    }
};

// 8b F-48: fixed caps, in UTF-16 code units. A reply over a cap, or with an embedded NUL in a
// credential field, is "malformed-response".
constexpr size_t kMaxUsernameChars = 256;
constexpr size_t kMaxDomainChars   = 256;
constexpr size_t kMaxPasswordChars = 1024;
constexpr size_t kMaxPromptChars   = 120;   // gesture prompt shown on the secure-desktop tile

// ---------------------------------------------------------------------------
// Tile texts (R3): an honest text per failure class, in English and Russian. The language is
// the reply's "lang", else the system UI language, else English.
// ---------------------------------------------------------------------------
enum class Text {
    Label,            // "Face Unlock" (large text under the tile image)
    PressArrow,       // idle, selected
    Scanning,
    Verified,         // degraded path: no provider sink, a second click signs in
    PackingFailed,
    NotRecognised,    // no-match, no-face, gesture-failed, motion-before-prompt, screen-suspected
    LockedOut,        // locked-out without a usable retry_after_s
    LockedOutSecs,    // locked-out with seconds: contains one %u
    Unavailable,      // pipe-unavailable: no pipe (the service is not running) or a dead connection
    NoPassword,       // no-credentials
    NoEnrollment,     // no-enrollment
    CameraBusy,       // camera-busy, camera-error, no-frames
    TooDark,          // too-dark
    PasswordRejected, // password-rejected, or Windows rejected the password just now
    UpdateNeeded,     // version-mismatch
    NeedsAttention,   // refusing service: not-owner, custody, no-models, lockout-store-error
    Failed,           // anything else: timeouts, engine faults, malformed replies, unknown reasons
    // 9d (V-04): the transport failures no longer share one "service is not running" text.
    ServiceTimeout,   // pipe-timeout: connected, but no reply inside the budget
    ServiceBusy,      // pipe-busy: every instance stayed busy for the whole budget
    ServiceUntrusted, // server-untrusted / pipe-access-denied: reinstall
    // 9d (V-03): Windows refused the sign-in for a reason other than the password.
    SignInFailed,
    GestureFallback,  // needs-gesture without a prompt: "Move your head as asked"
};

// "ru" or "en" for a reply language; an unknown or empty value falls back to the system UI
// language (Russian -> "ru"), else "en".
std::string ResolveLang(const std::string& replyLang);

// The text for `id` in `lang` ("ru" / anything else = English).
std::wstring TileText(Text id, const std::string& lang);

// The phase-2 instruction built on the CP side from the reply's `gesture` field -- one or more of
// turn_left / turn_right / nod, comma-separated, in the order they must be performed. Used when
// the reply carried no prompt. Empty for anything it does not recognise.
std::wstring GesturePromptFromKinds(const std::string& kinds, const std::string& lang);

// Map a reply (reason + retry_after_s) to the text the tile shows.
std::wstring FailureText(const std::string& reason, double retryAfterS, const std::string& lang);

// The class a reason falls into (exposed so the offline test can pin the map).
Text FailureClass(const std::string& reason);

// R2: drop C0 (< 0x20), DEL, C1 (U+0080..U+009F), the bidi controls U+061C (ALM, 9d V-07),
// U+200E..U+200F, U+202A..U+202E, U+2066..U+2069, and U+2028 / U+2029; then cap at kMaxPromptChars without
// splitting a surrogate pair.
std::wstring SanitizePromptText(const std::wstring& text);

// True iff `s` is 1..64 hex digits: the only shape of token / grant id that may be pasted into a
// request.
bool IsHexToken(const std::string& s);

// ---------------------------------------------------------------------------
// Requests (exposed for the offline test).
// ---------------------------------------------------------------------------
std::string BuildUnlockRequest(DWORD budgetMs);
std::string BuildGestureRequest(const std::string& token, DWORD budgetMs);   // token must be hex
std::string BuildReportRequest(const std::string& grantId, bool ok);          // grant id must be hex

// Parse a raw unlock / unlock_gesture reply. Returns true ONLY for a well-formed protocol-v2
// ok=true reply with a non-empty username and password and a hex grant_id. On failure out.reason
// is the service's reason, or "malformed-response" (not JSON, invalid UTF-8, a field of the wrong
// shape, ok:false without a reason) or "version-mismatch" (the reply is not protocol v2 -- then
// no credential is kept even if ok was true). Optional fields are filled whenever present.
bool ParseUnlockReply(const std::string& response, UnlockReply& out);

// Parse a report_result reply: true iff {"ok":true,...} with "v":2.
bool ParseReportReply(const std::string& response);

// ---------------------------------------------------------------------------
// Owner and transport.
// ---------------------------------------------------------------------------

// R1: the owner SID recorded by the installer (HKLM\Software\WindowsFaceUnlock\OriginalUserSid).
// False when it is missing or not a person's SID (S-1-5-21-* / S-1-12-1-*).
bool ReadOwnerSid(std::wstring& out);

// True for S-1-5-21-* and S-1-12-1-* (a person's account: local, domain or Entra ID).
bool IsPersonSid(const std::wstring& sid);

// 9d (V-05): the canonical string form of a SID string (ConvertStringSidToSid, then back), so
// "S-1-5-21-0123-..." and "S-1-5-21-123-..." are one account. False when it is not a SID.
bool CanonicalSidString(const std::wstring& sid, std::wstring& out);

// 9d (V-05): true iff both strings parse as SIDs and EqualSid says they are the same.
bool SidStringsEqual(const std::wstring& a, const std::wstring& b);

// Instant check that the service pipe exists (no connection is made).
bool ServicePipeExists();

// 9d (V-04): Timeout (connected, no reply in the budget), Busy (all instances busy for the whole
// budget) and AccessDenied (the pipe refused the open) are their own outcomes now.
enum class CallStatus { Ok, Unavailable, Untrusted, Oversize, Cancelled, Timeout, Busy, AccessDenied };

// One request/reply on `pipeName`. `buildRequest(remainingMs)` is called after the connect and the
// identity check, with the budget left at that moment. The server must run as `ownerSid` and the
// pipe object must be owned by it -- checked BEFORE anything is written. Every wait watches
// `cancelEvent` (may be null). The reply may carry a password: the caller must wipe `response`.
// A missing pipe is retried for at most `notFoundMs` of the budget (INFINITE = the whole budget).
using RequestBuilder = std::string (*)(DWORD remainingMs, const void* ctx);
CallStatus PipeCall(const wchar_t* pipeName, const std::wstring& ownerSid,
                    RequestBuilder buildRequest, const void* ctx,
                    std::string& response, DWORD timeoutMs, HANDLE cancelEvent,
                    ServerTrust* trust, DWORD notFoundMs = INFINITE);

// The reason token a transport outcome is reported as (exposed for the offline test):
// Untrusted -> server-untrusted, AccessDenied -> pipe-access-denied, Timeout -> pipe-timeout,
// Busy -> pipe-busy, Oversize -> malformed-response, Cancelled -> cancelled, else pipe-unavailable.
const char* ReasonFor(CallStatus st);

// Phase 1 / phase 2 / report. Any trust, transport or parse error -> false with out.reason set
// (a ReasonFor() token, "no-owner", or the service's reason).
bool RequestUnlock(UnlockReply& out, HANDLE cancelEvent, ServerTrust* trust = nullptr);
bool RequestUnlockGesture(const std::string& token, UnlockReply& out, HANDLE cancelEvent,
                          ServerTrust* trust = nullptr);
bool SendReportResult(const std::string& grantId, bool ok, HANDLE cancelEvent);

#ifdef FACEUNLOCK_TESTING
// Test builds only: replace the registry owner with `sid` (empty = back to the registry) and the
// pipe name used by RequestUnlock & co.
void TestSetOwnerOverride(const std::wstring& sid);
void TestSetPipeName(const std::wstring& name);
#endif

}  // namespace FaceUnlock
