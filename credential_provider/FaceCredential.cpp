#include "FaceCredential.h"
#include "helpers.h"
#include "PipeClient.h"
#include "guid.h"
#include "resource.h"
#include <new>

EXTERN_C IMAGE_DOS_HEADER __ImageBase;

namespace FaceUnlock {

static HRESULT AllocStr(PCWSTR src, PWSTR* dst) {
    size_t cch = wcslen(src) + 1;
    *dst = (PWSTR)CoTaskMemAlloc(cch * sizeof(WCHAR));
    if (!*dst) return E_OUTOFMEMORY;
    wcscpy_s(*dst, cch, src);
    return S_OK;
}

// Wipe a secret in place before releasing it. std::wstring storage is contiguous, so the
// buffer really is the one that held the plaintext.
static void ZeroString(std::wstring& s) {
    if (!s.empty()) SecureZeroMemory(&s[0], s.size() * sizeof(wchar_t));
    s.clear();
}

// NTSTATUS values after which the stored password itself is known to be unusable.
static bool IsPasswordFailure(NTSTATUS s) {
    return s == (NTSTATUS)0xC000006DL ||   // STATUS_LOGON_FAILURE
           s == (NTSTATUS)0xC000006AL ||   // STATUS_WRONG_PASSWORD
           s == (NTSTATUS)0xC0000071L ||   // STATUS_PASSWORD_EXPIRED
           s == (NTSTATUS)0xC0000224L;     // STATUS_PASSWORD_MUST_CHANGE
}

// ---------------------------------------------------------------------------
// ProviderEvents
// ---------------------------------------------------------------------------

void ProviderEvents::Set(ICredentialProviderEvents* pEvents, UINT_PTR context) {
    ICredentialProviderEvents* doomed = nullptr;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        doomed = m_pEvents;
        m_pEvents = pEvents;
        if (m_pEvents) m_pEvents->AddRef();
        m_context = context;
    }
    // Released outside the lock: a final Release runs foreign code.
    if (doomed) doomed->Release();
}

void ProviderEvents::Clear() {
    ICredentialProviderEvents* doomed = nullptr;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        doomed = m_pEvents;
        m_pEvents = nullptr;
        m_context = 0;
    }
    if (doomed) doomed->Release();
}

bool ProviderEvents::NotifyCredentialsChanged() {
    ICredentialProviderEvents* e = nullptr;
    UINT_PTR ctx = 0;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        e = m_pEvents;
        ctx = m_context;
        if (e) e->AddRef();          // keep it alive across the unlocked call
    }
    if (!e) return false;
    e->CredentialsChanged(ctx);
    e->Release();
    return true;
}

// ---------------------------------------------------------------------------
// Detached helper threads (R3). A thread that may outlive the object that started it holds a
// loader reference on this DLL and gives it back with FreeLibraryAndExitThread as its very last
// act, so the code it is running is never unmapped under it.
// ---------------------------------------------------------------------------
static HMODULE PinThisModule() {
    HMODULE mod = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,
                       reinterpret_cast<LPCWSTR>(&PinThisModule), &mod);
    return mod;
}

struct FaceCredential::WorkerCtx {
    FaceCredential* cred;     // AddRef'ed for the worker's lifetime
    HMODULE module;           // loader reference, freed by FreeLibraryAndExitThread
    HANDLE cancel;            // the worker's own handle to its cancel event
    unsigned gen;
};

namespace {
struct ReportCtx {
    std::string grantId;
    bool ok;
    HMODULE module;
};

DWORD WINAPI ReportThreadProc(LPVOID p) {
    ReportCtx* c = static_cast<ReportCtx*>(p);
    HMODULE mod = c->module;
    try {
        // Not cancellable on purpose: LogonUI tears the provider down right after a successful
        // sign-in, and the report must still reach the service. Bounded by kReportTimeoutMs.
        SendReportResult(c->grantId, c->ok, nullptr);
    } catch (...) {
    }
    delete c;
    if (mod) FreeLibraryAndExitThread(mod, 0);
    return 0;
}

void StartReporter(const std::string& grantId, bool ok) {
    if (!IsHexToken(grantId)) return;
    ReportCtx* c = new (std::nothrow) ReportCtx{ grantId, ok, PinThisModule() };
    if (!c) return;
    HANDLE t = CreateThread(nullptr, 0, ReportThreadProc, c, 0, nullptr);
    if (!t) {
        if (c->module) FreeLibrary(c->module);
        delete c;
        return;
    }
    CloseHandle(t);   // detached: it owns its context and its module reference
}
}  // anonymous namespace

