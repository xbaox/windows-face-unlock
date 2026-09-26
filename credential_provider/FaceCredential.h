#pragma once
#include <windows.h>
#include <credentialprovider.h>
#include <atomic>
#include <memory>
#include <mutex>
#include <string>

namespace FaceUnlock {

// Mutex-guarded holder for the PROVIDER-level event sink (ICredentialProvider::Advise).
//
// It lives in a shared_ptr held by BOTH the provider and the credential, rather than the
// credential keeping a back-pointer to its provider: LogonUI AddRefs the credential in
// GetCredentialAt, so the credential can outlive the provider and a raw back-pointer could
// dangle. The sink is registered in Advise, dropped in UnAdvise, and the worker thread only
// ever reaches LogonUI through NotifyCredentialsChanged(), which becomes a no-op once dropped.
class ProviderEvents {
public:
    void Set(ICredentialProviderEvents* pEvents, UINT_PTR context);
    void Clear();

    // Ask LogonUI to re-enumerate credentials. Copies + AddRefs the sink under the lock and
    // makes the call OUTSIDE it: CredentialsChanged re-enters the provider, and holding the
    // lock across that invites a deadlock. Returns false when nothing is registered -- the
    // degraded path, where the tile asks the user for a second click instead.
    bool NotifyCredentialsChanged();

private:
    std::mutex m_mtx;
    ICredentialProviderEvents* m_pEvents = nullptr;
    UINT_PTR m_context = 0;
};

// The face tile. Stage 9 (R1): an ICredentialProviderCredential2 tied to the product's owner --
// GetUserSid returns the owner SID, so LogonUI shows it only under the owner's user tile.
//
// Stage 9 (R3) worker model. Each scan runs on a CreateThread worker that holds (a) a reference
// on this credential and (b) a loader reference on this DLL, released with
// FreeLibraryAndExitThread as the thread's very last act. Every pipe wait also watches the
// worker's cancel event. So UnAdvise, the destructor and SetDeselected never block the LogonUI
// thread for long: they cancel, wait at most kJoinBoundMs, and then simply let go of the thread
// handle -- the worker still owns everything it touches, and the code it runs stays mapped until
// it has returned.
class FaceCredential : public ICredentialProviderCredential2 {
public:
    FaceCredential(std::shared_ptr<ProviderEvents> providerEvents, const std::wstring& ownerSid);
    ~FaceCredential();

    HRESULT Initialize(CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus);

    // IUnknown
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override;
    IFACEMETHODIMP_(ULONG) AddRef() override;
    IFACEMETHODIMP_(ULONG) Release() override;

    // ICredentialProviderCredential
    IFACEMETHODIMP Advise(ICredentialProviderCredentialEvents* pcpce) override;
    IFACEMETHODIMP UnAdvise() override;
    IFACEMETHODIMP SetSelected(BOOL* pbAutoLogon) override;
    IFACEMETHODIMP SetDeselected() override;
    IFACEMETHODIMP GetFieldState(DWORD dwFieldID,
                                 CREDENTIAL_PROVIDER_FIELD_STATE* pcpfs,
                                 CREDENTIAL_PROVIDER_FIELD_INTERACTIVE_STATE* pcpfis) override;
    IFACEMETHODIMP GetStringValue(DWORD dwFieldID, PWSTR* ppwsz) override;
    IFACEMETHODIMP GetBitmapValue(DWORD dwFieldID, HBITMAP* phbmp) override;
    IFACEMETHODIMP GetCheckboxValue(DWORD, BOOL*, PWSTR*) override { return E_NOTIMPL; }
    IFACEMETHODIMP GetSubmitButtonValue(DWORD dwFieldID, DWORD* pdwAdjacentTo) override;
    IFACEMETHODIMP GetComboBoxValueCount(DWORD, DWORD*, DWORD*) override { return E_NOTIMPL; }
    IFACEMETHODIMP GetComboBoxValueAt(DWORD, DWORD, PWSTR*) override { return E_NOTIMPL; }
    IFACEMETHODIMP SetStringValue(DWORD, PCWSTR) override { return S_OK; }
    IFACEMETHODIMP SetCheckboxValue(DWORD, BOOL) override { return E_NOTIMPL; }
    IFACEMETHODIMP SetComboBoxSelectedValue(DWORD, DWORD) override { return E_NOTIMPL; }
    IFACEMETHODIMP CommandLinkClicked(DWORD) override { return E_NOTIMPL; }
    IFACEMETHODIMP GetSerialization(CREDENTIAL_PROVIDER_GET_SERIALIZATION_RESPONSE* pcpgsr,
                                    CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* pcpcs,
                                    PWSTR* ppwszOptionalStatusText,
                                    CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) override;
    IFACEMETHODIMP ReportResult(NTSTATUS ntsStatus, NTSTATUS ntsSubstatus,
                                PWSTR* ppwszOptionalStatusText,
                                CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) override;

