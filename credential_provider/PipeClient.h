#pragma once
#include <windows.h>
#include <string>

// Minimal blocking named-pipe client matching the Python FaceService protocol.
// Sends a single JSON request and reads a single JSON reply. The timeout bounds the whole call:
// since 8b (F-03) the connect wait is capped by the remaining budget and is never 0.
namespace FaceUnlock {

// Outcome of the client-side pipe-server identity check (defense-in-depth
// against a squatter owning the pipe name). The real request path enforces the
// check on its own connection; a caller (e.g. the diagnostic harness) may pass
// one of these to observe the result.
struct ServerTrust {
    bool checked = false;      // was the identity check performed
    bool trusted = false;      // did the server's owner SID match an accepted owner
    std::wstring serverSid;    // server process owner SID (string form), if resolved
};

// Everything an unlock reply can carry. Grew a phase-1 gesture section in Stage 7-i: the
// service can now answer reason "needs-gesture" instead of a flat "no-match", asking the
// lockscreen to run an active gesture and come back with the token.
//
// `prompt` is wide because it is display text and may be non-ASCII (the service localizes it
// and json.dumps sends non-ASCII as \uXXXX, which the parser decodes). `gesture` and `token`
// stay narrow: they are ASCII by construction and go straight back out in the phase-2 request
// JSON. All three are EMPTY unless the reply actually carried them -- the parser never invents
// a value, so a caller must treat "needs-gesture with an empty token" as a plain failure.
struct UnlockReply {
    std::wstring username;   // valid only when ParseUnlockReply returned true
    std::wstring password;   // valid only when ParseUnlockReply returned true
    std::wstring domain;     // "." when the reply omitted it
    std::string  reason;     // failure reason token, or a parse error; empty on success
    std::string  gesture;    // "blink" | "turn_left" | "turn_right" | "nod", else empty
    std::wstring prompt;     // localized instruction to show on the tile, else empty
                             // (sanitized: no C0/DEL, at most kMaxPromptChars)
    std::string  token;      // 32 hex chars, replayed in the phase-2 request, else empty

    // 8b F-25: every UnlockReply copy of the password (worker locals, back-compat wrappers)
    // used to be freed unwiped -> plaintext residue in the LogonUI heap -> wipe on destruction,
    // on every path including an exception unwinding through the owner.
    ~UnlockReply() {
        if (!password.empty()) SecureZeroMemory(&password[0], password.size() * sizeof(wchar_t));
    }
};

// 8b F-48: server-supplied fields had no length bounds -> oversized or NUL-bearing values
// reached KerbPack (USHORT truncation) and the tile -> fixed caps, in UTF-16 code units.
// A reply over a cap, or with an embedded NUL in a credential field, is "malformed-response".
constexpr size_t kMaxUsernameChars = 256;
constexpr size_t kMaxDomainChars   = 256;
constexpr size_t kMaxPasswordChars = 1024;
constexpr size_t kMaxPromptChars   = 120;   // gesture prompt shown on the secure-desktop tile

// 8b F-48/F-04: one failure text for every reason -> a dead service looked like a face
// mismatch -> a few fixed texts chosen by reason. The server never supplies failure text.
// Kept here (not in FaceCredential) so the offline parser test can reach the mapping.
inline constexpr wchar_t kTextNotRecognised[] = L"Face not recognised. Use password tile instead.";
inline constexpr wchar_t kTextLockedOut[]     = L"Face sign-in temporarily locked. Use PIN or password.";
inline constexpr wchar_t kTextUnavailable[]   = L"Face Unlock service unavailable. Use PIN or password.";
inline constexpr wchar_t kTextPasswordRejected[] =
    L"Stored password was rejected \u2014 sign in with PIN and update it in Face Unlock";

// Map a failure reason token to one of the fixed texts above. Unknown reasons (including any
// future service reason such as "insecure-data-dir") fall back to kTextUnavailable.
const wchar_t* FailureTextForReason(const std::string& reason);

// Drop C0 controls (< 0x20) and DEL, then cap at kMaxPromptChars without splitting a
// surrogate pair. Applied by ParseUnlockReply to the gesture prompt.
std::wstring SanitizePromptText(const std::wstring& text);

// True iff `s` is 1..64 hex digits: the only shape of phase-1 token that may be pasted back
// into the phase-2 request. Exposed for the offline test.
bool IsHexToken(const std::string& s);

// Returns true on success; on success, fills `response` with the raw reply JSON. The reply
// may carry a password: the caller must wipe `response` after use.
// When verifyServer is true the server's owner SID is checked BEFORE anything is
// sent; on an untrusted server PipeCall returns false without writing the request
// (and, if `trust` is non-null, reports checked=true/trusted=false).
bool PipeCall(const std::wstring& pipeName,
              const std::string& requestJson,
              std::string& response,
              DWORD timeoutMs = 30000,
              bool verifyServer = false,
              ServerTrust* trust = nullptr);

// Convenience: assemble {"cmd":"unlock"} and fill `out`. Always verifies the server
// identity first. Any parse/trust/transport error -> returns false with out.reason set
// to a short token ("server-untrusted", "pipe-unavailable", or whatever the service
// said -- including "needs-gesture", in which case out.gesture/prompt/token carry the
// phase-1 challenge). `trust`, if non-null, receives the identity-check outcome.
bool RequestUnlock(UnlockReply& out, ServerTrust* trust = nullptr);

// Stage 7-i phase 2: replay the token from a "needs-gesture" reply so the service runs the
// identity-bound gesture round, and collect the credentials it grants. Verifies the server
// identity exactly like RequestUnlock. Bounded by its OWN, longer timeout: the round waits on
// a human, so the passive-unlock budget does not apply. `token` must be hex (that is what the
// service issues); anything else is refused locally with reason "gesture-token-invalid" rather
// than being pasted into the request JSON.
bool RequestUnlockGesture(const std::string& token,
                          UnlockReply& out,
                          ServerTrust* trust = nullptr);

// Back-compat overload: the pre-Stage-7-i signature, kept so the diagnostic harness and
// existing callers need no change. Thin wrapper over the UnlockReply form; the gesture
// fields are simply dropped.
bool RequestUnlock(std::wstring& username,
                   std::wstring& password,
                   std::wstring& domain,
                   std::string& errorOut,
                   ServerTrust* trust = nullptr);

// Parse a raw unlock reply (the JSON the service returns). Exposed so the offline
// unit test can exercise the parser without a live pipe or service. Returns true
// ONLY for a well-formed ok=true reply that carries a non-empty username AND
// password; on any failure returns false with out.reason set to a short token
// ("malformed-response", "no-match", or the service-supplied reason string).
// Gesture fields are filled whenever the reply carried them, ok or not.
bool ParseUnlockReply(const std::string& response, UnlockReply& out);

// Back-compat wrapper over ParseUnlockReply with the pre-Stage-7-i signature, so the
// existing unit tests and the harness keep compiling unchanged.
bool ParseUnlockResponse(const std::string& response,
                         std::wstring& username,
                         std::wstring& password,
                         std::wstring& domain,
                         std::string& errorOut);

}  // namespace FaceUnlock