// ---------------------------------------------------------------------------
// FaceCredential
// ---------------------------------------------------------------------------

FaceCredential::FaceCredential(std::shared_ptr<ProviderEvents> providerEvents,
                               const std::wstring& ownerSid)
    : m_cRef(1), m_cpus(CPUS_INVALID), m_providerEvents(std::move(providerEvents)),
      m_ownerSid(ownerSid), m_pEvents(nullptr),
      m_haveResult(false), m_resultTick(0), m_scanning(false), m_selected(false),
      m_aborted(false), m_gen(0), m_scansDisabled(false),
      m_thread(nullptr), m_threadId(0), m_cancel(nullptr) {
    DllAddRef();   // 8b F-24: the DLL must stay mapped while this object lives
}

FaceCredential::~FaceCredential() {
    // 8b F-48: a throwing destructor is std::terminate inside LogonUI -> contain.
    try {
        ICredentialProviderCredentialEvents* doomed = nullptr;
        {
            std::lock_guard<std::mutex> lk(m_mtx);
            doomed = m_pEvents;
            m_pEvents = nullptr;
        }
        if (doomed) doomed->Release();
        StopWorker();
        std::lock_guard<std::mutex> lk(m_mtx);
        ClearSecretsLocked();
        m_reportGrantId.clear();
    } catch (...) {
    }
    DllRelease();  // 8b F-24
}

HRESULT FaceCredential::Initialize(CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus) {
    m_cpus = cpus;
    return S_OK;
}

IFACEMETHODIMP FaceCredential::QueryInterface(REFIID riid, void** ppv) {
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (riid == IID_IUnknown || riid == IID_ICredentialProviderCredential ||
        riid == IID_ICredentialProviderCredential2) {
        *ppv = static_cast<ICredentialProviderCredential2*>(this);
        AddRef();
        return S_OK;
    }
    return E_NOINTERFACE;
}
IFACEMETHODIMP_(ULONG) FaceCredential::AddRef()  { return InterlockedIncrement(&m_cRef); }
IFACEMETHODIMP_(ULONG) FaceCredential::Release() { LONG c = InterlockedDecrement(&m_cRef); if (c == 0) delete this; return c; }

IFACEMETHODIMP FaceCredential::GetUserSid(PWSTR* sid) {
    // R1: the tile belongs to the product's owner only; LogonUI shows it under that user.
    if (!sid) return E_POINTER;
    *sid = nullptr;
    if (m_ownerSid.empty()) return E_UNEXPECTED;
    return AllocStr(m_ownerSid.c_str(), sid);
}

// 8b F-48 (all COM methods below that lock, allocate or wait): a C++ exception crossing the COM
// boundary is std::terminate inside LogonUI -> each body is wrapped.
IFACEMETHODIMP FaceCredential::Advise(ICredentialProviderCredentialEvents* e) {
    try {
        ICredentialProviderCredentialEvents* doomed = nullptr;
        {
            std::lock_guard<std::mutex> lk(m_mtx);
            doomed = m_pEvents;
            m_pEvents = e;
            if (e) e->AddRef();
        }
        if (doomed) doomed->Release();
        return S_OK;
    } catch (...) {
        return HResultFromCurrentException();
    }
}

IFACEMETHODIMP FaceCredential::UnAdvise() {
    try {
        // Unhook the sink under the lock, then stop the worker (bounded). Once this returns no
        // thread of ours calls into LogonUI: the sink is gone and an abandoned worker never
        // publishes. R3: the published result is wiped here too.
        ICredentialProviderCredentialEvents* doomed = nullptr;
        {
            std::lock_guard<std::mutex> lk(m_mtx);
            doomed = m_pEvents;
            m_pEvents = nullptr;
            ClearSecretsLocked();
        }
        if (doomed) doomed->Release();
        StopWorker();
        return S_OK;
    } catch (...) {
        return HResultFromCurrentException();
    }
}

