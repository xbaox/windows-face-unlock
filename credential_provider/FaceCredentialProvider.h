#pragma once
#include <windows.h>
#include <credentialprovider.h>
#include <memory>
#include <string>

#include "FaceCredential.h"

namespace FaceUnlock {

// Stage 9 (R1 / R3): the provider offers ONE tile, and only to the product's owner. It implements
// ICredentialProviderSetUserArray: LogonUI hands it the users it is about to show, and the tile
// exists only when the owner is among them. In CPUS_LOGON it also needs the service pipe to exist
// already (an instant check), so a sign-in right after boot does not offer a tile that can only
// fail. Other accounts on the PC see no face tile and sign in exactly as before.
class FaceCredentialProvider : public ICredentialProvider, public ICredentialProviderSetUserArray {
public:
    FaceCredentialProvider();
    ~FaceCredentialProvider();

    IFACEMETHODIMP QueryInterface(REFIID, void**) override;
    IFACEMETHODIMP_(ULONG) AddRef() override;
    IFACEMETHODIMP_(ULONG) Release() override;

    IFACEMETHODIMP SetUsageScenario(CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus, DWORD dwFlags) override;
    IFACEMETHODIMP SetSerialization(const CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION*) override;
    IFACEMETHODIMP Advise(ICredentialProviderEvents*, UINT_PTR) override;
    IFACEMETHODIMP UnAdvise() override;
    IFACEMETHODIMP GetFieldDescriptorCount(DWORD* pdwCount) override;
    IFACEMETHODIMP GetFieldDescriptorAt(DWORD dwIndex,
                                        CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR** ppcpfd) override;
    IFACEMETHODIMP GetCredentialCount(DWORD* pdwCount, DWORD* pdwDefault,
                                      BOOL* pbAutoLogonWithDefault) override;
    IFACEMETHODIMP GetCredentialAt(DWORD dwIndex, ICredentialProviderCredential** ppcpc) override;

    // ICredentialProviderSetUserArray
    IFACEMETHODIMP SetUserArray(ICredentialProviderUserArray* users) override;

private:
    bool OfferTile();
    LONG m_cRef;
    CREDENTIAL_PROVIDER_USAGE_SCENARIO m_cpus;
    FaceCredential* m_pCred;
    std::wstring m_ownerSid;       // read in SetUsageScenario; empty -> no tile
    bool m_ownerListed;            // the owner is among the users LogonUI will show
    // Shared with the credential so its worker thread can request a re-enumeration without
    // holding a pointer to this provider (the credential may outlive it -- see FaceCredential.h).
    std::shared_ptr<ProviderEvents> m_events;
};

}  // namespace FaceUnlock
