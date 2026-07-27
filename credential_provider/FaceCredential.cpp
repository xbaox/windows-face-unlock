#include "FaceCredential.h"
#include "helpers.h"
#include "PipeClient.h"
#include "guid.h"
#include <shlwapi.h>

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
    // Released outside the lock like every other call out of this class: a final Release runs
    // the sink's destructor, and we do not want foreign code running under our mutex.
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
    if (doomed) doomed->Release();   // released outside the lock, like every other call out
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
// FaceCredential
// ---------------------------------------------------------------------------

FaceCredential::FaceCredential(std::shared_ptr<ProviderEvents> providerEvents)
    : m_cRef(1), m_cpus(CPUS_INVALID), m_providerEvents(std::move(providerEvents)),
      m_abort(false), m_pEvents(nullptr),
      m_label(L"Face Unlock"), m_status(L"Look at the camera"),
      m_haveResult(false), m_scanning(false), m_selected(false) {}

FaceCredential::~FaceCredential() {
    // Drop the sink FIRST, then stop the worker: after this point the worker can no longer
    // reach LogonUI even if it is mid-flight, and the join below guarantees it is gone before
    // any member is destroyed.
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
}

HRESULT FaceCredential::Initialize(CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus) {
    m_cpus = cpus;
    return S_OK;
}

IFACEMETHODIMP FaceCredential::QueryInterface(REFIID riid, void** ppv) {
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (riid == IID_IUnknown || riid == IID_ICredentialProviderCredential) {
        *ppv = static_cast<ICredentialProviderCredential*>(this);
        AddRef();
        return S_OK;
    }
    return E_NOINTERFACE;
}
IFACEMETHODIMP_(ULONG) FaceCredential::AddRef()  { return InterlockedIncrement(&m_cRef); }
IFACEMETHODIMP_(ULONG) FaceCredential::Release() { LONG c = InterlockedDecrement(&m_cRef); if (c == 0) delete this; return c; }

IFACEMETHODIMP FaceCredential::Advise(ICredentialProviderCredentialEvents* e) {
    ICredentialProviderCredentialEvents* doomed = nullptr;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        doomed = m_pEvents;
        m_pEvents = e;
        if (e) e->AddRef();
    }
    if (doomed) doomed->Release();
    return S_OK;
}

IFACEMETHODIMP FaceCredential::UnAdvise() {
    // Same order as the destructor: unhook the sink under the lock, then join. Once this
    // returns, no thread of ours can call into LogonUI.
    ICredentialProviderCredentialEvents* doomed = nullptr;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        doomed = m_pEvents;
        m_pEvents = nullptr;
    }
    if (doomed) doomed->Release();
    StopWorker();
    return S_OK;
}

IFACEMETHODIMP FaceCredential::SetSelected(BOOL* pbAutoLogon) {
    // Do NOT auto-trigger verification when the tile becomes selected. The user must press
    // the submit arrow to start a scan -- this prevents continuous re-scanning whenever the
    // lock screen redraws or the tile is re-selected, and gives the user explicit control
    // over each attempt. A finished scan keeps its own status: LogonUI re-selects the tile
    // during the re-enumeration that follows CredentialsChanged, and overwriting the text
    // there would hide the result.
    *pbAutoLogon = FALSE;
    bool ready = false;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        m_selected = true;
        ready = m_haveResult;
    }
    if (!ready) SetStatus(L"Press the arrow to scan your face");
    return S_OK;
}

IFACEMETHODIMP FaceCredential::SetDeselected() {
    // Raise the flags and return AT ONCE -- deliberately no join here.
    //
    // Joining would block LogonUI's UI thread on a pipe call, freezing the lock screen for up
    // to the phase-2 timeout on the very ordinary "it asked me to blink, I changed my mind,
    // give me the PIN tile" path. Detaching the worker instead is not an option either: a
    // detached thread still running when LogonUI unloads this DLL is an access violation. So
    // the worker is simply told its output is unwanted and left to run itself out; it will
    // wipe its own copies and publish nothing.
    //
    // Both flags are raised under the SAME mutex the worker checks before publishing, which is
    // what makes that check atomic instead of a window. m_pEvents is NOT touched: only UnAdvise
    // owns the sink, and LogonUI does not Advise a second time.
    std::lock_guard<std::mutex> lk(m_mtx);
    m_selected = false;
    m_abort.store(true);
    return S_OK;
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
    std::lock_guard<std::mutex> lk(m_mtx);
    switch (dwFieldID) {
        case FIELD_LABEL:  return AllocStr(m_label.c_str(),  ppwsz);
        case FIELD_STATUS: return AllocStr(m_status.c_str(), ppwsz);
    }
    return E_INVALIDARG;
}