IFACEMETHODIMP FaceCredential::SetSelected(BOOL* pbAutoLogon) {
    // Never auto-scan on selection: the user presses the arrow to start each attempt. A finished
    // scan keeps its own status: LogonUI re-selects the tile during the re-enumeration that
    // follows CredentialsChanged, and overwriting the text there would hide the result.
    *pbAutoLogon = FALSE;
    try {
        bool ready = false;
        bool disabled = false;
        {
            std::lock_guard<std::mutex> lk(m_mtx);
            m_selected = true;
            ready = ResultFreshLocked();
            disabled = m_scansDisabled;
        }
        if (disabled) SetStatus(TileText(Text::PasswordRejected, Lang()));
        else if (!ready) SetStatus(TileText(Text::PressArrow, Lang()));
        return S_OK;
    } catch (...) {
        return HResultFromCurrentException();
    }
}

IFACEMETHODIMP FaceCredential::SetDeselected() {
    // Raise the flags, cancel the scan, wipe any result -- and return AT ONCE (no join on the
    // LogonUI thread). Stage 9 (R3, F-80 / F-82 / F-90): the cancel makes the old worker leave
    // within milliseconds, so coming back and pressing the arrow starts a fresh scan instead of
    // being swallowed; and a verified result can no longer outlive the user's attention.
    try {
        std::lock_guard<std::mutex> lk(m_mtx);
        m_selected = false;
        m_aborted = true;
        if (m_cancel) SetEvent(m_cancel);
        ClearSecretsLocked();
        return S_OK;
    } catch (...) {
        return HResultFromCurrentException();
    }
}

IFACEMETHODIMP FaceCredential::GetFieldState(DWORD dwFieldID,
                                             CREDENTIAL_PROVIDER_FIELD_STATE* pcpfs,
                                             CREDENTIAL_PROVIDER_FIELD_INTERACTIVE_STATE* pcpfis) {
    if (dwFieldID >= FIELD_COUNT) return E_INVALIDARG;
    *pcpfs  = s_FieldStatePairs[dwFieldID].cpfs;
    *pcpfis = s_FieldStatePairs[dwFieldID].cpfis;
    return S_OK;
}

IFACEMETHODIMP FaceCredential::GetStringValue(DWORD dwFieldID, PWSTR* ppwsz) {
    try {
        if (dwFieldID == FIELD_LABEL) return AllocStr(TileText(Text::Label, Lang()).c_str(), ppwsz);
        std::lock_guard<std::mutex> lk(m_mtx);
        if (dwFieldID == FIELD_STATUS) {
            if (m_status.empty()) m_status = TileText(Text::PressArrow, m_lang.empty() ? ResolveLang("") : m_lang);
            return AllocStr(m_status.c_str(), ppwsz);
        }
        return E_INVALIDARG;
    } catch (...) {
        return HResultFromCurrentException();
    }
}

IFACEMETHODIMP FaceCredential::GetBitmapValue(DWORD dwFieldID, HBITMAP* phbmp) {
    // Stage 9 (R3, F-88): our own tile image instead of the generic one.
    if (dwFieldID != FIELD_TILE_IMAGE || !phbmp) return E_INVALIDARG;
    *phbmp = static_cast<HBITMAP>(LoadImageW(reinterpret_cast<HINSTANCE>(&__ImageBase),
                                             MAKEINTRESOURCEW(IDB_TILE_IMAGE), IMAGE_BITMAP, 0, 0,
                                             LR_CREATEDIBSECTION));
    return *phbmp ? S_OK : HRESULT_FROM_WIN32(GetLastError());
}

IFACEMETHODIMP FaceCredential::GetSubmitButtonValue(DWORD dwFieldID, DWORD* pdwAdjacentTo) {
    if (dwFieldID != FIELD_SUBMIT) return E_INVALIDARG;
    *pdwAdjacentTo = FIELD_STATUS;
    return S_OK;
}

bool FaceCredential::ResultFreshLocked() {
    if (!m_haveResult) return false;
    if (GetTickCount() - m_resultTick > kResultTtlMs) {   // R3: at most 10 s old
        ClearSecretsLocked();
        return false;
    }
    return true;
}

bool FaceCredential::HasResult() {
    std::lock_guard<std::mutex> lk(m_mtx);
    // F-90: a result waiting while the user moved to another tile does not autologon.
    return m_selected && !m_scansDisabled && ResultFreshLocked();
}

std::string FaceCredential::Lang() {
    std::lock_guard<std::mutex> lk(m_mtx);
    return ResolveLang(m_lang);
}

