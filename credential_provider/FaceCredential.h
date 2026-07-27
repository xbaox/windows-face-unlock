#pragma once
#include <windows.h>
#include <credentialprovider.h>
#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

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

class FaceCredential : public ICredentialProviderCredential {
public:
    explicit FaceCredential(std::shared_ptr<ProviderEvents> providerEvents);
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

    // Provider-facing: has a finished scan left credentials waiting? Drives
    // pbAutoLogonWithDefault on the re-enumeration that CredentialsChanged triggers.
    bool HasResult();

private:
    void StartWorker();          // LogonUI thread only
    void StopWorker();           // LogonUI thread only: abort flag + join (UnAdvise / dtor)
    void WorkerMain();           // worker thread: runs the scan, then clears m_scanning
    void RunScan();              // worker thread: the scan itself
    void SetStatus(const std::wstring& text);          // unconditional (LogonUI thread)
    bool WorkerSetStatus(const std::wstring& text);    // worker: skipped once abandoned
    // True once this scan's output must be thrown away -- aborted, or the tile is no longer
    // selected. The caller MUST hold m_mtx: that is the whole point. SetDeselected raises both
    // flags under the same lock, so a publish either completes before it or does not happen.
    bool AbandonedLocked() const;
    void ClearSecretsLocked();                  // caller holds m_mtx

    LONG m_cRef;
    CREDENTIAL_PROVIDER_USAGE_SCENARIO m_cpus;
    std::shared_ptr<ProviderEvents> m_providerEvents;

    // Set once the worker should stop touching anything. Atomic because it is read by the
    // worker while the LogonUI thread writes it, without the mutex (the worker must be able
    // to observe it while blocked-then-returning from a pipe call).
    std::atomic<bool> m_abort;

    std::mutex m_mtx;            // guards EVERYTHING below
    ICredentialProviderCredentialEvents* m_pEvents;
    std::wstring m_label;
    std::wstring m_status;
    std::wstring m_user;         // saved credentials, valid only while m_haveResult
    std::wstring m_pw;
    std::wstring m_dom;
    bool m_haveResult;
    bool m_scanning;
    bool m_selected;             // is our tile the one the user is looking at
    std::thread m_worker;
};

}  // namespace FaceUnlock
