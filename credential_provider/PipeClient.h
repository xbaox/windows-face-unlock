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

// Convenience: assemble {"cmd":"unlock"} and parse out username/password/domain.
// Always verifies the server identity first. Any parse/trust/transport error ->
// returns false (errorOut carries a short reason, e.g. "server-untrusted").
// `trust`, if non-null, receives the identity-check outcome for diagnostics.
bool RequestUnlock(std::wstring& username,
                   std::wstring& password,
                   std::wstring& domain,
                   std::string& errorOut,
                   ServerTrust* trust = nullptr);

// Parse a raw unlock reply (the JSON the service returns) into credentials.
// Exposed so the offline unit test can exercise the parser without a live pipe
// or service. Returns true ONLY for a well-formed ok=true reply that carries a
// non-empty username AND password; on any failure returns false and sets
// errorOut to a short reason ("malformed-response", "no-match", or the
// service-supplied reason string).
bool ParseUnlockResponse(const std::string& response,
                         std::wstring& username,
                         std::wstring& password,
                         std::wstring& domain,
                         std::string& errorOut);

}  // namespace FaceUnlock