void FaceCredential::SetStatus(const std::wstring& text) {
    ICredentialProviderCredentialEvents* e = nullptr;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        m_status = text;
        e = m_pEvents;
        if (e) e->AddRef();          // survive the unlocked call
    }
    if (!e) return;                  // unadvised: the text is still stored for GetStringValue
    e->SetFieldString(this, FIELD_STATUS, text.c_str());
    e->Release();
}

bool FaceCredential::WorkerSetStatus(unsigned gen, const std::wstring& text) {
    ICredentialProviderCredentialEvents* e = nullptr;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        if (AbandonedLocked(gen)) return false;   // nobody is looking; do not touch the tile
        m_status = text;
        e = m_pEvents;
        if (e) e->AddRef();
    }
    if (e) {
        e->SetFieldString(this, FIELD_STATUS, text.c_str());
        e->Release();
    }
    return true;
}

bool FaceCredential::AbandonedLocked(unsigned gen) const {
    return m_aborted || !m_selected || gen != m_gen;
}

void FaceCredential::ClearSecretsLocked() {
    ZeroString(m_user);
    ZeroString(m_pw);
    ZeroString(m_dom);
    m_grantId.clear();
    m_haveResult = false;
    m_resultTick = 0;
}

HRESULT FaceCredential::StartWorker() {
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        // 8b F-04: after a rejected logon a new scan would resubmit the rejected password.
        if (m_scansDisabled) return S_OK;
        if (m_scanning && !m_aborted) return S_OK;   // a live scan is running: the click is a no-op
    }
    // A cancelled predecessor (the user left and came back) is let go of first -- bounded.
    StopWorker();

    HANDLE cancel = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!cancel) return HRESULT_FROM_WIN32(GetLastError());
    HANDLE workerCancel = nullptr;
    if (!DuplicateHandle(GetCurrentProcess(), cancel, GetCurrentProcess(), &workerCancel, 0,
                         FALSE, DUPLICATE_SAME_ACCESS)) {
        const HRESULT hr = HRESULT_FROM_WIN32(GetLastError());
        CloseHandle(cancel);
        return hr;
    }
    unsigned gen = 0;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        gen = ++m_gen;
        m_aborted = false;
        m_scanning = true;
        m_cancel = cancel;
    }
    SetStatus(TileText(Text::Scanning, Lang()));

    WorkerCtx* ctx = new (std::nothrow) WorkerCtx{ this, PinThisModule(), workerCancel, gen };
    HANDLE t = nullptr;
    DWORD tid = 0;
    if (ctx) {
        AddRef();                                // the worker's reference
        t = CreateThread(nullptr, 0, &FaceCredential::ScanThreadProc, ctx, 0, &tid);
        if (!t) Release();
    }
    if (!t) {
        const HRESULT hr = ctx ? HRESULT_FROM_WIN32(GetLastError()) : E_OUTOFMEMORY;
        if (ctx) {
            if (ctx->module) FreeLibrary(ctx->module);
            delete ctx;
        }
        CloseHandle(workerCancel);
        {
            std::lock_guard<std::mutex> lk(m_mtx);
            m_scanning = false;
            m_cancel = nullptr;
        }
        CloseHandle(cancel);
        SetStatus(TileText(Text::Failed, Lang()));
        return hr;
    }
    std::lock_guard<std::mutex> lk(m_mtx);
    m_thread = t;
    m_threadId = tid;
    return S_OK;
}

void FaceCredential::StopWorker() {
    HANDLE t = nullptr;
    DWORD tid = 0;
    HANDLE cancel = nullptr;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        m_aborted = true;
        t = m_thread;
        tid = m_threadId;
        cancel = m_cancel;
        m_thread = nullptr;
        m_threadId = 0;
        m_cancel = nullptr;
        if (cancel) SetEvent(cancel);
    }
    // The destructor can run ON the worker (its reference was the last one): never wait on
    // yourself. Anywhere else wait at most kJoinBoundMs, then let go -- the worker owns its
    // reference on us, its cancel handle and its module reference.
    if (t) {
        if (tid != GetCurrentThreadId()) WaitForSingleObject(t, kJoinBoundMs);
        CloseHandle(t);
    }
    if (cancel) CloseHandle(cancel);
}