IFACEMETHODIMP FaceCredential::GetBitmapValue(DWORD dwFieldID, HBITMAP* phbmp) {
    // Use default logo — custom bitmap omitted in MVP.
    if (dwFieldID != FIELD_TILE_IMAGE) return E_INVALIDARG;
    *phbmp = nullptr;
    return E_NOTIMPL;
}

IFACEMETHODIMP FaceCredential::GetSubmitButtonValue(DWORD dwFieldID, DWORD* pdwAdjacentTo) {
    if (dwFieldID != FIELD_SUBMIT) return E_INVALIDARG;
    *pdwAdjacentTo = FIELD_STATUS;
    return S_OK;
}

bool FaceCredential::HasResult() {
    std::lock_guard<std::mutex> lk(m_mtx);
    return m_haveResult;
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

bool FaceCredential::WorkerSetStatus(const std::wstring& text) {
    ICredentialProviderCredentialEvents* e = nullptr;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        if (AbandonedLocked()) return false;   // nobody is looking; do not touch the tile
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

bool FaceCredential::AbandonedLocked() const {
    return m_abort.load() || !m_selected;
}

void FaceCredential::ClearSecretsLocked() {
    ZeroString(m_user);
    ZeroString(m_pw);
    ZeroString(m_dom);
    m_haveResult = false;
}

void FaceCredential::StartWorker() {
    std::thread stale;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        if (m_scanning) return;               // a scan is already running: the click is a no-op
        stale = std::move(m_worker);          // a previous worker still needs joining
    }
    // Normally instant: an abandoned predecessor never starts phase 2, so it is already on its
    // way out. It can still be alive if the user left and came back inside the phase-1 timeout,
    // which is rare and bounded.
    if (stale.joinable()) stale.join();
    m_abort.store(false);
    {
        // Set AFTER the join, not before: the outgoing worker clears m_scanning as its last
        // act, so a flag raised earlier would be wiped by the thread we just waited for -- and
        // the next click would then start a second worker alongside this one.
        std::lock_guard<std::mutex> lk(m_mtx);
        m_scanning = true;
    }
    SetStatus(L"Scanning face...");
    std::thread t(&FaceCredential::WorkerMain, this);
    std::lock_guard<std::mutex> lk(m_mtx);
    m_worker = std::move(t);
}

void FaceCredential::StopWorker() {
    m_abort.store(true);
    std::thread t;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        t = std::move(m_worker);              // move OUT before joining: the worker takes the
    }                                         // same mutex to publish, so joining under it deadlocks
    if (t.joinable()) t.join();               // bounded: every pipe call has a hard timeout
    std::lock_guard<std::mutex> lk(m_mtx);
    m_scanning = false;
}

void FaceCredential::WorkerMain() {
    RunScan();
    // Clearing this here, rather than in StopWorker, is what keeps the tile usable now that
    // SetDeselected no longer joins: without it m_scanning would stay true forever after the
    // first scan and every later click would be swallowed as a "already running" no-op.
    std::lock_guard<std::mutex> lk(m_mtx);
    m_scanning = false;
}

