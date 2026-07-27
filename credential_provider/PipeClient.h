#pragma once
#include <windows.h>
#include <string>

// Minimal blocking named-pipe client matching the Python FaceService protocol.
// Sends a single JSON request and reads a single JSON reply. Timeout is best-effort.
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
    std::string  token;      // 32 hex chars, replayed in the phase-2 request, else empty
};

// Returns true on success; on success, fills `response` with the raw reply JSON.
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