DWORD WINAPI FaceCredential::ScanThreadProc(LPVOID p) {
    WorkerCtx* c = static_cast<WorkerCtx*>(p);
    FaceCredential* self = c->cred;
    HMODULE mod = c->module;
    HANDLE cancel = c->cancel;
    const unsigned gen = c->gen;
    delete c;
    // 8b F-48: an exception leaving a thread entry is std::terminate -> contain it; the scan
    // fails closed (the reply's password copies are wiped by UnlockReply's destructor).
    try {
        self->RunScan(gen, cancel);
    } catch (...) {
        try {
            self->WorkerSetStatus(gen, TileText(Text::Failed, self->Lang()));
        } catch (...) {
        }
    }
    try {
        std::lock_guard<std::mutex> lk(self->m_mtx);
        if (self->m_gen == gen) self->m_scanning = false;   // a newer scan owns the flag
    } catch (...) {
    }
    CloseHandle(cancel);
    self->Release();                 // may run the destructor, here, on this thread
    if (mod) FreeLibraryAndExitThread(mod, 0);
    return 0;
}

void FaceCredential::RunScan(unsigned gen, HANDLE cancel) {
    UnlockReply r;
    bool ok = RequestUnlock(r, cancel);
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        if (!r.lang.empty()) m_lang = r.lang;
    }
    const std::string lang = Lang();
    std::string failReason = r.reason;
    double retryAfter = r.retryAfterS;
    std::string grantId = r.grantId;

    // Phase 2 runs only when phase 1 handed back a USABLE challenge; "needs-gesture" without a
    // token or gesture is a plain failure -- nothing to run, and we must not invent one.
    if (!ok && r.reason == "needs-gesture" && !r.token.empty() && !r.gesture.empty()) {
        // The prompt the service sent (sanitized), else the CP's own text for the gesture kinds.
        std::wstring prompt = r.prompt;
        if (prompt.empty()) prompt = GesturePromptFromKinds(r.gesture, lang);
        if (prompt.empty()) prompt = TileText(Text::GestureFallback, lang);
        if (!WorkerSetStatus(gen, prompt)) return;
        UnlockReply g;
        if (RequestUnlockGesture(r.token, g, cancel)) {
            ZeroString(r.password);
            r.username = g.username;
            r.password = g.password;
            r.domain   = g.domain;
            grantId = g.grantId;
            ok = true;
        } else {
            failReason = g.reason;           // the phase-2 outcome is what the user just did
            retryAfter = g.retryAfterS;
        }
        ZeroString(g.password);
    }

    if (!ok) {
        ZeroString(r.password);
        if (failReason == "cancelled") return;   // the user left; nothing to say
        // R3: an honest text per failure class; "unavailable" only for a service not reachable.
        WorkerSetStatus(gen, FailureText(failReason, retryAfter, lang));
        return;
    }

    // Publish is ONE decision under ONE lock: store the credentials and earn the right to
    // announce them, or store nothing. SetDeselected raises its flags under this same mutex.
    bool publish = false;
    DWORD stamp = 0;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        if (!AbandonedLocked(gen) && !m_scansDisabled) {
            m_user = r.username;
            m_pw   = r.password;
            m_dom  = r.domain;
            m_grantId = grantId;
            m_haveResult = true;
            m_resultTick = stamp = GetTickCount();
            publish = true;
        }
    }
    ZeroString(r.password);
    if (!publish) return;            // abandoned: nothing stored, nothing sent, no notify
    // Set the degraded-path text BEFORE asking for a re-enumeration: if no provider sink is
    // registered, this is the only thing that tells the user a second click will sign them in.
    WorkerSetStatus(gen, TileText(Text::Verified, lang));
    if (m_providerEvents) m_providerEvents->NotifyCredentialsChanged();

    // R3: an unused result is wiped when its TTL runs out, not left for the next access.
    if (WaitForSingleObject(cancel, kResultTtlMs) == WAIT_TIMEOUT) {
        bool expired = false;
        {
            std::lock_guard<std::mutex> lk(m_mtx);
            if (m_haveResult && m_resultTick == stamp && m_gen == gen) {
                ClearSecretsLocked();
                expired = true;
            }
        }
        if (expired) WorkerSetStatus(gen, TileText(Text::PressArrow, lang));
    }
}

