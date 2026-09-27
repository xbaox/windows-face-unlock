// 9d cases for test_parser.cpp (included once, after its helpers). They drive the REAL
// FaceCredential (FaceCredential.cpp is part of the test build) against the in-process fake
// server, and the transport outcomes of PipeCall that 9d split apart.
//   V-01 (A-1)  a fresh result survives SetDeselected and UnAdvise, lives 10 s, is used once
//   V-02 (A-2)  report_result is synchronous inside ReportResult and bounded by 750 ms
//   V-03        the scans-off latch keeps its class: password vs any other refusal
//   V-04        timeout / busy / access denied / absent are distinct reasons and texts
//   V-05        an owner SID with leading zeros is the same account (EqualSid)
//   V-07        U+061C is sanitized; a busy-pipe wait sees the cancel event within 200 ms slices
//   V-08        field table: the provider label GUID sits on a hidden small text
//   W-05 (r2)   an UnAdvise right after the publish (inside CredentialsChanged) does not wait

#include "../FaceCredential.h"

// dll.cpp is not linked into the test: the credential's DLL reference is a no-op here.
namespace FaceUnlock {
void DllAddRef() {}
void DllRelease() {}
}  // namespace FaceUnlock

namespace t9d {

using namespace FaceUnlock;

const char* kGrant = R"({"v":2,"lang":"en","ok":true,"username":"u","password":"p","grant_id":"abcd"})";

bool WaitUntil(const std::function<bool()>& cond, DWORD ms) {
    const DWORD t0 = GetTickCount();
    while (GetTickCount() - t0 < ms) {
        if (cond()) return true;
        Sleep(20);
    }
    return cond();
}

// A server thread that answers ONE connection (ServeOnce) -- started before the client acts.
struct Serving {
    FakeServer srv;
    std::thread t;
    Serving(const std::wstring& pipe, const std::string& reply, bool silent = false) {
        srv.name = pipe;
        srv.reply = reply;
        srv.silent = silent;
        srv.ready = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        t = std::thread(ServeOnce, &srv);
        WaitForSingleObject(srv.ready, 5000);
    }
    ~Serving() {
        HANDLE poke = CreateFileW(srv.name.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                                  OPEN_EXISTING, 0, nullptr);
        if (poke != INVALID_HANDLE_VALUE) CloseHandle(poke);
        if (t.joinable()) t.join();
        CloseHandle(srv.ready);
    }
};

struct Ser {
    CREDENTIAL_PROVIDER_GET_SERIALIZATION_RESPONSE r = CPGSR_NO_CREDENTIAL_NOT_FINISHED;
    CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION cs{};
    PWSTR text = nullptr;
    CREDENTIAL_PROVIDER_STATUS_ICON icon = CPSI_NONE;
    HRESULT hr = E_FAIL;
    ~Ser() {
        if (cs.rgbSerialization) KerbUnpackFree(&cs);
        if (text) CoTaskMemFree(text);
    }
};

void GetSer(FaceCredential* c, Ser& s) {
    s.hr = c->GetSerialization(&s.r, &s.cs, &s.text, &s.icon);
}

std::wstring Status(FaceCredential* c) {
    PWSTR p = nullptr;
    std::wstring out;
    if (SUCCEEDED(c->GetStringValue(FIELD_STATUS, &p)) && p) {
        out = p;
        CoTaskMemFree(p);
    }
    return out;
}

// A credential with a published result: select, press the arrow (GetSerialization starts the
// worker), let the fake server grant, wait until the result is there.
FaceCredential* Published(const std::wstring& pipe, const std::wstring& owner, bool& ok) {
    auto events = std::make_shared<ProviderEvents>();
    FaceCredential* c = new FaceCredential(events, owner);
    c->Initialize(CPUS_UNLOCK_WORKSTATION);
    BOOL al = FALSE;
    c->SetSelected(&al);
    {
        Serving sv(pipe, kGrant);
        Ser s;
        GetSer(c, s);
        ok = SUCCEEDED(s.hr) && s.r == CPGSR_NO_CREDENTIAL_NOT_FINISHED &&
             WaitUntil([&] { return c->HasResult(); }, 5000);
    }
    return c;
}

// 9d-r2 (W-05): a provider-events sink whose CredentialsChanged -- called by the worker right
// after the publish, before the TTL wait -- runs UnAdvise on ANOTHER thread (as LogonUI would)
// and times it. Before W-05 the worker still counted as scanning there, so StopWorker waited
// kJoinBoundMs for a worker that was itself blocked in this call.
struct TimingSink : ICredentialProviderEvents {
    FaceCredential* cred = nullptr;
    DWORD unadviseMs = MAXDWORD;
    IFACEMETHODIMP QueryInterface(REFIID, void** ppv) override { *ppv = nullptr; return E_NOINTERFACE; }
    IFACEMETHODIMP_(ULONG) AddRef() override { return 2; }
    IFACEMETHODIMP_(ULONG) Release() override { return 1; }
    IFACEMETHODIMP CredentialsChanged(UINT_PTR) override {
        std::thread t([this] {
            const DWORD t0 = GetTickCount();
            cred->UnAdvise();
            unadviseMs = GetTickCount() - t0;
        });
        t.join();
        return S_OK;
    }
};

// Stop a scan that a GetSerialization without a result started (no server behind it).
void Quiet(FaceCredential* c) {
    c->SetDeselected();
}

void CredentialTests(const std::wstring& self) {
    const std::wstring pipe = L"\\\\.\\pipe\\FaceUnlockCpTest9d-" + std::to_wstring(GetCurrentProcessId());
    TestSetPipeName(pipe);
    TestSetOwnerOverride(self);

    {   // V-01: publish -> Deselect -> Select -> autologon exactly once
        bool ok = false;
        FaceCredential* c = Published(pipe, self, ok);
        Check("9d V-01: a scan publishes a result (fake server grant)", ok);
        c->SetDeselected();
        Check("9d V-01 (A-1): the fresh result survives SetDeselected", c->HasResult());
        BOOL al = TRUE;
        c->SetSelected(&al);
        Check("9d V-01: SetSelected never auto-logs on by itself", al == FALSE);
        Ser s1;
        GetSer(c, s1);
        Check("9d V-01: Deselect -> Select -> GetSerialization returns the credential",
              SUCCEEDED(s1.hr) && s1.r == CPGSR_RETURN_CREDENTIAL_FINISHED && s1.cs.rgbSerialization);
        Check("9d V-01: ... and HasResult is gone after that one use", !c->HasResult());

        // V-02: ReportResult is synchronous and bounded -- the server reads and never answers.
        DWORD took = 0;
        std::string got;
        {
            Serving sv(pipe, "", /*silent=*/true);
            PWSTR t = nullptr;
            CREDENTIAL_PROVIDER_STATUS_ICON ic;
            const DWORD t0 = GetTickCount();
            c->ReportResult(0 /*STATUS_SUCCESS*/, 0, &t, &ic);
            took = GetTickCount() - t0;
            got = sv.srv.received;
        }
        Check("9d V-02 (A-2): ReportResult sent report_result itself, before returning",
              got == R"({"cmd":"report_result","v":2,"grant_id":"abcd","ok":true})");
        Check("9d V-02 (A-2): a server that reads but never answers -> ReportResult back in <= 800 ms",
              took <= 800);
        std::printf("        (ReportResult against a silent server: %lu ms)\n", took);

        Ser s2;
        GetSer(c, s2);
        Check("9d V-01: a second GetSerialization gets no credential (one-shot)",
              SUCCEEDED(s2.hr) && s2.r != CPGSR_RETURN_CREDENTIAL_FINISHED && !s2.cs.rgbSerialization);
        Quiet(c);
        c->Release();
    }
    {   // V-01: publish -> UnAdvise -> Advise -> autologon exactly once
        bool ok = false;
        FaceCredential* c = Published(pipe, self, ok);
        const DWORD t0 = GetTickCount();
        c->UnAdvise();
        const DWORD took = GetTickCount() - t0;
        Check("9d V-01: UnAdvise does not wait for a worker that only holds the TTL (< 300 ms)",
              ok && took < 300);
        Check("9d V-01 (A-1): the fresh result survives UnAdvise", c->HasResult());
        c->Advise(nullptr);
        BOOL al = FALSE;
        c->SetSelected(&al);
        Ser s1, s2;
        GetSer(c, s1);
        Check("9d V-01: UnAdvise -> Advise -> GetSerialization returns the credential",
              s1.r == CPGSR_RETURN_CREDENTIAL_FINISHED && s1.cs.rgbSerialization);
        GetSer(c, s2);
        Check("9d V-01: ... exactly once", s2.r != CPGSR_RETURN_CREDENTIAL_FINISHED);
        Quiet(c);
        c->Release();
    }
    {   // W-05: UnAdvise between the publish and the TTL wait
        auto events = std::make_shared<ProviderEvents>();
        TimingSink sink;
        events->Set(&sink, 0);
        FaceCredential* c = new FaceCredential(events, self);
        sink.cred = c;
        c->Initialize(CPUS_UNLOCK_WORKSTATION);
        BOOL al = FALSE;
        c->SetSelected(&al);
        bool ok = false;
        {
            Serving sv(pipe, kGrant);
            Ser s;
            GetSer(c, s);
            ok = WaitUntil([&] { return sink.unadviseMs != MAXDWORD; }, 5000);
        }
        Check("9d-r2 W-05: CredentialsChanged ran after the publish", ok && c->HasResult());
        Check("9d-r2 W-05: UnAdvise right after the publish does not wait for the worker (< 300 ms)",
              ok && sink.unadviseMs < 300);
        std::printf("        (UnAdvise inside CredentialsChanged: %lu ms)\n", sink.unadviseMs);
        events->Clear();
        Quiet(c);
        c->Release();
    }
    {   // V-03: a non-password refusal latches with "sign-in failed", not "password rejected"
        bool ok = false;
        FaceCredential* c = Published(pipe, self, ok);
        Ser s;
        GetSer(c, s);
        PWSTR t = nullptr;
        CREDENTIAL_PROVIDER_STATUS_ICON ic;
        c->ReportResult((NTSTATUS)0xC000006EL /*STATUS_ACCOUNT_RESTRICTION*/, 0, &t, &ic);
        BOOL al = FALSE;
        c->SetDeselected();
        c->SetSelected(&al);
        Check("9d V-03: account restriction -> 'Sign-in failed' on SetSelected",
              ok && Status(c) == TileText(Text::SignInFailed, "en"));
        Ser s2;
        GetSer(c, s2);
        Check("9d V-03: ... and on GetSerialization; no credential, no scan",
              s2.r != CPGSR_RETURN_CREDENTIAL_FINISHED && Status(c) == TileText(Text::SignInFailed, "en") &&
              !c->HasResult());
        c->Release();
    }
    {   // V-03: a password refusal latches with "password rejected" (and is reported ok=false)
        bool ok = false;
        FaceCredential* c = Published(pipe, self, ok);
        Ser s;
        GetSer(c, s);
        std::string got;
        {
            Serving sv(pipe, R"({"v":2,"lang":"en","ok":true})");
            PWSTR t = nullptr;
            CREDENTIAL_PROVIDER_STATUS_ICON ic;
            c->ReportResult((NTSTATUS)0xC000006AL /*STATUS_WRONG_PASSWORD*/, 0, &t, &ic);
            got = sv.srv.received;
        }
        BOOL al = FALSE;
        c->SetSelected(&al);
        Check("9d V-03: wrong password -> 'Windows rejected the saved password'",
              ok && Status(c) == TileText(Text::PasswordRejected, "en"));
        Check("9d V-03: ... reported to the service as ok=false, synchronously",
              got == R"({"cmd":"report_result","v":2,"grant_id":"abcd","ok":false})");
        c->Release();
    }
    {   // V-01: the result lives 10 s at most
        bool ok = false;
        FaceCredential* c = Published(pipe, self, ok);
        Sleep(FaceCredential::kResultTtlMs + 400);
        Check("9d V-01: 10 s after the publish there is no result", ok && !c->HasResult());
        Ser s;
        GetSer(c, s);
        Check("9d V-01: ... and GetSerialization returns none", s.r != CPGSR_RETURN_CREDENTIAL_FINISHED);
        Quiet(c);
        c->Release();
    }
    TestSetPipeName(L"");
    TestSetOwnerOverride(L"");
}

std::string BuildPing(DWORD, const void*) { return R"({"cmd":"ping"})"; }

void TransportTests9d(const std::wstring& self) {
    const std::wstring pipe = L"\\\\.\\pipe\\FaceUnlockCpTest9dT-" + std::to_wstring(GetCurrentProcessId());
    TestSetPipeName(pipe);
    TestSetOwnerOverride(self);

    {   // V-02: report_result against a silent server and against no server
        DWORD took = 0;
        bool ok = true;
        {
            Serving sv(pipe, "", true);
            const DWORD t0 = GetTickCount();
            ok = SendReportResult("abcd", true, nullptr);
            took = GetTickCount() - t0;
        }
        Check("9d V-02: report to a server that reads and never answers -> false in <= 800 ms",
              !ok && took <= 800);
        const DWORD t1 = GetTickCount();
        ok = SendReportResult("abcd", true, nullptr);
        const DWORD took2 = GetTickCount() - t1;
        Check("9d V-02: report with no server at all -> false in < 400 ms", !ok && took2 < 400);
        std::printf("        (silent server %lu ms, no server %lu ms)\n", took, took2);
    }
    {   // V-04: connected, no reply inside the budget -> pipe-timeout
        std::string resp;
        CallStatus st = CallStatus::Ok;
        {
            Serving sv(pipe, "", true);
            st = PipeCall(pipe.c_str(), self, &BuildPing, nullptr, resp, 500, nullptr, nullptr);
        }
        Check("9d V-04: silent server -> Timeout -> 'pipe-timeout'",
              st == CallStatus::Timeout && std::string(ReasonFor(st)) == "pipe-timeout");
    }
    {   // V-04 / V-07: every instance busy -> pipe-busy; the busy wait sees a cancel quickly
        Serving sv(pipe, "", true);
        HANDLE holder = CreateFileW(pipe.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                                    OPEN_EXISTING, 0, nullptr);
        std::string resp;
        const CallStatus st = PipeCall(pipe.c_str(), self, &BuildPing, nullptr, resp, 600, nullptr, nullptr);
        Check("9d V-04: the only instance stays busy for the whole budget -> 'pipe-busy'",
              holder != INVALID_HANDLE_VALUE && st == CallStatus::Busy &&
              std::string(ReasonFor(st)) == "pipe-busy");
        HANDLE cancel = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        std::thread canceller([&] { Sleep(150); SetEvent(cancel); });
        const DWORD t0 = GetTickCount();
        const CallStatus st2 = PipeCall(pipe.c_str(), self, &BuildPing, nullptr, resp, 10000, cancel, nullptr);
        const DWORD took = GetTickCount() - t0;
        canceller.join();
        CloseHandle(cancel);
        Check("9d V-07: busy pipe + cancel -> 'cancelled' within 400 ms (<= 200 ms slices)",
              st2 == CallStatus::Cancelled && took < 400);
        if (holder != INVALID_HANDLE_VALUE) CloseHandle(holder);
    }
    {   // V-04: the pipe refuses the open -> pipe-access-denied (reinstall text), not "unavailable"
        PSECURITY_DESCRIPTOR sd = nullptr;
        ConvertStringSecurityDescriptorToSecurityDescriptorW(L"D:P(A;;GA;;;SY)", SDDL_REVISION_1, &sd, nullptr);
        SECURITY_ATTRIBUTES sa{ sizeof(sa), sd, FALSE };
        HANDLE h = CreateNamedPipeW(pipe.c_str(), PIPE_ACCESS_DUPLEX,
                                    PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT, 1,
                                    4096, 4096, 0, &sa);
        std::string resp;
        const DWORD t0 = GetTickCount();
        const CallStatus st = PipeCall(pipe.c_str(), self, &BuildPing, nullptr, resp, 3000, nullptr, nullptr);
        const DWORD took = GetTickCount() - t0;
        Check("9d V-04: access denied on open -> 'pipe-access-denied' at once",
              h != INVALID_HANDLE_VALUE && st == CallStatus::AccessDenied && took < 500 &&
              std::string(ReasonFor(st)) == "pipe-access-denied");
        if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
        LocalFree(sd);
    }
    {   // V-04: no pipe at all -> pipe-unavailable
        std::string resp;
        const CallStatus st = PipeCall(pipe.c_str(), self, &BuildPing, nullptr, resp, 400, nullptr, nullptr);
        Check("9d V-04: no pipe -> 'pipe-unavailable'",
              st == CallStatus::Unavailable && std::string(ReasonFor(st)) == "pipe-unavailable");
    }
    {   // V-05: the recorded owner written with leading zeros is the same account
        std::wstring padded = self;
        const std::wstring pre = L"S-1-5-21-";
        if (padded.rfind(pre, 0) == 0) padded.insert(pre.size(), L"00");
        std::wstring canon;
        Check("9d V-05: CanonicalSidString drops leading zeros",
              padded != self && CanonicalSidString(padded, canon) && canon == self);
        Check("9d V-05: SidStringsEqual(padded, canonical) and not a different SID",
              SidStringsEqual(padded, self) &&
              !SidStringsEqual(self, L"S-1-5-21-1111111111-2222222222-3333333333-1001") &&
              !SidStringsEqual(L"garbage", self));
        TestSetOwnerOverride(padded);
        std::wstring owner;
        Check("9d V-05: ReadOwnerSid returns the canonical form", ReadOwnerSid(owner) && owner == self);
        UnlockReply r;
        ServerTrust tr;
        bool ok = false;
        {
            Serving sv(pipe, kGrant);
            ok = RequestUnlock(r, nullptr, &tr);
        }
        Check("9d V-05: a server run by the owner is trusted although the record has leading zeros",
              ok && tr.trusted);
        TestSetOwnerOverride(self);
    }
    TestSetPipeName(L"");
    TestSetOwnerOverride(L"");
}

void StaticTests9d() {
    // V-04 classes and texts
    Check("9d V-04: pipe-unavailable / pipe-timeout / pipe-busy / untrusted have their own texts",
          FailureClass("pipe-unavailable") == Text::Unavailable &&
          FailureClass("pipe-timeout") == Text::ServiceTimeout &&
          FailureClass("pipe-busy") == Text::ServiceBusy &&
          FailureClass("server-untrusted") == Text::ServiceUntrusted &&
          FailureClass("pipe-access-denied") == Text::ServiceUntrusted);
    Check("9d V-04: 'unavailable' says unavailable, not 'not running' (EN/RU)",
          TileText(Text::Unavailable, "en") == L"Face Unlock service is unavailable. Use PIN or password." &&
          TileText(Text::Unavailable, "ru") == L"Служба Face Unlock недоступна. Войдите по PIN-коду или паролю.");
    Check("9d V-04: the untrusted text asks for a reinstall (EN/RU)",
          TileText(Text::ServiceUntrusted, "en").find(L"reinstall") != std::wstring::npos &&
          TileText(Text::ServiceUntrusted, "ru").find(L"переустановите") != std::wstring::npos);
    Check("9d V-03: the sign-in-failed text (EN/RU)",
          TileText(Text::SignInFailed, "en") == L"Sign-in failed — use PIN or password." &&
          TileText(Text::SignInFailed, "ru") == L"Вход не выполнен — используйте PIN или пароль.");
    // V-07
    CasePrompt("9d V-07: U+061C (ALM) stripped", "a\\u061Cb", L"ab");
    // V-08 field table
    int labelGuid = 0, logoGuid = 0;
    bool labelOk = false, largeOk = false;
    for (DWORD i = 0; i < FIELD_COUNT; ++i) {
        const auto& d = s_FieldDescriptors[i];
        if (d.dwFieldID != i) labelOk = false;
        if (IsEqualGUID(d.guidFieldType, ProviderLabelFieldGuid())) {
            ++labelGuid;
            labelOk = d.cpft == CPFT_SMALL_TEXT && s_FieldStatePairs[i].cpfs == CPFS_HIDDEN;
        }
        if (IsEqualGUID(d.guidFieldType, ProviderLogoFieldGuid())) ++logoGuid;
        if (d.cpft == CPFT_LARGE_TEXT)
            largeOk = IsEqualGUID(d.guidFieldType, GUID_NULL) &&
                      s_FieldStatePairs[i].cpfs != CPFS_HIDDEN;
    }
    Check("9d V-08: exactly one field carries CPFG_CREDENTIAL_PROVIDER_LABEL: a hidden CPFT_SMALL_TEXT",
          labelGuid == 1 && labelOk);
    Check("9d V-08: the visible CPFT_LARGE_TEXT 'Face Unlock' carries no field GUID", largeOk);
    Check("9d V-08: the logo GUID stays on the tile image",
          logoGuid == 1 && s_FieldDescriptors[FIELD_TILE_IMAGE].cpft == CPFT_TILE_IMAGE &&
          IsEqualGUID(s_FieldDescriptors[FIELD_TILE_IMAGE].guidFieldType, ProviderLogoFieldGuid()));
    Check("9d V-02: report budget is 750 ms, a missing pipe gives up after 250 ms",
          kReportTimeoutMs == 750 && kReportNotFoundMs == 250);
}

}  // namespace t9d
