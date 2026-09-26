#include "FaceCredentialProvider.h"
#include "FaceCredential.h"
#include "PipeClient.h"
#include "helpers.h"
#include <new>

namespace FaceUnlock {

FaceCredentialProvider::FaceCredentialProvider()
    : m_cRef(1), m_cpus(CPUS_INVALID), m_pCred(nullptr), m_ownerListed(false),
      m_events(std::make_shared<ProviderEvents>()) {
    DllAddRef();   // 8b F-24: the DLL must stay mapped while this object lives
}

FaceCredentialProvider::~FaceCredentialProvider() {
    // Drop the sink before releasing the credential: LogonUI may still hold its own reference,
    // so the credential can outlive us, and it must not call back into a dead provider. The
    // shared holder makes that safe -- after Clear() the notify is a no-op.
    // 8b F-48: a destructor must not throw (std::terminate in LogonUI) -> contain.
    try {
        m_events->Clear();
    } catch (...) {
    }
    if (m_pCred) { m_pCred->Release(); m_pCred = nullptr; }
    DllRelease();  // 8b F-24
}

IFACEMETHODIMP FaceCredentialProvider::QueryInterface(REFIID riid, void** ppv) {
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (riid == IID_IUnknown || riid == IID_ICredentialProvider) {
        *ppv = static_cast<ICredentialProvider*>(this);
    } else if (riid == IID_ICredentialProviderSetUserArray) {
        *ppv = static_cast<ICredentialProviderSetUserArray*>(this);
    } else {
        return E_NOINTERFACE;
    }
    AddRef();
    return S_OK;
}
IFACEMETHODIMP_(ULONG) FaceCredentialProvider::AddRef()  { return InterlockedIncrement(&m_cRef); }
IFACEMETHODIMP_(ULONG) FaceCredentialProvider::Release() { LONG c = InterlockedDecrement(&m_cRef); if (c == 0) delete this; return c; }

IFACEMETHODIMP FaceCredentialProvider::SetUsageScenario(CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus, DWORD) {
    // Never offer the face tile in a remote (RDP) session: there is no local camera to scan.
    // The CP reads no TOML config, so RDP-off is hardcoded in C++. CREDUI (UAC and other
    // credential prompts) and CHANGE_PASSWORD are not supported either -- by design, documented.
    if (GetSystemMetrics(SM_REMOTESESSION) != 0) {
        return E_NOTIMPL;
    }

    switch (cpus) {
        case CPUS_LOGON:
        case CPUS_UNLOCK_WORKSTATION: {
            m_cpus = cpus;
            // R1: the tile exists only for the recorded owner. No owner recorded -> no tile.
            std::wstring owner;
            try {
                if (!ReadOwnerSid(owner)) owner.clear();
                m_ownerSid = owner;
            } catch (...) {
                return HResultFromCurrentException();
            }
            if (m_ownerSid.empty()) return E_NOTIMPL;
            if (!m_pCred) {
                // 8b F-48: the constructor allocates and could throw through this boundary.
                try {
                    m_pCred = new (std::nothrow) FaceCredential(m_events, m_ownerSid);
                } catch (...) {
                    m_pCred = nullptr;
                    return HResultFromCurrentException();
                }
                if (!m_pCred) return E_OUTOFMEMORY;
            }
            // Stage 9 (D-68): re-initialise on EVERY call, so a second scenario is never served
            // with the first one's message type.
            HRESULT hr = m_pCred->Initialize(cpus);
            if (FAILED(hr)) { m_pCred->Release(); m_pCred = nullptr; return hr; }
            return S_OK;
        }
        default:
            return E_NOTIMPL;
    }
}

IFACEMETHODIMP FaceCredentialProvider::SetSerialization(const CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION*) {
    return E_NOTIMPL;
}

// The gesture round is asynchronous: the provider-level sink is the only way a worker thread can
// tell LogonUI "re-enumerate, I have a credential". Kept in a shared, mutex-guarded holder.
// 8b F-48: both take a std::mutex, whose lock() can throw -> contained at the COM boundary.
IFACEMETHODIMP FaceCredentialProvider::Advise(ICredentialProviderEvents* pcpe, UINT_PTR upAdviseContext) {
    try {
        m_events->Set(pcpe, upAdviseContext);
        return S_OK;
    } catch (...) {
        return HResultFromCurrentException();
    }
}
IFACEMETHODIMP FaceCredentialProvider::UnAdvise() {
    try {
        m_events->Clear();
        return S_OK;
    } catch (...) {
        return HResultFromCurrentException();
    }
}

IFACEMETHODIMP FaceCredentialProvider::SetUserArray(ICredentialProviderUserArray* users) {
    // R1: remember whether the owner is among the users LogonUI will show. In
    // CPUS_UNLOCK_WORKSTATION the array holds the locked user only, so a PC locked by anyone
    // else gets no face tile.
    try {
        m_ownerListed = false;
        if (!users || m_ownerSid.empty()) return S_OK;
        DWORD count = 0;
        if (FAILED(users->GetCount(&count))) return S_OK;
        for (DWORD i = 0; i < count && !m_ownerListed; ++i) {
            ICredentialProviderUser* u = nullptr;
            if (FAILED(users->GetAt(i, &u)) || !u) continue;
            PWSTR sid = nullptr;
            if (SUCCEEDED(u->GetSid(&sid)) && sid) {
                if (_wcsicmp(sid, m_ownerSid.c_str()) == 0) m_ownerListed = true;
                CoTaskMemFree(sid);
            }
            u->Release();
        }
        return S_OK;
    } catch (...) {
        m_ownerListed = false;
        return HResultFromCurrentException();
    }
}

bool FaceCredentialProvider::OfferTile() {
    if (!m_pCred || !m_ownerListed) return false;
    // R3: at sign-in (boot, sign-out) the per-user service usually does not exist yet; offer the
    // tile only when its pipe does. The check makes no connection.
    if (m_cpus == CPUS_LOGON && !ServicePipeExists()) return false;
    return true;
}

IFACEMETHODIMP FaceCredentialProvider::GetFieldDescriptorCount(DWORD* pdwCount) {
    *pdwCount = FIELD_COUNT;
    return S_OK;
}

IFACEMETHODIMP FaceCredentialProvider::GetFieldDescriptorAt(DWORD dwIndex,
                                                            CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR** ppcpfd) {
    if (dwIndex >= FIELD_COUNT) return E_INVALIDARG;
    CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR src = s_FieldDescriptors[dwIndex];
    auto* out = (CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR*)CoTaskMemAlloc(sizeof(src));
    if (!out) return E_OUTOFMEMORY;
    *out = src;
    // Label string must be its own CoTaskMem allocation.
    size_t cch = wcslen(src.pszLabel) + 1;
    out->pszLabel = (PWSTR)CoTaskMemAlloc(cch * sizeof(WCHAR));
    if (!out->pszLabel) { CoTaskMemFree(out); return E_OUTOFMEMORY; }
    wcscpy_s(out->pszLabel, cch, src.pszLabel);
    *ppcpfd = out;
    return S_OK;
}

IFACEMETHODIMP FaceCredentialProvider::GetCredentialCount(DWORD* pdwCount, DWORD* pdwDefault,
                                                          BOOL* pbAutoLogonWithDefault) {
    *pdwCount = 0;
    *pdwDefault = CREDENTIAL_PROVIDER_NO_DEFAULT;
    *pbAutoLogonWithDefault = FALSE;
    // 8b F-48: HasResult() takes the credential's mutex -> contained; the safe answer is no tile.
    try {
        if (!OfferTile()) return S_OK;
        *pdwCount = 1;
        // Stage 9 (F-89): the face tile is no longer the default on every screen -- only in the
        // enumeration that CredentialsChanged asked for because a scan finished and a fresh
        // credential is waiting. Then it is default AND auto-logs on: that turns "the face (and
        // gesture) round succeeded" into a sign-in without a second click. An idle lock screen
        // never fires the camera by itself.
        if (m_pCred->HasResult()) {
            *pdwDefault = 0;
            *pbAutoLogonWithDefault = TRUE;
        }
    } catch (...) {
        *pdwCount = 0;
        *pdwDefault = CREDENTIAL_PROVIDER_NO_DEFAULT;
        *pbAutoLogonWithDefault = FALSE;
        return HResultFromCurrentException();
    }
    return S_OK;
}

IFACEMETHODIMP FaceCredentialProvider::GetCredentialAt(DWORD dwIndex, ICredentialProviderCredential** ppcpc) {
    if (dwIndex != 0 || !m_pCred) return E_INVALIDARG;
    return m_pCred->QueryInterface(IID_ICredentialProviderCredential, (void**)ppcpc);
}

}  // namespace FaceUnlock