IFACEMETHODIMP FaceCredential::GetSerialization(
    CREDENTIAL_PROVIDER_GET_SERIALIZATION_RESPONSE* pcpgsr,
    CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* pcpcs,
    PWSTR* ppwszOptionalStatusText,
    CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) {

    // Default safe response: nothing happened yet, so the user can switch to PIN/password at once.
    *pcpgsr = CPGSR_NO_CREDENTIAL_NOT_FINISHED;
    *pcpsiOptionalStatusIcon = CPSI_NONE;
    if (ppwszOptionalStatusText) *ppwszOptionalStatusText = nullptr;

    bool packed = false;
    try {
        // 8b F-25: the copies wipe themselves on every exit.
        struct SecretCopies {
            std::wstring user, pw, dom;
            ~SecretCopies() { ZeroString(pw); ZeroString(user); ZeroString(dom); }
        } c;
        std::string grantId;
        bool haveResult = false;
        bool disabled = false;
        {
            std::lock_guard<std::mutex> lk(m_mtx);
            disabled = m_scansDisabled;
            haveResult = !disabled && ResultFreshLocked();
            if (disabled) ClearSecretsLocked();
            if (haveResult) {
                c.user = m_user; c.pw = m_pw; c.dom = m_dom;
                grantId = m_grantId;
                // R3: one-shot -- consumed now, whatever LogonUI does with it next.
                ClearSecretsLocked();
            }
        }

        // 8b F-04: a logon with our credential was rejected earlier in this LogonUI session.
        if (disabled) {
            SetStatus(TileText(Text::PasswordRejected, Lang()));
            return S_OK;
        }

        if (haveResult) {
            HRESULT hr = E_FAIL;
            try {
                hr = KerbPackInteractiveUnlock(c.dom, c.user, c.pw, m_cpus, pcpcs);
            } catch (...) {
                hr = E_FAIL;
            }
            packed = SUCCEEDED(hr);
            ZeroString(c.pw);
            ZeroString(c.user);
            ZeroString(c.dom);
            if (FAILED(hr)) {
                SetStatus(TileText(Text::PackingFailed, Lang()));
                *pcpsiOptionalStatusIcon = CPSI_ERROR;
                return S_FALSE;
            }
            {
                std::lock_guard<std::mutex> lk(m_mtx);
                m_reportGrantId = grantId;       // settled by ReportResult (protocol v2)
            }
            pcpcs->clsidCredentialProvider = CLSID_FaceCredentialProvider;
            *pcpgsr = CPGSR_RETURN_CREDENTIAL_FINISHED;
            return S_OK;
        }

        return StartWorker();
    } catch (...) {
        const HRESULT hr = HResultFromCurrentException();
        if (packed) KerbUnpackFree(pcpcs);
        *pcpgsr = CPGSR_NO_CREDENTIAL_NOT_FINISHED;
        return hr;
    }
}

IFACEMETHODIMP FaceCredential::ReportResult(NTSTATUS ntsStatus, NTSTATUS ntsSubstatus,
                                            PWSTR* ppwszOptionalStatusText,
                                            CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) {
    *ppwszOptionalStatusText = nullptr;
    *pcpsiOptionalStatusIcon = CPSI_NONE;
    try {
        std::string grantId;
        {
            std::lock_guard<std::mutex> lk(m_mtx);
            grantId.swap(m_reportGrantId);
            ClearSecretsLocked();                // R3: nothing survives a ReportResult
        }
        const bool success = ntsStatus >= 0;     // NT_SUCCESS
        const bool pwBad = !success &&
                           (IsPasswordFailure(ntsStatus) || IsPasswordFailure(ntsSubstatus));
        // §2.1: tell the service what became of its grant. Success commits it (lockout reset,
        // adaptation); a password failure makes the service refuse until a new password is
        // saved. Any other failure (account locked, restrictions) is not reported: the grant is
        // then abandoned and nothing is committed.
        if (!grantId.empty() && (success || pwBad)) StartReporter(grantId, success);
        if (success) return S_OK;
        // 8b F-04: never resubmit a credential Windows refused, for the rest of this session.
        {
            std::lock_guard<std::mutex> lk(m_mtx);
            m_scansDisabled = true;
        }
        SetStatus(pwBad ? TileText(Text::PasswordRejected, Lang()) : TileText(Text::Failed, Lang()));
        return S_OK;
    } catch (...) {
        return HResultFromCurrentException();
    }
}

}  // namespace FaceUnlock