    // ICredentialProviderCredential2
    IFACEMETHODIMP GetUserSid(PWSTR* sid) override;

    // Provider-facing: has a finished scan left a FRESH credential waiting (TTL, one-shot) while
    // the tile is still selected? Drives pbAutoLogonWithDefault on the re-enumeration that
    // CredentialsChanged triggers.
    bool HasResult();

    // A verified result lives at most this long (R3), and is used once.
    static constexpr DWORD kResultTtlMs = 10000;
    // UnAdvise / destructor / a restarting scan wait this long for a cancelled worker (R3).
    static constexpr DWORD kJoinBoundMs = 1000;

private:
    struct WorkerCtx;
    static DWORD WINAPI ScanThreadProc(LPVOID ctx);

    HRESULT StartWorker();       // LogonUI thread only
    void StopWorker();           // cancel + bounded wait + let go (UnAdvise / dtor / restart)
    void RunScan(unsigned gen, HANDLE cancel);            // worker thread
    void SetStatus(const std::wstring& text);             // unconditional (LogonUI thread)
    bool WorkerSetStatus(unsigned gen, const std::wstring& text);   // skipped once abandoned
    // True once scan `gen`'s output must be thrown away -- aborted, superseded, or the tile is no
    // longer selected. The caller MUST hold m_mtx.
    bool AbandonedLocked(unsigned gen) const;
    void ClearSecretsLocked();                  // caller holds m_mtx
    bool ResultFreshLocked();                   // caller holds m_mtx; wipes an expired result
    std::string Lang();                         // the tile language (last reply, else system)

    LONG m_cRef;
    CREDENTIAL_PROVIDER_USAGE_SCENARIO m_cpus;
    std::shared_ptr<ProviderEvents> m_providerEvents;
    const std::wstring m_ownerSid;

    std::mutex m_mtx;            // guards EVERYTHING below
    ICredentialProviderCredentialEvents* m_pEvents;
    std::wstring m_status;
    std::wstring m_user;         // saved credentials, valid only while m_haveResult
    std::wstring m_pw;
    std::wstring m_dom;
    std::string m_grantId;       // the grant these credentials came from (protocol v2)
    std::string m_reportGrantId; // the grant just handed to LogonUI, reported in ReportResult
    std::string m_lang;          // "en" / "ru" from the last reply; empty -> system language
    bool m_haveResult;
    DWORD m_resultTick;
    bool m_scanning;
    bool m_selected;             // is our tile the one the user is looking at
    bool m_aborted;              // the current scan's output is unwanted
    unsigned m_gen;              // generation of the current scan
    // 8b F-04: LogonUI rejected the credential we submitted -> no new scan for the life of this
    // object. Stage 9 (§2.1): the service now also keeps a persistent flag, so the next lock
    // screen refuses too until a new password is saved.
    bool m_scansDisabled;
    HANDLE m_thread;             // current worker, or null
    DWORD m_threadId;
    HANDLE m_cancel;             // our copy of the current worker's cancel event, or null
};

}  // namespace FaceUnlock