void FaceCredential::RunScan() {
    UnlockReply r;
    bool ok = RequestUnlock(r);

    // Phase 2 runs only when phase 1 handed back a USABLE challenge. "needs-gesture" with an
    // empty token or gesture is treated as a plain failure: there is nothing to run, and we
    // must not invent one.
    if (!ok && r.reason == "needs-gesture" && !r.token.empty() && !r.gesture.empty()) {
        // The prompt arrives ready to display, already localized and already wide. Pushing it
        // goes through the gate: an abandoned scan must not write on a tile the user left.
        // Only push if the service actually sent a prompt -- an empty one would blank the tile
        // instead of leaving "Scanning face..." up. A gate refusal means we are done.
        if (!r.prompt.empty()) {
            if (!WorkerSetStatus(r.prompt)) { ZeroString(r.password); return; }
        } else {
            std::lock_guard<std::mutex> lk(m_mtx);
            if (AbandonedLocked()) { ZeroString(r.password); return; }
        }
        UnlockReply g;
        if (RequestUnlockGesture(r.token, g)) {
            ZeroString(r.password);
            r.username = g.username;
            r.password = g.password;
            r.domain   = g.domain;
            ok = true;
        }
        ZeroString(g.password);
    }

    if (!ok) {
        ZeroString(r.password);
        WorkerSetStatus(L"Face not recognised. Use password tile instead.");
        return;
    }

    // The success publish is ONE decision taken under ONE lock: store the credentials and earn
    // the right to announce them, or store nothing at all. Because SetDeselected raises its
    // flags under this same mutex, there is no window in which the result lands after the user
    // has walked away -- and an abandoned result is wiped below rather than left in memory.
    bool publish = false;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        if (!AbandonedLocked()) {
            m_user = r.username;
            m_pw   = r.password;
            m_dom  = r.domain;
            m_haveResult = true;
            publish = true;
        }
    }
    ZeroString(r.password);
    if (!publish) return;              // abandoned: nothing stored, nothing sent, no notify
    // Set the degraded-path text BEFORE asking for a re-enumeration: if no provider sink is
    // registered, this is the only thing that tells the user the scan worked and a second
    // click will sign them in.
    WorkerSetStatus(L"Face verified - press the arrow");
    if (m_providerEvents) m_providerEvents->NotifyCredentialsChanged();
}

IFACEMETHODIMP FaceCredential::GetSerialization(
    CREDENTIAL_PROVIDER_GET_SERIALIZATION_RESPONSE* pcpgsr,
    CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* pcpcs,
    PWSTR* ppwszOptionalStatusText,
    CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) {

    // Default safe response: tell LogonUI nothing happened yet, so the user can immediately
    // switch to the password tile or wait for the scan to finish.
    *pcpgsr = CPGSR_NO_CREDENTIAL_NOT_FINISHED;
    *pcpsiOptionalStatusIcon = CPSI_NONE;
    if (ppwszOptionalStatusText) *ppwszOptionalStatusText = nullptr;

    std::wstring user, pw, dom;
    bool haveResult = false;
    bool scanning = false;
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        haveResult = m_haveResult;
        scanning = m_scanning;
        if (haveResult) { user = m_user; pw = m_pw; dom = m_dom; }
    }

    if (haveResult) {
        // Packing happens HERE, on the LogonUI thread, from the copy the worker left behind.
        HRESULT hr = E_FAIL;
        try {
            hr = KerbPackInteractiveUnlock(dom, user, pw, m_cpus, pcpcs);
        } catch (...) {
            hr = E_FAIL;
        }
        ZeroString(pw);
        ZeroString(user);
        ZeroString(dom);
        if (FAILED(hr)) {
            SetStatus(L"Credential packing failed");
            *pcpsiOptionalStatusIcon = CPSI_ERROR;
            return S_FALSE;
        }
        // Consumed: wipe the stored copy too. If LogonUI then rejects the logon (a stale
        // stored password, say) the next click starts a fresh scan instead of replaying a
        // credential that has already been refused -- and HasResult() going false stops the
        // autologon from firing again on its own.
        {
            std::lock_guard<std::mutex> lk(m_mtx);
            ClearSecretsLocked();
        }
        pcpcs->clsidCredentialProvider = CLSID_FaceCredentialProvider;
        *pcpgsr = CPGSR_RETURN_CREDENTIAL_FINISHED;
        return S_OK;
    }

    // A scan is already in flight: a second click must not start a second one.
    if (scanning) return S_OK;

    StartWorker();
    return S_OK;
}

IFACEMETHODIMP FaceCredential::ReportResult(NTSTATUS ntsStatus, NTSTATUS ntsSubstatus,
                                            PWSTR* ppwszOptionalStatusText,
                                            CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) {
    (void)ntsStatus; (void)ntsSubstatus;
    *ppwszOptionalStatusText = nullptr;
    *pcpsiOptionalStatusIcon = CPSI_NONE;
    return S_OK;
}

}  // namespace FaceUnlock
